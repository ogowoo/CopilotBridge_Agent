

import asyncio, json, re, time, uuid
from os.path import commonprefix
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field
from agent import AgentRunner, AgentPaused, CopilotServiceUnavailable, CopilotConversationFull, validate_token_budget
from tools import ToolRegistry
from session_store import SessionStore

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
validate_token_budget(CFG.get("agent", {}))
PROFILE = BASE / "EdgeProfile"
URL = CFG["copilot_url"]
TIMEOUT = float(CFG.get("reply_timeout_seconds", 180))
STABLE = float(CFG.get("stable_seconds", 3))
POLL = float(CFG.get("poll_interval_seconds", .35))
DIAG = bool(CFG.get("diagnostics", True))

# API model ID -> possible labels shown by the Copilot web interface.
# Keep the first label as the preferred name. Add labels here if your tenant
# displays different wording.
MODELS = {
    "copilot-auto": {
        "ui_names": ["Auto", "自动"],
        "description": "由 Microsoft 365 Copilot 自动选择回答模式",
    },
    "copilot-quick-response": {
        "ui_names": ["Quick response", "Quick", "快速响应", "快速"],
        "description": "优先快速响应",
    },
    "copilot-think-deeper": {
        "ui_names": ["Think deeper", "Think Deeper", "深入思考"],
        "description": "使用更深入的推理模式",
    },
    "copilot-gpt": {
        "ui_names": ["GPT"],
        "description": "使用 Copilot 菜单中的 GPT 模型",
    },
    "copilot-gpt-5-6": {
        "ui_names": ["GPT-5.6", "GPT 5.6", "GPT5.6"],
        "description": "使用 Copilot 菜单中的 GPT-5.6 模型",
    },
    "copilot-sol": {
        "ui_names": ["Sol", "SOL"],
        "description": "使用 Copilot 菜单中的 Sol 模型",
    },
    "copilot-think": {
        "ui_names": ["Think", "思考"],
        "description": "使用 Copilot 菜单中的 Think 模型",
    },
}

# Optional config.json extension. This lets you add or override models without
# editing app.py. Example:
# "models": {"copilot-custom": {"ui_names": ["Custom"], "description": "Custom model"}}
for custom_id, custom_item in CFG.get("models", {}).items():
    if isinstance(custom_item, dict) and custom_item.get("ui_names"):
        MODELS[custom_id] = {
            "ui_names": list(custom_item["ui_names"]),
            "description": custom_item.get("description", custom_id),
        }

# Backward compatibility with the model name used by v7.0.0.
MODEL_ALIASES = {
    "copilot-chat-basic-web": "copilot-auto",
}
DEFAULT_MODEL = CFG.get("default_model", "copilot-auto")

pw = ctx = page = None
lock = asyncio.Lock()
RUNTIME = {}
ACTIVE_SESSION_ID = None

def runtime_update(session_id=None, **values):
    sid = session_id or ACTIVE_SESSION_ID
    if not sid:
        return
    item = RUNTIME.setdefault(sid, {"phase": "idle", "updated": time.time(), "answer_preview": "", "prompt_preview": "", "detail": ""})
    item.update(values)
    item["updated"] = time.time()


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class Request(BaseModel):
    model: str = DEFAULT_MODEL
    messages: list[Message] = Field(min_length=1)
    stream: bool = True
    new_conversation: bool = True

class SessionMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=120000)
    model: str | None = None
    agent_mode: bool = True

class AgentRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    max_steps: int | None = Field(default=None, ge=1, le=10000)
    session_id: str | None = Field(default=None, min_length=1, max_length=100)
    reuse_session: bool = True
    workspace: str | None = None


def log(s):
    print(f"[{datetime.now():%H:%M:%S}] {s}", flush=True)


def normalize_model(model_id):
    normalized = MODEL_ALIASES.get(model_id, model_id)
    if normalized not in MODELS:
        raise HTTPException(
            400,
            detail={
                "message": f"不支持的模型: {model_id}",
                "type": "invalid_request_error",
                "available_models": list(MODELS),
            },
        )
    return normalized


async def diagnostics(tag):
    if not DIAG or page is None: return
    try:
        (BASE / "last_page.txt").write_text(await page.locator("body").inner_text(), encoding="utf-8")
        if bool(CFG.get("diagnostic_html", False)):
            (BASE / "last_page.html").write_text(await page.content(), encoding="utf-8")
    except Exception: pass

async def visible(selectors):
    for selector in selectors:
        loc = page.locator(selector)
        for i in range(await loc.count() - 1, -1, -1):
            try:
                if await loc.nth(i).is_visible():
                    return loc.nth(i)
            except Exception:
                pass
    return None


async def visible_locator(locators):
    for loc in locators:
        try:
            count = await loc.count()
            for i in range(count - 1, -1, -1):
                item = loc.nth(i)
                if await item.is_visible():
                    return item
        except Exception:
            pass
    return None


async def dismiss():
    b = await visible([
        'button:has-text("Accept")',
        'button:has-text("接受")',
        'button:has-text("Got it")',
        'button:has-text("知道了")',
        'button:has-text("Not now")',
        'button:has-text("暂不")',
    ])
    if b:
        try:
            await b.click(timeout=1000)
        except Exception:
            pass


async def new_chat():
    b = await visible([
        'button[aria-label*="New chat" i]',
        'a[aria-label*="New chat" i]',
        'button:has-text("New chat")',
        'button:has-text("新建聊天")',
    ])
    if b:
        await b.click()
        await asyncio.sleep(1)
    else:
        await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
        await asyncio.sleep(2)


async def model_button():
    all_names = [name for item in MODELS.values() for name in item["ui_names"]]
    exact = re.compile(
        r"^(?:" + "|".join(re.escape(name) for name in all_names) + r")$",
        re.I,
    )
    button = await visible_locator([
        page.get_by_role("button", name=exact),
        page.locator('button[aria-haspopup="menu"]').filter(has_text=exact),
        page.locator('button[aria-haspopup="listbox"]').filter(has_text=exact),
        page.locator('[role="button"][aria-haspopup="menu"]').filter(has_text=exact),
        page.locator('[role="button"][aria-haspopup="listbox"]').filter(has_text=exact),
    ])
    return button


