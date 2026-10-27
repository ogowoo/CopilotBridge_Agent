"""routes_config.py — 设置与运行环境端点：配置读写、沙箱档位、插件、浏览器模式、工作区/目录选择、调试快照

从 api.py 拆出（第五批解耦）：只做请求/响应适配。共享状态与基础设施仍在
api.py，这里经 ``a.xxx`` 访问（保持测试对 api 模块的打桩语义）；
本模块用自带 APIRouter 注册路由，由 api.py 在末尾 include。
"""
from __future__ import annotations

import asyncio, hmac, json, logging, re, threading, time, uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request as HttpRequest
from fastapi.responses import (StreamingResponse, FileResponse, RedirectResponse,
                               JSONResponse)
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from contextlib import AsyncExitStack

from . import api as a
from . import bridge as b
from . import i18n as backend_i18n
from . import prompt_lang

router = APIRouter()


@router.post("/v1/agent/debug/download")
async def agent_debug_download(payload: dict | None = None):
    """诊断：立即把当前页面的第一个下载候选存到 data/debug-download/。"""
    stem = str((payload or {}).get("stem") or "")
    # 点下载要驱动页面：独占一个窗口，免得把正在跑的任务页面点走
    try:
        async with b._window("", wait_timeout=10):
            return await b.debug_download_now(stem)
    except b.CopilotBusy as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/v1/agent/debug/panel")
async def agent_debug_panel():
    """诊断：Copilot 右侧面板（文件预览）候选块一览。"""
    return await b.debug_side_panel()


@router.get("/v1/agent/debug/page")
async def agent_debug_page(limit: int = 6000):
    """只读诊断：返回当前页面的纯文本尾部（排查界面语言/锚点，不改任何状态）。

    非任务上下文：直接读共享页，不占窗口、不导航。
    """
    try:
        url = b.current_page().url if b.current_page() else None
    except Exception:
        url = None
    try:
        text = await b.body_text()
    except Exception as exc:
        return {"url": url, "error": str(exc), "text": ""}
    lim = max(200, int(limit))
    return {"url": url, "len": len(text), "text": text[-lim:]}


@router.get("/v1/agent/sandbox")
async def get_sandbox():
    """查询当前沙箱档位与可选值。"""
    return {"mode": str(getattr(b.AGENT_TOOLS, "sandbox_mode", "workspace-write")),
            "modes": list(b.SANDBOX_MODES),
            "plan_mode": bool(getattr(b.AGENT_TOOLS, "plan_mode", False))}


@router.post("/v1/agent/sandbox")
async def set_sandbox(payload: dict | None = None):
    """切换沙箱档位：read-only / workspace-write / danger-full-access。"""
    mode = str((payload or {}).get("mode") or "").strip().lower()
    if mode not in b.SANDBOX_MODES:
        raise HTTPException(400, f"mode 必须是 {list(b.SANDBOX_MODES)} 之一")
    b.AGENT_TOOLS.sandbox_mode = mode
    b.AGENT_CFG["sandbox_mode"] = mode
    persist = bool((payload or {}).get("persist"))
    if persist:
        cfg_path = b.BASE / "config.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg.setdefault("agent", {})["sandbox_mode"] = mode
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception as exc:
            raise HTTPException(500, f"写 config.json 失败: {exc}")
    return {"mode": mode, "persisted": persist}


