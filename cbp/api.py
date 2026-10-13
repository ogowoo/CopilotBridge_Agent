"""HTTP 路由层：FastAPI 应用与全部端点。

浏览器自动化、会话编排与全局状态在 bridge.py；本模块只做请求/响应适配。
"""

import asyncio
import hmac
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse, RedirectResponse
from playwright.async_api import async_playwright

from . import bridge as b
from . import copilot_delete as cd
from .bridge import Message, Request, SessionMessageRequest, AgentRequest

# ── 访问控制 ──
# api_token：配置后所有 /v1/* 端点要求 "Authorization: Bearer <token>"。
# 非回环地址监听（0.0.0.0/局域网）且未配置 token 时拒绝启动——该服务能读写
# 文件、操作浏览器，裸奔到公网/内网是不可接受的风险。
API_TOKEN = str(b.CFG.get("api_token") or "")
_SERVER_HOST = str(b.CFG.get("server", {}).get("host", "127.0.0.1"))
if _SERVER_HOST not in ("127.0.0.1", "localhost", "::1") and not API_TOKEN:
    raise SystemExit(
        f"拒绝启动：server.host={_SERVER_HOST} 是非回环地址，但未配置 api_token。"
        "请在 config.json 设置 api_token，或把 host 改回 127.0.0.1。"
    )


@asynccontextmanager
async def lifespan(app):
    b.PROFILE.mkdir(exist_ok=True)
    b.pw = await async_playwright().start()
    try:
        await b._start_browser(bool(b.CFG.get("headless", False)))
    except Exception:
        # 浏览器启动失败时回收 Playwright 进程，不留孤儿
        try:
            await b.pw.stop()
            b.pw = None
        except Exception:
            pass
        raise
    b.log("API 与浏览器已就绪")
    # 服务真正启动时才归档"上一轮自升级已生效"标记（放这里而非模块导入时：
    # 测试/脚本 import cbp.bridge 不应误清该标记）
    applied = b.mark_self_upgrade_applied()
    if applied:
        b.log(f"[自升级] 已加载上一轮自升级改动（{len(applied)} 条记录），待重启标记归档")
    # 企业租户重启后 SSO/ADFS 回跳可能耗时几十秒，就绪后再做模型发现
    if await b.wait_copilot_ready():
        try:
            disc = await b.discover_models()
            added = disc.get("added") or []
            if added:
                b.log(f"从 Copilot 菜单自动发现新模型: {', '.join(added)}")
            elif disc.get("scanned"):
                b.log("模型菜单未发现新模型")
            else:
                b.log(f"模型菜单扫描未完成（不影响已有模型）: {disc.get('reason') or '未知原因'}")
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


@app.middleware("http")
async def token_auth(request, call_next):
    if API_TOKEN and request.url.path.startswith("/v1/"):
        from fastapi.responses import JSONResponse
        auth = request.headers.get("authorization", "")
        header_token = request.headers.get("x-cbp-token", "")
        token = request.query_params.get("token", "")
        # 常量时间比较：避免用逐字符比较泄漏前缀长度（本机场景风险低，但成本几乎为零）
        ok = any(hmac.compare_digest(candidate, API_TOKEN)
                 for candidate in (auth[len("Bearer "):] if auth.startswith("Bearer ") else auth,
                                   header_token, token)
                 if candidate)
        if not ok:
            return JSONResponse({"detail": "未授权：缺少或错误的 api_token"}, status_code=401)
    return await call_next(request)


class _RedactTokenFilter(logging.Filter):
    """把访问日志里的 token=xxx 抹掉：?token= 便捷用法不能让密钥进日志。"""

    _RE = re.compile(r"(token=)[^&\s\"]+", re.I)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.args, tuple):
                record.args = tuple(self._RE.sub(r"\1***", a) if isinstance(a, str) else a
                                    for a in record.args)
            elif isinstance(record.msg, str):
                record.msg = self._RE.sub(r"\1***", record.msg)
        except Exception:
            pass
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactTokenFilter())


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


@app.get("/")
async def root():
    return {"name": "Copilot Local Bridge Agent", "version": "10.2.1", "docs": "/docs",
            "models": "/v1/models", "agent": "/v1/agent/completions"}