async def select_model(model_id):
    normalized = normalize_model(model_id)
    target_names = MODELS[normalized]["ui_names"]
    button = await model_button()
    if not button:
        await diagnostics("no_model_button")
        raise RuntimeError("找不到 Copilot 模型选择按钮")

    # The modern Copilot button can have no useful inner_text. Its aria-label
    # contains values such as "Think deeper, Model Selector".
    try:
        current_text = (await button.inner_text()).strip()
    except Exception:
        current_text = ""
    try:
        current_label = (await button.get_attribute("aria-label") or "").strip()
    except Exception:
        current_label = ""
    current_values = [current_text.casefold(), current_label.casefold()]
    if any(name.casefold() in value for name in target_names for value in current_values if value):
        log(f"模型已是 {normalized} ({current_label or current_text})")
        return normalized

    try:
        await button.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        await button.click(timeout=8000)
    except Exception:
        # A Fluent UI animation can keep the element unstable. Force click is
        # safe here because the locator resolved to the explicit model button.
        await button.click(timeout=5000, force=True)
    await asyncio.sleep(.6)

    target = re.compile(r"^(?:" + "|".join(re.escape(name) for name in target_names) + r")$", re.I)
    option = await visible_locator([
        page.get_by_role("option", name=target),
        page.get_by_role("menuitem", name=target),
        page.get_by_role("menuitemradio", name=target),
        page.get_by_text(target, exact=True),
        page.get_by_role("option", name=target, exact=False),
        page.get_by_role("menuitem", name=target, exact=False),
        page.get_by_role("menuitemradio", name=target, exact=False),
        page.get_by_text(target, exact=False),
    ])
    if not option:
        try: await page.keyboard.press("Escape")
        except Exception: pass
        await diagnostics("model_option_missing")
        raise RuntimeError(f"模型菜单中找不到 {target_names[0]}")
    try:
        await option.click(timeout=8000)
    except Exception:
        await option.click(timeout=5000, force=True)
    await asyncio.sleep(.8)
    log(f"模型已切换为 {normalized}")
    return normalized

async def input_box():
    x = await visible([
        'textarea[placeholder]',
        'textarea',
        '[contenteditable="true"][role="textbox"]',
        '[contenteditable="true"]',
        '[role="textbox"]',
    ])
    if not x:
        await diagnostics("no_input")
        raise RuntimeError("找不到输入框，请重新运行 login.py")
    return x


async def body_text():
    try:
        return (await page.locator("body").inner_text()).replace("\r\n", "\n")
    except Exception:
        return ""


def extract_latest_answer(text):
    # Select only the final Copilot said block to avoid sidebar/history text.
    marker = "Copilot said:"
    if marker not in text:
        return ""
    answer = text.rsplit(marker, 1)[1]
    end_markers = [
        "\nSources\n",
        "\nMessage Copilot\n",
        "\nAI-generated content may be incorrect",
    ]
    positions = [answer.find(m) for m in end_markers if answer.find(m) >= 0]
    if positions:
        answer = answer[:min(positions)]
    ignored = {
        "Copy", "Like", "Dislike", "More", "Share", "Regenerate",
        "复制", "来源", "赞", "踩", "更多", "分享", "重新生成",
    }
    lines = []
    for line in answer.splitlines():
        line = line.rstrip()
        if line.strip() in ignored:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


async def current_answer():
    return extract_latest_answer(await body_text())


async def generating():
    return bool(await visible([
        'button[aria-label*="Stop" i]',
        'button:has-text("Stop generating")',
        'button:has-text("停止生成")',
    ]))


def delta(old, new):
    if not old:
        return new
    if new.startswith(old):
        return new[len(old):]
    stripped = old.rstrip()
    if new.startswith(stripped):
        return new[len(stripped):]
    return ""


async def count_user_markers():
    text = await body_text()
    return text.count("You said:")


async def box_value(box):
    try:
        tag = await box.evaluate("el => el.tagName.toLowerCase()")
        if tag in {"textarea", "input"}:
            return (await box.input_value()).strip()
        return (await box.inner_text()).strip()
    except Exception:
        return ""


async def _body_contains_turn_marker(marker: str) -> bool:
    if not marker:
        return False
    try:
        return marker in await body_text()
    except Exception:
        return False


