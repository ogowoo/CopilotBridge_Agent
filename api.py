"""HTTP 路由层：FastAPI 应用与全部端点。

浏览器自动化、会话编排与全局状态在 bridge.py；本模块只做请求/响应适配。
"""

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from playwright.async_api import async_playwright

import bridge as b
from bridge import Message, Request, SessionMessageRequest, AgentRequest


@asynccontextmanager
async def lifespan(app):
    b.PROFILE.mkdir(exist_ok=True)
    b.pw = await async_playwright().start()
    await b._start_browser(bool(b.CFG.get("headless", False)))
    b.log("API 与浏览器已就绪")
    try:
        added = await b.discover_models()
        if added:
            b.log(f"从 Copilot 菜单自动发现新模型: {', '.join(added)}")
        else:
            b.log("模型菜单未发现新模型")
    except Exception as exc:
        b.log(f"模型菜单自动发现失败（不影响已有模型）: {exc}")
    cleanup_task = asyncio.create_task(b.periodic_session_cleanup())
    resume_task = (asyncio.create_task(b.auto_resume_interrupted())
                   if bool(b.AGENT_CFG.get("auto_resume", False)) else None)
    try:
        yield
    finally:
        cleanup_task.cancel()
        if resume_task:
            resume_task.cancel()
        for task in (cleanup_task, resume_task):
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
        await b.shutdown_browser()


app = FastAPI(title="Copilot Local Bridge Agent", version="10.2.1", lifespan=lifespan)


def sse(data):
    if isinstance(data, str):
        return f"data: {data}\n\n"
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


async def stream_response(req, prompt):
    rid = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    async with b.lock:
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


async def nonstream(req, prompt):
    parts = []
    rid = "chatcmpl-" + uuid.uuid4().hex
    async with b.lock:
        selected_model = await b.send(prompt, req.new_conversation, req.model)
        async for part in b.updates():
            parts.append(part)
    return {
        "id": rid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": selected_model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "".join(parts)},
            "finish_reason": "stop",
        }],
    }


async def stream_agent(req: AgentRequest):
    try:
        result, sid = await b.run_agent(req)
        yield sse({"session_id": sid, "agent_steps": result.steps,
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": result.content}, "finish_reason": "stop"}]})
    except Exception as exc:
        yield sse({"error": {"message": str(exc), "type": "agent_error"}})
    yield sse("[DONE]")


@app.get("/")
async def root():
    return {"name": "Copilot Local Bridge Agent", "version": "10.2.1", "docs": "/docs",
            "models": "/v1/models", "agent": "/v1/agent/completions"}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "busy": b.lock.locked(),
        "page_url": b.page.url if b.page else None,
        "default_model": b.DEFAULT_MODEL,
        "headless": b.HEADLESS_CURRENT,
    }


@app.get("/v1/agent/browser")
async def browser_state():
    return {"headless": b.HEADLESS_CURRENT,
            "config_headless": bool(b.CFG.get("headless", False))}


@app.post("/v1/agent/browser")
async def set_browser_mode(payload: dict):
    """切换自动化浏览器窗口的显示/隐藏。

    headless 是启动参数，切换必然重建浏览器上下文（EdgeProfile 保留，
    登录态不受影响）。有请求/任务正在使用浏览器时返回 409 拒绝切换。
    persist=true 可把新模式写回 config.json。
    """
    target = bool(payload.get("headless"))
    if target == b.HEADLESS_CURRENT and b.page is not None:
        return {"headless": b.HEADLESS_CURRENT, "changed": False, "fallback": False}
    if b.lock.locked():
        raise HTTPException(409, "有请求/任务正在使用浏览器，请稍后再切换")
    async with b.lock:
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


@app.get("/v1/models")
async def models():
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": created,
                "owned_by": "microsoft-copilot",
                "description": item["description"],
            }
            for model_id, item in b.MODELS.items()
        ],
    }