@app.get("/health")
async def health():
    try:
        page_url = b.page.url if b.page else None
    except Exception:
        page_url = None  # 浏览器异常时健康检查不应自己 500
    return {
        "status": "ok" if page_url else "degraded",
        "busy": b.lock.locked(),
        "page_url": page_url,
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
    target_raw = payload.get("headless")
    if not isinstance(target_raw, bool):
        raise HTTPException(422, "headless 必须是布尔值")
    target = target_raw
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
    data = [
        {
            "id": model_id,
            "object": "model",
            "created": created,
            "owned_by": "microsoft-copilot",
            "description": item["description"],
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
                "available": p.configured,
            })
    return {"object": "list", "data": data}


@app.post("/v1/models/refresh")
async def refresh_models():
    """重新打开 Copilot 模型菜单枚举选项，把新出现的模型并入列表。

    返回结构化结果：scanned=false 时 reason 说明为什么没扫成（浏览器不在
    对话页/未登录/前端改版），界面据此给出明确提示而不是静默。
    """
    async with b.lock:
        try:
            result = await b.discover_models()
        except Exception as exc:
            await b.diagnostics("models_refresh_failed")
            raise HTTPException(500, f"模型菜单读取失败: {exc}")
    available = [mid for mid, item in b.MODELS.items() if b.copilot_model_available(item)]
    return {"added": result["added"], "scanned": result["scanned"],
            "reason": result["reason"], "labels": result["labels"],
            "url": result["url"], "available_models": available,
            "all_models": list(b.MODELS)}


@app.get("/v1/chat/completions")
async def help_get():
    return {
        "message": "请使用 POST",
        "available_models": b.all_model_ids(),
    }


@app.post("/v1/chat/completions")
async def chat(req: Request):
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
        return await nonstream(req, prompts[-1])
    except HTTPException:
        raise
    except Exception as e:
        await b.diagnostics("error")
        raise HTTPException(500, str(e)) from e


@app.get("/ui", include_in_schema=False)
async def web_ui():
    # 目录形式提供服务：页面里 style.css/app.js 用相对路径引用，
    # 必须以 /ui/ 结尾的基地址解析，否则相对引用会落到站点根路径。
    return RedirectResponse("/ui/")


@app.get("/ui/", include_in_schema=False)
async def web_ui_index():
    return FileResponse(b.BASE / "webui" / "index.html")


@app.get("/ui/{asset}", include_in_schema=False)
async def web_ui_asset(asset: str):
    # 静态资产白名单，禁止路径穿越
    if asset not in ("style.css", "app.js"):
        raise HTTPException(404, "Not Found")
    return FileResponse(b.BASE / "webui" / asset)


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
            "token_budget": b.TOKEN_BUDGET, "conv_stats": b.AGENT_SESSIONS.conv_stats(sid),
            "snapshots": b.SNAPSHOTS.summary(sid)}


@app.get("/v1/agent/sessions/{sid}/snapshots")
async def session_snapshots(sid: str):
    """任务改动快照摘要：可回滚的文件清单。"""
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    return b.SNAPSHOTS.summary(sid)


@app.post("/v1/agent/sessions/{sid}/rollback")
async def session_rollback(sid: str):
    """回滚该 Session 期间的全部工作区改动（修改还原、新建删除）。"""
    async with b.lock:
        result = await b.rollback_session_changes(sid)
    return result


