"""HTTP 路由层：FastAPI 应用与全部端点。

浏览器自动化、会话编排与全局状态在 bridge.py；本模块只做请求/响应适配。
"""

import asyncio
import hmac
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from contextlib import asynccontextmanager, AsyncExitStack

from fastapi import FastAPI, HTTPException, Request as HttpRequest
from fastapi.responses import (StreamingResponse, FileResponse, RedirectResponse,
                             JSONResponse)
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from playwright.async_api import async_playwright
from . import i18n as backend_i18n
from . import prompt_lang

from . import bridge as b
from . import copilot_delete as cd
from . import lifecycle
from .bridge import Message, Request, SessionMessageRequest, AgentRequest

# ── 访问控制 ──
# api_token：配置后所有 /v1/* 端点要求 "Authorization: Bearer <token>"。
# 非回环地址监听（0.0.0.0/局域网）且未配置 token 时拒绝启动——该服务能读写
# 文件、操作浏览器，裸奔到公网/内网是不可接受的风险。
API_TOKEN = str(b.CFG.get("api_token") or "")
# 同源/CSRF 防护（默认开启）：拦"会改状态"的跨站请求。非浏览器客户端（curl/评测脚本）
# 不带 Origin/Sec-Fetch-*，因此照常可用。可用 csrf_protect=false 关闭（不推荐）。
CSRF_PROTECT = bool(b.CFG.get("csrf_protect", True))
CSRF_ALLOWED_ORIGINS = b.CFG.get("csrf_allowed_origins") or []
_SERVER_HOST = str(b.CFG.get("server", {}).get("host", "127.0.0.1"))
if _SERVER_HOST not in ("127.0.0.1", "localhost", "::1") and not API_TOKEN:
    raise SystemExit(
        f"拒绝启动：server.host={_SERVER_HOST} 是非回环地址，但未配置 api_token。"
        "请在 config.json 设置 api_token，或把 host 改回 127.0.0.1。"
    )


# 浏览器启动状态：API 不再等浏览器就绪才监听端口——没有 Copilot（未登录/
# 租户不可用/浏览器坏）时，WebUI 与提供商模型（DeepSeek/Kimi/OpenAI）必须照常可用。
BOOT: dict = {"browser_state": "idle", "browser_error": None, "copilot_ready": False,
              "task": None, "generation": 0, "lock": asyncio.Lock()}


async def boot_browser() -> None:
    """后台拉起浏览器 → 等 Copilot 就绪 → 模型发现 → 续跑中断会话。

    任何失败都只记状态与日志，不抛出：API 与提供商路径不受影响。
    """
    BOOT["generation"] += 1
    gen = BOOT["generation"]
    BOOT["browser_state"] = "starting"
    BOOT["browser_error"] = None
    BOOT["copilot_ready"] = False
    try:
        b.PROFILE.mkdir(exist_ok=True)
        if b.pw is None:
            b.pw = await async_playwright().start()
        await b._start_browser(bool(b.CFG.get("headless", False)))
        if gen != BOOT["generation"]:
            return                      # 已被更新的启动请求取代
        BOOT["browser_state"] = "browser_up"
        applied = b.mark_self_upgrade_applied()
        if applied:
            b.log(f"[自升级] 已加载上一轮自升级改动（{len(applied)} 条记录），待重启标记归档")
        if await b.wait_copilot_ready():
            BOOT["copilot_ready"] = True
            BOOT["browser_state"] = "ready"
            b.log("API 与浏览器已就绪")
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
            if bool(b.AGENT_CFG.get("auto_resume", False)):
                try:
                    await b.auto_resume_interrupted()
                except Exception as exc:
                    b.log(f"自动恢复会话失败: {exc}")
        else:
            BOOT["browser_state"] = "needs_login"
            b.log("Copilot 未就绪（可能需要在自动化窗口完成登录）——"
                  "服务与提供商模型照常可用，可稍后点『重试浏览器』")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        BOOT["browser_state"] = "failed"
        BOOT["browser_error"] = str(exc)
        b.log(f"浏览器启动失败（不影响 API 与提供商模型）: {exc}")
        # 有任务正在用窗口时**不要**停掉 Playwright：那会把用户正看着的窗口一起关掉
        try:
            _busy = bool(b.WINDOWS.running())
        except Exception:
            _busy = False
        if not _busy:
            # 上下文也要一起丢掉：只停 Playwright 而留着 ctx，下一次「重试浏览器」
            # 会复用已经死掉的 context（表现为重试永远 failed / needs_login）。
            try:
                if b.ctx is not None:
                    await b.ctx.close()
            except Exception:
                pass
            b.ctx = None
            b.set_shared_page(None)
            try:
                if b.pw is not None:
                    await b.pw.stop()
            except Exception:
                pass
            b.pw = None
        else:
            b.log("有任务正在使用浏览器，保留当前窗口（不再停止 Playwright）")