@app.post("/v1/models/refresh")
async def refresh_models():
    """重新打开 Copilot 模型菜单枚举选项，把新出现的模型并入列表。"""
    async with b.lock:
        try:
            added = await b.discover_models()
        except Exception as exc:
            await b.diagnostics("models_refresh_failed")
            raise HTTPException(500, f"模型菜单读取失败: {exc}")
    return {"added": added, "available_models": list(b.MODELS)}


@app.get("/v1/chat/completions")
async def help_get():
    return {
        "message": "请使用 POST",
        "available_models": list(b.MODELS),
    }


@app.post("/v1/chat/completions")
async def chat(req: Request):
    # Validate before creating StreamingResponse, so an invalid model returns HTTP 400.
    req.model = b.normalize_model(req.model)
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
        return await nonstream(req, prompts[-1])
    except HTTPException:
        raise
    except Exception as e:
        await b.diagnostics("error")
        raise HTTPException(500, str(e)) from e


@app.get("/ui", include_in_schema=False)
async def web_ui():
    return FileResponse(b.BASE / "webui" / "index.html")


@app.get("/v1/agent/sessions")
async def sessions(include_archived: bool = False):
    return {"sessions": b.AGENT_SESSIONS.list(include_archived)}


@app.get("/v1/agent/sessions/{sid}")
async def session(sid: str):
    x = b.AGENT_SESSIONS.get(sid)
    if not x:
        raise HTTPException(404, "Session 不存在")
    return {"session": x, "steps": b.AGENT_SESSIONS.steps(sid),
            "files": b.AGENT_SESSIONS.file_events(sid),
            "messages": b.AGENT_SESSIONS.messages(sid),
            "token_budget": b.TOKEN_BUDGET, "conv_stats": b.AGENT_SESSIONS.conv_stats(sid)}


@app.post("/v1/agent/sessions/{sid}/actions/{action}")
async def control(sid: str, action: str):
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    maps = {"pause": {"state": "paused"}, "resume": {"state": "ready", "cancel_requested": False, "rollover_requested": False},
            "cancel": {"state": "cancelled", "cancel_requested": True}, "compact": {"compact_requested": True},
            "rollover": {"rollover_requested": True}, "archive": {"state": "archived", "archived": True}}
    if action not in maps:
        raise HTTPException(400, "未知操作")
    return b.AGENT_SESSIONS.patch(sid, **maps[action])


@app.delete("/v1/agent/sessions/{sid}")
async def delete_session(sid: str, delete_remote: bool = False, force_local: bool = False):
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    if b.lock.locked() and b.ACTIVE_SESSION_ID == sid:
        raise HTTPException(409, "Session 正在运行，不能删除")

    remote_results = []
    if delete_remote:
        urls = list(dict.fromkeys(state.get("conversation_urls") or []))
        if state.get("current_url") and state["current_url"] not in urls:
            urls.append(state["current_url"])
        async with b.lock:
            for index, url in enumerate(urls):
                marker = b._agent_chat_marker(sid, index)
                remote_results.append(await b.delete_copilot_conversation(url, marker))
            b._forget_selected_model()  # 删除过程中页面在多个会话间跳转
        failed = [x for x in remote_results if not x["deleted"]]
        if failed and not force_local:
            return {
                "deleted": False,
                "local_deleted": False,
                "remote_results": remote_results,
                "message": "部分 Copilot 会话删除失败，本地 Session 已保留。可检查后使用 force_local=true 仅清理本地记录。",
            }

    local_deleted = b.AGENT_SESSIONS.delete(sid)
    b.RUNTIME.pop(sid, None)
    return {
        "deleted": local_deleted,
        "local_deleted": local_deleted,
        "remote_results": remote_results,
    }