# ── 设置页：配置读写（白名单）──────────────────────────────────────
# 只允许改「有明确语义、不会把服务搞挂」的键；api_token 这类敏感项只读且打码。
CONFIG_SCHEMA: dict[str, dict] = {
    # 外观/界面（前端本地保存，这里只登记，便于设置页统一展示）
    # 提示词语言：决定发给模型的规则用什么语言写（auto = 跟随界面语言）
  "agent.prompt_language": {"kind": "enum", "values": ["auto", "zh", "tw", "en"],
                            "label": "提示词语言", "group": "exec"},
  "sandbox_mode": {"kind": "enum", "values": ["read-only", "workspace-write",
                                                "danger-full-access"],
                     "group": "exec", "label": "权限档位（工具能改什么）"},
    "browser_startup": {"kind": "enum", "values": ["lazy", "eager"], "group": "browser",
                        "label": "浏览器启动方式"},
    "headless": {"kind": "bool", "group": "browser", "label": "无头模式（不显示浏览器窗口）"},
    "auto_resume": {"kind": "bool", "group": "exec", "label": "服务重启后自动续跑任务"},
    "allow_copilot_file": {"kind": "bool", "group": "exec", "label": "允许 copilot_file 直读文件"},
    "diagnostics": {"kind": "bool", "group": "exec", "label": "保留诊断信息"},
    "spans_enabled": {"kind": "bool", "group": "exec", "label": "记录工具耗时区间"},
    "default_model": {"kind": "str", "group": "exec", "label": "默认模型"},
    "agent.auto_team_enabled": {"kind": "bool", "group": "exec",
                                "label": "自动派发子代理（待办≥2 且互不重叠时）"},
    "agent.rate_limit_suspend": {"kind": "bool", "group": "exec",
                                 "label": "Copilot 限流时挂起（到点自动继续，可换提供商模型）"},
    "agent.copilot_windows": {"kind": "int", "min": 1, "max": 4, "group": "exec",
                              "label": "Copilot 自动化窗口数（并行任务数；1=串行）"},
    "agent.todo_nudge_every": {"kind": "int", "min": 0, "max": 200, "group": "exec",
                               "label": "每 N 步提醒模型更新待办（0=关闭）"},
    "agent.subagent_parallel": {"kind": "int", "min": 1, "max": 8, "group": "exec",
                                "label": "子代理并发数"},
    "agent.subagent_max_steps": {"kind": "int", "min": 5, "max": 500, "group": "exec",
                                 "label": "子代理最多步数"},
    "agent.approval_scope": {"kind": "enum", "values": ["each", "session"], "group": "exec",
                             "label": "审批范围"},
    "plain_code_limit": {"kind": "int", "min": 500, "max": 50000, "group": "exec",
                         "label": "代码明文单次写入上限（字符，超出改用 Base64 分块）"},
    "agent.context_token_budget": {"kind": "int", "min": 2000, "max": 2000000, "group": "exec",
                                   "label": "上下文预算（token）"},
    "agent.max_sessions": {"kind": "int", "min": 1, "max": 500, "group": "data",
                           "label": "最多保留会话数"},
    "agent.snapshot_retention": {"kind": "int", "min": 0, "max": 500, "group": "data",
                                 "label": "文件快照保留份数"},
    "agent.completed_session_retention_days": {"kind": "int", "min": 0, "max": 365,
                                               "group": "data", "label": "已完成会话保留天数"},

    # ── 危险区（设置页单列，默认关闭）─────────────────────────────
    "agent.allow_self_upgrade": {"kind": "bool", "default": False,
                           "label": "允许 Agent 修改服务自身代码（自升级）",
                           "group": "danger"},
}


def _cfg_get(path: str, default=None):
    node = b.CFG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _cfg_set(path: str, value) -> None:
    parts = path.split(".")
    node = b.CFG
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _coerce(key: str, raw):
    spec = CONFIG_SCHEMA[key]
    kind = spec["kind"]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        try:
            val = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} 需要整数")
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and val < lo:
            raise HTTPException(400, f"{key} 不能小于 {lo}")
        if hi is not None and val > hi:
            raise HTTPException(400, f"{key} 不能大于 {hi}")
        return val
    if kind == "enum":
        val = str(raw).strip().lower()
        if val not in spec["values"]:
            raise HTTPException(400, f"{key} 必须是 {spec['values']} 之一")
        return val
    return str(raw)


@router.get("/v1/config")
async def get_config():
    """设置页读取：白名单键的当前值 + 规格（界面据此渲染控件）。"""
    values = {k: _cfg_get(k, b.CFG.get(k) if "." not in k else None) for k in CONFIG_SCHEMA}
    # 没配过的键显示**当前生效值**，避免输入框空着让人以为没生效（窗口数默认 1）
    if values.get("agent.copilot_windows") in (None, ""):
        try:
            values["agent.copilot_windows"] = b.WINDOWS.max_windows
        except Exception:
            values.setdefault("agent.copilot_windows", 1)
    secrets = {"api_token": "已设置" if b.CFG.get("api_token") else "未设置"}
    return {"values": values, "schema": CONFIG_SCHEMA, "readonly": secrets}