async def _wait_turn_marker(marker: str, timeout: float = 30) -> bool:
    """Confirm the exact submitted turn, independent of virtualized message counts."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _body_contains_turn_marker(marker):
            return True
        await asyncio.sleep(POLL)
    return False


async def sent_successfully(box, before_count, prompt):
    marker_match = re.search(r"\[CBP-TURN:[0-9a-f]{12}\]", prompt)
    marker = marker_match.group(0) if marker_match else ""
    for _ in range(20):
        await asyncio.sleep(.25)
        value = await box_value(box)
        if marker:
            if not value and await _body_contains_turn_marker(marker):
                return True
            continue
        now_count = await count_user_markers()
        if now_count > before_count:
            return True
        if not value and prompt in await body_text():
            return True
    return False


async def click_send_button():
    button = await visible([
        'button[aria-label="Send"]',
        'button[aria-label*="Send" i]',
        'button[title*="Send" i]',
        'button[aria-label*="发送"]',
        'button[title*="发送"]',
        'button[data-testid*="send" i]',
    ])
    if button:
        try:
            await button.click(timeout=2000)
            return True
        except Exception:
            pass
    return False


async def send(prompt, make_new, model_id):
    await dismiss()
    if make_new:
        await new_chat()
    # Select after New chat because opening a new conversation can reset Auto.
    selected_model = await select_model(model_id)

    box = await input_box()
    await box.click()
    try:
        await box.fill(prompt)
    except Exception:
        await box.press("Control+A")
        await box.press_sequentially(prompt)
    before_count = await count_user_markers()

    await box.press("Enter")
    if await sent_successfully(box, before_count, prompt):
        await diagnostics("sent")
        log("问题已发送")
        return selected_model

    await box.click()
    if await click_send_button() and await sent_successfully(box, before_count, prompt):
        await diagnostics("sent_button")
        log("问题已通过发送按钮提交")
        return selected_model

    await box.click()
    await box.press("Control+Enter")
    if await sent_successfully(box, before_count, prompt):
        await diagnostics("sent_ctrl_enter")
        log("问题已通过 Ctrl+Enter 提交")
        return selected_model

    full = await conversation_full_check()
    if full:
        await diagnostics("send_failed_conversation_full")
        raise CopilotConversationFull(full)
    await diagnostics("send_failed")
    raise RuntimeError("问题停留在输入框，Enter、发送按钮和 Ctrl+Enter 均未触发发送")


async def completion_signal():
    text = await body_text()
    answer = text.rsplit("Copilot said:", 1)[1] if "Copilot said:" in text else ""
    has_sources = "\nSources\n" in answer or answer.rstrip().endswith("Sources")
    stop_button = await visible([
        'button[aria-label*="Stop" i]',
        'button:has-text("Stop generating")',
        'button:has-text("停止生成")',
    ])
    return has_sources or stop_button is None


async def updates():
    deadline = time.monotonic() + TIMEOUT
    previous = ""
    emitted = 0
    unchanged_since = None
    started = False
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL)
        current = await current_answer()
        if not current:
            continue
        if current == previous:
            unchanged_since = unchanged_since or time.monotonic()
        else:
            unchanged_since = None
        stable_prefix = commonprefix([previous, current]) if previous else ""
        if len(stable_prefix) > emitted:
            part = stable_prefix[emitted:]
            emitted = len(stable_prefix)
            if part:
                if not started:
                    started = True
                    log("已识别 Copilot said 区域，开始流式返回")
                yield part
        previous = current
        settled = unchanged_since is not None and time.monotonic() - unchanged_since >= STABLE
        if current and settled and await completion_signal():
            if len(current) > emitted:
                yield current[emitted:]
                emitted = len(current)
            await diagnostics("success")
            log(f"回答完成，共 {len(current)} 字符")
            return
    final = await current_answer()
    if final and await completion_signal():
        if len(final) > emitted:
            yield final[emitted:]
        await diagnostics("success_late")
        log(f"回答延迟完成，共 {len(final)} 字符")
        return
    await diagnostics("timeout")
    raise TimeoutError("等待回答超时，请查看 last_page.txt")


def sse(data):
    if isinstance(data, str):
        return f"data: {data}\n\n"
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


async def stream_response(req, prompt):
    rid = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    async with lock:
        try:
            selected_model = await send(prompt, req.new_conversation, req.model)
            yield sse({
                "id": rid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": selected_model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            })
            async for part in updates():
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
            log("客户端已断开")
            raise
        except Exception as e:
            await diagnostics("error")
            log(f"失败: {e}")
            yield sse({"error": {"message": str(e), "type": "bridge_error"}})
            yield sse("[DONE]")


async def nonstream(req, prompt):
    parts = []
    rid = "chatcmpl-" + uuid.uuid4().hex
    async with lock:
        selected_model = await send(prompt, req.new_conversation, req.model)
        async for part in updates():
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


@asynccontextmanager
async def lifespan(app):
    global pw, ctx, page
    PROFILE.mkdir(exist_ok=True)
    pw = await async_playwright().start()
    headless = bool(CFG.get("headless", False))
    launch_kwargs = dict(
        user_data_dir=str(PROFILE),
        channel=CFG.get("browser_channel", "msedge"),
        headless=headless,
        viewport={"width": 1440, "height": 1000},
    )
    if headless:
        # 降低无头模式被页面自动化检测的概率。
        launch_kwargs["args"] = ["--disable-blink-features=AutomationControlled"]
    try:
        ctx = await pw.chromium.launch_persistent_context(**launch_kwargs)
    except Exception:
        if not headless:
            raise
        # 部分环境（旧版 Edge/安全策略）不支持无头持久化上下文，回退有窗口模式。
        log("无头模式启动失败，已回退为有窗口模式；请升级 Edge 或将 headless 改回 false")
        launch_kwargs["headless"] = False
        ctx = await pw.chromium.launch_persistent_context(**launch_kwargs)
    log("浏览器模式：" + ("无头（不显示窗口）" if launch_kwargs["headless"] else "有窗口"))
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    log("API 与浏览器已就绪")
    cleanup_task = asyncio.create_task(periodic_session_cleanup())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        try:
            await ctx.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass


app = FastAPI(title="Copilot Local Bridge Agent", version="10.2.1", lifespan=lifespan)


@app.get("/")
async def root():
    return {"name": "Copilot Local Bridge Agent", "version": "10.2.1", "docs": "/docs", "models": "/v1/models", "agent": "/v1/agent/completions"}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "busy": lock.locked(),
        "page_url": page.url if page else None,
        "default_model": DEFAULT_MODEL,
    }


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
            for model_id, item in MODELS.items()
        ],
    }


@app.get("/v1/chat/completions")
async def help_get():
    return {
        "message": "请使用 POST，或运行 python test_stream.py",
        "available_models": list(MODELS),
    }


@app.post("/v1/chat/completions")
async def chat(req: Request):
    # Validate before creating StreamingResponse, so an invalid model returns HTTP 400.
    req.model = normalize_model(req.model)
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
        await diagnostics("error")
        raise HTTPException(500, str(e)) from e


AGENT_CFG = CFG.get("agent", {})
AGENT_WORKSPACE = (BASE / AGENT_CFG.get("workspace", "workspace")).resolve()
AGENT_TOOLS = ToolRegistry(AGENT_WORKSPACE, AGENT_CFG)
SESSION_DB = Path(AGENT_CFG.get("session_store", "data/agent.db"))
if not SESSION_DB.is_absolute(): SESSION_DB = BASE / SESSION_DB
AGENT_SESSIONS = SessionStore(SESSION_DB)
AGENT_SESSIONS.recover_interrupted()
async def periodic_session_cleanup():
    while True: await asyncio.sleep(3600)

def _has_complete_agent_action(text: str) -> bool:
    try:
        from agent import AgentRunner
        return AgentRunner._parse(text).get("type") in {"tool", "final"}
    except Exception:
        return False

# Copilot 限流/服务不可用的典型整段提示。仅在回答很短（<=600 字符）且命中
# 特征时判定为服务问题；可解析的 Agent JSON 动作优先于本判定，不会误判。
COPILOT_UNAVAILABLE_PATTERNS = [
    r"temporarily unable to respond",
    r"try again later",
    r"too many requests",
    r"rate\s*limit(?:ed)?",
    r"something went wrong",
    r"currently (?:busy|unavailable)",
    r"service (?:is )?currently unavailable",
    r"having trouble (?:responding|processing)",
    r"请稍后(?:再试|重试)",
    r"稍后(?:再试|重试)",
    r"暂时无法(?:响应|处理|为你服务|回答)",
    r"请求(?:过多|过于频繁|频率过高)",
    r"服务(?:暂时)?不可用",
    r"出了点问题",
]


def copilot_unavailable_reason(text: str) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    if not value or len(value) > 600:
        return ""
    # 含有 Agent 动作结构的文本一律视为正常回答，不参与服务判定。
    if re.search(r"['\"]type['\"]\s*:", value):
        return ""
    for pattern in COPILOT_UNAVAILABLE_PATTERNS:
        if re.search(pattern, value, re.I):
            return value[:200]
    return ""


# Copilot 对话长度达到上限的典型提示（英文/中文）。命中后的正确处理是
# 切换新会话，而不是原地重试，因此与服务限流分开识别。
CONVERSATION_FULL_PATTERNS = [
    r"reached (?:its|the) (?:maximum |max )?(?:length|limit)",
    r"conversation (?:is|has become) too long",
    r"start a new (?:conversation|chat) to continue",
    r"longer than (?:the )?(?:maximum|allowed)",
    r"对话(?:已)?达到(?:长度)?上限",
    r"对话(?:内容)?(?:过长|太多|超出)",
    r"(?:请)?开始新(?:的)?(?:对话|会话|聊天)(?:以继续|再继续)",
    r"新(?:建)?(?:对话|会话|聊天)以继续",
]


def conversation_full_reason(text: str) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    if not value:
        return ""
    for pattern in CONVERSATION_FULL_PATTERNS:
        m = re.search(pattern, value, re.I)
        if m:
            return value[max(0, m.start() - 60):m.end() + 60]
    return ""


async def conversation_full_check() -> str:
    """在整页文本中探测对话上限横幅；剔除最近一条回答正文，
    避免把 Copilot 正常回答里的相关措辞误判为上限提示。"""
    body = await body_text()
    answer = extract_latest_answer(body)
    if answer:
        body = body.replace(answer, " ")
    return conversation_full_reason(body)


async def wait_agent_answer(previous: str = "", pause_check=None) -> str:
    """Wait using inactivity timeout, not a fixed per-turn wall-clock timeout.

    TIMEOUT now means: fail only when no answer progress is observed for this
    many seconds. Every text change and every visible generation cycle refreshes
    the idle deadline. A separate hard deadline prevents an infinite wait.
    Malformed but finished output is returned to AgentRunner for repair.
    """
    started_at = time.monotonic()
    hard_timeout = float(CFG.get("reply_hard_timeout_seconds", max(TIMEOUT * 5, 900)))
    hard_deadline = started_at + hard_timeout
    idle_deadline = started_at + TIMEOUT
    current = ""
    last_change = started_at
    generation_seen = False
    candidate_seen = False
    last_full_check = started_at

    while time.monotonic() < hard_deadline:
        await asyncio.sleep(POLL)
        if pause_check and pause_check():
            raise AgentPaused("用户请求暂停，回合等待已中断")
        now = time.monotonic()
        still_generating = await generating()
        value = (await current_answer()).strip()

        # 对话已满时 Copilot 不会开始生成：快速失败，交给上层换会话重发。
        if not still_generating and not candidate_seen and not generation_seen:
            if now - last_full_check >= 5:
                last_full_check = now
                full = await conversation_full_check()
                if full:
                    await diagnostics("agent_conversation_full_early")
                    raise CopilotConversationFull(full)

        if still_generating:
            generation_seen = True
            idle_deadline = now + TIMEOUT

        is_candidate = bool(value) and (value != previous or generation_seen)
        if is_candidate:
            candidate_seen = True
            if value != current:
                current = value
                last_change = now
                idle_deadline = now + TIMEOUT

        if current and not still_generating:
            quiet_for = now - last_change
            if _has_complete_agent_action(current) and quiet_for >= max(1.0, POLL * 3):
                await diagnostics("agent_answer_ready")
                return current
            # 服务限流/不可用提示不是 Agent 回答：立即识别并交给上层退避重试，
            # 不进入解析失败计数，更不会触发新建会话。
            unavailable = copilot_unavailable_reason(current)
            if unavailable and quiet_for >= max(1.0, POLL * 3):
                await diagnostics("copilot_service_unavailable")
                raise CopilotServiceUnavailable(unavailable)
            # Do not wait until TIMEOUT for a finished malformed response. Let
            # AgentRunner parse/recover it and issue a repair turn if necessary.
            if generation_seen and quiet_for >= max(STABLE, 3.0):
                await diagnostics("agent_answer_needs_repair")
                return current

        if now >= idle_deadline:
            final = (await current_answer()).strip()
            if not candidate_seen and not generation_seen:
                full = await conversation_full_check()
                if full:
                    await diagnostics("agent_conversation_full_idle")
                    raise CopilotConversationFull(full)
            unavailable = copilot_unavailable_reason(final)
            if unavailable and not _has_complete_agent_action(final):
                await diagnostics("copilot_service_unavailable_idle")
                raise CopilotServiceUnavailable(unavailable)
            if final and (final != previous or generation_seen or candidate_seen):
                await diagnostics("agent_answer_idle_return")
                return final
            await diagnostics("agent_answer_idle_timeout")
            raise TimeoutError(
                f"Copilot 连续 {int(TIMEOUT)} 秒没有产生新的回答进度，请查看 last_page.txt"
            )

    final = (await current_answer()).strip()
    if not candidate_seen and not generation_seen:
        full = await conversation_full_check()
        if full:
            await diagnostics("agent_conversation_full_hard")
            raise CopilotConversationFull(full)
    unavailable = copilot_unavailable_reason(final)
    if unavailable and not _has_complete_agent_action(final):
        await diagnostics("copilot_service_unavailable_hard")
        raise CopilotServiceUnavailable(unavailable)
    if final and (final != previous or generation_seen or candidate_seen):
        await diagnostics("agent_answer_hard_return")
        return final
    await diagnostics("agent_answer_hard_timeout")
    raise TimeoutError(
        f"Copilot 单步等待超过硬上限 {int(hard_timeout)} 秒，请查看 last_page.txt"
    )

async def agent_chat_once(prompt: str, model_id: str, make_new: bool = False, pause_check=None) -> str:
    """Send one Agent turn and bind the response to a unique visible turn marker.

    Copilot virtualizes long chat DOM nodes, so the number of visible "You said"
    blocks can stay unchanged. The marker is unique for every request and cannot
    be confused with a prior turn or text left in the composer.
    """
    previous = "" if make_new else (await current_answer()).strip()
    turn_id = uuid.uuid4().hex[:12]
    marker = f"[CBP-TURN:{turn_id}]"
    tagged_prompt = prompt.rstrip() + "\n\n" + marker
    runtime_update(phase="sending", prompt_preview=tagged_prompt[-12000:], answer_preview="",
                   detail=f"正在提交 Agent 回合 {turn_id}", make_new=make_new)
    await send(tagged_prompt, make_new, model_id)

    # Require the exact unique marker to appear in the rendered conversation.
    # This replaces the unreliable visible-message-count check.
    if not await _wait_turn_marker(marker, min(TIMEOUT, 45)):
        full = await conversation_full_check()
        if full:
            await diagnostics("agent_conversation_full_at_send")
            raise CopilotConversationFull(full)
        await diagnostics("agent_turn_marker_missing")
        raise TimeoutError(f"未确认 Agent 回合 {turn_id} 进入 Copilot 会话，已停止以避免读取旧回答")

    runtime_update(phase="waiting", detail=f"Agent 回合 {turn_id} 已确认，等待 Copilot 回答")
    return await wait_agent_answer(previous, pause_check=pause_check)

async def run_agent(req: AgentRequest):
    global ACTIVE_SESSION_ID
    if not AGENT_CFG.get("enabled", True): raise HTTPException(503, "Agent 功能已禁用")
    req.model=normalize_model(req.model)
    task="\n".join(f"[{m.role}] {m.content.strip()}" for m in req.messages if m.content.strip())
    if not task: raise HTTPException(400,"至少需要一条消息")
    sid=req.session_id or ("task-"+uuid.uuid4().hex[:12])
    ACTIVE_SESSION_ID = sid
    runtime_update(sid, phase="queued", detail="任务已进入执行队列", answer_preview="", prompt_preview="")
    async with lock:
        existing=AGENT_SESSIONS.get(sid)
        workspace=(req.workspace.strip() if req.workspace else None) or (existing or {}).get("workspace") or str(AGENT_WORKSPACE)
        AGENT_TOOLS.set_root(workspace,False)
        if not existing:
            existing=AGENT_SESSIONS.create(sid,task,str(AGENT_TOOLS.root),req.model)
            effective_task = task
        else:
            effective_task = existing.get("original_task") or task
            if task.strip() and task.strip() != effective_task.strip():
                effective_task += "\n\n用户追加任务：\n" + task
                pending = list(existing.get("pending") or [])
                pending.append(task)
                # 追加指令不再强制新建会话：Agent 会在当前会话直接下达，保留完整上下文。
                AGENT_SESSIONS.patch(sid, pending=pending, state="ready")
        AGENT_SESSIONS.patch(sid,workspace=str(AGENT_TOOLS.root),model=req.model)
        def pause_check():
            st = AGENT_SESSIONS.get(sid) or {}
            return st.get("state") == "paused"
        async def turn(prompt,model,force_new=False):
            st=AGENT_SESSIONS.get(sid)
            make_new = force_new or not st.get("current_url")
            marker = _agent_chat_marker(sid, int(st.get("conversation_index", 0)))
            # Copilot derives the sidebar title from the beginning of the first
            # message. This is more stable than automating the Rename editor.
            send_prompt = (marker + "\n" + prompt) if make_new else prompt
            try:
                ans=await agent_chat_once(send_prompt,model,make_new,pause_check=pause_check)
            except AgentPaused:
                # 回合已发送到 Copilot：保存会话 URL，恢复后继续同一聊天，
                # 避免暂停-恢复后重复创建新会话。
                if make_new and page.url:
                    paused_url = await stable_conversation_url(timeout=6) or (page.url or "")
                    stp=AGENT_SESSIONS.get(sid); urls=list(stp.get("conversation_urls") or [])
                    if paused_url and paused_url not in urls: urls.append(paused_url)
                    if paused_url:
                        AGENT_SESSIONS.patch(sid,current_url=paused_url,conversation_urls=urls)
                raise
            # 等 Copilot 把会话 ID 路由进 URL 再保存；超时兜底原地址保证连续。
            final_url = await stable_conversation_url(timeout=15) or (page.url or "")
            st=AGENT_SESSIONS.get(sid); urls=list(st.get("conversation_urls") or [])
            is_new_url = bool(final_url and final_url not in urls)
            if is_new_url: urls.append(final_url)
            if final_url:
                AGENT_SESSIONS.patch(sid,current_url=final_url,conversation_urls=urls)
            # The marker is already embedded in the first prompt. Do not navigate
            # away after receiving an answer merely to rename the sidebar row;
            # that navigation can invalidate the next-turn DOM baseline.
            if is_new_url:
                runtime_update(sid, detail="新 Copilot 会话已绑定唯一标识")
            return ans
        def get(): return AGENT_SESSIONS.get(sid)
        def save(**kw): return AGENT_SESSIONS.patch(sid,**kw)
        def add_step(x):
            st=get(); AGENT_SESSIONS.add_step(sid,int(x.get("step",st["total_steps"]+1)),int(st["conversation_index"]),x)
        def add_file(x): AGENT_SESSIONS.add_file_event(sid,x)
        runner=AgentRunner(AGENT_TOOLS,turn,AGENT_CFG,get,save,add_step,add_file)
        try:
            result = await runner.run(effective_task,req.model)
            runtime_update(sid, phase="completed", detail=result.content, answer_preview=result.content)
            return result, sid
        except Exception as exc:
            runtime_update(sid, phase="failed", detail=str(exc))
            AGENT_SESSIONS.patch(sid,state="failed",last_error=str(exc))
            raise
        finally:
            ACTIVE_SESSION_ID = None

async def stream_agent(req: AgentRequest):
    try:
        result,sid=await run_agent(req)
        yield sse({"session_id":sid,"agent_steps":result.steps,"choices":[{"index":0,"delta":{"role":"assistant","content":result.content},"finish_reason":"stop"}]})
    except Exception as exc: yield sse({"error":{"message":str(exc),"type":"agent_error"}})
    yield sse("[DONE]")
@app.get("/ui",include_in_schema=False)
async def web_ui(): return FileResponse(BASE/"webui"/"index.html")
@app.get("/v1/agent/sessions")
async def sessions(include_archived:bool=False): return {"sessions":AGENT_SESSIONS.list(include_archived)}
@app.get("/v1/agent/sessions/{sid}")
async def session(sid:str):
    x=AGENT_SESSIONS.get(sid)
    if not x: raise HTTPException(404,"Session 不存在")
    return {"session":x,"steps":AGENT_SESSIONS.steps(sid),"files":AGENT_SESSIONS.file_events(sid),"messages":AGENT_SESSIONS.messages(sid)}
@app.post("/v1/agent/sessions/{sid}/actions/{action}")
async def control(sid:str,action:str):
    if not AGENT_SESSIONS.get(sid): raise HTTPException(404,"Session 不存在")
    maps={"pause":{"state":"paused"},"resume":{"state":"ready","cancel_requested":False,"rollover_requested":False},"cancel":{"state":"cancelled","cancel_requested":True},"compact":{"compact_requested":True},"rollover":{"rollover_requested":True},"archive":{"state":"archived","archived":True}}
    if action not in maps: raise HTTPException(400,"未知操作")
    return AGENT_SESSIONS.patch(sid,**maps[action])
def _agent_chat_marker(session_id: str, conversation_index: int) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]+", "-", str(session_id)).strip("-") or "session"
    return f"[CBP-AGENT][{safe[:36]}][C{int(conversation_index):03d}]"


async def _rename_sidebar_chat(url: str, title: str) -> dict:
    """Best-effort rename. Supports Copilot's inline editor and keyboard UI."""
    result = {"url": url, "title": title, "renamed": False, "error": None}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=120000)
        await asyncio.sleep(1.2)
        await _open_chat_sidebar()
        link = await _conversation_sidebar_link(url)
        if not link:
            raise RuntimeError("左侧 Chats 列表中找不到新建会话")
        row = await _chat_row_from_link(link)
        await row.scroll_into_view_if_needed(timeout=5000)
        await row.hover(timeout=5000)
        await asyncio.sleep(.3)
        more = await visible_locator([
            row.get_by_role("button", name=re.compile(r"More|更多|Options|选项|Pin and More", re.I)),
            row.locator('button[aria-label*="More" i]'),
            row.locator('button[aria-label*="更多"]'),
            row.locator('button').last,
        ])
        if not more:
            # Accessibility hint on this tenant says Tab exposes Pin and More.
            await link.focus()
            for _ in range(3):
                await page.keyboard.press("Tab")
                await asyncio.sleep(.15)
                focused = page.locator(':focus')
                label = ((await focused.get_attribute('aria-label')) or '') if await focused.count() else ''
                if re.search(r"More|更多|options", label, re.I):
                    more = focused; break
        if not more:
            raise RuntimeError("找不到该会话行的 More 按钮")
        try: await more.click(timeout=6000)
        except Exception: await more.click(timeout=3000, force=True)
        await asyncio.sleep(.4)
        rename_item = await visible_locator([
            page.get_by_role("menuitem", name=re.compile(r"^Rename$|^重命名$", re.I)),
            page.get_by_text(re.compile(r"^Rename$|^重命名$", re.I), exact=True),
        ])
        if not rename_item:
            raise RuntimeError("More 菜单中找不到 Rename")
        try: await rename_item.click(timeout=6000)
        except Exception: await rename_item.click(timeout=3000, force=True)
        await asyncio.sleep(.6)

        # Copilot currently edits the title inline, often without a dialog.
        editor = await visible_locator([
            row.locator('input'), row.locator('textarea'),
            row.locator('[contenteditable="true"]'),
            page.locator('input[aria-label*="Rename" i]'),
            page.locator('input[aria-label*="title" i]'),
            page.locator('[contenteditable="true"][aria-label*="Rename" i]'),
            page.get_by_role("textbox").last,
        ])
        if not editor:
            raise RuntimeError("找不到会话重命名输入框")
        try:
            tag = (await editor.evaluate("el => el.tagName.toLowerCase()"))
            if tag in {"input", "textarea"}:
                await editor.fill(title)
            else:
                await editor.click(); await page.keyboard.press("Control+A"); await page.keyboard.insert_text(title)
            await editor.press("Enter")
        except Exception:
            await editor.click(); await page.keyboard.press("Control+A"); await page.keyboard.insert_text(title); await page.keyboard.press("Enter")
        await asyncio.sleep(.8)
        marker = page.get_by_text(title, exact=True)
        if await marker.count():
            result["renamed"] = True
        else:
            result["error"] = "已提交重命名，但左侧列表未显示完整标题"
    except Exception as exc:
        result["error"] = str(exc)
        await diagnostics("rename_agent_chat_failed")
    return result

