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
    # （以前这句写在 cbp/bridge.py 的模块级，导致任何 import 都会误伤在跑的任务。）
    try:
        b.AGENT_SESSIONS.recover_interrupted()
    except Exception as exc:
        b.log(f"恢复中断会话失败（忽略）：{exc}")
    boot = None
    mode = str(b.CFG.get("browser_startup", "lazy")).lower()
    if mode == "eager":
        boot = asyncio.create_task(boot_browser())
        BOOT["task"] = boot
        b.log("API 已启动（browser_startup=eager：浏览器在后台预热）")
    else:
        b.log("API 已启动（browser_startup=lazy：首次使用 Copilot 时才启动浏览器）")
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


app = FastAPI(title="Copilot Local Bridge Agent", version="10.3.0-preview.2", lifespan=lifespan)

def _lang_of(request) -> str:
    """从 X-CBP-Lang 头取界面语言（缺省中文）。"""
    try:
        raw = (request.headers.get(_LANG_HEADER) or "zh").lower()[:2]
    except Exception:
        return "zh"
    return raw


# ── 错误文案本地化：全局异常处理器按 X-CBP-Lang 翻译 detail ─────────────
# 只改**展示**：日志、协议、模型看到的仍是中文原文（评测与协议绑定）。
_LANG_HEADER = "X-CBP-Lang"


# ── 提示词语言：每个请求按 X-CBP-Lang 设一次 contextvar ──────────────────
# 由它决定发给模型的提示词用简体/繁體/English（见 cbp/prompt_lang.py）。
# 放在中间件里设，端点与它创建的后台任务（asyncio 任务继承 context）都能读到。
@app.middleware("http")
async def _prompt_lang_middleware(request: HttpRequest, call_next):
    try:
        prompt_lang.set_request_lang(request.headers.get(_LANG_HEADER) or "zh")
    except Exception:
        pass
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def _localized_http_error(request: HttpRequest, exc: StarletteHTTPException):
    lang = (request.headers.get(_LANG_HEADER) or "zh").lower()[:2]
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
for _name in ("uvicorn.access", "uvicorn.error", "cbp", ""):
    try:
        logging.getLogger(_name).addFilter(_redact)
    except Exception:
        pass


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


async def stream_agent(req: AgentRequest):
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


@app.get("/")
async def root():
    return {"name": "Copilot Local Bridge Agent", "version": "10.3.0-preview.2", "docs": "/docs",
            "models": "/v1/models", "agent": "/v1/agent/completions"}


@app.get("/health")
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
        "copilot_ready": bool(BOOT.get("copilot_ready")),
        "browser_state": BOOT.get("browser_state"),
        "browser_error": BOOT.get("browser_error"),
        "busy": b.any_window_busy(),
        # 窗口池现状（max/windows/busy/waiting/running）：预览分支的多窗口排障入口
        "windows": b.WINDOWS.stats(),
        "page_url": page_url,
        "default_model": b.DEFAULT_MODEL,
        "headless": b.HEADLESS_CURRENT,
    }


@app.post("/v1/agent/debug/download")
async def agent_debug_download(payload: dict | None = None):
    """诊断：立即把当前页面的第一个下载候选存到 data/debug-download/。"""
    stem = str((payload or {}).get("stem") or "")
    # 点下载要驱动页面：独占一个窗口，免得把正在跑的任务页面点走
    try:
        async with b._window("", wait_timeout=10):
            return await b.debug_download_now(stem)
    except b.CopilotBusy as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/v1/agent/sandbox")
async def get_sandbox():
    """查询当前沙箱档位与可选值。"""
    return {"mode": str(getattr(b.AGENT_TOOLS, "sandbox_mode", "workspace-write")),
            "modes": list(b.SANDBOX_MODES),
            "plan_mode": bool(getattr(b.AGENT_TOOLS, "plan_mode", False))}


@app.post("/v1/agent/sandbox")
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
    "agent.context_token_budget": {"kind": "int", "min": 2000, "max": 2000000, "group": "exec",
                                   "label": "上下文预算（token）"},
    "agent.max_sessions": {"kind": "int", "min": 1, "max": 500, "group": "data",
                           "label": "最多保留会话数"},
    "agent.snapshot_retention": {"kind": "int", "min": 0, "max": 500, "group": "data",
                                 "label": "文件快照保留份数"},
    "agent.completed_session_retention_days": {"kind": "int", "min": 0, "max": 365,
                                               "group": "data", "label": "已完成会话保留天数"},
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


@app.get("/v1/config")
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


@app.post("/v1/config")
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


@app.post("/v1/agent/deep-research")
async def agent_deep_research(payload: dict | None = None, request: HttpRequest = None):
    """一键深研：research 插件收集证据 → Agent 综合成带来源的报告（新会话）。"""
    data = payload or {}
    query = str(data.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "query 不能为空")
    try:
        return await b.deep_research_task(query, str(data.get("model") or ""),
                                          int(data.get("angles") or 3), 3,
                                          lang=_lang_of(request))
    except b.ToolError as exc:
        raise HTTPException(400, str(exc))


@app.get("/v1/plugins/registry")
async def plugin_registry():
    """插件注册表：已安装版本/来源/启停状态 + 备份列表。"""
    return b.PLUGINS.registry_view()


@app.post("/v1/plugins/install")
async def plugin_install(payload: dict | None = None):
    """安装插件：{source, name?, force?}。source 可为目录 / .zip / git URL。"""
    data = payload or {}
    try:
        return b.PLUGINS.install(str(data.get("source") or ""),
                                 str(data.get("name") or ""),
                                 bool(data.get("force")))
    except b.PluginError as exc:
        raise HTTPException(400, str(exc))


@app.post("/v1/plugins/uninstall")
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


@app.get("/v1/plugins/i18n")
async def plugin_i18n():
    """已加载插件自带的译文（前端合并进字典，让 tr() 覆盖插件文案）。"""
    return b.PLUGINS.i18n()