@router.post("/v1/config")
async def set_config(payload: dict | None = None):
    """设置页保存：只接受白名单键；persist=true 时写回 config.json。"""
    data = payload or {}
    incoming = data.get("values") or {}
    if not isinstance(incoming, dict) or not incoming:
        raise HTTPException(400, "values 不能为空")
    unknown = [k for k in incoming if k not in CONFIG_SCHEMA]
    if unknown:
        raise HTTPException(400, f"不支持修改这些键：{unknown}")
    applied = {k: _coerce(k, incoming[k]) for k in incoming}
    for key, val in applied.items():
        _cfg_set(key, val)
        if key == "sandbox_mode":
            b.AGENT_TOOLS.sandbox_mode = val
            b.AGENT_CFG["sandbox_mode"] = val
        elif key == "headless":
            b.AGENT_CFG["headless"] = val
        elif key.startswith("agent."):
            b.AGENT_CFG[key.split(".", 1)[1]] = val
            if key == "agent.allow_self_upgrade":
                # 即时生效：危险区开关一拨就能用，不用重启服务
                b.AGENT_TOOLS.allow_self_upgrade = val
        elif key == "auto_resume":
            b.AGENT_CFG["auto_resume"] = val

    persisted = False
    if data.get("persist", True):
        cfg_path = b.BASE / "config.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            for key, val in applied.items():
                node = cfg
                parts = key.split(".")
                for part in parts[:-1]:
                    nxt = node.get(part)
                    if not isinstance(nxt, dict):
                        nxt = {}
                        node[part] = nxt
                    node = nxt
                node[parts[-1]] = val
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            persisted = True
        except Exception as exc:
            raise HTTPException(500, f"写 config.json 失败: {exc}")
    b.log("设置已更新：" + ", ".join(f"{k}={v}" for k, v in applied.items()))
    # 窗口数变化要**立即生效**：调大下次 acquire 时懒开窗，调小关空闲窗口
    if "agent.copilot_windows" in applied:
        try:
            await b.WINDOWS.set_max(int(applied["agent.copilot_windows"]))
            b.log(f"Copilot 自动化窗口数已调整为 {b.WINDOWS.max_windows}")
        except Exception as exc:
            b.log(f"调整 Copilot 窗口数失败（忽略）：{exc}")
    # 子代理并发数变化：提供商逻辑槽要跟着调（否则 8 个扇出只有 4 个能跑，其余等超时）
    if "agent.subagent_parallel" in applied:
        try:
            await b.SLOTS.set_max(max(4, int(applied["agent.subagent_parallel"])))
        except Exception as exc:
            b.log(f"调整提供商并发槽失败（忽略）：{exc}")
    return {"applied": applied, "persisted": persisted}


@router.get("/v1/plugins/registry")
async def plugin_registry():
    """插件注册表：已安装版本/来源/启停状态 + 备份列表。"""
    return b.PLUGINS.registry_view()


@router.post("/v1/plugins/install")
async def plugin_install(payload: dict | None = None):
    """安装插件：{source, name?, force?}。source 可为目录 / .zip / git URL。"""
    data = payload or {}
    try:
        return b.PLUGINS.install(str(data.get("source") or ""),
                                 str(data.get("name") or ""),
                                 bool(data.get("force")))
    except b.PluginError as exc:
        raise HTTPException(400, str(exc))


@router.post("/v1/plugins/uninstall")
async def plugin_uninstall(payload: dict | None = None):
    """卸载插件：{name, keep_backup?}。"""
    data = payload or {}
    name = str(data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "需要 name")
    try:
        return b.PLUGINS.uninstall(name, bool(data.get("keep_backup", True)))
    except b.PluginError as exc:
        raise HTTPException(400, str(exc))


@router.get("/v1/plugins/i18n")
async def plugin_i18n():
    """已加载插件自带的译文（前端合并进字典，让 tr() 覆盖插件文案）。"""
    return b.PLUGINS.i18n()


@router.get("/v1/plugins/panels")
async def plugin_panels():
    """已加载插件声明的声明式面板（前端按 view 渲染）。"""
    return {"panels": b.PLUGINS.panels()}


@router.get("/v1/plugins")
async def list_plugins():
    """插件列表：名称/版本/状态/权限/贡献点（与 DSH 插件语义兼容）。"""
    return {"plugins": b.PLUGINS.list()}


@router.post("/v1/plugins/{name}/{action}")
async def plugin_action(name: str, action: str):
    """插件启停/重载：action = enable | disable | reload。"""
    try:
        if action == "enable":
            return {"ok": True, "plugin": b.PLUGINS.enable(name)}
        if action == "disable":
            return {"ok": True, "plugin": b.PLUGINS.disable(name)}
        if action == "reload":
            return {"ok": True, "plugin": b.PLUGINS.reload(name)}
    except b.PluginError as exc:
        raise HTTPException(400, str(exc))
    raise HTTPException(404, f"未知动作: {action}")