async def _open_chat_sidebar():
    """Ensure the Chats list is visible before locating a conversation row."""
    chats = page.get_by_text(re.compile(r"^Chats$|^聊天$", re.I), exact=True)
    try:
        if await chats.count() and await chats.first.is_visible():
            return
    except Exception:
        pass
    toggle = await visible_locator([
        page.get_by_role("button", name=re.compile(r"Chats|Chat history|聊天|聊天记录|Open navigation|导航", re.I)),
        page.locator('button[aria-label*="chat" i]'),
        page.locator('button[aria-label*="聊天"]'),
        page.locator('button[aria-label*="navigation" i]'),
    ])
    if toggle:
        try:
            await toggle.click(timeout=5000)
        except Exception:
            await toggle.click(timeout=3000, force=True)
        await asyncio.sleep(.8)


CONVERSATION_URL_RE = re.compile(r"/chat/conversation/[0-9A-Za-z-]{8,}")


async def stable_conversation_url(timeout: float = 12.0) -> str:
    """Wait until the Copilot URL carries a conversation id and return it.

    New chats open at the base /chat address; Copilot routes the conversation
    id into the URL asynchronously. Persisting the base address breaks delete
    targeting later, so poll briefly. Returns "" when no id shows up in time.
    """
    deadline = time.monotonic() + max(0.5, timeout)
    url = ""
    while time.monotonic() < deadline:
        try:
            url = page.url or ""
        except Exception:
            url = ""
        if CONVERSATION_URL_RE.search(url):
            return url
        await asyncio.sleep(0.5)
    return ""