@app.post("/v1/agent/sessions/{sid}/messages")
async def continue_session_message(sid: str, req: SessionMessageRequest):
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    if state.get("state") in {"cancelled", "archived"}:
        raise HTTPException(409, f"Session 状态为 {state.get('state')}，不能继续")
    content = req.content.strip()
    if not content:
        raise HTTPException(400, "消息不能为空")
    model_id = b.normalize_model(req.model or state.get("model") or b.DEFAULT_MODEL)
    b.AGENT_SESSIONS.add_message(sid, "user", content, int(state.get("conversation_index", 0)))

    if req.agent_mode:
        # Continue as an Agent task. This executes returned tool actions instead
        # of showing the raw {type:tool,...} JSON as a chat answer.
        agent_req = AgentRequest(
            model=model_id,
            messages=[Message(role="user", content=content)],
            stream=False,
            session_id=sid,
            reuse_session=True,
            workspace=state.get("workspace"),
        )
        result, _ = await b.run_agent(agent_req)
        answer = result.content
        current = b.AGENT_SESSIONS.get(sid)
        b.AGENT_SESSIONS.add_message(sid, "assistant", answer, int(current.get("conversation_index", 0)))
        return {"session_id": sid, "role": "assistant", "content": answer,
                "url": current.get("current_url"), "agent_steps": result.steps,
                "mode": "agent"}

    # Plain chat mode keeps the same Copilot web conversation and does not run tools.
    return await b.plain_chat_turn(sid, content, model_id)


@app.post("/v1/agent/sessions/{sid}/continue")
async def continue_agent_session(sid: str, req: SessionMessageRequest):
    req.agent_mode = True
    return await continue_session_message(sid, req)


@app.get("/v1/agent/sessions/{sid}/runtime")
async def session_runtime(sid: str):
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    runtime = dict(b.RUNTIME.get(sid) or {})
    runtime.update({
        "session_id": sid,
        "session_state": state.get("state"),
        "total_steps": state.get("total_steps", 0),
        "conversation_steps": state.get("conversation_steps", 0),
        "estimated_tokens": state.get("estimated_tokens", 0),
        "conversation_index": state.get("conversation_index", 0),
        "current_url": state.get("current_url"),
        "token_budget": b.TOKEN_BUDGET,
        "busy": b.lock.locked() and b.ACTIVE_SESSION_ID == sid,
    })
    return runtime


@app.post("/v1/agent/sessions/{sid}/activate")
async def activate_session(sid: str):
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    url = state.get("current_url")
    if not url:
        raise HTTPException(409, "该 Session 暂无 Copilot 会话 URL")
    await b.page.bring_to_front()
    if b.page.url != url:
        await b.page.goto(url, wait_until="domcontentloaded", timeout=120000)
        b._forget_selected_model()
    b.runtime_update(sid, phase="activated", detail="已切换到该 Session 的 Copilot 页面")
    return {"activated": True, "url": b.page.url, "headless": bool(b.CFG.get("headless", False))}


@app.get("/v1/agent/workspace")
async def workspace():
    return {"workspace": str(b.AGENT_TOOLS.root)}


@app.post("/v1/agent/workspace")
async def set_workspace(payload: dict):
    return {"workspace": b.AGENT_TOOLS.set_root(str(payload.get("path", "")), bool(payload.get("create", False)))}


@app.get("/v1/agent/tools")
async def agent_tools():
    return {"workspace": str(b.AGENT_TOOLS.root), "tools": b.AGENT_TOOLS.descriptions()}


@app.post("/v1/agent/completions")
async def agent_completions(req: AgentRequest):
    if not req.agent_mode:
        # 普通对话模式：创建 Session 绑定 Copilot 会话，但不跑工具循环
        return await b.plain_completions(req)
    if req.stream:
        return StreamingResponse(stream_agent(req), media_type="text/event-stream")
    result, sid = await b.run_agent(req)
    return {"id": "agent-" + uuid.uuid4().hex, "object": "chat.completion",
            "created": int(time.time()), "model": req.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": result.content}, "finish_reason": "stop"}],
            "agent_steps": result.steps, "session_id": sid, "workspace": str(b.AGENT_TOOLS.root)}