@router.api_route("/v1/plugins/{plugin}/api/{path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def plugin_api(plugin: str, path: str, request: HttpRequest):
    """插件自定义接口的统一分发（启停不需要重启服务）。"""
    try:
        return await b.PLUGINS.dispatch_api(plugin, request.method, path, request)
    except b.PluginError as exc:
        raise HTTPException(404, str(exc))


@router.get("/v1/agent/debug/page")
async def agent_debug_page():
    """诊断：当前自动化页面截图 + 下载入口（只读，排障用）。"""
    return await b.debug_page_snapshot()


@router.get("/v1/agent/browser")
async def browser_state():
    return {"headless": b.HEADLESS_CURRENT,
            "config_headless": bool(b.CFG.get("headless", False))}


@router.post("/v1/agent/browser")
async def set_browser_mode(payload: dict):
    await a.ensure_browser()   # 按需启动：走到 Copilot 路径才拉起浏览器
    """切换自动化浏览器窗口的显示/隐藏。

    headless 是启动参数，切换必然重建浏览器上下文（EdgeProfile 保留，
    登录态不受影响）。有请求/任务正在使用浏览器时返回 409 拒绝切换。
    persist=true 可把新模式写回 config.json。
    """
    target_raw = payload.get("headless")
    if target_raw is None:
        # 只启动、不切换模式（"启动自动化浏览器"按钮就是这么调的）
        if b.current_page() is not None:
            return {"headless": b.HEADLESS_CURRENT, "changed": False, "fallback": False}
        actual = await b._start_browser(b.HEADLESS_CURRENT)
        return {"headless": actual, "changed": True, "fallback": False}
    if not isinstance(target_raw, bool):
        raise HTTPException(422, "headless 必须是布尔值")
    target = target_raw
    if target == b.HEADLESS_CURRENT and b.current_page() is not None:
        return {"headless": b.HEADLESS_CURRENT, "changed": False, "fallback": False}
    if b.any_window_busy():
        raise HTTPException(409, "有请求/任务正在使用浏览器，请稍后再切换")
    actual = await b._start_browser(target)
    result = {"headless": actual, "changed": True, "fallback": actual != target}
    if payload.get("persist"):
        try:
            b.CFG["headless"] = actual
            (b.BASE / "config.json").write_text(
                json.dumps(b.CFG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            result["persisted"] = True
        except Exception as exc:
            result["persisted"] = False
            result["persist_error"] = str(exc)
    return result


@router.post("/v1/agent/pick-directory")
async def pick_directory(payload: dict | None = None):
    """弹系统文件夹对话框（DSH native 后端的等价实现）；失败时前端回退到 browse。"""
    start = str((payload or {}).get("start") or "")
    try:
        return await b.pick_directory_native(start)
    except b.ToolError as exc:
        raise HTTPException(400, str(exc))


@router.post("/v1/agent/browse/mkdir")
async def browse_mkdir(payload: dict | None = None):
    """在工作目录选择器里新建子目录：{parent, name} → {path}。"""
    data = payload or {}
    try:
        return b.create_directory(str(data.get("parent") or ""), str(data.get("name") or ""))
    except b.ToolError as exc:
        raise HTTPException(400, str(exc))


@router.get("/v1/agent/browse")
async def browse_dirs(path: str | None = None):
    """列目录（只列子目录），供工作目录选择器；不传 path 从当前工作区开始。"""
    return b.browse_directories(path)


@router.get("/v1/agent/workspace")
async def workspace():
    return {"workspace": str(b.AGENT_TOOLS.root)}


@router.post("/v1/agent/workspace")
async def set_workspace(payload: dict):
    if b.any_task_busy():
        raise HTTPException(409, "有任务正在运行，不能切换工作区")
    new_root = b.AGENT_TOOLS.set_root(str(payload.get("path", "")), bool(payload.get("create", False)))
    b.DEFAULT_WORKSPACE = new_root  # 作为之后新任务的默认工作区
    return {"workspace": new_root}


@router.post("/v1/agent/browser/retry")
async def browser_retry():
    """重新初始化浏览器（登录完成后、或上次启动失败时用），不必重启服务。"""
    old = a.BOOT.get("task")
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(a.boot_browser())
    a.BOOT["task"] = task
    return {"started": True, "state": a.BOOT["browser_state"]}