async def _conversation_link_by_title(marker: str):
    """Sidebar fallback: locate a conversation link by its full CBP marker text.

    Used for legacy records whose saved URL has no conversation id, where
    URL-based matching cannot identify the row.
    """
    try:
        nodes = page.get_by_text(marker, exact=False)
        for index in range(await nodes.count() - 1, -1, -1):
            node = nodes.nth(index)
            try:
                if not await node.is_visible():
                    continue
                link = node.locator("xpath=ancestor-or-self::a[1]")
                if await link.count():
                    return link.first
                row = node.locator("xpath=ancestor::*[.//a][1]")
                if await row.count():
                    link = row.first.locator("a").first
                    if await link.count():
                        return link
            except Exception:
                pass
    except Exception:
        pass
    return None


async def _conversation_sidebar_link(url: str):
    """Find the conversation link in the left history list using its URL id/path."""
    if not CONVERSATION_URL_RE.search(url or ""):
        # 无会话 ID 的基础地址会匹配到所有聊天链接，禁止用于定位。
        return None
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    chat_id = path.rsplit("/", 1)[-1] if path else ""
    selectors = []
    if chat_id:
        selectors.extend([
            f'a[href*="{chat_id}"]',
            f'[role="link"][href*="{chat_id}"]',
        ])
    if path:
        selectors.extend([
            f'a[href*="{path}"]',
            f'[role="link"][href*="{path}"]',
        ])
    for selector in selectors:
        loc = page.locator(selector)
        for index in range(await loc.count() - 1, -1, -1):
            item = loc.nth(index)
            try:
                await item.scroll_into_view_if_needed(timeout=3000)
                if await item.is_visible():
                    return item
            except Exception:
                pass
    return None


