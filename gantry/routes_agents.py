"""routes_agents.py — 子代理 / 深研 / 扇出 / 对抗式审计 / 记忆 / 自升级 端点

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


@router.post("/v1/agent/deep-research")
async def agent_deep_research(payload: dict | None = None, request: HttpRequest = None):
    """一键深研：research 插件收集证据 → Agent 综合成带来源的报告（新会话）。"""
    data = payload or {}
    query = str(data.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "query 不能为空")
    try:
        return await b.deep_research_task(query, str(data.get("model") or ""),
                                          int(data.get("angles") or 3), 3,
                                          lang=a._lang_of(request))
    except b.ToolError as exc:
        raise HTTPException(400, str(exc))


@router.post("/v1/agent/subagents/{sid}/send")
async def subagent_send(sid: str, payload: dict | None = None):
    """继续一个子代理会话（追加一轮）。"""
    data = payload or {}
    msg = str(data.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "message 不能为空")
    try:
        return await b.continue_subagent(sid, msg, str(data.get("model") or ""),
                                         int(data.get("max_steps") or 40))
    except b.ToolError as exc:
        raise HTTPException(400, str(exc))


@router.post("/v1/agent/subagents/{sid}/fork")
async def subagent_fork(sid: str, payload: dict | None = None):
    """从某个会话分叉出新的子代理（复制最近对话）。"""
    data = payload or {}
    try:
        return await b.fork_subagent(sid, str(data.get("description") or ""),
                                     int(data.get("keep_messages") or 60))
    except b.ToolError as exc:
        raise HTTPException(400, str(exc))


@router.post("/v1/agent/fanout")
async def agent_fanout(payload: dict | None = None):
    """确定性扇出：把 N 个任务直接各派一个子代理（不经模型判断）。

    {tasks: ["任务1", "任务2"] | [{task, description}], model?, workspace?, max_steps?}
    返回父会话 id 与每个子任务的结果；结果同时写进父会话消息，可在会话列表里查看。
    """
    data = payload or {}
    try:
        return await b.fanout(data.get("tasks") or [], str(data.get("model") or ""),
                              str(data.get("workspace") or ""),
                              int(data.get("max_steps") or 40),
                              str(data.get("title") or ""))
    except b.ToolError as exc:
        raise HTTPException(400, str(exc))


@router.post("/v1/agent/audit")
async def agent_audit(payload: dict | None = None):
    """对抗式审计：多名审计员 + 裁判核验，循环到只剩小问题（后台跑，返回会话 id）。

    {objective, target?, auditors?, max_rounds?, auto_fix?, model?}
    结果与每轮报告写进 `audit-*` 会话（消息 + 步骤），前端刷新会话即可看到进度。
    """
    data = payload or {}
    objective = str(data.get("objective") or data.get("task") or "").strip()
    if not objective:
        raise HTTPException(400, "objective 不能为空（审计目标/说明）")
    sid = "audit-" + uuid.uuid4().hex[:10]
    ws = str(b.AGENT_TOOLS.root)
    b.AGENT_SESSIONS.create(sid, "对抗式审计: " + objective[:80], ws,
                            str(data.get("model") or b.DEFAULT_MODEL))

    async def _run():
        tok = b._SESSION_CTX.set(sid)
        try:
            await b.adversarial_audit(
                target=str(data.get("target") or ""), objective=objective,
                auditors=int(data.get("auditors") or 3),
                max_rounds=int(data.get("max_rounds") or 3),
                auto_fix=bool(data.get("auto_fix", True)),
                model=str(data.get("model") or ""), session_id=sid)
        except Exception as exc:
            b.AGENT_SESSIONS.patch(sid, state="failed", last_error=str(exc))
        finally:
            b._SESSION_CTX.reset(tok)

    asyncio.create_task(_run())
    return {"session_id": sid, "started": True}


@router.get("/v1/agent/subagents")
async def list_subagents(parent: str | None = None, limit: int = 50):
    """子代理列表：id / 任务 / 状态 / 进度 / 父会话（供 WebUI 子代理面板）。"""
    rows = b.AGENT_SESSIONS.list_subagents(parent, limit)
    out = []
    for s in rows:
        rt = b.RUNTIME.get(s["id"]) or {}
        out.append({
            "id": s["id"],
            "parent_id": s.get("parent_id") or "",
            "description": s.get("description") or (s.get("title") or "")[:80],
            "task": (s.get("original_task") or "")[:500],
            "state": s.get("state"),
            "model": s.get("model"),
            "steps": int(s.get("total_steps") or 0),
            "created": s.get("created"),
            "updated": s.get("updated"),
            "last_error": s.get("last_error"),
            "phase": rt.get("phase"),
            "detail": (rt.get("detail") or "")[:200],
        })
    return {"subagents": out, "count": len(out)}


# ── 自升级（修改服务自身代码）───────────────────────────────────
@router.get("/v1/agent/memory")
async def list_memory(query: str = "", scope: str = "all", limit: int = 100):
    """长期记忆清单/检索：跨会话累积的事实、偏好、结论与踩坑。"""
    scopes = b.memory_scopes(scope)
    store = b.MEMORY
    items = (store.search(query, scopes, limit=min(limit, 50)) if query.strip()
             else store.list(scopes, limit=limit))
    return {"query": query, "scopes": scopes, "stats": store.stats(scopes),
            "count": len(items),
            "items": [{k: m.get(k) for k in ("id", "scope", "kind", "text", "tags",
                                             "pinned", "use_count", "created", "updated",
                                             "source_session")} for m in items]}


@router.post("/v1/agent/memory/{mid}/pin")
async def pin_memory(mid: int, pinned: bool = True, scope: str = "all"):
    """置顶/取消置顶一条记忆：置顶的会在每次检索时恒定注入提示词。"""
    if not b.MEMORY.set_pinned(mid, pinned, scopes=b.memory_scopes(scope)):
        raise HTTPException(404, "记忆不存在或不属于当前可见作用域")
    return {"id": mid, "pinned": pinned}


@router.delete("/v1/agent/memory/{mid}")
async def delete_memory(mid: int, scope: str = "all"):
    """删除一条记忆（作用域做同属校验，不能跨工作区删）。"""
    if not b.MEMORY.forget(mid, scopes=b.memory_scopes(scope)):
        raise HTTPException(404, "记忆不存在或不属于当前可见作用域")
    return {"deleted": True, "id": mid}


@router.get("/v1/agent/self/status")
async def self_status():
    """自升级状态：是否启用、白名单、待重启标记、自升级快照作用域。"""
    return b.self_upgrade_status()


@router.get("/v1/agent/self/snapshots")
async def self_snapshots(sid: str | None = None):
    """自升级改动的快照摘要（不传 sid 时列出所有自升级作用域）。"""
    if sid:
        return b.SNAPSHOTS.summary(b.self_scope(sid))
    scopes = b.self_upgrade_status()["snapshot_scopes"]
    return {"scopes": [{"scope": s,
                        "session_id": s[len("_self_"):] if s.startswith("_self_") else s,
                        "summary": b.SNAPSHOTS.summary(s)} for s in scopes]}


@router.post("/v1/agent/self/rollback")
async def self_rollback(sid: str | None = None, last: bool = False):
    """回滚服务自身代码的自升级改动（last=true 时取最近一次）。"""
    if b.any_task_busy():
        raise HTTPException(409, "有任务正在运行，不能回滚服务代码（先暂停/终止任务）")
    result = await b.rollback_self_upgrade(sid, use_last=last)
    return result