@app.post("/v1/agent/sessions/{sid}/model")
async def set_session_model(sid: str, req: b.SessionModelRequest):
    """切换该会话使用的模型：立即持久化，下一回合生效，服务重启后仍沿用。

    会话级模型是续跑的依据——resume/auto_resume 都用它，所以这里落库后
    就不会再出现"重启后悄悄换回 Auto"。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    requested = (req.model or "").strip()
    model_id = b.normalize_model_soft(requested)
    b.AGENT_SESSIONS.patch(sid, model=model_id)
    updated = b.AGENT_SESSIONS.get(sid)
    note = "" if model_id == requested else f"{requested} 已失效，回退为 {model_id}"
    return {**updated, "applies": "下一回合生效（服务重启后仍沿用）", "note": note}


# ── 自升级（修改服务自身代码）───────────────────────────────────
@app.get("/v1/agent/memory")
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


@app.post("/v1/agent/memory/{mid}/pin")
async def pin_memory(mid: int, pinned: bool = True, scope: str = "all"):
    """置顶/取消置顶一条记忆：置顶的会在每次检索时恒定注入提示词。"""
    if not b.MEMORY.set_pinned(mid, pinned, scopes=b.memory_scopes(scope)):
        raise HTTPException(404, "记忆不存在或不属于当前可见作用域")
    return {"id": mid, "pinned": pinned}


@app.delete("/v1/agent/memory/{mid}")
async def delete_memory(mid: int, scope: str = "all"):
    """删除一条记忆（作用域做同属校验，不能跨工作区删）。"""
    if not b.MEMORY.forget(mid, scopes=b.memory_scopes(scope)):
        raise HTTPException(404, "记忆不存在或不属于当前可见作用域")
    return {"deleted": True, "id": mid}


@app.get("/v1/agent/self/status")
async def self_status():
    """自升级状态：是否启用、白名单、待重启标记、自升级快照作用域。"""
    return b.self_upgrade_status()


@app.get("/v1/agent/self/snapshots")
async def self_snapshots(sid: str | None = None):
    """自升级改动的快照摘要（不传 sid 时列出所有自升级作用域）。"""
    if sid:
        return b.SNAPSHOTS.summary(b.self_scope(sid))
    scopes = b.self_upgrade_status()["snapshot_scopes"]
    return {"scopes": [{"scope": s,
                        "session_id": s[len("_self_"):] if s.startswith("_self_") else s,
                        "summary": b.SNAPSHOTS.summary(s)} for s in scopes]}


@app.post("/v1/agent/self/rollback")
async def self_rollback(sid: str | None = None, last: bool = False):
    """回滚服务自身代码的自升级改动（last=true 时取最近一次）。"""
    async with b.lock:
        result = await b.rollback_self_upgrade(sid, use_last=last)
    return result


@app.post("/v1/agent/sessions/{sid}/actions/{action}")
async def control(sid: str, action: str):
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    maps = {"pause": {"state": "paused"}, "resume": {"state": "ready", "cancel_requested": False, "rollover_requested": False},
            "cancel": {"state": "cancelled", "cancel_requested": True}, "compact": {"compact_requested": True},
            "rollover": {"rollover_requested": True}, "archive": {"state": "archived", "archived": True}}
    if action not in maps:
        raise HTTPException(400, "未知操作")
    prev_state = (b.AGENT_SESSIONS.get(sid) or {}).get("state")
    if action == "resume":
        if prev_state in {"paused", "waiting", "ready", "failed"}:
            # 恢复即续跑：直接从检查点拉起 runner，不再需要手动"继续执行"
            started = await b.resume_session_task(sid)
        else:
            # 终态会话（completed/cancelled/archived）不改成 ready 也不拉起
            started = False
            return {**b.AGENT_SESSIONS.get(sid), "runner_started": False,
                    "note": f"会话状态为 {prev_state}，不支持恢复"}
        result = dict(b.AGENT_SESSIONS.get(sid) or {})
        result["runner_started"] = started
        return result
    return b.AGENT_SESSIONS.patch(sid, **maps[action])


@app.delete("/v1/agent/sessions/{sid}")
async def delete_session(sid: str, delete_remote: bool = False, force_local: bool = False):
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    # delete_remote + force_local 是 WebUI「重试远端并强制清理本地记录」的合法组合：
    # 先尽力删远端，失败的条目如实返回，再清本地。不视为参数冲突。

    remote_results = []
    if delete_remote:
        urls = list(dict.fromkeys(state.get("conversation_urls") or []))
        if state.get("current_url") and state["current_url"] not in urls:
            urls.append(state["current_url"])
        async with b.lock:
            # 运行权判定必须在锁内复检：锁外判完再取锁之间，runner 可能刚好
            # 拿到锁开始执行（TOCTOU），此时删掉 sessions 行会让它的落库全崩。
            if b.ACTIVE_SESSION_ID == sid:
                raise HTTPException(409, "Session 正在运行，不能删除")
            for index, url in enumerate(urls):
                marker = b._agent_chat_marker(sid, index)
                remote_results.append(await cd.delete_copilot_conversation(url, marker))
            # 对账式终验：按 [CBP-AGENT][sid] 标记清点侧栏——补删漏网会话，
            # 并把弹窗识别误判的失败条目改判为已删除（以侧栏实际残留为准）。
            remote_results = await cd.finalize_session_remote_deletion(sid, remote_results)
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
    if local_deleted:
        # 会话记录已删除，其快照不再可回滚（回滚端点要求会话存在）：回收磁盘。
        # 自升级作用域 _self_<sid> 同样要清——否则孤儿作用域会被"回滚最近一次
        # 自升级"选中，把服务代码恢复到某个已不存在任务的旧原像。
        b.SNAPSHOTS.clear(sid)
        b.SNAPSHOTS.clear(b.self_scope(sid))
    return {
        "deleted": local_deleted,
        "local_deleted": local_deleted,
        "remote_results": remote_results,
        "remote_skipped": "未记录任何远端会话 URL，跳过远端删除" if delete_remote and not remote_results else None,
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
    model_id = b.normalize_model_soft(req.model or state.get("model") or b.DEFAULT_MODEL)
    if model_id != (req.model or state.get("model")):
        b.AGENT_SESSIONS.patch(sid, model=model_id)  # 清理会话里遗留的失效模型
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
        try:
            result, _ = await b.run_agent(agent_req)
        except Exception as exc:
            raise _map_agent_error(exc)
        answer = result.content
        current = b.AGENT_SESSIONS.get(sid)
        b.AGENT_SESSIONS.add_message(sid, "assistant", answer, int(current.get("conversation_index", 0)))
        return {"session_id": sid, "role": "assistant", "content": answer,
                "url": current.get("current_url"), "agent_steps": result.steps,
                "mode": "agent"}

    # Plain chat mode keeps the same Copilot web conversation and does not run tools.
    pm = b.providers_mod.split_provider_model(model_id)
    if pm and pm[0] in b.PROVIDERS:
        return await b.provider_session_chat(sid, content, model_id)
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
    if b.page is None:
        raise HTTPException(503, "浏览器未就绪")
    if b.lock.locked():
        raise HTTPException(409, "有请求/任务正在使用浏览器，请稍后再切换页面")
    async with b.lock:
        # 回合等待中导航全局 page 会把回答错绑到别的会话，必须持锁
        await b.page.bring_to_front()
        if b.page.url != url:
            await b.page.goto(url, wait_until="domcontentloaded", timeout=120000)
            b._forget_selected_model()
    b.runtime_update(sid, phase="activated", detail="已切换到该 Session 的 Copilot 页面")
    return {"activated": True, "url": b.page.url, "headless": b.HEADLESS_CURRENT}


@app.get("/v1/agent/workspace")
async def workspace():
    return {"workspace": str(b.AGENT_TOOLS.root)}


@app.post("/v1/agent/workspace")
async def set_workspace(payload: dict):
    if b.lock.locked():
        raise HTTPException(409, "有任务正在运行，不能切换工作区")
    new_root = b.AGENT_TOOLS.set_root(str(payload.get("path", "")), bool(payload.get("create", False)))
    b.DEFAULT_WORKSPACE = new_root  # 作为之后新任务的默认工作区
    return {"workspace": new_root}


@app.get("/v1/agent/tools")
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
    if isinstance(exc, TimeoutError):
        return HTTPException(504, f"等待 Copilot 回答超时: {exc}")
    return HTTPException(500, str(exc))


@app.post("/v1/agent/completions")
async def agent_completions(req: AgentRequest):
    if not req.agent_mode:
        # 普通对话模式：创建 Session 绑定 Copilot 会话，但不跑工具循环
        return await b.plain_completions(req)
    if req.stream:
        return StreamingResponse(stream_agent(req), media_type="text/event-stream")
    try:
        result, sid = await b.run_agent(req)
    except Exception as exc:
        raise _map_agent_error(exc)
    return {"id": "agent-" + uuid.uuid4().hex, "object": "chat.completion",
            "created": int(time.time()), "model": req.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": result.content}, "finish_reason": "stop"}],
            "agent_steps": result.steps, "session_id": sid, "workspace": str(b.AGENT_TOOLS.root)}