async def ensure_browser(wait_seconds: float = 240) -> bool:
    """按需启动浏览器：真正要用 Copilot 时才拉起，提供商路径永不触发。

    并发请求只会启动一次（同一把锁 + 复用进行中的任务）；浏览器本来就绪则
    直接返回。返回 copilot_ready（False 表示需要登录或启动失败）。
    """
    try:
        if BOOT["copilot_ready"] and b.current_page() is not None and not b.current_page().is_closed():
            return True
    except Exception:
        pass
    async with BOOT["lock"]:
        try:
            if BOOT["copilot_ready"] and b.current_page() is not None and not b.current_page().is_closed():
                return True
        except Exception:
            pass
        task = BOOT.get("task")
        if task is None or task.done():
            task = asyncio.create_task(boot_browser())
            BOOT["task"] = task
    try:
        # shield：等待超时不要取消后台启动，让它继续跑完（SSO 可能很久）
        await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
    except asyncio.TimeoutError:
        b.log(f"浏览器启动仍在进行（已等 {wait_seconds:.0f} 秒），本次请求先按未就绪处理")
    except Exception:
        pass
    return bool(BOOT["copilot_ready"])


@asynccontextmanager
async def lifespan(app):
    # 按需启动（默认）：启动时不碰浏览器——没有 Copilot 也能用 WebUI 与提供商模型。
    # 需要热启动（例如常驻自用）就把 config.json 的 browser_startup 设为 "eager"。
    # 上次服务异常退出时留下的 running/waiting 会话，标记为可从检查点恢复。
    # （以前这句写在 gantry/bridge.py 的模块级，导致任何 import 都会误伤在跑的任务。）
    try:
        b.AGENT_SESSIONS.recover_interrupted()
    except Exception as exc:
        b.log(f"恢复中断会话失败（忽略）：{exc}")
    boot = None
    mode = str(b.CFG.get("browser_startup", "lazy")).lower()
    if mode == "eager":
        boot = asyncio.create_task(boot_browser())
        BOOT["task"] = boot
        b.log(f"{_APP_NAME} v{_APP_VERSION} 已启动（browser_startup=eager：浏览器在后台预热）")
    else:
        b.log(f"{_APP_NAME} v{_APP_VERSION} 已启动（browser_startup=lazy：首次使用 Copilot 时才启动浏览器）")
    cleanup_task = asyncio.create_task(b.periodic_session_cleanup())
    suspend_task = asyncio.create_task(periodic_suspend_resume())
    # 插件系统：启动时加载已启用的插件（data/plugins.json 记启停）
    try:
        b.PLUGINS.load_all()
        loaded = [p["name"] for p in b.PLUGINS.list() if p.get("status") == "loaded"]
        if loaded:
            b.log(f"插件已加载: {', '.join(loaded)}")
    except Exception as exc:
        b.log(f"插件加载异常（不影响主服务）: {exc}")
    try:
        yield
    finally:
        pending = [t for t in (boot, cleanup_task, suspend_task,
                              BOOT.get("task")) if t is not None]
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        await b.shutdown_browser()


b.ENSURE_BROWSER = ensure_browser   # 桥接层按需拉起浏览器的入口

async def periodic_suspend_resume():
    """限流挂起的会话：窗口到点后自动继续（受 agent.auto_resume 控制）。

    挂起的目的就是"别在窗口里空转"——由这个 30 秒一次的巡检统一收口，
    而不是让 runner 在退避循环里逐次撞墙。
    """
    while True:
        try:
            await asyncio.sleep(30)
            if not bool(b.AGENT_CFG.get("auto_resume", True)):
                continue
            now = time.time()
            rows = [r for r in b.AGENT_SESSIONS.list(include_archived=True)
                    if str(r.get("state")) == "suspended"]
            for row in rows:
                end = float(row.get("retry_resume_at") or 0)
                if not end or end > now:
                    continue
                sid = row.get("id")
                try:
                    await b.resume_session_task(sid)
                    b.AGENT_SESSIONS.patch(sid, retry_resume_at=None, retry_attempt=0,
                                           suspend_reason=None,
                                           last_error="限流窗口已过，已自动继续")
                    b.log(f"限流窗口结束，自动继续会话 {sid}")
                except Exception as exc:
                    b.log(f"挂起会话自动继续失败（{sid}）：{exc}")
        except asyncio.CancelledError:
            return
        except Exception as exc:                     # noqa: BLE001 周期任务不能死
            try:
                b.log(f"挂起巡检异常: {exc}")
            except Exception:
                pass