async def _chat_row_from_link(link):
    """Return the smallest sidebar row that owns the link and its hidden More button."""
    candidates = [
        link.locator('xpath=ancestor::*[@role="listitem"][1]'),
        link.locator('xpath=ancestor::*[@role="treeitem"][1]'),
        link.locator('xpath=ancestor::li[1]'),
        link.locator('xpath=ancestor::div[count(.//a) = 1 and .//button][1]'),
        link.locator('xpath=..'),
    ]
    for row in candidates:
        try:
            if await row.count() and await row.first.is_visible():
                return row.first
        except Exception:
            pass
    return link.locator('xpath=..')


async def delete_copilot_conversation(url: str, expected_title: str) -> dict:
    """Delete a saved Copilot conversation by URL, with a same-session marker guard.

    URL is the durable identity. Sidebar title suffix Cnnn is only a safety guard,
    because conversation_urls order can differ from conversation_index after retries.
    """
    result = {"url": url, "expected_title": expected_title, "deleted": False,
              "error": None, "method": "url-plus-session-marker"}
    try:
        marker_match = re.match(r"^(\[CBP-AGENT\]\[[^\]]+\])\[C\d+\]$", expected_title)
        if not marker_match:
            raise RuntimeError("拒绝删除：目标没有合法 CBP-AGENT 标识")
        session_prefix = marker_match.group(1)

        has_conv_id = bool(CONVERSATION_URL_RE.search(url or ""))
        if has_conv_id:
            await page.goto(url, wait_until="domcontentloaded", timeout=120000)
            await asyncio.sleep(1.5)
        await _open_chat_sidebar()

        # Locate the exact saved conversation URL. Do not infer Cnnn from list order.
        link = await _conversation_sidebar_link(url) if has_conv_id else None
        if not link:
            # 兜底：历史数据保存的是无会话 ID 的基础地址，按完整标记文本定位。
            link = await _conversation_link_by_title(expected_title)
        if not link:
            raise RuntimeError("左侧 Chats 中找不到与保存 URL 对应的会话")
        row = await _chat_row_from_link(link)
        await row.scroll_into_view_if_needed(timeout=5000)
        row_text = (await row.inner_text()).strip()
        if session_prefix not in row_text:
            raise RuntimeError(
                f"URL 对应会话不属于当前 Session，拒绝删除；需要前缀: {session_prefix}"
            )

        await row.hover(timeout=5000)
        await asyncio.sleep(.35)
        more = await visible_locator([
            row.get_by_role("button", name=re.compile(r"More|更多|Options|选项|Pin and More", re.I)),
            row.locator('button[aria-label*="More" i]'),
            row.locator('button[title*="More" i]'),
            row.locator('button[aria-label*="更多"]'),
            row.locator('button').last,
        ])
        if not more:
            await link.focus()
            for _ in range(4):
                await page.keyboard.press("Tab")
                await asyncio.sleep(.15)
                focused = page.locator(":focus")
                label = ((await focused.get_attribute("aria-label")) or "") if await focused.count() else ""
                if re.search(r"More|更多|Options|选项", label, re.I):
                    more = focused
                    break
        if not more:
            raise RuntimeError("找到目标会话，但找不到 More 按钮")

        try:
            await more.click(timeout=8000)
        except Exception:
            await more.click(timeout=5000, force=True)
        await asyncio.sleep(.5)

        delete_item = await visible_locator([
            page.get_by_role("menuitem", name=re.compile(r"^Delete$|^删除$", re.I)),
            page.get_by_role("button", name=re.compile(r"^Delete$|^删除$", re.I)),
            page.get_by_text(re.compile(r"^Delete$|^删除$", re.I), exact=True),
        ])
        if not delete_item:
            raise RuntimeError("会话菜单中找不到 Delete/删除")
        try:
            await delete_item.click(timeout=8000)
        except Exception:
            await delete_item.click(timeout=5000, force=True)
        await asyncio.sleep(.6)

        # Copilot 确认弹窗在各租户实现不同（role=dialog / alertdialog /
        # Fluent DialogSurface），且带入场动画。轮询等待弹窗出现后再定位
        # Delete/删除 确认按钮，配两级兜底，防止漏点导致删除失败。
        confirm = None
        confirm_deadline = time.monotonic() + 10
        while time.monotonic() < confirm_deadline and confirm is None:
            dialog = await visible_locator([
                page.get_by_role("alertdialog"),
                page.get_by_role("dialog"),
                page.locator('[role="alertdialog"]'),
                page.locator('[role="dialog"][aria-modal="true"]'),
                page.locator(".fui-DialogSurface"),
            ])
            if dialog:
                confirm = await visible_locator([
                    dialog.get_by_role("button", name=re.compile(r"^Delete$|^删除$|Delete conversation|Delete chat|确认删除|删除对话", re.I)),
                    dialog.locator('button').filter(has_text=re.compile(r"^Delete$|^删除$", re.I)),
                    dialog.locator('button[type="submit"]'),
                ])
                if not confirm:
                    # 兜底一：对话框内排除 Cancel/取消 后剩下的可点击按钮即确认键。
                    try:
                        cancel_re = re.compile(r"Cancel|取消|No, thanks|否", re.I)
                        buttons = dialog.locator("button")
                        for bi in range(await buttons.count() - 1, -1, -1):
                            btn = buttons.nth(bi)
                            if not await btn.is_visible():
                                continue
                            label = ((await btn.inner_text()) or "") + " " + ((await btn.get_attribute("aria-label")) or "")
                            if not cancel_re.search(label):
                                confirm = btn
                                break
                    except Exception:
                        pass
            if not confirm:
                # 兜底二：全局精确匹配 Delete/删除 按钮（删除菜单已关闭，页面唯一）。
                confirm = await visible_locator([
                    page.get_by_role("button", name=re.compile(r"^Delete$|^删除$", re.I)),
                ])
            if not confirm:
                await asyncio.sleep(0.4)
        if not confirm:
            raise RuntimeError("确认窗口中找不到 Delete/删除按钮")
        # 点击确认后校验弹窗确实关闭；未关闭说明点击落在遮罩/动画上，重试。
        for _attempt in range(3):
            try:
                await confirm.click(timeout=8000)
            except Exception:
                try:
                    await confirm.click(timeout=5000, force=True)
                except Exception:
                    pass
            await asyncio.sleep(1.0)
            still_open = await visible_locator([
                page.get_by_role("alertdialog"),
                page.get_by_role("dialog"),
                page.locator('[role="alertdialog"]'),
                page.locator('[role="dialog"][aria-modal="true"]'),
                page.locator(".fui-DialogSurface"),
            ])
            if not still_open:
                break
        await asyncio.sleep(0.5)

        async def _still_listed() -> bool:
            if has_conv_id:
                probe = await _conversation_sidebar_link(url)
            else:
                probe = await _conversation_link_by_title(expected_title)
            if not probe:
                return False
            try:
                return bool(await probe.is_visible())
            except Exception:
                return False

        # 侧栏是虚拟滚动列表，删除后可能短暂残留：轮询等待，必要时刷新再判定。
        gone_deadline = time.monotonic() + 8
        while time.monotonic() < gone_deadline:
            if not await _still_listed():
                break
            await asyncio.sleep(0.8)
        if await _still_listed():
            try:
                await page.reload(wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2)
                await _open_chat_sidebar()
            except Exception:
                pass
        if await _still_listed():
            raise RuntimeError("点击删除后，目标 URL 仍存在于 Chats 列表")
        result["deleted"] = True
    except Exception as exc:
        result["error"] = str(exc)
        await diagnostics("delete_agent_url_marker_failed")
    return result

