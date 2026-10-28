"""routes_core.py — 系统与入口端点：健康检查、模型列表、chat/completions、静态 UI、关停/重启、spans/tools、上传文件、SSE 与流式辅助

从 api.py 拆出（第五批解耦）：只做请求/响应适配。共享状态与基础设施仍在
api.py，这里经 ``a.xxx`` 访问（保持测试对 api 模块的打桩语义）；
本模块用自带 APIRouter 注册路由，由 api.py 在末尾 include。
"""
from __future__ import annotations

import asyncio, hmac, json, logging, re, threading, time, uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request as HttpRequest

from .version import VERSION as _APP_VERSION
from .brand import APP_NAME as _APP_NAME
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


def sse(data):
    if isinstance(data, str):
        return f"data: {data}\n\n"
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def _chat_sid() -> str:
    """普通对话（/v1/chat/completions）的窗口簿记 id。

    它没有会话库记录，但同样要**独占一个自动化窗口**：以前这里只拿全局 `lock`、
    直接驱动共享页，多窗口下会和正在窗口 #1 上跑的 Agent 任务抢同一个标签页
    （两边的输入框/回答互相覆盖，表现为"回答串到别的问题上"）。
    """
    return "chat-" + uuid.uuid4().hex[:12]


async def stream_response(req, prompt):
    rid = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    try:
        async with b._window(_chat_sid(), need_page=True):
            try:
                selected_model = await b.send(prompt, req.new_conversation, req.model)
                yield sse({
                    "id": rid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": selected_model,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                })
                async for part in b.updates():
                    yield sse({
                        "id": rid,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": selected_model,
                        "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}],
                    })
                yield sse({
                    "id": rid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": selected_model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                })
                yield sse("[DONE]")
            except asyncio.CancelledError:
                b.log("客户端已断开")
                raise
            except Exception as e:
                await b.diagnostics("error")
                b.log(f"失败: {e}")
                yield sse({"error": {"message": str(e), "type": "bridge_error"}})
                yield sse("[DONE]")
    except b.CopilotBusy as exc:
        # 窗口全忙：明确告诉调用方是"等不到窗口"，而不是"Copilot 报错"
        b.log(f"普通对话未拿到自动化窗口: {exc}")
        yield sse({"error": {"message": f"{exc}（可在设置里调大「Copilot 自动化窗口数」）",
                             "type": "window_busy"}})
        yield sse("[DONE]")


async def stream_agent(req: a.AgentRequest):
    try:
        result, sid = await b.run_agent(req, attach_paths=b.save_image_attachments(req.attachments))
        yield sse({"session_id": sid, "agent_steps": result.steps,
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": result.content}, "finish_reason": "stop"}]})
    except Exception as exc:
        yield sse({"error": {"message": str(exc), "type": "agent_error"}})
    yield sse("[DONE]")


async def stream_provider(p, model: str, messages: list):
    """API 提供商的流式响应，转发为 OpenAI chunk 格式。"""
    rid = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    model_id = f"{p.name}/{model}"
    yield sse({"id": rid, "object": "chat.completion.chunk", "created": created,
               "model": model_id,
               "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    try:
        async for part in p.stream(model, messages):
            yield sse({"id": rid, "object": "chat.completion.chunk", "created": created,
                       "model": model_id,
                       "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}]})
        yield sse({"id": rid, "object": "chat.completion.chunk", "created": created,
                   "model": model_id,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    except Exception as exc:
        yield sse({"error": {"message": str(exc), "type": "provider_error"}})
    yield sse("[DONE]")


@router.get("/")
async def root():
    return {"name": _APP_NAME, "version": _APP_VERSION, "docs": "/docs",
            "models": "/v1/models", "agent": "/v1/agent/completions"}


@router.get("/health")
async def health():
    try:
        page_url = b.page.url if b.page else None
    except Exception:
        page_url = None  # 浏览器异常时健康检查不应自己 500
    # 起始状态与"页面是否可用"分开表达：没有 Copilot 时服务仍是 ok（提供商可用），
    # 只是 copilot_ready=false，WebUI 据此显示提示与「重试浏览器」。
    return {
        "event_source": b.event_source(),   # P2-10 第三期：当前读取路径（columns/projection）
        "status": "ok",
        "copilot_ready": bool(a.BOOT.get("copilot_ready")),
        "browser_state": a.BOOT.get("browser_state"),
        "browser_error": a.BOOT.get("browser_error"),
        "busy": b.any_window_busy(),
        # 窗口池现状（max/windows/busy/waiting/running）：预览分支的多窗口排障入口
        "windows": b.WINDOWS.stats(),
        "page_url": page_url,
        "default_model": b.DEFAULT_MODEL,
        "headless": b.HEADLESS_CURRENT,
    }


@router.post("/v1/shutdown")
async def shutdown_service():
    """终止本服务（WebUI「终止服务/退出」按钮）。

    先返回响应、再延迟触发停机：直接在这里停会让前端拿到"连接被重置"，看起来
    像操作失败。正在运行的任务会被中断——状态都在 SQLite 里，重启后可恢复。
    优雅停机拿不到 uvicorn server 时由 gantry/lifecycle.py 退回信号，并始终有
    "宽限期后硬退出"兜底（按钮必须真的能停掉服务）。
    """
    if a.lifecycle.shutdown_pending():
        return {"stopping": True, "already": True, "note": "停机已在进行中",
            "note_code": "shutdown_pending"}
    # 用线程定时器而不是 loop.call_later：与事件循环生命周期无关（服务可能由
    # 不同方式启动/重启），也让"先回响应再停"可被测试观察到。
    a.lifecycle.schedule("shutdown", 0.4)
    return {"stopping": True,
            "note": "服务即将终止。正在运行的任务会中断（状态已落库，重启后可恢复）。",
            "note_code": "shutdown_soon"}


@router.get("/v1/uploads/{name}")
async def serve_upload(name: str):
    """给前端供"粘贴附件"图片（聊天区缩略图/放大查看用）。

    文件名是服务端生成的（随机 hex + 扩展名），这里仍做严格校验：
    只允许 [A-Za-z0-9._-]、禁止路径分隔符与 `..`，并且只允许落在 data/uploads
    目录内（解析后的真实路径必须以其开头）。
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or ".." in name:
        raise HTTPException(400, "非法文件名")
    base = (b.BASE / "data" / "uploads").resolve()
    target = (base / name).resolve()
    if not str(target).lower().startswith(str(base).lower()) or not target.is_file():
        raise HTTPException(404, "附件不存在")
    return FileResponse(str(target))


@router.get("/v1/models")
async def models():
    created = int(time.time())
    data = [
        {
            "id": model_id,
            "object": "model",
            "created": created,
            "owned_by": "microsoft-copilot",
            "description": item["description"],
            # 英文说明：WebUI 英文界面用它当 <option title>（否则原生提示是中文）
            "description_en": item.get("description_en") or item["description"],
            # 菜单实况可见性：扫描没见到的不显示在 WebUI
            "available": b.copilot_model_available(item),
        }
        for model_id, item in b.MODELS.items()
    ]
    # API 提供商模型（provider/model），未配置 Key 的标注不可用，WebUI 不显示
    for pname, p in b.PROVIDERS.items():
        for m in p.models:
            data.append({
                "id": f"{pname}/{m}",
                "object": "model",
                "created": created,
                "owned_by": pname,
                "description": p.description + ("" if p.configured else "（未配置 API Key）"),
                "description_en": p.description + ("" if p.configured else " (API key not configured)"),
                "available": p.configured,
            })
    return {"object": "list", "data": data}


@router.post("/v1/models/refresh")
async def refresh_models():
    await a.ensure_browser()   # 按需启动：走到 Copilot 路径才拉起浏览器
    """重新打开 Copilot 模型菜单枚举选项，把新出现的模型并入列表。

    返回结构化结果：scanned=false 时 reason 说明为什么没扫成（浏览器不在
    对话页/未登录/前端改版），界面据此给出明确提示而不是静默。
    """
    # 扫模型菜单要独占地驱动一个窗口：以前只拿全局锁，多窗口下会和任务抢共享页
    try:
        async with b._window("", wait_timeout=15):
            try:
                result = await b.discover_models()
            except Exception as exc:
                await b.diagnostics("models_refresh_failed")
                raise HTTPException(500, f"模型菜单读取失败: {exc}") from exc
    except b.CopilotBusy as exc:
        raise HTTPException(409, f"{exc}（稍后再试，或调大「Copilot 自动化窗口数」）") from exc
    available = [mid for mid, item in b.MODELS.items() if b.copilot_model_available(item)]
    return {"added": result["added"], "scanned": result["scanned"],
            "reason": result["reason"], "labels": result["labels"],
            "url": result["url"], "available_models": available,
            "all_models": list(b.MODELS)}


@router.get("/v1/chat/completions")
async def help_get():
    return {
        "message": "请使用 POST",
        "available_models": b.all_model_ids(),
    }


@router.post("/v1/chat/completions")
async def chat(req: a.Request):
    # Validate before creating StreamingResponse, so an invalid model returns HTTP 400.
    req.model = b.normalize_model(req.model)
    pm = b.providers_mod.split_provider_model(req.model)
    if pm and pm[0] in b.PROVIDERS:
        # API 提供商（OpenAI 兼容 HTTP API）：直接透传 messages
        p = b.PROVIDERS[pm[0]]
        messages = [{"role": m.role, "content": m.content} for m in req.messages]
        if req.stream:
            return StreamingResponse(
                stream_provider(p, pm[1], messages),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        try:
            answer = await p.complete(pm[1], messages)
        except b.providers_mod.ProviderUnavailable as exc:
            raise HTTPException(503, str(exc))
        except b.providers_mod.ProviderError as exc:
            raise HTTPException(400, str(exc))
        return {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
                "created": int(time.time()), "model": req.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                             "finish_reason": "stop"}]}
    prompts = [m.content.strip() for m in req.messages if m.role == "user" and m.content.strip()]
    if not prompts:
        raise HTTPException(400, "至少需要一条 user 消息")
    if req.stream:
        return StreamingResponse(
            stream_response(req, prompts[-1]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        return await a.nonstream(req, prompts[-1])
    except b.CopilotBusy as exc:
        # 所有窗口都被占着：这是可重试的忙，不是服务端错误
        raise HTTPException(409, f"{exc}（可在设置里调大「Copilot 自动化窗口数」）") from exc
    except HTTPException:
        raise
    except Exception as e:
        await b.diagnostics("error")
        raise HTTPException(500, str(e)) from e


@router.get("/ui", include_in_schema=False)
async def web_ui():
    # 目录形式提供服务：页面里 style.css/app.js 用相对路径引用，
    # 必须以 /ui/ 结尾的基地址解析，否则相对引用会落到站点根路径。
    return RedirectResponse("/ui/")


@router.get("/ui/", include_in_schema=False)
async def web_ui_index():
    return FileResponse(b.BASE / "webui" / "index.html", headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/ui/{asset:path}", include_in_schema=False)
async def web_ui_asset(asset: str):
    # 静态资产白名单，禁止路径穿越
    if asset in ("style.css", "app.js", "boot.js", "i18n.js"):
        return FileResponse(b.BASE / "webui" / asset, headers={"Cache-Control": "no-cache, must-revalidate"})
    # ES 模块（app.js 解耦）：只允许 js/<名字>.js，不许 ".."、不许子目录再嵌套
    if re.fullmatch(r"js/[0-9A-Za-z._-]+\.js", asset):
        return FileResponse(b.BASE / "webui" / asset, headers={"Cache-Control": "no-cache, must-revalidate"})
    raise HTTPException(404, "Not Found")


@router.get("/v1/search")
async def search_messages(q: str = "", limit: int = 30):
    """跨会话搜索对话内容（WebUI 侧栏的"全局搜索"）。

    只搜 messages.content（对话正文）；标题过滤由前端本地做，因此这里不重复。
    返回命中消息 + 其会话的标题/状态，供点开跳转。
    """
    keyword = (q or "").strip()
    if len(keyword) < 1:
        raise HTTPException(400, "搜索词不能为空")
    hits = b.AGENT_SESSIONS.search_messages(keyword, limit=max(1, min(int(limit), 100)))
    return {"query": keyword, "count": len(hits), "results": hits}


@router.post("/v1/restart")
async def restart_service():
    """重启本服务（WebUI「重启服务」菜单）。

    与停机同样的安全模型：**先返回响应、再延迟触发**（直接在这里退会让前端拿到
    "连接被重置"，看起来像失败）。流程是优雅退出 → 由 start.py 在 `server.run()`
    返回后 `os.execv` 就地重启，所以端口不会出现竞争窗口。

    正在运行的任务会中断（状态都在 SQLite 里）；开了 `agent.auto_resume` 的会
    在新进程启动后自动续跑。
    """
    if a.lifecycle.shutdown_pending() or a.lifecycle.restart_requested():
        return {"restarting": True, "already": True,
                "note": "停机/重启已在进行中", "note_code": "restart_pending"}
    a.lifecycle.schedule("restart", 0.4)
    return {"restarting": True,
            "note": "服务正在重启。正在运行的任务会中断（状态已落库）；"
                    "本页会在服务恢复后自动刷新。",
            "note_code": "restart_soon"}


@router.get("/v1/agent/spans")
async def list_spans(limit: int = 200, session_id: str | None = None):
    """最近的操作 span（JSONL 尾部）：回合/工具调用，字段对齐 OTel GenAI 语义约定。"""
    items = b.SPANS.tail(limit=limit)
    if session_id:
        items = [x for x in items
                 if (x.get("attributes") or {}).get("gantry.session_id") == session_id]
    return {"count": len(items), "enabled": b.SPANS.enabled, "path": str(b.SPANS.path),
            "items": items}


@router.get("/v1/agent/tools")
async def agent_tools():
    return {"workspace": str(b.AGENT_TOOLS.root), "tools": b.AGENT_TOOLS.descriptions()}


def _map_agent_error(exc: Exception) -> Exception:
    """把桥接层异常映射为合适的 HTTP 状态码，而不是一律 500。"""
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, b.CopilotServiceUnavailable):
        return HTTPException(503, f"Copilot 服务暂时不可用: {exc}")
    if isinstance(exc, b.CopilotConversationFull):
        return HTTPException(409, f"Copilot 对话达到长度上限: {exc}")
    if isinstance(exc, b.CopilotBusy):
        # 窗口全忙：可重试的忙，不是 500
        return HTTPException(409, f"{exc}（可在设置里调大「Copilot 自动化窗口数」）")
    if isinstance(exc, TimeoutError):
        return HTTPException(504, f"等待 Copilot 回答超时: {exc}")
    return HTTPException(500, str(exc))


@router.post("/v1/agent/completions")
async def agent_completions(req: a.AgentRequest, request: HttpRequest = None):
    prompt_lang.set_request_lang(request.headers.get(a._LANG_HEADER) if request is not None else None)
    # @文件引用展开：新任务也可以 @工作区里的文件（内容随消息一起发）。
    try:
        from .atrefs import expand_at_mentions
        from pathlib import Path as _Path
        _root = _Path(str(req.workspace or b.AGENT_TOOLS.root or (b.BASE / "workspace"))).resolve()
        for _m in (req.messages or []):
            if _m.role == "user" and "@" in (_m.content or ""):
                _expanded, _refs = expand_at_mentions(_m.content, _root)
                if _refs:
                    _m.content = _expanded
    except Exception as _e:
        b.log(f"@ 引用展开失败（忽略）: {_e}")
    if not req.agent_mode:
        # 普通对话模式：创建 Session 绑定 Copilot 会话，但不跑工具循环
        try:
            return await b.plain_completions(req)
        except Exception as exc:
            raise _map_agent_error(exc)
    if req.stream:
        return StreamingResponse(stream_agent(req), media_type="text/event-stream")
    try:
        result, sid = await b.run_agent(req, attach_paths=b.save_image_attachments(req.attachments))
    except Exception as exc:
        raise _map_agent_error(exc)
    return {"id": "agent-" + uuid.uuid4().hex, "object": "chat.completion",
            "created": int(time.time()), "model": req.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": result.content}, "finish_reason": "stop"}],
            "agent_steps": result.steps, "session_id": sid, "workspace": str(b.AGENT_TOOLS.root)}