from .version import VERSION as _APP_VERSION
from .brand import APP_NAME as _APP_NAME, APP_TITLE as _APP_TITLE, env as _brand_env
app = FastAPI(title=_APP_TITLE, version=_APP_VERSION, lifespan=lifespan)

# 语言头：新名优先，旧的 X-CBP-Lang 继续接受（旧客户端/旧脚本不坏）。
_LANG_HEADER = "X-Gantry-Lang"
_LANG_HEADER_LEGACY = "X-CBP-Lang"


def _lang_of(request) -> str:
    """从 X-Gantry-Lang（兼容 X-CBP-Lang）头取界面语言，缺省中文。"""
    try:
        raw = (request.headers.get(_LANG_HEADER)
           or request.headers.get(_LANG_HEADER_LEGACY) or "zh").lower()[:2]
    except Exception:
        return "zh"
    return raw




# ── 提示词语言：每个请求按语言头设一次 contextvar ────────────────────────
# 由它决定发给模型的提示词用简体/繁體/English（见 gantry/prompt_lang.py）。
# 放在中间件里设，端点与它创建的后台任务（asyncio 任务继承 context）都能读到。
@app.middleware("http")
async def _prompt_lang_middleware(request: HttpRequest, call_next):
    try:
        _lang_raw = (request.headers.get(_LANG_HEADER)
                     or request.headers.get(_LANG_HEADER_LEGACY) or "zh")
        prompt_lang.set_request_lang(_lang_raw)
    except Exception:
        pass
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def _localized_http_error(request: HttpRequest, exc: StarletteHTTPException):
    lang = (request.headers.get(_LANG_HEADER)
            or request.headers.get(_LANG_HEADER_LEGACY) or "zh").lower()[:2]
    detail = exc.detail
    if isinstance(detail, str):
        detail = backend_i18n.translate(detail, lang)
    return JSONResponse({"detail": detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))



# ── 同源/CSRF 防护（仅拦"会改状态"的方法）──────────────────────────────
# 威胁模型：服务默认无 token，任意网页都能用 no-cors 简单请求向 127.0.0.1 发 POST
# （不触发 CORS 预检），从而取消/归档任务、回滚服务代码、删会话。仅靠"读不到响应"
# 挡不住副作用。这里按浏览器会发的三个信号判断：Origin / Referer / Sec-Fetch-Site。
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_local_origin(value: str) -> bool:
    """origin/referer 是否属于本机回环（含端口比较由调用方完成）。"""
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
            return False
        # 端口必须与请求一致（同源）；未写端口时按同主机放行
        return True
    except Exception:
        return False


@app.middleware("http")
async def origin_guard(request, call_next):
    """阻断跨站写入。非浏览器客户端（无 Origin/Sec-Fetch-*）一律放行。"""
    if CSRF_PROTECT and request.method.upper() in _WRITE_METHODS:
        from fastapi.responses import JSONResponse

        site = (request.headers.get("sec-fetch-site") or "").lower()
        origin = request.headers.get("origin") or ""
        referer = request.headers.get("referer") or ""
        allow_extra = tuple(CSRF_ALLOWED_ORIGINS or ())
        if site in ("cross-site", "same-site"):
            return JSONResponse({"detail": "拒绝跨站写入请求（Sec-Fetch-Site）"}, status_code=403)
        if origin:
            if origin in allow_extra:
                pass
            elif _is_local_origin(origin):
                want = request.url.port or (443 if request.url.scheme == "https" else 80)
                from urllib.parse import urlsplit
                got = urlsplit(origin).port
                if got and int(got) != int(want):
                    return JSONResponse({"detail": "拒绝来自其它端口的写入请求（跨源）"},
                                        status_code=403)
            else:
                return JSONResponse({"detail": "拒绝跨站写入请求（Origin 非本机）"}, status_code=403)
        elif referer and not _is_local_origin(referer) and referer not in allow_extra:
            return JSONResponse({"detail": "拒绝跨站写入请求（Referer 非本机）"}, status_code=403)
    resp = await call_next(request)
    # 安全响应头：CSP 里保留 'unsafe-inline' 是因为 UI 目前用了内联 onclick，
    # 去掉会直接让界面失效（已记为后续重构项）；其余指令仍然收紧可加载源。
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'")
    return resp