@app.delete("/v1/agent/sessions/{sid}")
async def delete_session(sid: str, delete_remote: bool = False, force_local: bool = False):
    state = AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    if lock.locked() and ACTIVE_SESSION_ID == sid:
        raise HTTPException(409, "Session 正在运行，不能删除")

    remote_results = []
    if delete_remote:
        urls = list(dict.fromkeys(state.get("conversation_urls") or []))
        if state.get("current_url") and state["current_url"] not in urls:
            urls.append(state["current_url"])
        async with lock:
            for index, url in enumerate(urls):
                marker = _agent_chat_marker(sid, index)
                remote_results.append(await delete_copilot_conversation(url, marker))
        failed = [x for x in remote_results if not x["deleted"]]
        if failed and not force_local:
            return {
                "deleted": False,
                "local_deleted": False,
                "remote_results": remote_results,
                "message": "部分 Copilot 会话删除失败，本地 Session 已保留。可检查后使用 force_local=true 仅清理本地记录。",
            }

    local_deleted = AGENT_SESSIONS.delete(sid)
    RUNTIME.pop(sid, None)
    return {
        "deleted": local_deleted,
        "local_deleted": local_deleted,
        "remote_results": remote_results,
    }
@app.post("/v1/agent/sessions/{sid}/messages")
async def continue_session_message(sid: str, req: SessionMessageRequest):
    state = AGENT_SESSIONS.get(sid)
    if not state: raise HTTPException(404, "Session 不存在")
    if state.get("state") in {"cancelled", "archived"}:
        raise HTTPException(409, f"Session 状态为 {state.get('state')}，不能继续")
    content = req.content.strip()
    if not content: raise HTTPException(400, "消息不能为空")
    model_id = normalize_model(req.model or state.get("model") or DEFAULT_MODEL)
    AGENT_SESSIONS.add_message(sid, "user", content, int(state.get("conversation_index", 0)))

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
        result, _ = await run_agent(agent_req)
        answer = result.content
        current = AGENT_SESSIONS.get(sid)
        AGENT_SESSIONS.add_message(sid, "assistant", answer, int(current.get("conversation_index", 0)))
        return {"session_id": sid, "role": "assistant", "content": answer,
                "url": current.get("current_url"), "agent_steps": result.steps,
                "mode": "agent"}

    # Plain chat mode keeps the same Copilot web conversation and does not run tools.
    global ACTIVE_SESSION_ID
    async with lock:
        ACTIVE_SESSION_ID = sid
        runtime_update(sid, phase="manual_sending", detail="正在发送普通对话", prompt_preview=content, answer_preview="")
        url = state.get("current_url")
        make_new = not bool(url)
        if url and page.url != url:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=120000)
                await asyncio.sleep(1)
            except Exception:
                make_new = True
        previous = "" if make_new else (await current_answer()).strip()
        try:
            await send(content, make_new, model_id)
            answer = (await wait_agent_answer(previous)).strip()
            current = AGENT_SESSIONS.get(sid)
            chat_url = await stable_conversation_url(timeout=10) or (page.url or "")
            urls = list(current.get("conversation_urls") or [])
            if chat_url and chat_url not in urls: urls.append(chat_url)
            AGENT_SESSIONS.add_message(sid, "assistant", answer, int(current.get("conversation_index", 0)))
            if chat_url:
                AGENT_SESSIONS.patch(sid, current_url=chat_url, conversation_urls=urls, model=model_id)
            else:
                AGENT_SESSIONS.patch(sid, conversation_urls=urls, model=model_id)
            runtime_update(sid, phase="manual_completed", answer_preview=answer[-12000:], detail="普通对话完成")
            return {"session_id":sid,"role":"assistant","content":answer,"url":page.url,"mode":"chat"}
        finally:
            ACTIVE_SESSION_ID = None