@app.get("/v1/plugins/panels")
async def plugin_panels():
    """已加载插件声明的声明式面板（前端按 view 渲染）。"""
    return {"panels": b.PLUGINS.panels()}


@app.get("/v1/plugins")
async def list_plugins():
    """插件列表：名称/版本/状态/权限/贡献点（与 DSH 插件语义兼容）。"""
    return {"plugins": b.PLUGINS.list()}


@app.post("/v1/plugins/{name}/{action}")
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


@app.api_route("/v1/plugins/{plugin}/api/{path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def plugin_api(plugin: str, path: str, request: HttpRequest):
    """插件自定义接口的统一分发（启停不需要重启服务）。"""
    try:
        return await b.PLUGINS.dispatch_api(plugin, request.method, path, request)
    except b.PluginError as exc:
        raise HTTPException(404, str(exc))


@app.post("/v1/agent/subagents/{sid}/send")
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


@app.post("/v1/agent/subagents/{sid}/fork")
async def subagent_fork(sid: str, payload: dict | None = None):
    """从某个会话分叉出新的子代理（复制最近对话）。"""
    data = payload or {}
    try:
        return await b.fork_subagent(sid, str(data.get("description") or ""),
                                     int(data.get("keep_messages") or 60))
    except b.ToolError as exc:
        raise HTTPException(400, str(exc))


@app.post("/v1/agent/fanout")
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


@app.get("/v1/agent/subagents")
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


@app.get("/v1/agent/debug/page")
async def agent_debug_page():
    """诊断：当前自动化页面截图 + 下载入口（只读，排障用）。"""
    return await b.debug_page_snapshot()


@app.get("/v1/agent/browser")
async def browser_state():
    return {"headless": b.HEADLESS_CURRENT,
            "config_headless": bool(b.CFG.get("headless", False))}


@app.post("/v1/agent/browser")
async def set_browser_mode(payload: dict):
    await ensure_browser()   # 按需启动：走到 Copilot 路径才拉起浏览器
    """切换自动化浏览器窗口的显示/隐藏。

    headless 是启动参数，切换必然重建浏览器上下文（EdgeProfile 保留，
    登录态不受影响）。有请求/任务正在使用浏览器时返回 409 拒绝切换。
    persist=true 可把新模式写回 config.json。
    """
    target_raw = payload.get("headless")
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


@app.post("/v1/shutdown")
async def shutdown_service():
    """终止本服务（WebUI「终止服务/退出」按钮）。

    先返回响应、再延迟触发停机：直接在这里停会让前端拿到"连接被重置"，看起来
    像操作失败。正在运行的任务会被中断——状态都在 SQLite 里，重启后可恢复。
    优雅停机拿不到 uvicorn server 时由 cbp/lifecycle.py 退回信号，并始终有
    "宽限期后硬退出"兜底（按钮必须真的能停掉服务）。
    """
    if lifecycle.shutdown_pending():
        return {"stopping": True, "already": True, "note": "停机已在进行中",
            "note_code": "shutdown_pending"}
    # 用线程定时器而不是 loop.call_later：与事件循环生命周期无关（服务可能由
    # 不同方式启动/重启），也让"先回响应再停"可被测试观察到。
    lifecycle.schedule("shutdown", 0.4)
    return {"stopping": True,
            "note": "服务即将终止。正在运行的任务会中断（状态已落库，重启后可恢复）。",
            "note_code": "shutdown_soon"}


@app.get("/v1/uploads/{name}")
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


@app.post("/v1/models/refresh")
async def refresh_models():
    await ensure_browser()   # 按需启动：走到 Copilot 路径才拉起浏览器
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
    except b.CopilotBusy as exc:
        # 所有窗口都被占着：这是可重试的忙，不是服务端错误
        raise HTTPException(409, f"{exc}（可在设置里调大「Copilot 自动化窗口数」）") from exc
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
    return FileResponse(b.BASE / "webui" / "index.html", headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/ui/{asset}", include_in_schema=False)
async def web_ui_asset(asset: str):
    # 静态资产白名单，禁止路径穿越
    if asset not in ("style.css", "app.js", "boot.js", "i18n.js"):
        raise HTTPException(404, "Not Found")
    return FileResponse(b.BASE / "webui" / asset, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/v1/agent/sessions")
async def sessions(include_archived: bool = False, include_subagents: bool = True):
    """会话列表。默认**不返回子代理会话**——它们已经挂在父会话下（子代理面板可看），
    再占一行会把主列表淹掉。需要时传 include_subagents=true。"""
    rows = b.AGENT_SESSIONS.list(include_archived)
    # 默认返回**全部**（含子代理，带 parent_id）：WebUI 据此把子会话折叠到父会话下；
    # 需要扁平列表的调用方显式传 include_subagents=false。
    if not include_subagents:
        rows = [x for x in rows if not str(x.get("parent_id") or "").strip()]
    # 列表行也要带上"含子代理"的合计：侧栏与详情页/KPI 必须同一口径，否则同一个
    # 任务在两处显示两个步数/Token（用户实测）。用递归版本（含孙代理）。
    try:
        agg = b.AGENT_SESSIONS.subagent_totals_all()
        for x in rows:
            a = agg.get(str(x.get("id") or ""))
            if a and (a.get("count") or a.get("running")):
                x["subagent_count"] = a["count"]
                x["subagent_running"] = a["running"]
                x["subagent_steps"] = a["steps"]
                x["subagent_tokens"] = a["tokens"]
    except Exception:
        pass
    return {"sessions": rows, "subagent_count": sum(
        1 for x in rows if str(x.get("parent_id") or "").strip())}


@app.get("/v1/agent/sessions/{sid}")
async def session(sid: str):
    x = b.AGENT_SESSIONS.get(sid)
    if not x:
        raise HTTPException(404, "Session 不存在")
    try:
        _sub = b.AGENT_SESSIONS.subagent_stats(sid)
    except Exception:
        _sub = {"count": 0, "steps": 0, "tokens": 0}
    x = dict(x)
    x["subagent_count"] = _sub.get("count", 0)
    x["subagent_running"] = _sub.get("running", 0)
    x["subagent_steps"] = _sub.get("steps", 0)
    x["subagent_tokens"] = _sub.get("tokens", 0)
    return {"session": x, "steps": b.AGENT_SESSIONS.steps(sid),
            "files": b.AGENT_SESSIONS.file_events(sid),
            "messages": b.AGENT_SESSIONS.messages(sid),
            "token_budget": b.TOKEN_BUDGET, "conv_stats": b.AGENT_SESSIONS.conv_stats(sid),
            "snapshots": b.SNAPSHOTS.summary(sid)}


@app.get("/v1/agent/sessions/{sid}/snapshots")
async def session_snapshots(sid: str):
    """任务改动快照摘要：可回滚的文件清单 + 版本点列表。"""
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    summary = b.SNAPSHOTS.summary(sid)
    summary["versions"] = b.SNAPSHOTS.versions(sid)
    return summary


def _session_workspace_root(sid: str) -> Path:
    state = b.AGENT_SESSIONS.get(sid) or {}
    return Path(str(state.get("workspace") or b.AGENT_TOOLS.root))


@app.get("/v1/agent/sessions/{sid}/changes")
async def session_changes(sid: str):
    """任务完成后的「改了哪些文件」：每个文件的行级增删 + present 登记的交付物。

    数据来自**快照原像**（写操作前的状态）对比当前文件，不依赖 git；任务还在跑时
    也可以调用（前端只在完成/空闲时展示）。二进制文件只列路径，不给行数。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    try:
        data = b.SNAPSHOTS.changes(sid, default_root=_session_workspace_root(sid))
    except Exception as exc:
        raise HTTPException(500, f"统计改动失败: {exc}") from exc
    data["deliverables"] = b.declared_deliverables(sid)
    data["session_state"] = state.get("state")
    return data


@app.get("/v1/agent/sessions/{sid}/changes/diff")
async def session_change_diff(sid: str, path: str):
    """单个文件的 unified diff（只允许该会话快照里记录过的路径，杜绝任意读文件）。"""
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    out = b.SNAPSHOTS.diff(sid, path, default_root=_session_workspace_root(sid))
    if out is None:
        raise HTTPException(404, "该路径不在本任务的改动记录里")
    return out


class RollbackRequest(BaseModel):
    version: int | None = None


@app.post("/v1/agent/sessions/{sid}/rollback")
async def session_rollback(sid: str, req: RollbackRequest | None = None):
    """回滚该 Session 的工作区改动。

    不带 version：整任务回滚到开始前；带 version：撤销该版本之后的写操作。
    """
    version = req.version if req else None
    if b.session_running(sid):
        raise HTTPException(409, "Session 正在运行，不能回滚（先暂停或终止再回滚）")
    result = await b.rollback_session_changes(sid, version=version)
    return result


class SessionTagRequest(BaseModel):
    tag: str = Field(default="", max_length=40)


@app.post("/v1/agent/sessions/{sid}/tag")
async def set_session_tag(sid: str, req: SessionTagRequest):
    """给会话打标签（列表按标签分组；空串=取消标签）。

    标签是纯本地的组织手段：不参与 Agent 逻辑，也不影响恢复/续跑。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    tag = (req.tag or "").strip()
    b.AGENT_SESSIONS.patch(sid, tag=tag)
    return {"session_id": sid, "tag": tag}


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
    return {**updated, "applies": "下一回合生效（服务重启后仍沿用）",
            "applies_code": "next_turn", "note": note}


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
    if b.any_task_busy():
        raise HTTPException(409, "有任务正在运行，不能回滚服务代码（先暂停/终止任务）")
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
        if prev_state in {"paused", "waiting", "suspended", "ready", "failed"}:
            # 恢复即续跑：直接从检查点拉起 runner，不再需要手动"继续执行"
            # was_running 决定提示语：runner 还活着（占着窗口/逻辑槽）时只唤醒不新建
            was_running = b.session_running(sid)
            started = await b.resume_session_task(sid)
        else:
            # 终态会话（completed/cancelled/archived）不改成 ready 也不拉起
            started = False
            return {**b.AGENT_SESSIONS.get(sid), "runner_started": False,
                    "note": f"会话状态为 {prev_state}，不支持恢复", "note_kind": "err",
                    "note_code": "resume_terminal", "note_vars": {"state": str(prev_state)}}
        result = dict(b.AGENT_SESSIONS.get(sid) or {})
        result["runner_started"] = started
        if started:
            result["note"] = "已恢复，正在续跑任务"
            result["note_kind"] = "ok"
        elif was_running or result.get("state") == "ready":
            # runner 还活着（正停在暂停循环里），已把状态改回 ready 唤醒它
            result["note"] = "已解除暂停：该会话的 runner 仍在运行，会继续往下跑"
            result["note_kind"] = "ok"
        else:
            result["note"] = "未能拉起 runner（会话可能正被其它任务占用），请稍后重试"
            result["note_kind"] = "err"
        return result
    return b.AGENT_SESSIONS.patch(sid, **maps[action])


@app.get("/v1/agent/sessions/{sid}/deletion-plan")
async def session_deletion_plan(sid: str):
    """删除前先问一句：这个任务用过 Copilot 网页会话吗？

    WebUI 用它做成"一次点击、自动判断"的删除：用过 Copilot 就一并删远端记录，
    只跑过 API 提供商模型的就只删本地——用户不必自己分辨该点哪个菜单项。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    urls = list(dict.fromkeys(state.get("conversation_urls") or []))
    if state.get("current_url") and state["current_url"] not in urls:
        urls.append(state["current_url"])
    model = str(state.get("model") or "")
    used_copilot = bool(urls) or model.startswith("copilot")
    return {
        "session_id": sid,
        "used_copilot": used_copilot,
        "model": model,
        "conversation_urls": urls,
        "remote_count": len(urls),
        "running": b.session_running(sid),
        # 本地记录永远可删；远端只在真用过 Copilot 时才有意义
        "suggest_delete_remote": used_copilot and bool(urls),
    }


@app.delete("/v1/agent/sessions/{sid}")
async def delete_session(sid: str, delete_remote: bool = False, force_local: bool = False,
                         request: HttpRequest = None):
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
        if b.session_running(sid):
            raise HTTPException(409, "Session 正在运行，不能删除")
        try:
            # 删除要导航 + 点侧栏，必须独占一个窗口；以前只拿全局锁，多窗口下会
            # 和任务抢同一个页面（删到一半被别的回合导航走 → 删错会话）。
            # 浏览器还没起来时（只有本地记录 / 测试）不为了删除去拉起浏览器：
            # 没有页面时远端删除本来就会逐条报失败，force_local 仍能清本地。
            stack = AsyncExitStack()
            try:
                if b.current_page() is not None:
                    await stack.enter_async_context(b._window("", wait_timeout=15))
                # 取到窗口后再复检一次：等待窗口期间该会话可能刚被拉起 runner
                if b.session_running(sid):
                    raise HTTPException(409, "Session 正在运行，不能删除")
                for index, url in enumerate(urls):
                    marker = b._agent_chat_marker(sid, index)
                    remote_results.append(await cd.delete_copilot_conversation(url, marker))
                # 对账式终验：按 [CBP-AGENT][sid] 标记清点侧栏——补删漏网会话，
                # 并把弹窗识别误判的失败条目改判为已删除（以侧栏实际残留为准）。
                remote_results = await cd.finalize_session_remote_deletion(sid, remote_results)
                b._forget_selected_model()  # 删除过程中页面在多个会话间跳转
            finally:
                await stack.aclose()
        except b.CopilotBusy as exc:
            raise HTTPException(409, f"{exc}（删除需要独占一个浏览器窗口）") from exc
        failed = [x for x in remote_results if not x["deleted"]]
        # force_local=true（WebUI 的"一并删，本地必须清掉"）不在这里中断：
        # 远端删不掉时仍然要删本地，并把失败的远端明细如实带回（否则用户看到的就是
        # "任务删不掉"——实测报错"左侧 Chats 中找不到…"时任务会留在列表里）。
        if failed and not force_local:
            lang = _lang_of(request)
            for item in remote_results:
                if item.get("error"):
                    item["error"] = backend_i18n.translate(str(item["error"]), lang)
            return {
                "deleted": False,
                "local_deleted": False,
                "remote_results": remote_results,
                "message": backend_i18n.translate(
                    "部分 Copilot 会话删除失败，本地 Session 已保留。可检查后使用 "
                    "force_local=true 仅清理本地记录。", lang),
            }

    local_deleted = b.AGENT_SESSIONS.delete(sid)
    b.RUNTIME.pop(sid, None)
    b.cancel_message_queue(sid)   # 清理该会话的排队消息与 worker
    b.discard_provider_agent_history(sid)   # 清理提供商 Agent 对话历史
    b.discard_interrupts(sid)               # 清理强行插入的中断信号
    if local_deleted:
        # 会话记录已删除，其快照不再可回滚（回滚端点要求会话存在）：回收磁盘。
        # 自升级作用域 _self_<sid> 同样要清——否则孤儿作用域会被"回滚最近一次
        # 自升级"选中，把服务代码恢复到某个已不存在任务的旧原像。
        b.SNAPSHOTS.clear(sid)
        b.SNAPSHOTS.clear(b.self_scope(sid))
    lang = _lang_of(request)
    for item in remote_results:
        if item.get("error"):
            item["error"] = backend_i18n.translate(str(item["error"]), lang)
    return {
        "deleted": local_deleted,
        "local_deleted": local_deleted,
        "remote_results": remote_results,
        "remote_skipped": backend_i18n.translate("未记录任何远端会话 URL，跳过远端删除", lang)
        if delete_remote and not remote_results else None,
    }


class MessageEditRequest(BaseModel):
    content: str = Field(min_length=1, max_length=200000)


@app.patch("/v1/agent/sessions/{sid}/messages/{mid}")
async def edit_session_message(sid: str, mid: int, req: MessageEditRequest):
    """编辑一条已入库的消息（WebUI 对话里的铅笔按钮）。

    ⚠ 语义要说清：只改本地历史。**已发给网页 Copilot 的内容无法撤回**；提供商
    会话则会把内存里的对话历史一并同步（否则模型下一回合仍看到旧文本）。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    msgs = {m["id"]: m for m in b.AGENT_SESSIONS.messages(sid)}
    old = msgs.get(int(mid))
    if old is None:
        raise HTTPException(404, "消息不存在")
    content = req.content.strip()
    if not content:
        raise HTTPException(400, "内容不能为空")
    if not b.AGENT_SESSIONS.update_message(sid, int(mid), content):
        raise HTTPException(500, "写入失败")
    patched = b.sync_provider_history_message(sid, str(old.get("content") or ""), content)
    note = ("已更新本地历史"
            + ("；提供商会话的上下文也已同步" if patched else "")
            + ("；已发给网页 Copilot 的内容无法撤回" if old.get("role") == "user" else ""))
    return {"updated": True, "message_id": int(mid), "history_synced": patched, "note": note}


@app.delete("/v1/agent/sessions/{sid}/messages/{mid}")
async def delete_session_message(sid: str, mid: int):
    """删除一条已入库的消息（只影响本地历史，语义同上）。"""
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    msgs = {m["id"]: m for m in b.AGENT_SESSIONS.messages(sid)}
    old = msgs.get(int(mid))
    if old is None:
        raise HTTPException(404, "消息不存在")
    if not b.AGENT_SESSIONS.delete_message(sid, int(mid)):
        raise HTTPException(500, "删除失败")
    patched = b.sync_provider_history_message(sid, str(old.get("content") or ""), None)
    return {"deleted": True, "message_id": int(mid), "history_synced": patched}


@app.get("/v1/search")
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


@app.post("/v1/agent/sessions/{sid}/messages")
async def continue_session_message(sid: str, req: SessionMessageRequest,
                                  request: HttpRequest = None):
    prompt_lang.set_request_lang(request.headers.get(_LANG_HEADER) if request is not None else None)
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    if state.get("state") == "archived":
        raise HTTPException(409, "Session 已归档，不能继续；请新建任务")
    if state.get("state") == "cancelled":
        # 用户在被取消的会话里又发了消息 = 明确想接着做：等价于点「恢复」，
        # 保留历史与工作区，把状态拉回 ready 再走正常入队（以前这里直接 409，
        # 界面上只得到一句原始报错，用户实测以为是坏了）。
        b.AGENT_SESSIONS.patch(sid, state="ready", cancel_requested=False)
        state = b.AGENT_SESSIONS.get(sid) or state
        b.log(f"会话 {sid} 状态为 cancelled，收到新消息后自动恢复")
    content = req.content.strip()
    attach_paths = b.save_image_attachments(req.attachments)
    if not content and not attach_paths:
        raise HTTPException(400, "消息不能为空")
    if not content:
        content = "请分析我附上的图片。"
    model_id = b.normalize_model_soft(req.model or state.get("model") or b.DEFAULT_MODEL)
    if model_id != (req.model or state.get("model")):
        b.AGENT_SESSIONS.patch(sid, model=model_id)  # 清理会话里遗留的失效模型
    # 先把用户消息落库（历史里立即可见），再入队异步处理，避免阻塞 HTTP 响应。
    # 附件只记文件名（data/uploads 下），绝对路径会因服务目录搬家而失效。
    message_id = b.AGENT_SESSIONS.add_message(sid, "user", content, int(state.get("conversation_index", 0)),
                                              attachments=[p.name for p in attach_paths])
    b.runtime_update(sid, phase="followup_queued", detail=f"跟进消息已排队：{content[:80]}")
    return await b.enqueue_session_message(sid, content, model_id,
                                           bool(req.agent_mode), attach_paths, message_id)


@app.post("/v1/agent/sessions/{sid}/continue")
async def continue_agent_session(sid: str, req: SessionMessageRequest):
    req.agent_mode = True
    return await continue_session_message(sid, req)


class InjectRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    queue: str = "turn"   # turn=整轮重排（默认）；step=立即生效并抢占批量剩余动作


@app.post("/v1/agent/sessions/{sid}/inject")
async def inject_session_message(sid: str, req: InjectRequest):
    """强行插入指令（类似 DSH 的 Ctrl+J）：不抢锁、不等这轮跑完，立即生效。

    区别于 /messages 的"排队跟进"（那要等当前 run 结束再起一轮）：这里把指令写进
    会话的 pending，运行中的 Agent **在每个回合开头都会重读 state**
    （agent.py: `state = self.state_getter()`），因此下一回合就优先执行——用于
    "打断当前计划、插入新要求"。

    会话空闲时 pending 没人消费，改为按普通跟进入队，避免指令悬空。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    if state.get("state") in {"cancelled", "archived"}:
        raise HTTPException(409, f"Session 状态为 {state.get('state')}，不能插入指令")
    content = req.content.strip()
    if not content:
        raise HTTPException(400, "指令不能为空")
    conv_idx = int(state.get("conversation_index", 0))
    running = b.session_running(sid)
    b.AGENT_SESSIONS.add_message(sid, "user", content, conv_idx)
    step = {"step": int(state.get("total_steps", 0)) + 1, "status": "user_inject",
            "reason": f"运行中插入指令：{content[:120]}",
            "result": {"chars": len(content), "running": running}}
    b.AGENT_SESSIONS.add_step(sid, step["step"], conv_idx, step)
    if running:
        # 两级队列（P1-8）：默认 turn（整轮按新指令重排）；queue=step 时进 next-step
        # 队列——立即注入下一条提示词，并在批量执行途中抢占剩余动作。
        _queue = str(getattr(req, "queue", None) or "turn").lower()
        if _queue == "step":
            pending_steps = [str(x) for x in (state.get("pending_steps") or []) if str(x).strip()]
            pending_steps.append(content)
            b.AGENT_SESSIONS.patch(sid, pending_steps=pending_steps)
            b.request_interrupt(sid)
            b.runtime_update(sid, detail=f"已插入紧急指令（next-step）：{content[:80]}")
            return {"session_id": sid, "injected": True, "running": True, "queued": False,
                    "interrupted": True, "queue": "step", "pending_steps": len(pending_steps),
                    "note": "已插入 next-step 队列：立即生效，且批量中的剩余动作会被放弃",
                    "note_code": "inject_step"}
        pending = [str(x) for x in (state.get("pending") or []) if str(x).strip()]
        pending.append(content)
        b.AGENT_SESSIONS.patch(sid, pending=pending)
        # 立即中断当前回合（提供商：取消 HTTP 调用；网页：仅在还没产出内容时中止
        # 等待），这样新指令不必等这一轮跑完。
        b.request_interrupt(sid)
        b.runtime_update(sid, detail=f"已插入指令并中断当前回合：{content[:80]}")
        return {"session_id": sid, "injected": True, "running": True, "queued": False,
                "interrupted": True, "pending": len(pending),
                "note": "已插入并中断当前回合：Agent 会立即按新指令继续",
                "note_code": "inject_turn"}
    # 空闲会话：pending 不会有人消费 -> 走队列起一轮（等同普通跟进）
    queued = await b.enqueue_session_message(
        sid, content, b.normalize_model_soft(state.get("model") or b.DEFAULT_MODEL),
        True, None, None)
    return {"session_id": sid, "injected": True, "running": False, "queued": True,
            "position": queued.get("position"),
            "note": "会话当前空闲，已按跟进排队执行（运行中插入才会即时生效）",
            "note_code": "inject_idle"}


def _runtime_snapshot(sid: str, state: dict) -> dict:
    """运行时快照的**唯一**构造点：HTTP `/runtime` 与 SSE 都调它。

    教训：SSE 之前只推 `RUNTIME` 原始内容，缺 session_state/total_steps/queue_pending/
    retry_resume_at/token_budget 等字段，前端收到后把状态置成「—」、KPI 清零、限流倒计时
    隐藏，下一秒 1 秒轮询又改回来——任务运行中界面每秒抖动。两条读取路径必须给出同一份事实。
    """
    runtime = dict(b.RUNTIME.get(sid) or {})
    try:
        _sub = b.AGENT_SESSIONS.subagent_stats(sid)
    except Exception:
        _sub = {"count": 0, "steps": 0, "tokens": 0}
    runtime.update({
        "session_id": sid,
        "session_state": (state or {}).get("state"),
        "total_steps": (state or {}).get("total_steps", 0),
        "conversation_steps": (state or {}).get("conversation_steps", 0),
        "estimated_tokens": (state or {}).get("estimated_tokens", 0),
        "conversation_index": (state or {}).get("conversation_index", 0),
        "current_url": (state or {}).get("current_url"),
        "token_budget": b.TOKEN_BUDGET,
        # 并行窗口：busy 只看"这个会话是否正占着一个窗口/逻辑槽"
        "busy": b.session_running(sid),
        "windows": b.WINDOWS.stats(),
        "queue_pending": b.queue_depth(sid),
        # 子代理合计（KPI 要显示"含子代理"的步数/token，否则跑完 4 个子代理界面看不出来）
        "subagent_steps": _sub.get("steps", 0),
        "subagent_tokens": _sub.get("tokens", 0),
        "subagent_count": _sub.get("count", 0),
        # 服务不可用的**持续可见**信号：last_error 在恢复后才清，比 retry_resume_at 持久。
        # 前端据此在顶栏显示"⚠ 服务不可用/自动重试"小标（不弹遮罩，不打断操作）。
        "service_error": (state or {}).get("last_error") or "",
        "service_waiting": (state or {}).get("state") == "waiting",
        # 仍有后代在跑的个数：父会话即使已 completed，界面也不能笼统显示"已完成"
        "subagent_running": _sub.get("running", 0),
        "retry_resume_at": (state or {}).get("retry_resume_at"),
        "retry_attempt": (state or {}).get("retry_attempt", 0),
        "rate_limited_since": (state or {}).get("rate_limited_since"),
        # 破坏性工具待授权：前端据此显示"授权执行 <tool>"入口（否则用户只能手工 curl）。
        # 只有真的处于 waiting 才暴露：历史会话可能残留旧值（bridge.save 已负责清理），
        # 那会让界面把已完成的任务显示成"需要授权"，且每秒重新弹出来。
        "approval_pending": ((state or {}).get("approval_pending")
                             if (state or {}).get("state") == "waiting" else None),
        # 授权范围：once（逐参数）/ session（本会话内该工具都放行），前端据此措辞
        "approval_scope": str(b.AGENT_CFG.get("approval_scope", "once")).lower(),
        # 目标（goal 模式）与计划（todo_write）：让界面能显示"第几轮/还剩几项"，
        # 否则 /goal 跑起来之后用户只能从时间线猜进度
        "sandbox_mode": str(getattr(b.AGENT_TOOLS, "sandbox_mode", "workspace-write")),
        "plan_mode": bool((state or {}).get("plan_mode")),
        "plan": (state or {}).get("plan"),
        "goal": (state or {}).get("goal"),
        "todos": (state or {}).get("todos"),
    })
    return runtime


@app.get("/v1/agent/sessions/{sid}/events")
async def session_events(sid: str):
    """SSE 实时推送：runtime 快照 / 队列变化 / 新消息 / 会话列表变化。

    前端用 EventSource 连它，就不用轮询了——提供商流式输出因此是真·逐字。
    EventSource 不能带自定义请求头，token 走 `?token=`（鉴权中间件已支持）。
    连接断开（客户端离开/服务重启）时在 finally 里退订，不留内存。
    """
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    queue = b.subscribe_events(sid)

    async def gen():
        try:
            # 先送一份当前快照，客户端连上即有内容，不必等第一次变化
            snap = _runtime_snapshot(sid, b.AGENT_SESSIONS.get(sid) or {})
            yield f"data: {json.dumps({'type': 'runtime', 'data': snap}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"        # 心跳：防代理/浏览器把空闲连接掐掉
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            b.unsubscribe_events(sid, queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


@app.post("/v1/agent/sessions/{sid}/answer")
async def agent_answer(sid: str, payload: dict | None = None):
    """回答 Agent 的中途提问（ask_user_question 阻塞等待的就是这里）。"""
    fut = b.EXTRA_ANSWERS.get(sid)
    if fut is None or fut.done():
        # 已经没有挂起提问：顺手把 runtime 里的 question 清掉，
        # 否则前端那条提问条会因为陈旧快照一直显示（再点还报错）。
        b.runtime_update(sid, question=None)
        raise HTTPException(404, "该会话没有等待回答的问题")
    answer = str((payload or {}).get("answer") or "")
    fut.set_result(answer)
    b.runtime_update(sid, question=None)   # 立刻清，别等轮询
    return {"ok": True, "session_id": sid, "answer": answer}


@app.get("/v1/agent/sessions/{sid}/runtime")
async def session_runtime(sid: str):
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    return _runtime_snapshot(sid, state)


@app.get("/v1/agent/sessions/{sid}/prompt")
async def session_prompt(sid: str):
    """查看该会话的提示词：初始（首回合完整内容）与最近一条。

    初始提示词是排查"Agent 为什么这么做"的第一现场（含系统提示词、工具清单、
    项目约定、验证闭环规则）。旧会话未落库时会**按当前工作区近似重建**并标注。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    initial = (state.get("initial_prompt") or "").strip()
    runtime = b.RUNTIME.get(sid) or {}
    out = {"session_id": sid, "task": state.get("original_task") or "",
           "initial_prompt": initial, "rebuilt": False, "rebuild_error": None,
           "last_prompt": runtime.get("prompt_preview") or ""}
    if not initial:
        try:
            out["initial_prompt"] = b.rebuild_initial_prompt(state)
            out["rebuilt"] = True
        except Exception as exc:
            out["rebuild_error"] = str(exc)
    return out


# ── 跟进消息队列：查看 / 修改 / 删除排队中的消息 ──────────────────────
@app.get("/v1/agent/sessions/{sid}/queue")
async def list_queue(sid: str):
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    return {"session_id": sid, "pending": b.queue_items(sid), "depth": b.queue_depth(sid)}


@app.delete("/v1/agent/sessions/{sid}/queue/{item_id}")
async def delete_queue_item(sid: str, item_id: str):
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    if not b.remove_queue_item(sid, item_id):
        raise HTTPException(404, "队列条目不存在（可能已被处理）")
    return {"session_id": sid, "removed": True, "depth": b.queue_depth(sid)}


class QueueEditRequest(BaseModel):
    content: str | None = None
    model: str | None = None


@app.post("/v1/agent/sessions/{sid}/queue/{item_id}")
async def edit_queue_item(sid: str, item_id: str, req: QueueEditRequest):
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    if not req.content and not req.model:
        raise HTTPException(400, "content 或 model 至少提供一个")
    if not b.update_queue_item(sid, item_id, req.content, req.model):
        raise HTTPException(404, "队列条目不存在（可能已被处理）")
    item = b.find_queue_item(sid, item_id)
    return {"session_id": sid, "updated": True,
            "item": {"id": item["id"], "content": item["content"], "model_id": item["model_id"]} if item else None}


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
    if b.any_window_busy():
        raise HTTPException(409, "有请求/任务正在使用浏览器，请稍后再切换页面")
    try:
        # 独占一个窗口再导航：多窗口下"共享页"可能正被某个任务的窗口用着
        async with b._window("", wait_timeout=10):
            # 回合等待中导航别的窗口会把回答错绑到别的会话，必须独占
            await b.page.bring_to_front()
            if b.page.url != url:
                await b.page.goto(url, wait_until="domcontentloaded", timeout=120000)
                b._forget_selected_model()
            shown_url = b.page.url
    except b.CopilotBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    b.runtime_update(sid, phase="activated", detail="已切换到该 Session 的 Copilot 页面")
    return {"activated": True, "url": shown_url, "headless": b.HEADLESS_CURRENT}


@app.post("/v1/agent/pick-directory")
async def pick_directory(payload: dict | None = None):
    """弹系统文件夹对话框（DSH native 后端的等价实现）；失败时前端回退到 browse。"""
    start = str((payload or {}).get("start") or "")
    try:
        return await b.pick_directory_native(start)
    except b.ToolError as exc:
        raise HTTPException(400, str(exc))


@app.post("/v1/agent/browse/mkdir")
async def browse_mkdir(payload: dict | None = None):
    """在工作目录选择器里新建子目录：{parent, name} → {path}。"""
    data = payload or {}
    try:
        return b.create_directory(str(data.get("parent") or ""), str(data.get("name") or ""))
    except b.ToolError as exc:
        raise HTTPException(400, str(exc))


@app.get("/v1/agent/browse")
async def browse_dirs(path: str | None = None):
    """列目录（只列子目录），供工作目录选择器；不传 path 从当前工作区开始。"""
    return b.browse_directories(path)


@app.get("/v1/agent/workspace")
async def workspace():
    return {"workspace": str(b.AGENT_TOOLS.root)}


@app.post("/v1/agent/workspace")
async def set_workspace(payload: dict):
    if b.any_task_busy():
        raise HTTPException(409, "有任务正在运行，不能切换工作区")
    new_root = b.AGENT_TOOLS.set_root(str(payload.get("path", "")), bool(payload.get("create", False)))
    b.DEFAULT_WORKSPACE = new_root  # 作为之后新任务的默认工作区
    return {"workspace": new_root}


@app.post("/v1/agent/browser/retry")
async def browser_retry():
    """重新初始化浏览器（登录完成后、或上次启动失败时用），不必重启服务。"""
    old = BOOT.get("task")
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(boot_browser())
    BOOT["task"] = task
    return {"started": True, "state": BOOT["browser_state"]}


@app.get("/v1/agent/sessions/{sid}/event-log")
async def session_event_log(sid: str, limit: int = 200, kinds: str = ""):
    """会话的 append-only 事件日志（P2-10），只读，一次性返回历史事件。

    与 `/events` 的区别：`/events` 是 **SSE 实时推送**（前端 EventSource 长连接，
    调用方要按流读取，普通 GET 会一直挂着——这是设计如此）；本路由用于排障与审计。
    `kinds` 支持逗号分隔过滤（如 `kinds=turn_begin,turn_done`）。
    """
    kind_filter = tuple(k.strip() for k in kinds.split(",") if k.strip()) or None
    events = b.AGENT_SESSIONS.events(sid, limit=limit, kinds=kind_filter)
    return {"session_id": sid, "count": len(events), "kinds": list(kind_filter or ()),
            "events": events}


@app.get("/v1/agent/sessions/{sid}/projection")
async def session_projection(sid: str):
    """由事件流重建的状态快照（P2-10 第二期），只读；并附与数据库列的一致性核对。

    `consistency` 为空列表表示"事件足以重建列里的事实"——这是第二期的验收标准。
    运行时读取路径仍走数据库列；本路由只是旁路视图，不改变任何行为。
    """
    from .projection import compare_with_columns, project

    proj = project(b.AGENT_SESSIONS.events(sid, limit=10000))
    con = b.AGENT_SESSIONS._connect()
    turns = [dict(r) for r in con.execute(
        "SELECT prompt_hash,status,steps_after FROM turn_results WHERE session_id=?", (sid,))]
    steps = [dict(r) for r in con.execute(
        "SELECT step_no,tool,status FROM step_dispatch WHERE session_id=?", (sid,))]
    return {"session_id": sid, "projection": proj,
            "consistency": compare_with_columns(proj, turns, steps)}


@app.get("/v1/agent/sessions/{sid}/approvals")
async def list_approvals(sid: str):
    """本会话待用的一次性授权（allowed-once，无 allow-always）。"""
    return {"pending": b.AGENT_SESSIONS.pending_approvals(sid),
            "session": b.AGENT_SESSIONS.get(sid) or {},
            "approval_pending": (b.AGENT_SESSIONS.get(sid) or {}).get("approval_pending")}


@app.post("/v1/agent/sessions/{sid}/approvals")
async def grant_approval(sid: str, payload: dict):
    """用户下达一次性授权：{"tool": "delete_path", "args_hash": "<可选，精确到参数>"}

    不传 args_hash 时，按会话状态里记录的 approval_pending 精确授权（推荐：避免
    授权被用在意料之外的同名操作上）。
    """
    session = b.AGENT_SESSIONS.get(sid)
    if not session:
        raise HTTPException(404, "会话不存在")
    tool = str(payload.get("tool") or "").strip()
    args_hash = str(payload.get("args_hash") or "").strip()
    if not args_hash:
        pend = session.get("approval_pending") or {}
        if pend.get("tool") == tool and pend.get("args_hash"):
            args_hash = str(pend["args_hash"])
    if not tool or not args_hash:
        raise HTTPException(400, "需要 tool 与 args_hash（或先用 approval_pending 记录）")
    # 授权范围：once（逐参数，用后即焚，默认）/ session（本会话内该工具都放行）
    scope = str(payload.get("scope") or b.AGENT_CFG.get("approval_scope", "once")).lower()
    if scope == "session":
        b.AGENT_SESSIONS.grant_session_approval(sid, tool)
        note = "已授予本会话级授权；该工具在本会话内不再逐次询问"
        note_code = "approval_session_granted"
    else:
        b.AGENT_SESSIONS.grant_approval(sid, tool, args_hash)
        note = "已授权一次；该授权用后即焚，下次同类操作需重新授权。"
        note_code = "approval_granted"
    # SessionStore 没有 update()（只有 patch()）：原先这里抛 AttributeError → 授权接口 500，
    # 也就是"审批功能在 UI/API 侧根本走不通"。端到端验证才发现。
    try:
        # 同时清掉 approval_pending：否则前端授权横幅会一直挂着（已授权却还在提示）
        b.AGENT_SESSIONS.patch(sid, last_error=None, approval_pending=None)
    except Exception:
        pass
    # 时间线要有一条**正面**记录：否则拦下那条 "approval_required" 会成为唯一
    # 显示，看起来像"一直没授权"（用户实测反馈）。
    try:
        _step_no = int((session or {}).get("total_steps", 0)) + 1
        _step = {"step": _step_no, "status": "approval_granted", "tool": tool,
                 "reason": ("已授予本会话级授权" if scope == "session" else "已授权一次（用后即焚）"),
                 "result": {"tool": tool, "args_hash": args_hash, "scope": scope}}
        b.AGENT_SESSIONS.add_step(sid, _step_no, int((session or {}).get("conversation_index", 0)), _step)
    except Exception:
        pass
    # 恢复时明确告诉模型"已授权"，否则它可能沿用历史里"需要授权"的判断而放弃重试
    try:
        _hint = ("用户已授权对 %s 执行**这一次**操作（参数指纹 %s%s），请继续刚才被拦下的步骤。"
                 % (tool, args_hash,
                    "，本会话内该工具不再逐次询问" if scope == "session" else "，用后即焚"))
        b.set_resume_hint(sid, _hint)
    except Exception:
        pass
    return {"ok": True, "tool": tool, "args_hash": args_hash,
            "scope": scope, "note": note, "note_code": note_code}


@app.post("/v1/restart")
async def restart_service():
    """重启本服务（WebUI「重启服务」菜单）。

    与停机同样的安全模型：**先返回响应、再延迟触发**（直接在这里退会让前端拿到
    "连接被重置"，看起来像失败）。流程是优雅退出 → 由 start.py 在 `server.run()`
    返回后 `os.execv` 就地重启，所以端口不会出现竞争窗口。

    正在运行的任务会中断（状态都在 SQLite 里）；开了 `agent.auto_resume` 的会
    在新进程启动后自动续跑。
    """
    if lifecycle.shutdown_pending() or lifecycle.restart_requested():
        return {"restarting": True, "already": True,
                "note": "停机/重启已在进行中", "note_code": "restart_pending"}
    lifecycle.schedule("restart", 0.4)
    return {"restarting": True,
            "note": "服务正在重启。正在运行的任务会中断（状态已落库）；"
                    "本页会在服务恢复后自动刷新。",
            "note_code": "restart_soon"}


@app.get("/v1/agent/spans")
async def list_spans(limit: int = 200, session_id: str | None = None):
    """最近的操作 span（JSONL 尾部）：回合/工具调用，字段对齐 OTel GenAI 语义约定。"""
    items = b.SPANS.tail(limit=limit)
    if session_id:
        items = [x for x in items
                 if (x.get("attributes") or {}).get("cbp.session_id") == session_id]
    return {"count": len(items), "enabled": b.SPANS.enabled, "path": str(b.SPANS.path),
            "items": items}


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
    if isinstance(exc, b.CopilotBusy):
        # 窗口全忙：可重试的忙，不是 500
        return HTTPException(409, f"{exc}（可在设置里调大「Copilot 自动化窗口数」）")
    if isinstance(exc, TimeoutError):
        return HTTPException(504, f"等待 Copilot 回答超时: {exc}")
    return HTTPException(500, str(exc))


@app.post("/v1/agent/completions")
async def agent_completions(req: AgentRequest, request: HttpRequest = None):
    prompt_lang.set_request_lang(request.headers.get(_LANG_HEADER) if request is not None else None)
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