@app.middleware("http")
async def token_auth(request, call_next):
    # 配置了 token 时，接口与文档（/docs、/openapi.json）都要鉴权：
    # 否则任何能访问 loopback 的页面都能读到全部路由与参数 schema。
    guarded = ("/v1/", "/docs", "/redoc", "/openapi.json")
    if API_TOKEN and request.url.path.startswith(guarded):
        from fastapi.responses import JSONResponse
        auth = request.headers.get("authorization", "")
        header_token = (request.headers.get("x-gantry-token")
                        or request.headers.get("x-cbp-token") or "")
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
        """按**最终文本**脱敏：uvicorn 的 args 既可能是 tuple、也可能是 dict，
        只处理 tuple 会漏（dict 形式的访问日志会把 ?token= 原样写盘）。"""
        try:
            text = record.getMessage()
            if "token=" in text.lower():
                record.msg = self._RE.sub(r"\1***", text)
                record.args = ()          # 已合并进 msg，避免二次格式化
        except Exception:
            pass
        return True


_redact = _RedactTokenFilter()
for _name in ("uvicorn.access", "uvicorn.error", "gantry", ""):
    try:
        logging.getLogger(_name).addFilter(_redact)
    except Exception:
        pass








async def nonstream(req, prompt):
    parts = []
    rid = "chatcmpl-" + uuid.uuid4().hex
    async with b._window(_chat_sid(), need_page=True):
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


# ── 端点拆分（第五批）：APIRouter 注册 + 原样重导出（保测试打桩语义）──
from .routes_core import (  # noqa: F401
    sse,
    _chat_sid,
    stream_response,
    stream_agent,
    stream_provider,
    root,
    health,
    shutdown_service,
    serve_upload,
    models,
    refresh_models,
    help_get,
    chat,
    web_ui,
    web_ui_index,
    web_ui_asset,
    search_messages,
    restart_service,
    list_spans,
    agent_tools,
    _map_agent_error,
    agent_completions,
)
from .routes_config import (  # noqa: F401
    agent_debug_download,
    agent_debug_page,
    get_sandbox,
    set_sandbox,
    CONFIG_SCHEMA,
    _cfg_get,
    _cfg_set,
    _coerce,
    get_config,
    set_config,
    plugin_registry,
    plugin_install,
    plugin_uninstall,
    plugin_i18n,
    plugin_panels,
    list_plugins,
    plugin_action,
    plugin_api,
    agent_debug_page,
    browser_state,
    set_browser_mode,
    pick_directory,
    browse_mkdir,
    browse_dirs,
    workspace,
    set_workspace,
    browser_retry,
)
from .routes_agents import (  # noqa: F401
    agent_deep_research,
    subagent_send,
    subagent_fork,
    agent_fanout,
    agent_audit,
    list_subagents,
    list_memory,
    pin_memory,
    delete_memory,
    self_status,
    self_snapshots,
    self_rollback,
)
from .routes_sessions import (  # noqa: F401
    sessions,
    session,
    session_snapshots,
    _session_workspace_root,
    session_changes,
    session_change_diff,
    RollbackRequest,
    session_rollback,
    SessionTagRequest,
    set_session_tag,
    set_session_model,
    control,
    session_deletion_plan,
    delete_session,
    MessageEditRequest,
    edit_session_message,
    delete_session_message,
    continue_session_message,
    continue_agent_session,
    InjectRequest,
    inject_session_message,
    _runtime_snapshot,
    session_events,
    agent_answer,
    session_runtime,
    session_prompt,
    list_queue,
    delete_queue_item,
    QueueEditRequest,
    edit_queue_item,
    activate_session,
    session_event_log,
    session_projection,
    list_approvals,
    grant_approval,
)
from . import routes_core, routes_config, routes_agents, routes_sessions  # noqa: E402
for _routes_mod in (routes_core, routes_config, routes_agents, routes_sessions):
    app.include_router(_routes_mod.router)