@app.post("/v1/agent/sessions/{sid}/continue")
async def continue_agent_session(sid: str, req: SessionMessageRequest):
    req.agent_mode = True
    return await continue_session_message(sid, req)

@app.get("/v1/agent/sessions/{sid}/runtime")
async def session_runtime(sid: str):
    state = AGENT_SESSIONS.get(sid)
    if not state: raise HTTPException(404, "Session 不存在")
    runtime = dict(RUNTIME.get(sid) or {})
    runtime.update({
        "session_id": sid,
        "session_state": state.get("state"),
        "total_steps": state.get("total_steps", 0),
        "conversation_steps": state.get("conversation_steps", 0),
        "estimated_tokens": state.get("estimated_tokens", 0),
        "conversation_index": state.get("conversation_index", 0),
        "current_url": state.get("current_url"),
        "busy": lock.locked() and ACTIVE_SESSION_ID == sid,
    })
    return runtime

@app.post("/v1/agent/sessions/{sid}/activate")
async def activate_session(sid: str):
    state = AGENT_SESSIONS.get(sid)
    if not state: raise HTTPException(404, "Session 不存在")
    url = state.get("current_url")
    if not url: raise HTTPException(409, "该 Session 暂无 Copilot 会话 URL")
    await page.bring_to_front()
    if page.url != url:
        await page.goto(url, wait_until="domcontentloaded", timeout=120000)
    runtime_update(sid, phase="activated", detail="已切换到该 Session 的 Copilot 页面")
    return {"activated": True, "url": page.url, "headless": bool(CFG.get("headless", False))}

@app.get("/v1/agent/workspace")
async def workspace(): return {"workspace":str(AGENT_TOOLS.root)}
@app.post("/v1/agent/workspace")
async def set_workspace(payload:dict): return {"workspace":AGENT_TOOLS.set_root(str(payload.get("path","")),bool(payload.get("create",False)))}
@app.get("/v1/agent/tools")
async def agent_tools(): return {"workspace":str(AGENT_TOOLS.root),"tools":AGENT_TOOLS.descriptions()}
@app.post("/v1/agent/completions")
async def agent_completions(req:AgentRequest):
    if req.stream: return StreamingResponse(stream_agent(req),media_type="text/event-stream")
    result,sid=await run_agent(req)
    return {"id":"agent-"+uuid.uuid4().hex,"object":"chat.completion","created":int(time.time()),"model":req.model,"choices":[{"index":0,"message":{"role":"assistant","content":result.content},"finish_reason":"stop"}],"agent_steps":result.steps,"session_id":sid,"workspace":str(AGENT_TOOLS.root)}
