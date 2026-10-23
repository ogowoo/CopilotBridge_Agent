"""Bridge 层：配置、运行状态、Playwright 浏览器自动化与 Agent 会话编排。

HTTP 路由在 api.py；本模块不定义任何 FastAPI 端点。
"""

import asyncio, base64, collections, contextlib, json, os, re, shutil, time, uuid
from os.path import commonprefix
from datetime import datetime
from pathlib import Path
from typing import Literal
from fastapi import HTTPException
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field
from .agent import (AGENT_ACTION_SCHEMA, AgentRunner, AgentPaused, AgentInterrupted,
                    CopilotRateLimited, CopilotSendFailed, CopilotServiceUnavailable,
                    CopilotConversationFull, validate_token_budget)
from . import providers as providers_mod
from .tools_extra import register_extra_tools, ExtraToolError
from .plugins import PluginHost, PluginError
from .memory import MemoryStore
from .session_store import SessionStore
from .snapshots import SnapshotStore
from .spans import SpanRecorder, set_recorder
from .tools import SANDBOX_MODES, ToolError, ToolRegistry

BASE = Path(__file__).resolve().parent.parent  # 包在 cbp/ 内，项目根目录再上一层
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
        "description_en": "Let Microsoft 365 Copilot pick the response mode",
    },
    "copilot-quick-response": {
        "ui_names": ["Quick response", "Quick", "快速响应", "快速"],
        "description": "优先快速响应",
        "description_en": "Prefer fast responses",
    },
    "copilot-think-deeper": {
        "ui_names": ["Think deeper", "Think Deeper", "深入思考"],
        "description": "使用更深入的推理模式",
        "description_en": "Use deeper reasoning mode",
    },
    "copilot-gpt": {
        "ui_names": ["GPT"],
        "description": "使用 Copilot 菜单中的 GPT 模型",
        "description_en": "Use the GPT model from the Copilot menu",
    },
    "copilot-gpt-5-6": {
        "ui_names": ["GPT-5.6", "GPT 5.6", "GPT5.6"],
        "description": "使用 Copilot 菜单中的 GPT-5.6 模型",
        "description_en": "Use the GPT-5.6 model from the Copilot menu",
    },
    # GPT 父项下二级子菜单的叶子选项（实测菜单里只有这两个可选）：
    # 内置一份，未扫描前也能直接选中；扫描后若菜单改名会被自动发现覆盖。
    "copilot-gpt-5-6-sol-quick-response": {
        # 页面实测标签会省掉后缀（"GPT-5.6 Sol Quick"），全列出来免得锚定匹配落空
        "ui_names": ["GPT-5.6 Sol Quick response", "GPT-5.6 Sol Quick",
                     "Sol Quick response", "Sol Quick"],
        "description": "GPT-5.6 Sol 快速响应（GPT 子菜单）",
        "description_en": "GPT-5.6 Sol Quick response (GPT sub-menu)",
    },
    "copilot-gpt-5-6-sol-think-deeper": {
        # 真事：菜单/按钮上实际显示 "GPT-5.6 Sol Think"（没有 deeper），
        # 而这里只有 "GPT-5.6 Sol Think deeper" → 每回合都报"未在菜单中找到"，
        # 白跑一次菜单展开。把实测变体都列上。
        "ui_names": ["GPT-5.6 Sol Think deeper", "GPT-5.6 Sol Think",
                     "Sol Think deeper", "Sol Think"],
        "description": "GPT-5.6 Sol 深入思考（GPT 子菜单）",
        "description_en": "GPT-5.6 Sol Think deeper (GPT sub-menu)",
    },
    "copilot-sol": {
        "ui_names": ["Sol", "SOL"],
        "description": "使用 Copilot 菜单中的 Sol 模型",
        "description_en": "Use the Sol model from the Copilot menu",
    },
    "copilot-think": {
        "ui_names": ["Think", "思考"],
        "description": "使用 Copilot 菜单中的 Think 模型",
        "description_en": "Use the Think model from the Copilot menu",
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

# API 提供商（OpenAI 兼容 HTTP API），与 Copilot 网页自动化并列的模型来源。
# 模型 ID 形如 "deepseek/deepseek-chat"，API Key 走环境变量。
PROVIDERS = providers_mod.build_providers(CFG)

# Copilot 页面可访问性文本锚点。桥接依赖这些文本定位回合与回答边界；
# 租户界面语言不同（例如中文/日文界面）时，在 config.json 的 "markers" 中
# 追加对应文案即可——配置值会并入下方默认列表，任一命中即生效。
MARKERS = {
    "assistant_turn": ["Copilot said:", "Copilot の回答:", "Copilot からの応答:",
                       "Copilot からの回答:"],
    "user_turn": ["You said:", "あなたの発言:", "あなた:", "ユーザー:"],
    "answer_end": ["\nSources\n", "\n出典\n", "\n参考資料\n", "\nMessage Copilot\n",
                   "\nCopilot へのメッセージ\n", "\nメッセージを入力\n",
                   "AI-generated content may be incorrect",
                   "AI によって生成されたコンテンツは正しくない可能性があります"],
    "sources": ["Sources", "出典", "参考資料"],
    "ignored_lines": ["Copy", "Like", "Dislike", "More", "Share", "Regenerate",
                      "复制", "来源", "赞", "踩", "更多", "分享", "重新生成",
                      "コピー", "高評価", "低評価", "その他", "共有", "再生成"],
}
# 选择器里用的按钮/提示文案（日文系统实测收不到回信，就是这类锚点没命中）。
SELECTOR_TEXT = {
    "stop": ["Stop generating", "停止生成", "生成を停止", "生成を中止"],
    "stop_aria": ["Stop", "停止"],
    "new_chat": ["New chat", "新建聊天", "新しいチャット", "新規チャット"],
    "send_aria": ["Send", "发送", "送信"],
    "dismiss": ["Accept", "接受", "Got it", "知道了", "Not now", "暂不",
                "同意する", "あとで", "OK", "閉じる"],
}
for key, extra in CFG.get("markers", {}).items():
    if isinstance(extra, list):
        if key in MARKERS:
            for value in extra:
                if isinstance(value, str) and value and value not in MARKERS[key]:
                    MARKERS[key].append(value)
        if key in SELECTOR_TEXT:
            for value in extra:
                if isinstance(value, str) and value and value not in SELECTOR_TEXT[key]:
                    SELECTOR_TEXT[key].append(value)


def _last_turn(text: str, markers: list) -> tuple:
    """Return (position, length) of the last occurring marker, or (-1, 0)."""
    best_pos, best_len = -1, 0
    for marker in markers:
        pos = text.rfind(marker)
        if pos > best_pos:
            best_pos, best_len = pos, len(marker)
    return best_pos, best_len

pw = ctx = None
_PAGE_RAW = None          # 共享页（窗口 0，启动时创建）
lock = asyncio.Lock()      # 兼容保留：仅作"有任务在跑"的旧式标记，不再用于串行
RUNTIME = {}
ACTIVE_SESSION_ID = None

# ── 任务级上下文（并行的前提）────────────────────────────────────────
# 为什么不能只靠模块全局：并行跑两个子代理时，`ACTIVE_SESSION_ID` /
# `AGENT_TOOLS.current_session_id` / "是否允许网页 Copilot 工具" 都是**全局单值**，
# 两个任务会互相覆盖——A 的 finally 一执行就把 B 的设置改回去（结果串台、工具集错乱）。
# contextvars 在 `asyncio.create_task`/`gather` 时**按任务复制**，天然隔离；
# 全局变量继续保留，只为兼容旧调用与"最近活跃会话"这类展示用途。
import contextvars

_SESSION_CTX: contextvars.ContextVar = contextvars.ContextVar("cbp_session", default=None)
_WEB_TOOLS_CTX: contextvars.ContextVar = contextvars.ContextVar("cbp_web_tools", default=None)
_SUBAGENT_DEPTH_CTX: contextvars.ContextVar = contextvars.ContextVar("cbp_subagent_depth",
                                                                    default=0)

# ── 自动化窗口（多窗口并行）────────────────────────────────────────────
# 一个窗口 = 同一个持久化上下文里的一个 page；`page` 是代理，按任务上下文解析。
_PAGE_CTX: contextvars.ContextVar = contextvars.ContextVar("cbp_page", default=None)
_WIN_CTX: contextvars.ContextVar = contextvars.ContextVar("cbp_window", default=None)
_TOOLS_CTX: contextvars.ContextVar = contextvars.ContextVar("cbp_tools", default=None)


class _PageProxy:
    """把 `page.xxx` 解析成"当前任务绑定的窗口页"，没有窗口时退回共享页。

    为什么用代理而不是改 57 处调用点：桥接里 `page.locator(...)` 遍地都是，
    逐个改成函数调用既容易漏、也让 diff 无法审计；代理只在一个地方生效。
    """

    def _target(self):
        ctx_page = _PAGE_CTX.get()
        if ctx_page is not None:
            return ctx_page
        # 测试/宿主可能直接 `bridge.page = fake_page`（把代理换掉）：优先尊重它
        if not isinstance(page, _PageProxy):
            return page
        return _PAGE_RAW

    def __getattr__(self, name):
        target = self._target()
        if target is None:
            raise RuntimeError("浏览器页面尚未就绪")
        return getattr(target, name)

    def __bool__(self):
        return self._target() is not None

    def __repr__(self):
        target = self._target()
        return f"<PageProxy {'ready' if target is not None else 'empty'}>"


class CopilotBusy(RuntimeError):
    """所有自动化窗口都在忙（并发上限已满）。"""


page = _PageProxy()


def current_page():
    """当前任务的页面（没有任务上下文时是共享页/被替换的全局 page）。"""
    ctx_page = _PAGE_CTX.get()
    if ctx_page is not None:
        return ctx_page
    if not isinstance(page, _PageProxy):
        return page          # 测试注入了假页面
    return _PAGE_RAW


def set_shared_page(p) -> None:
    global _PAGE_RAW
    _PAGE_RAW = p


def current_window():
    return _WIN_CTX.get()


def task_tools():
    """当前任务的工具注册表（没绑定时是全局 AGENT_TOOLS）。"""
    return _TOOLS_CTX.get() or AGENT_TOOLS



def current_session() -> str | None:
    """当前任务的会话 id：**优先取任务上下文**，其次模块全局。"""
    sid = _SESSION_CTX.get()
    return sid if sid is not None else ACTIVE_SESSION_ID


def web_copilot_allowed() -> bool:
    """当前任务是否允许"需要 Copilot 网页会话"的工具（网页通道为真）。

    注意回落顺序：任务上下文 → **全局标志**（`set_web_copilot_tools` 设的）。
    不能直接默认 True：显式关掉网页工具的场景（提供商会话、单测）会被悄悄放开，
    实测就是 `test_web_tool_provider_gate` 抓到的这个问题。
    """
    v = _WEB_TOOLS_CTX.get()
    if v is not None:
        return bool(v)
    # 任务级注册表优先：并行任务各自 disable/enable，互不影响
    return bool(getattr(task_tools(), "allow_web_copilot_tools", True))


def subagent_chain_depth() -> int:
    """**当前任务链**的子代理深度（并行安全；不是"并发活跃数"）。"""
    try:
        return int(_SUBAGENT_DEPTH_CTX.get() or 0)
    except Exception:
        return 0


def subagent_parallel() -> int:
    """扇出并发上限。

    - API 提供商模型：`agent.subagent_parallel`（默认 4）；
    - 网页 Copilot 通道：= **自动化窗口数**（`agent.copilot_windows`，默认 1）。
      每个窗口是一个独立标签页，所以"并行几个子代理就开几个窗口"；
      窗口数配成 1 时行为和以前一样（顺序执行）。
    """
    try:
        n = int(AGENT_CFG.get("subagent_parallel", 4))
    except Exception:
        n = 4
    return max(1, min(n, 8))


def web_subagent_parallel() -> int:
    """网页 Copilot 通道的扇出上限 = **自动化窗口数**（每个窗口一个标签页）。

    窗口数可在设置里改（1–4）；没开窗口时就是 1（顺序执行，和以前一致）。
    """
    try:
        windows = max(1, int(getattr(WINDOWS, "max_windows", 1) or 1))
    except Exception:
        windows = 1
    return max(1, min(subagent_parallel(), windows))


def subagent_max_steps() -> int:
    """子代理默认步数上限（用户实测：默认 40 步常在做完前就用尽）。

    40 是当初拍的值，对"读几个大文件 + 分批写 + 验证"的任务明显偏紧：浏览器通道
    每回合只推进一点，40 步时任务往往才做到一半（实测 4 个子代理分别停在 39/40/41
    步）。默认放到 80，并允许用 `agent.subagent_max_steps` 调（夹在 10~200）。
    注意代价：步数上限越高，Copilot 通道消耗的回合越多（每回合都很贵）。
    """
    try:
        n = int(AGENT_CFG.get("subagent_max_steps", 80))
    except Exception:
        n = 80
    return max(10, min(n, 200))


def model_is_provider(model: str) -> bool:
    """该模型是否走 HTTP 提供商（而非网页 Copilot 通道）。"""
    try:
        pm = providers_mod.split_provider_model(str(model or ""))
    except Exception:
        return False
    return bool(pm and pm[0] in PROVIDERS)


# ── SSE 事件总线（每个会话一组订阅队列）──────────────────────────────
# 让前端不必轮询：runtime 快照直接推给浏览器，提供商流式输出就是真·逐字。
# 队列有界（慢客户端只丢事件、不拖慢 Agent）；没有订阅者时几乎零开销。
_EVENT_SUBS: dict[str, set] = {}
_EVENT_LOOPS: dict = {}          # 队列 -> 创建它的事件循环（跨线程推送要用）
_EVENT_QUEUE_MAX = 64


def _safe_put(q, payload: dict) -> None:
    """入队；满了就丢最旧的，保证最新状态能送达（慢客户端绝不拖慢 Agent）。"""
    try:
        q.put_nowait(payload)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
            q.put_nowait(payload)
        except Exception:
            pass


def subscribe_events(sid: str):
    q: asyncio.Queue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAX)
    _EVENT_SUBS.setdefault(sid, set()).add(q)
    try:
        _EVENT_LOOPS[q] = asyncio.get_running_loop()
    except RuntimeError:
        pass
    return q


def unsubscribe_events(sid: str, q) -> None:
    _EVENT_LOOPS.pop(q, None)
    subs = _EVENT_SUBS.get(sid)
    if not subs:
        return
    subs.discard(q)
    if not subs:
        _EVENT_SUBS.pop(sid, None)


def publish_event(sid: str | None, kind: str, data=None) -> None:
    """向该会话的订阅者推送一个事件；无人订阅或队列满时静默丢弃。

    跨线程调用（脚本、后台线程）时转交到该队列所属的事件循环——asyncio.Queue
    不是线程安全的，直接 put_nowait 不会唤醒等待中的消费者，事件会静默丢失。
    """
    if not sid:
        return
    subs = _EVENT_SUBS.get(sid)
    if not subs:
        return
    payload = {"type": kind}
    if data is not None:
        payload["data"] = data
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    for q in list(subs):
        loop = _EVENT_LOOPS.get(q)
        if loop is not None and loop is not running and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(_safe_put, q, payload)
                continue
            except RuntimeError:
                pass
        _safe_put(q, payload)


def runtime_update(session_id=None, **values):
    # 任务级会话优先：并行时全局 ACTIVE_SESSION_ID 会被别的任务覆盖，
    # 用它兜底会把 A 的进度写到 B 的界面上（界面表现为两个任务状态互相跳）
    sid = session_id or current_session() or ACTIVE_SESSION_ID
    if not sid:
        return
    item = RUNTIME.setdefault(sid, {"phase": "idle", "updated": time.time(), "answer_preview": "", "prompt_preview": "", "detail": ""})
    item.update(values)
    item["updated"] = time.time()
    if _EVENT_SUBS.get(sid):           # 只在有人订阅时才付出序列化成本
        publish_event(sid, "runtime", dict(item))


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=200000)


class Request(BaseModel):
    model: str = DEFAULT_MODEL
    messages: list[Message] = Field(min_length=1)
    stream: bool = True
    new_conversation: bool = True

class SessionMessageRequest(BaseModel):
    content: str = Field(default="", max_length=120000)
    model: str | None = None
    agent_mode: bool = True
    # 粘贴的图片/截图附件（data URL），仅 Copilot 网页会话支持
    attachments: list[str] | None = None

class SessionModelRequest(BaseModel):
    """切换某个会话使用的模型（下一回合生效，服务重启后仍沿用）。"""
    model: str = Field(min_length=1, max_length=200)

class AgentRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    max_steps: int | None = Field(default=None, ge=1, le=10000)
    session_id: str | None = Field(default=None, min_length=1, max_length=100)
    reuse_session: bool = True
    workspace: str | None = None
    # False 时为普通对话模式：只在该 Session 绑定的 Copilot 会话里问答，
    # 不启动 Agent 工具循环。
    agent_mode: bool = True
    # 粘贴的图片/截图附件（data URL），仅 Copilot 网页会话支持
    attachments: list[str] | None = None
    # 计划模式：本轮强制只读，模型产出方案后调用 exit_plan_mode 结束
    plan_mode: bool = False
    # 沙箱档位覆盖（read-only / workspace-write / danger-full-access）；不传用配置
    sandbox_mode: str | None = None


def log(s):
    print(f"[{datetime.now():%H:%M:%S}] {s}", flush=True)


def normalize_model(model_id):
    # API 提供商模型（provider/model 格式）直接放行，有效性在调用时校验
    pm = providers_mod.split_provider_model(model_id)
    if pm and pm[0] in PROVIDERS:
        return model_id
    normalized = MODEL_ALIASES.get(model_id, model_id)
    if normalized not in MODELS:
        raise HTTPException(
            400,
            detail={
                "message": f"不支持的模型: {model_id}",
                "type": "invalid_request_error",
                "available_models": all_model_ids(),
            },
        )
    return normalized


def normalize_model_soft(model_id) -> str:
    """宽松归一：失效模型 ID（如历史会话遗留的已删除模型）回退默认模型。

    用于会话恢复/跟进场景：严格 400 会让老会话永远无法继续。
    """
    try:
        return normalize_model(model_id)
    except HTTPException:
        return DEFAULT_MODEL


# 上次成功扫描 Copilot 菜单看到的标签（casefold）；None = 尚未成功扫描
DISCOVERED_LABELS: set | None = None
# 本次扫描命中的内置模型数：为 0 说明菜单整体改名/大改版，此时放行全部内置模型
DISCOVERY_MATCHED: int = 0


def copilot_model_available(item: dict) -> bool:
    """模型是否在菜单实况里真实可选（决定 WebUI 下拉是否显示）。

    严格对齐菜单叶子：只有名字确实出现在扫描到的叶子集合里才算可用。
    父项容器（如 GPT 本身）不是可选项，选中它无法切换，故一并隐藏；
    实测菜单的真实叶子是 Auto / Quick response / Think deeper /
    GPT-5.6 Sol Quick response / GPT-5.6 Sol Think deeper。
    兜底：未扫描过，或扫到了但一个内置模型都没命中（改名/大改版）时全放行，
    避免下拉变空。
    """
    if DISCOVERED_LABELS is None or DISCOVERY_MATCHED == 0:
        return True
    return any(name.casefold() in DISCOVERED_LABELS for name in item["ui_names"])


def all_model_ids() -> list:
    """Copilot 网页模型 + 已配置提供商的模型。"""
    ids = list(MODELS)
    for pname, p in PROVIDERS.items():
        ids.extend(f"{pname}/{m}" for m in p.models)
    return ids


async def diagnostics(tag):
    if not DIAG or current_page() is None: return
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
    b = await visible([f'button:has-text("{t}")' for t in SELECTOR_TEXT["dismiss"]])
    if b:
        try:
            await b.click(timeout=1000)
        except Exception:
            pass


async def new_chat():
    """开一个空对话。**必须验证真的空了**。

    实测（task-316f6502774b）：新任务第一轮 prompt 里明明是新任务（建 ok.txt），
    模型却接着另一条"pyshot 快捷键"对话的思路去读 pyshot/main.py。
    说明点"New chat"之后页面并没有变成空对话——后续 prompt 落在了旧上下文里，
    任务被"串"到上一条对话上（用户："为什么会窜到 pyshot 里的？"）。
    """
    async def _empty() -> bool:
        try:
            return await count_user_markers() == 0
        except Exception:
            return False

    b = await visible(
        [f'button[aria-label*="{t}" i]' for t in SELECTOR_TEXT["new_chat"]]
        + [f'a[aria-label*="{t}" i]' for t in SELECTOR_TEXT["new_chat"]]
        + [f'button:has-text("{t}")' for t in SELECTOR_TEXT["new_chat"]]
    )
    if b:
        await b.click()
        await asyncio.sleep(1)
    else:
        await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
        await asyncio.sleep(2)

    if not await _empty():
        # 点击没生效 / 页面被复用：直接回基础地址重试一次
        log("新建对话后页面仍有历史消息，回基础地址重试")
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
        except Exception:
            pass
        for _ in range(20):                 # 最多等 10s
            await asyncio.sleep(0.5)
            if await _empty():
                break
    if not await _empty():
        raise CopilotSendFailed(
            "新对话未创建成功（页面仍在旧会话）：已中止，避免把新任务接到上一条对话的上下文里。"
            "请点『重试浏览器』或稍后再试。")
    # 新聊天会把模型重置为 Copilot 默认（Auto）：缓存记为 copilot-auto，
    # 注意这是页面行为而非我们的 default_model 配置。
    _forget_selected_model(reset_to="copilot-auto")


# 上次成功确认/切换的模型。新版 Copilot 的模型按钮经常没有可读文本，
# 无法廉价确认当前模型；有缓存即可跳过"开菜单→点选项"的整段耗时操作。
SELECTED_MODEL = None


def get_selected_model() -> str | None:
    """当前窗口缓存的模型（没有窗口时用全局值，兼容旧路径与测试）。"""
    win = _WIN_CTX.get()
    return win.selected_model if win is not None else SELECTED_MODEL


def set_selected_model(value: str | None) -> None:
    SELECTED_MODEL = value
    win = _WIN_CTX.get()
    if win is not None:
        win.selected_model = value


def _forget_selected_model(reset_to: str | None = None):
    """Invalidate the model cache, optionally recording a known reset target.

    new_chat() resets the selector to the tenant default, so callers can pass
    DEFAULT_MODEL to keep cache hits for default-model requests.
    """
    set_selected_model(reset_to)


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
    if not button:
        # 兜底：当前选中的模型可能是菜单里新出现、尚未收录的模型，
        # 按钮文本不匹配任何已知名字，改按结构特征定位模型选择器。
        button = await visible_locator([
            page.locator('button[aria-label*="Model Selector" i]'),
            page.locator('button[aria-label*="模型"][aria-haspopup]'),
            page.locator('button[aria-label*="model" i][aria-haspopup="menu"]'),
            page.locator('button[aria-label*="model" i][aria-haspopup="listbox"]'),
        ])
    return button


def _model_id_from_label(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    return f"copilot-{slug}" if slug else ""


_LABEL_NOISE = re.compile(r"[\s·•\-\._\(\)\[\]:：,，/|]+")


def normalize_label(text: str) -> str:
    """标签归一：小写 + 去掉空格/标点/分隔符，用于容错比较。

    实测 Copilot 菜单里的标签会有细微差别（"GPT-5.6 Sol Think" vs
    "GPT-5.6 Sol Think deeper"、全角/半角标点、多余空格），用锚定正则匹配
    会直接落空，然后白跑一遍菜单探测。
    """
    return _LABEL_NOISE.sub("", str(text or "").lower())


def label_matches(target_names, label: str) -> bool:
    """该菜单标签是否对应目标模型（归一后相等，或一方包含另一方）。"""
    got = normalize_label(label)
    if not got:
        return False
    for name in target_names:
        want = normalize_label(name)
        if not want:
            continue
        if got == want:
            return True
        # "solthink" ⊂ "gpt56solthinkdeeper" 之类：短的一方至少 6 个字符才允许包含匹配，
        # 否则 "gpt" 会匹配一切。
        if len(got) >= 6 and len(want) >= 6 and (got in want or want in got):
            return True
    return False


def _menu_target_locators(target):
    """Locate a model entry by name across the menu roles Copilot uses."""
    return [
        page.get_by_role("option", name=target),
        page.get_by_role("menuitem", name=target),
        page.get_by_role("menuitemradio", name=target),
        page.get_by_text(target, exact=True),
        page.get_by_role("option", name=target, exact=False),
        page.get_by_role("menuitem", name=target, exact=False),
        page.get_by_role("menuitemradio", name=target, exact=False),
        page.get_by_text(target, exact=False),
    ]


MENU_ITEM_ROLES = ("option", "menuitemradio", "menuitemcheckbox", "menuitem")
MENU_CONTAINER_SEL = ('[role="menu"]', '[role="listbox"]', '.fui-MenuList',
                      '[class*="MenuList" i]', '[data-testid*="menu" i]')


async def _visible_menu_items():
    """All currently visible model menu entries as (locator, label, submenu_hinted).

    角色并不统一：一级项多为 menuitemradio/option，二级飞出的子菜单项在不同
    租户/版本里可能是 menuitemcheckbox，甚至只是菜单容器里的普通子元素。
    因此先按角色收集，再对可见菜单容器的直接子项做兜底扫描，按标签去重。
    """
    entries = []
    seen: set = set()

    async def add(item):
        try:
            if not await item.is_visible():
                return
            raw_text = (await item.inner_text() or "")
            # 先取第一行再规整空白：inner_text 会把 标题/描述/已展开子项
            # 拼成多行；若先折叠空白，多行会拼成一个不存在的假模型名
            # （例如 "GPT-5.6 Sol Think deeper"），之后选择必失败。
            first_line = raw_text.splitlines()[0] if raw_text else ""
            label = re.sub(r"\s+", " ", first_line).strip()
            if not label or len(label) > 40 or label in seen:
                return
            seen.add(label)
            entries.append((item, label, await _submenu_hinted(item)))
        except Exception:
            pass

    for role in MENU_ITEM_ROLES:
        items = page.get_by_role(role)
        for i in range(await items.count()):
            await add(items.nth(i))
    # 兜底：可见菜单容器内的直接子项（role 命名不统一时靠结构识别）
    for sel in MENU_CONTAINER_SEL:
        containers = page.locator(sel)
        for ci in range(min(await containers.count(), 6)):
            container = containers.nth(ci)
            try:
                if not await container.is_visible():
                    continue
                kids = container.locator("> *")
                for ki in range(await kids.count()):
                    await add(kids.nth(ki))
            except Exception:
                continue
    return entries


async def _submenu_hinted(item) -> bool:
    """条目"看起来像"子菜单父项——仅用于决定能否点击展开。

    悬停是安全的，点击叶子会真的切换模型，所以必须有依据才允许点。
    实测 Copilot 的父项标记并不统一：GPT 这类项可能只有 aria-haspopup="true"
    （不是 "menu"）、aria-expanded，或仅一个 chevron 图标，只认 "menu" 会漏。
    用一次 evaluate 取全部信号：逐条 locator 查询会把菜单扫描拖慢几十秒。
    """
    try:
        info = await item.evaluate(
            """el => ({
                popup: (el.getAttribute('aria-haspopup') || '').toLowerCase(),
                expanded: el.hasAttribute('aria-expanded'),
                icon: !!el.querySelector(
                    '[data-icon-name*="Chevron" i],[class*="submenuIndicator" i],' +
                    '[class*="Chevron" i],[class*="expandIcon" i]')
            })""",
            timeout=1000,   # 飞出的子菜单项常已随父菜单失效：短超时，别等默认 30s
        )
    except Exception:
        return False
    if info.get("popup") in {"menu", "true", "listbox", "tree", "dialog"}:
        return True
    return bool(info.get("expanded") or info.get("icon"))


async def _menu_label_set() -> set:
    return {label for _item, label, _hint in await _visible_menu_items()}


async def _expand_submenu(parent, allow_click: bool = False, timeout: int = 1200) -> None:
    """悬停展开子菜单（Fluent UI 悬停即开）；allow_click 时才补一次点击。

    timeout 取小值：探测过程中条目可能已随父菜单收起，干等默认 30s/3s
    会把一次模型刷新拖成几十秒。
    """
    try:
        await parent.hover(timeout=timeout)
        await asyncio.sleep(.4)
    except Exception:
        pass
    if allow_click:
        try:
            await parent.click(timeout=timeout)
            await asyncio.sleep(.4)
        except Exception:
            pass


async def _menu_signature() -> tuple:
    """轻量菜单签名：可见菜单容器内每个条目的首行标签。

    轮询"菜单是否变多"时不能每轮都跑完整 role 扫描（每次约 0.4s，
    一次刷新要轮询几十次）。这里一次 evaluate 拿完，开销约几十毫秒。
    """
    try:
        items = await page.evaluate(
            """() => {
                const out = [];
                const sel = '[role="menu"],[role="listbox"],.fui-MenuList,[class*="MenuList" i]';
                for (const c of document.querySelectorAll(sel)) {
                    const st = getComputedStyle(c);
                    if (st.display === 'none' || st.visibility === 'hidden' || !c.offsetParent) continue;
                    for (const el of c.children) {
                        const t = (el.innerText || '').split('\\n')[0].trim();
                        if (t) out.push(t);
                    }
                }
                return out;
            }"""
        )
        return tuple(items or ())
    except Exception:
        return ()


async def _wait_growth(base_labels: set, tries: int = 6, delay: float = 0.25) -> bool:
    """等待菜单项变多：Fluent 子菜单有展开动画，展开后立刻查会落空。"""
    for _ in range(max(1, tries)):
        if set(await _menu_signature()) - base_labels:
            return True
        await asyncio.sleep(delay)
    return False


async def _probe_submenu(item, hinted: bool, base_labels: set) -> bool:
    """尝试展开条目，用"菜单项是否变多"判定它到底是不是父项。

    分级探测（菜单里叶子占多数，对叶子做全套会白等几十秒）：
    - 任何条目：悬停 + 短轮询（Fluent 菜单悬停即开子菜单，对叶子无副作用）
    - 带父项特征（aria-haspopup/aria-expanded/chevron）的条目：再试
      键盘 → 键（ARIA 菜单模式展开子菜单，对叶子是空操作）
      以及一次点击（叶子被点击会真的切换模型，所以只对有依据的做）
    """
    try:
        if not await item.is_visible():   # 条目可能已随父菜单收起：直接跳过
            return False
    except Exception:
        return False
    await _expand_submenu(item, allow_click=False)
    if await _wait_growth(base_labels, tries=6 if hinted else 2):
        return True
    if not hinted:
        return False
    try:
        await item.focus(timeout=800)
        await page.keyboard.press("ArrowRight")
        if await _wait_growth(base_labels, tries=4):
            return True
    except Exception:
        pass
    await _expand_submenu(item, allow_click=True)
    return await _wait_growth(base_labels)


async def dump_menu_diagnostics() -> str:
    """把当前可见的菜单 DOM/文本落盘（last_menu.html / last_menu.txt）。

    模型菜单扫不到（尤其二级子菜单）时，这两份快照是唯一能看到真实
    角色与结构的现场：AI/维护者据此改选择器，无需盲猜。
    """
    try:
        parts, texts = [], []
        for sel in MENU_CONTAINER_SEL:
            loc = page.locator(sel)
            for i in range(min(await loc.count(), 5)):
                el = loc.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    parts.append(await el.evaluate("el => el.outerHTML"))
                    texts.append(await el.inner_text())
                except Exception:
                    continue
            if parts:
                break
        if parts:
            (BASE / "last_menu.html").write_text(
                "\n\n<!-- ===== menu ===== -->\n\n".join(parts)[:400000], encoding="utf-8")
            (BASE / "last_menu.txt").write_text("\n\n-----\n\n".join(texts)[:80000], encoding="utf-8")
            return "last_menu.html"
    except Exception:
        pass
    return ""


COPILOT_URL_MARK = re.compile(r"m365\.cloud\.microsoft|/chat|copilot\.microsoft\.com", re.I)


async def ensure_copilot_page() -> str:
    """确保自动化浏览器停在 Copilot 对话页——模型菜单只在对话页存在。

    Outlook(OWA) 自动化、会话删除、普通对话都会让浏览器停在别的页面；
    不先回到对话页就直接扫模型菜单，会找不到按钮而静默失败。
    """
    if current_page() is None:
        raise RuntimeError("自动化浏览器尚未启动")
    current = ""
    try:
        current = page.url or ""
    except Exception:
        current = ""
    if COPILOT_URL_MARK.search(current):
        return current
    base = str(CFG.get("copilot_url") or "https://m365.cloud.microsoft/chat/")
    await page.goto(base, wait_until="domcontentloaded", timeout=120000)
    await wait_copilot_ready(90)
    await asyncio.sleep(1.0)
    try:
        return page.url or base
    except Exception:
        return base


async def discover_models() -> dict:
    """打开 Copilot 模型菜单，枚举可见选项（含二级子菜单）并并入 MODELS。

    返回结构化结果而不是裸列表：调用方需要区分"扫描成功但无新增"与
    "压根没扫成"（浏览器不在对话页/未登录/UI 改版），否则界面只能静默。

    已知名字（含各 ui_names 别名）直接跳过；带 aria-haspopup 的父项会
    逐个 hover/click 展开后继续枚举其子菜单，父项本身不作为模型收录。
    新出现的叶子选项按标签生成 copilot-<slug> 形式的 ID。
    只读操作：结束后 Escape 逐级关闭菜单，不改变当前选中模型。
    """
    result = {"added": [], "scanned": False, "labels": [], "reason": "", "url": "",
              "menu_dump": ""}
    try:
        result["url"] = await ensure_copilot_page()
    except Exception as exc:
        result["reason"] = f"无法打开 Copilot 对话页: {exc}"
        return result
    button = await model_button()
    if not button:
        result["reason"] = "未找到模型菜单按钮：页面可能未就绪/未登录，或 Copilot 前端改版"
        await diagnostics("models_refresh_no_button")
        return result
    try:
        await button.click(timeout=5000)
    except Exception:
        await button.click(timeout=3000, force=True)
    await asyncio.sleep(.6)
    labels_order: list[str] = []
    seen_labels: set = set()
    parents: set = set()      # 实测能展开出子菜单的父项（不算模型）
    probed: set = set()       # 已探测过的条目标签，避免重复悬停
    leaf_labels = set()       # 叶子项（真正可选中）；父项只是容器，不算可用模型
    result_menu_dump = ""
    try:
        # 逐轮探测：每轮把未见过的条目悬停一遍，谁能展开出子菜单谁就是父项。
        # 关键点：**每轮都增量累积可见项**——飞出的子菜单在鼠标移开后就消失，
        # 只在最后统一收集会漏掉二级菜单（实测踩过）。
        for _depth in range(3):
            entries = await _visible_menu_items()
            if not entries:
                break
            base_labels = {lbl for _i, lbl, _h in entries}
            for _i, lbl, _h in entries:
                if lbl not in seen_labels:
                    seen_labels.add(lbl)
                    labels_order.append(lbl)
            grew = False
            for item, label, hinted in entries:
                if label in probed:
                    continue
                probed.add(label)
                if await _probe_submenu(item, await _submenu_hinted(item), base_labels):
                    parents.add(label)
                    grew = True
                    break   # 菜单结构已变，重新收集后再继续探测
            if not grew:
                break
        # 收尾再累积一次（此刻可能仍开着某个子菜单），并把菜单现场落盘
        for _i, lbl, _h in await _visible_menu_items():
            if lbl not in seen_labels:
                seen_labels.add(lbl)
                labels_order.append(lbl)
        result_menu_dump = await dump_menu_diagnostics()
    finally:
        for _ in range(3):  # 子菜单可能多级展开，多按几次 Escape 确保全部关闭
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(.2)
            except Exception:
                pass
    labels = [lbl for lbl in labels_order if lbl not in parents]
    leaf_labels = {lbl.casefold() for lbl in labels}
    global DISCOVERED_LABELS, DISCOVERY_MATCHED
    # 半渲染/子菜单未飞出时只枚举到 1 个叶子：这种"扫描结果"会把其它模型
    # 全部标成不可用（WebUI 下拉大面积消失），且在下一次成功扫描前不自愈。
    # 因此只在扫到 ≥2 个叶子时更新可见性集合；空结果保留上次快照。
    if len(leaf_labels) >= 2 or not DISCOVERED_LABELS:
        DISCOVERED_LABELS = leaf_labels  # 只记叶子：GPT 这类子菜单父项不是可选模型
    added = []
    known_names = {name.casefold() for item in MODELS.values() for name in item["ui_names"]}
    for label in labels:
        if label.casefold() in known_names:
            continue
        model_id = _model_id_from_label(label)
        if not model_id or model_id in MODELS:
            continue
        MODELS[model_id] = {
            "ui_names": [label],
            "description": f"从 Copilot 菜单自动发现的模型（{label}）",
        }
        known_names.add(label.casefold())
        added.append(model_id)
    # 命中数决定可用性判定的严格程度：0 命中（菜单整体改名）时放行全部内置模型
    DISCOVERY_MATCHED = sum(
        1 for item in MODELS.values()
        if any(name.casefold() in leaf_labels for name in item["ui_names"])
    )
    result.update({"added": added, "scanned": True, "labels": labels,
                   "menu_dump": result_menu_dump, "parents": sorted(parents),
                   "matched_builtins": DISCOVERY_MATCHED})
    if not labels:
        result["reason"] = "模型菜单打开了但没有枚举到任何选项（可能是渲染延迟或前端改版）"
        await diagnostics("models_refresh_empty_menu")
    elif not parents:
        result["reason"] = ("菜单已枚举但未探测到子菜单父项；若列表缺少二级模型，"
                            "请把服务目录的 last_menu.html 发给维护者")
    return result


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
    current_values = (current_text, current_label)
    # 按逗号/斜杠分段后精确比较，防止 "GPT" 子串命中 "GPT-5.6, Model Selector"
    # 之类的 aria-label 导致误判"已是目标模型"而跳过切换。
    def _norm(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip().casefold())
    segments = set()
    for value in current_values:
        if not value:
            continue
        segments.add(_norm(value))
        for part in re.split(r"[,，/|]", value):
            if part.strip():
                segments.add(_norm(part))
    targets = {_norm(name) for name in target_names}
    if segments & targets:
        set_selected_model(normalized)
        log(f"模型已是 {normalized} ({current_label or current_text})")
        return normalized
    other_known = {_norm(name) for mid, item in MODELS.items() if mid != normalized
                   for name in item["ui_names"]}
    if get_selected_model() == normalized and not (segments & other_known):
        # 按钮文本为空或无法识别（未命中任何已知模型），且没有证据表明
        # 当前选中的是其他模型：信任上次成功选择，跳过开菜单的耗时操作。
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
    option = await visible_locator(_menu_target_locators(target))
    if not option:
        # 目标可能藏在二级子菜单（如 GPT 父项飞出更多模型）：逐个探测展开。
        # 不再依赖 aria-haspopup 判定父项——悬停安全，按效果确认是否父项。
        entries = await _visible_menu_items()
        base_labels = {lbl for _i, lbl, _h in entries}
        for parent, label, _hint in entries:
            if not await _probe_submenu(parent, await _submenu_hinted(parent), base_labels):
                continue
            # 子菜单展开有动画延迟，立刻找可能落空：短轮询三次
            for _try in range(3):
                option = await visible_locator(_menu_target_locators(target))
                if option:
                    break
                await asyncio.sleep(.4)
            if option:
                break
            base_labels = await _menu_label_set()
    if not option:
        # 归一化兜底：把可见菜单项按 normalize_label 比较一次，命中就直接点它。
        # （精确正则用的是整串锚定，标签少一个后缀就落空）
        try:
            for item, label, _hint in await _visible_menu_items():
                if label_matches(target_names, label):
                    option = item
                    log(f"模型标签按归一化匹配命中：{label!r} → {normalized}")
                    break
        except Exception as exc:
            log(f"归一化匹配失败（忽略）：{exc}")
    if not option:
        try: await page.keyboard.press("Escape")
        except Exception: pass
        await diagnostics("model_option_missing")
        # 菜单结构变化或标签未收录不应打死整个回合：按当前已选模型继续，
        # 但必须让用户看见——否则重启后"模型悄悄变回 Auto"完全无从察觉。
        fallback = get_selected_model() or normalized
        log(f"警告：模型菜单中找不到 {target_names[0]}，已按当前选中模型继续")
        if current_session():
            runtime_update(current_session(), model_warning=(
                f"模型 {target_names[0]} 未在 Copilot 菜单中找到，本回合沿用 "
                f"{MODELS.get(fallback, {}).get('ui_names', [fallback])[0]}"))
        return fallback
    try:
        await option.click(timeout=8000)
    except Exception:
        await option.click(timeout=5000, force=True)
    await asyncio.sleep(.8)
    set_selected_model(normalized)
    log(f"模型已切换为 {normalized}")
    return normalized

async def attach_file_to_copilot(file_path: Path) -> None:
    """把本地文件附加到 Copilot 输入框（不发送）。

    Copilot 的 input[type=file] 通常是隐藏的：先尝试点开附件按钮使其出现，
    再用 set_input_files 直接注入（对隐藏控件同样有效）。
    """
    file_input = page.locator('input[type="file"]')
    if not await file_input.count():
        btn = await visible([
            'button[aria-label*="Attach" i]',
            'button[aria-label*="Add attachment" i]',
            'button[aria-label*="Upload" i]',
            'button[aria-label*="附件"]',
            'button[aria-label*="上传"]',
            'button[aria-label*="Add" i]',
        ])
        if btn:
            try:
                await btn.click(timeout=3000)
            except Exception:
                await btn.click(timeout=2000, force=True)
            await asyncio.sleep(.6)
    if not await file_input.count():
        await diagnostics("no_file_input")
        raise RuntimeError("找不到 Copilot 文件上传控件")
    await file_input.first.set_input_files(str(file_path))
    # 固定 2s 对**大截图**不够：上传+解析没完成就把消息发出去，Copilot 收到的
    # 就是"没有附件的纯文本"，看着就像"贴图识别失败"。这里多等一会儿，
    # 有附件缩略图出现就提前结束；选择器不匹配时按最长等待兜底（行为不变）。
    await asyncio.sleep(2)
    _chip_sels = [
        '[data-testid*="attachment" i]',
        '[data-testid*="file" i]',
        '.attachment-pill',
        'img[src^="blob:"]',
    ]
    _deadline = 10.0
    _waited = 0.0
    while _waited < _deadline:
        _found = 0
        for _sel in _chip_sels:
            try:
                _found += await page.locator(_sel).count()
            except Exception:
                pass
        if _found:
            break
        await asyncio.sleep(0.5)
        _waited += 0.5
    if _waited:
        log(f"等待附件 {file_path.name} 上传完成 {_waited:.1f}s")


MAX_IMAGE_ATTACHMENTS = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def save_image_attachments(images) -> list[Path]:
    """把前端粘贴的图片/截图（data URL）落盘，返回文件路径列表。

    前端在"继续对话 / 新任务"里粘贴截图时，把剪贴板图片读成 data URL 随消息
    一起 POST 过来；这里解码落盘到 data/uploads，之后由 attach_file_to_copilot
    附加到 Copilot 输入框。上限 4 张、单张 8MB，超限或解码失败静默跳过。
    """
    if not images:
        return []
    upload_dir = BASE / "data" / "uploads"
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return []
    paths: list[Path] = []
    for i, item in enumerate(list(images)[:MAX_IMAGE_ATTACHMENTS]):
        if not item or not isinstance(item, str):
            continue
        m = re.match(r"^data:image/([A-Za-z0-9+.-]+);base64,(.*)$", item, re.S)
        ext, b64 = "png", item
        if m:
            ext = (m.group(1).lower().replace("jpeg", "jpg") or "png")
            b64 = m.group(2)
        try:
            data = base64.b64decode(b64, validate=False)
        except Exception:
            continue
        if not data or len(data) > MAX_IMAGE_BYTES:
            continue
        p = upload_dir / f"{uuid.uuid4().hex[:12]}_{i}.{ext}"
        try:
            p.write_bytes(data)
            paths.append(p)
        except Exception:
            continue
    return paths


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


async def wait_copilot_ready(timeout: float = 120) -> bool:
    """等待 SSO/ADFS 重定向链走完、Copilot 输入框可用。

    企业租户（E3/ADFS 联合身份）重启后会自动经过
    login.microsoftonline.com → 公司 ADFS（如 sts.dsv.com）→ 回跳，
    全程无需手动登录但可能耗时几十秒。发现模型/自动恢复等启动动作
    必须等它就绪，否则会在登录页上误操作。
    """
    from urllib.parse import urlparse
    host = urlparse(URL).netloc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            url = page.url or ""
            if host and host in url:
                box = await visible([
                    'textarea[placeholder]', 'textarea',
                    '[contenteditable="true"][role="textbox"]',
                    '[contenteditable="true"]', '[role="textbox"]',
                ])
                if box:
                    return True
        except Exception:
            pass
        await asyncio.sleep(2)
    log("等待 Copilot 就绪超时（仍在登录/跳转中），跳过启动期自动化")
    return False



MD_JS = r"""/* 把 Copilot 回答区域的 DOM 转成 Markdown。
 * 之前用 innerText 提取 → 代码块/列表/标题/粗体全被拍成纯文本，
 * 聊天区拿到的"原文"就没有格式可渲染（用户实测对比：网页有格式、这里全是纯文本）。
 * 转换规则刻意简化：只覆盖 Copilot 回答里常见的结构，未知元素回退为文本。 */
window.__cbp_html2md = function(root){
  const SKIP = new Set(['SCRIPT','STYLE','BUTTON','SVG','INPUT','SELECT','TEXTAREA','NOSCRIPT']);
  const SKIP_CLASS = ['citation','copilot-citation','attribution','sr-only','visually-hidden'];
  function walk(node){
    if(!node) return '';
    if(node.nodeType === 3) return node.textContent;            // 文本节点
    if(node.nodeType !== 1) return '';                           // 非元素
    const tag = node.tagName;
    if(SKIP.has(tag)) return '';                                 // UI 元素不进文本
    // 代码块头部（语言名 + 复制按钮）：它的下一个兄弟才是代码编辑器
    if (node.nextElementSibling && node.nextElementSibling.querySelector
        && node.nextElementSibling.querySelector('[data-virtualized-code-find-root]')) return '';
    const cls = (node.className && String(node.className)) || '';
    if(SKIP_CLASS.some(c => cls.toLowerCase().includes(c))) return '';
    // 跳过 Copilot 的"思考了 X 秒"头与引用角标
    if(/reasoned|thinking|思考了/i.test(cls)) return '';
    // Copilot 自绘代码块（不是 <pre>）：带 data-virtualized-code-find-root 的容器。
    // 代码行在 data-line-index 的 div 里，行号在 data-line-number（空）里。
    // 语言标签在它前面的兄弟节点（header）里。
    // 只处理**恰好含一个**代码块的容器；含多个的是包装容器，让子节点各自处理
    const codeRoots = node.querySelector ? node.querySelectorAll('[data-virtualized-code-find-root]') : [];
    if (codeRoots.length === 1) {
      // Copilot 有时把编号列表塞进代码编辑器（实测过："1. 验证输入" 在 data-line-index 里）。
      // 识别：所有"代码行"都是"N. xxx"编号 → 转回有序列表，不当代码块。
      const _lines = [...node.querySelectorAll('[data-line-index]')].map(el => el.textContent.trim());
      if (_lines.length && _lines.every((t, i) => new RegExp('^' + (i + 1) + '\\.\\s').test(t))) {
        return _lines.map(t => t + '\n').join('') + '\n';
      }
      const codeLines = [...node.querySelectorAll('[data-line-index]')].map(el => el.textContent);
      // 语言标签：从前面兄弟里找，但要排除按钮/引用里的 span（否则会把 "+1"、"gnu"
      // 之类的引用标记当成语言名）；且必须是已知的语言名才认。
      const KNOWN_LANGS = new Set(['python','javascript','typescript','js','ts','powershell',
        'shell','bash','sh','zsh','json','yaml','yml','xml','html','css','sql','go','rust',
        'java','c','cpp','csharp','cs','ruby','php','swift','kotlin','scala','r','lua',
        'perl','dockerfile','makefile','ini','toml','markdown','md','text','plain']);
      let lang = '';
      let p = node.previousElementSibling;
      while (p && !lang) {
        const spans = p.querySelectorAll ? p.querySelectorAll('span') : [];
        for (const s of spans) {
          if (s.closest('button') || s.closest('[class*="citation" i]')) continue;  // 引用/按钮里的不算
          const cand = s.textContent.trim().split(/\s/)[0].toLowerCase();
          if (KNOWN_LANGS.has(cand)) { lang = cand; break; }
        }
        p = p.previousElementSibling;
      }
      return '\n```' + lang + '\n' + codeLines.join('\n') + '\n```\n\n';
    }
    const kids = [...node.childNodes].map(walk).join('');
    switch(tag){
      case 'H1': return '\n# '+kids.trim()+'\n\n';
      case 'H2': return '\n## '+kids.trim()+'\n\n';
      case 'H3': return '\n### '+kids.trim()+'\n\n';
      case 'H4': return '\n#### '+kids.trim()+'\n\n';
      case 'P': return kids.trim()+'\n\n';
      case 'BR': return '\n';
      case 'STRONG': case 'B': return '**'+kids+'**';
      case 'EM': case 'I': return '*'+kids+'*';
      case 'CODE': {
        // 行内 code（在 pre 里的是代码块，由 PRE 分支处理）
        if(node.closest('pre')) return kids;
        return '`'+kids.replace(/`/g,"\\`")+'`';
      }
      case 'PRE': {
        // 代码块：先清掉行号/复制按钮再走子节点（行号 span 会直接变成文本，实测过
        // "1$content" 这样漏进来——必须先清再取内容）。
        node.querySelectorAll('[class*="line-number" i],[class*="lineno" i],button').forEach(el=>el.remove());
        // 语言标签：优先 data-language，其次 header 里第一个 span 的文本（排除按钮）
        let lang = '';
        const head = node.parentElement ? node.parentElement.querySelector('[class*="header" i],[class*="lang" i],[data-language]') : null;
        if(head){ lang = (head.getAttribute('data-language')||'').trim()
                  || ((head.querySelector('span')||{}).textContent||'').trim().split(/\s/)[0].toLowerCase(); }
        if(!lang){ const m = cls.match(/language-(\w+)/); if(m) lang = m[1]; }
        const code = [...node.childNodes].map(walk).join('');
        return '\n```'+lang+'\n'+code.replace(/\n+$/,'')+'\n```\n\n';
      }
      case 'UL': return [...node.children].map(li=>'- '+walk(li).trim()+'\n').join('')+'\n';
      case 'OL': return [...node.children].map((li,ix)=>(ix+1)+'. '+walk(li).trim()+'\n').join('')+'\n';
      case 'LI': return kids;
      case 'BLOCKQUOTE': return kids.trim().split('\n').map(l=>'> '+l).join('\n')+'\n\n';
      case 'TABLE': {
        const rows = [...node.querySelectorAll('tr')].map(tr=>
          [...tr.children].map(td=>walk(td).trim().replace(/\|/g,'\\|')).join(' | '));
        if(!rows.length) return '';
        const sep = rows[0] ? rows[0].split('|').map(()=>'---').join('|') : '';
        return '\n| '+rows[0]+' |\n|'+sep+'|\n'+rows.slice(1).map(r=>'| '+r+' |').join('\n')+'\n\n';
      }
      case 'A': {
        const href = node.getAttribute('href')||'';
        const text = kids.trim()||href;
        return /^https?:/i.test(href) ? '['+text+']('+href+')' : text;
      }
      case 'HR': return '\n---\n\n';
      case 'DIV': case 'SECTION': case 'ARTICLE': case 'SPAN': case 'MAIN':
      default: {
        // 代码块头部（语言名 + 复制按钮）是 UI 元素，不是回答内容
        if(/header/i.test(cls) && node.parentElement && /code-block/i.test(node.parentElement.className||'')) return '';
        // 块级容器：内容后跟换行；行内容器直接拼接
        const block = /^(DIV|SECTION|ARTICLE|P|MAIN|FIGURE)$/.test(tag);
        return kids + (block && kids.trim() ? '\n' : '');
      }
    }
  }
  return walk(root).replace(/\n{3,}/g,'\n\n').trim();
};"""

async def body_text():
    try:
        return (await page.locator("body").inner_text()).replace("\r\n", "\n")
    except Exception:
        return ""


async def body_text_raw():
    """取**未经渲染折叠**的正文（textContent），用来救回 JSON 参数里的空白。

    为什么需要两套：`inner_text` 是"渲染后"文本，HTML 会把**连续空格折叠成一个**、
    行首缩进也会被吃掉。Agent 回复是一段 JSON 动作，模型常把缩进写进参数
    （如 `new: "        u32.ShowWindow(...)"`,8 个空格）——经 inner_text 变成 1 个，
    于是 `old == new`,replace_text 空操作、文件没变、模型以为没生效、再改一次…
    真实时间线里 58 次 replace_text 有 **15 次 old==new**,整段任务空转几十步、
    换了 6 次 Copilot 会话（用户："一直在重复替换空格/制表的动作"）。
    `textContent` 保留原始空白，但块间缺换行——因此只作**补充来源**，不替换主流程。
    """
    try:
        return (await page.evaluate(
            "() => (document.querySelector('main') || document.body).textContent || ''"
        ) or "").replace("\r\n", "\n")
    except Exception:
        return ""


def extract_latest_answer(text):
    # Select only the final Copilot said block to avoid sidebar/history text.
    pos, marker_len = _last_turn(text or "", MARKERS["assistant_turn"])
    if pos < 0:
        return ""
    answer = text[pos + marker_len:]
    positions = [answer.find(m) for m in MARKERS["answer_end"] if answer.find(m) >= 0]
    if positions:
        answer = answer[:min(positions)]
    ignored = set(MARKERS["ignored_lines"])
    lines = []
    for line in answer.splitlines():
        line = line.rstrip()
        if line.strip() in ignored:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


async def _wait_dom_stable(timeout: float = 8.0, interval: float = 0.6) -> None:
    """等答案区域的 DOM 稳定。

    Copilot 的代码块是**异步渲染**的：`innerText` 先稳定（wait_agent_answer 返回），
    但代码编辑器（`data-virtualized-code-find-root`）可能还要几百毫秒才出现。
    不等它就直接取 markdown，代码块会缺失——实测过：存储的消息里代码块是纯文本，
    而等 DOM 渲染完再取的诊断文件里代码块齐全。
    """
    last = -1
    stable = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            n = await page.locator("body").evaluate("el => el.innerHTML.length")
        except Exception:
            break
        if n == last:
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
            last = n
        await asyncio.sleep(interval)


async def answer_markdown(plain: str = "") -> str:
    """把最近一条 Copilot 回答的 DOM 转成 Markdown（保留代码块/列表/标题/粗体）。

    之前用 `innerText` 提取 → 所有格式被拍平，聊天区拿到的"原文"就没有格式可渲染。
    **不猜 Copilot 的 class 名**（它经常改）：用纯文本答案的**首行 + 末行**同时定位，
    取"同时包含两者的最深容器"——那就是整个答案区域（只拿首行会只得到第一段）。
    失败时回退纯文本路径（Agent 协议不受影响——解析器自己剥 ``` 围栏）。
    """
    try:
        await page.evaluate(MD_JS)
        first, last = "", ""
        if plain:
            lines_ = [l.strip() for l in plain.splitlines()
                      if l.strip() and not l.strip().startswith(("CBP-", "[CBP"))]
            if lines_:
                first = lines_[0][:50]
                last = lines_[-1][-50:] if len(lines_) > 1 else first
        if not first:
            return ""
        js = """(args) => {
          const first = args[0], last = args[1] || args[0];
          const cands = document.querySelectorAll('article,section,div,main,[role="main"],[role="group"]');
          let best = null;
          for (const el of cands) {
            const t = el.textContent || '';
            if (!t.includes(first) || !t.includes(last)) continue;
            const r = el.getBoundingClientRect();
            if (r.height < 30 || r.width < 150) continue;
            if (!best || (best !== el && best.contains(el))) best = el;   // 最深的（被别的候选包含的）
          }
          if (!best) return '';
          return {md: window.__cbp_html2md(best), dom: best.outerHTML.slice(0, 300000)};
        }"""
        res = await page.evaluate(js, [first, last])
        if not res or not isinstance(res, dict):
            return ""
        md = (res.get("md") or "").strip()
        if res.get("dom"):
            try:
                (BASE / "data" / "answer_dom.html").write_text(res["dom"], encoding="utf-8")
            except Exception:
                pass
        return md
    except Exception:
        return ""


def _status_before_marker(text: str) -> str:
    """回答区上方那行"状态气泡"文本（Copilot 会把思考状态同时渲染在答案上方）。

    这是判断"占位文本不是答案"的**结构信号**：状态短语会轮换（Checking that now… /
    Lining things up… / Digging in… …），穷举不可靠，但"回答区上方最后一行"与
    "Copilot said: 后面那行"相同，就说明答案还没开始。
    """
    pos, _marker_len = _last_turn(str(text or ""), MARKERS["assistant_turn"])
    if pos < 0:
        return ""
    lines = [ln.strip() for ln in str(text)[:pos].splitlines() if ln.strip()]
    return lines[-1] if lines else ""


async def current_answer():
    # Agent 模式必须保持纯文本：回复是 JSON 动作，markdown 转换可能破坏它。
    # 聊天显示的 markdown 版本由 plain_chat_turn 单独取（见 answer_markdown）。
    raw = await body_text()
    text = extract_latest_answer(raw)
    # 思考占位必须在这里就剔掉：流式收集按"公共前缀增长"输出，占位文本一旦被当成
    # 答案发出去，真答案替换整段时前缀为空、后续内容永远发不出去。
    if text and len(text) <= 80 and text == _status_before_marker(raw):
        return ""
    return _strip_thinking(await _prefer_unfolded(text))


async def _prefer_unfolded(text: str) -> str:
    """若渲染文本把 JSON 参数里的连续空格折叠了，改用 textContent 版本。

    只做**保守替换**：两份里都找得到动作、且原文版本确实保留了更多连续空白，
    才采用原文版本（否则保持原行为，避免 textContent 缺换行带来的副作用）。
    """
    try:
        if not text or '"tool"' not in text:
            return text
        raw = extract_latest_answer(await body_text_raw())
        if not raw or '"tool"' not in raw:
            return text
        def _longest_space_run(s: str) -> int:
            import re as _re
            runs = _re.findall(r" {2,}", s)
            return max((len(x) for x in runs), default=0)
        if _longest_space_run(raw) > _longest_space_run(text):
            return raw
    except Exception:
        pass
    return text


async def generating():
    return bool(await visible(
        [f'button[aria-label*="{t}" i]' for t in SELECTOR_TEXT["stop_aria"]]
        + [f'button:has-text("{t}")' for t in SELECTOR_TEXT["stop"]]
    ))


async def count_user_markers():
    text = await body_text()
    return sum(text.count(m) for m in MARKERS["user_turn"])


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
    button = await visible(
        [f'button[aria-label="{t}"]' for t in SELECTOR_TEXT["send_aria"]]
        + [f'button[aria-label*="{t}" i]' for t in SELECTOR_TEXT["send_aria"]]
        + [f'button[title*="{t}" i]' for t in SELECTOR_TEXT["send_aria"]]
        + ['button[data-testid*="send" i]']
    )
    if button:
        try:
            await button.click(timeout=2000)
            return True
        except Exception:
            pass
    return False


async def send(prompt, make_new, model_id):
    if not await ensure_browser_ready():
        raise CopilotSendFailed(
            "Copilot 浏览器未就绪（未登录或启动失败）：可改用提供商模型，"
            "或在界面点『重试浏览器』后重试")
    # 若恰逢重启后的 SSO/ADFS 回跳，先等它就绪；**等不到就明确失败**，
    # 否则会一直挂在 phase=sending（实测挂了 2 分钟以上，任务像是卡死）。
    if not await wait_copilot_ready(90):
        raise CopilotSendFailed(
            "Copilot 未就绪（未登录或页面未加载完）：本轮无法发送。"
            "请在自动化浏览器窗口登录 Copilot，或改用提供商模型后重试。")
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
    raise CopilotSendFailed("问题停留在输入框，Enter、发送按钮和 Ctrl+Enter 均未触发发送")


async def completion_signal():
    text = await body_text()
    pos, marker_len = _last_turn(text, MARKERS["assistant_turn"])
    answer = text[pos + marker_len:] if pos >= 0 else ""
    stripped = answer.rstrip()
    has_sources = any(("\n" + s + "\n") in answer or stripped.endswith(s)
                      for s in MARKERS["sources"])
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








# 当前实际运行的浏览器模式（无头启动失败回退后与配置值可能不同）。
HEADLESS_CURRENT = bool(CFG.get("headless", False))


def _launch_kwargs(headless: bool) -> dict:
    kwargs = dict(
        user_data_dir=str(PROFILE),
        channel=CFG.get("browser_channel", "msedge"),
        headless=headless,
        viewport={"width": 1440, "height": 1000},
    )
    if headless:
        # 降低无头模式被页面自动化检测的概率。
        kwargs["args"] = ["--disable-blink-features=AutomationControlled"]
    return kwargs


async def _start_browser(headless: bool) -> bool:
    """(Re)launch the persistent Edge context with the given headless mode.

    headless is a launch-time parameter: toggling it always relaunches the
    context. EdgeProfile persists, so the Copilot login survives the switch.
    Caller must hold `lock`. Returns the mode actually in effect (headless
    launch failure falls back to a visible window).
    """
    global ctx, HEADLESS_CURRENT
    # 已有上下文且在用的窗口：**不要关重建**（多窗口下会把用户正看的标签页全关掉，
    # 表现就是"开任务时窗口闪退"）。只有显式切换 headless 模式才需要重建。
    if ctx is not None:
        try:
            busy = bool(WINDOWS.running()) or bool(getattr(WINDOWS, "stats", lambda: {})()
                                                    .get("busy"))
        except Exception:
            busy = False
        if (not busy) and bool(headless) == bool(HEADLESS_CURRENT):
            try:
                if ctx.pages:
                    set_shared_page(ctx.pages[0])
                    WINDOWS.attach_existing(_PAGE_RAW)   # 复用已有页面当 1 号窗口
            except Exception:
                pass
            # 复用分支也要确保页面**真的在 Copilot 上**：上一轮启动失败会留下
            # about:blank / 登录页；只返回不复位的话，点「重试浏览器」永远停在
            # needs_login（实测：ERR_TIMED_OUT 之后重试一直是 needs_login）。
            try:
                cur = _PAGE_RAW.url if _PAGE_RAW is not None else ""
            except Exception:
                cur = ""
            if _PAGE_RAW is None or not COPILOT_URL_MARK.search(cur or ""):
                try:
                    if _PAGE_RAW is None and ctx.pages:
                        set_shared_page(ctx.pages[0])
                    if _PAGE_RAW is not None:
                        await _PAGE_RAW.goto(URL, wait_until="domcontentloaded", timeout=120000)
                        _forget_selected_model()
                except Exception as exc:
                    log(f"复用窗口导航到 Copilot 失败（继续，由就绪检测判定）: {exc}")
            return HEADLESS_CURRENT
        if busy:
            log("有任务正在使用浏览器，跳过重建（沿用当前窗口）")
            return HEADLESS_CURRENT
        try:
            await ctx.close()
        except Exception:
            pass
        ctx = None
        set_shared_page(None)
    launch_kwargs = _launch_kwargs(headless)
    try:
        ctx = await pw.chromium.launch_persistent_context(**launch_kwargs)
    except Exception:
        if not headless:
            raise
        # 部分环境（旧版 Edge/安全策略）不支持无头持久化上下文，回退有窗口模式。
        log("无头模式启动失败，已回退为有窗口模式；请升级 Edge 或将 headless 改回 false")
        launch_kwargs["headless"] = False
        ctx = await pw.chromium.launch_persistent_context(**launch_kwargs)
    HEADLESS_CURRENT = launch_kwargs["headless"]
    log("浏览器模式：" + ("无头（不显示窗口）" if HEADLESS_CURRENT else "有窗口"))
    set_shared_page(ctx.pages[0] if ctx.pages else await ctx.new_page())
    # 共享页登记成 1 号窗口：windows=1 时任务复用它，不新开标签页
    try:
        WINDOWS.attach_existing(_PAGE_RAW)
    except Exception:
        pass
    await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    _forget_selected_model()  # 页面重载，模型选择状态未知
    return HEADLESS_CURRENT






















AGENT_CFG = CFG.get("agent", {})
AGENT_WORKSPACE = (BASE / AGENT_CFG.get("workspace", "workspace")).resolve()
AGENT_TOOLS = ToolRegistry(AGENT_WORKSPACE, AGENT_CFG)
# 全局作用域：服务安装目录的 AGENT.md / skills 对所有工作区生效
AGENT_TOOLS.set_global_root(BASE)

# ── 扩展工具（解耦模块 cbp/tools_extra.py）：搜索/网页/命令/任务清单/图片/提问 ──
# 视觉与提问需要桥接层注入回调；没有 Copilot 网页时会优雅降级。
EXTRA_ANSWERS: dict[str, "asyncio.Future[str]"] = {}


async def _vision_via_copilot(path: str, question: str) -> dict:
    """read_image 的视觉后端：把图片上传给 Copilot 并返回它的描述。"""
    target = AGENT_TOOLS._path(path)
    if not target.is_file():
        raise ToolError(f"文件不存在: {path}")
    await attach_file_to_copilot(target)
    model = DEFAULT_MODEL
    if current_session():
        st = AGENT_SESSIONS.get(current_session())
        if st and st.get("model"):
            model = st["model"]
    answer = await agent_chat_once(question, model, False)
    return {"path": path, "answer": answer}


async def _ask_user_via_webui(question: str, options: list[str]) -> dict:
    """ask_user_question 的提问后端：注册 Future 等 /answer 端点回填。"""
    # 用任务级会话 id：并行任务各自提问时不能共用同一个 Future（会串到别人的答案上）
    sid = current_session() or ""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()
    EXTRA_ANSWERS[sid] = fut
    try:
        runtime_update(phase="waiting_question", detail=question,
                       question={"text": question, "options": options})
        timeout = float(AGENT_CFG.get("question_timeout_seconds", 600))
        try:
            answer = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return {"answer": "", "unanswered": True,
                    "detail": f"用户 {timeout:.0f} 秒内未回答；请按最合理假设继续并说明"}
        return {"answer": answer}
    finally:
        EXTRA_ANSWERS.pop(sid, None)
        # 关键：把 runtime 里的 question 清掉。以前这里只 pop 了 Future，
        # `question` 字段一直留在 RUNTIME 里 → 前端每秒轮询都会把提问条重新画出来,
        # 用户回答完它还在；再点一次只能收到"没有等待回答的问题"（实测）。
        runtime_update(sid, question=None)


TOOLS_EXTRA = register_extra_tools(
    AGENT_TOOLS, AGENT_CFG,
    vision_fn=_vision_via_copilot,
    ask_fn=_ask_user_via_webui,
    session_getter=current_session,
    # 引擎定义在文件后部：用 lambda 延迟解析，避免前向引用
    subagent_fn=lambda *a, **k: run_subagent(*a, **k),
    ralph_fn=lambda *a, **k: run_ralph(*a, **k),
    workflow_fn=lambda *a, **k: run_workflow(*a, **k),
    subagent_send_fn=lambda *a, **k: continue_subagent(*a, **k),
    subagent_fork_fn=lambda *a, **k: fork_subagent(*a, **k),
    subagent_list_fn=lambda *a, **k: list_subagents(*a, **k))


def declared_deliverables(sid: str) -> list[dict]:
    """本次任务通过 `present` 登记过的交付物。

    注意是**内存态**（重启后为空）：所以任务完成卡片以"文件改动"为主，
    交付物只是额外标注，不会因为这里为空就什么都不显示。
    """
    try:
        return TOOLS_EXTRA.deliverables_for(sid)
    except Exception:
        return []

# 会话状态钩子：扩展工具用它读写当前会话的 goal/todos（Agent 循环据此续轮）
AGENT_TOOLS.state_getter = lambda: (AGENT_SESSIONS.get(current_session()) or {})
AGENT_TOOLS.state_saver = lambda **kw: AGENT_SESSIONS.patch(current_session(), **kw)
# 任务级读值器：工具里的"当前会话 / 是否允许网页工具"按任务取值（并行的前提）。
# 并行子代理各自的任务上下文互不干扰；单任务时等价于原来的全局行为。
AGENT_TOOLS.session_id_fn = current_session
AGENT_TOOLS.web_copilot_allowed_fn = web_copilot_allowed

# ── 插件系统（cbp/plugins.py，与 DSH 插件语义兼容）────────────────────
# 贡献点：Agent 工具（AGENT_TOOLS）、模型提供商（PROVIDERS）、HTTP 路由、会话事件。
# 启停记录在 data/plugins.json；load_all() 在服务启动时由 api.py 的 lifespan 调用。
PLUGINS = PluginHost(BASE, registry=AGENT_TOOLS, providers=PROVIDERS,
                     provider_factory=lambda name, cfg: providers_mod.ApiProvider(name, cfg)
                     if hasattr(providers_mod, "ApiProvider") else cfg,
                     log_fn=lambda m: print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True))
# 延迟解析：run_plugin_task 定义在文件后部
PLUGINS.run_task_fn = lambda task, model="", workspace="", prefix="cron": run_plugin_task(
    task, model, workspace, prefix)
# 延迟解析：save_workspace_file 定义在文件后部
PLUGINS.save_file_fn = lambda rel, data: save_workspace_file(rel, data)
# 新任务缺省工作区：POST /v1/agent/workspace 会更新它
DEFAULT_WORKSPACE = str(AGENT_WORKSPACE)
# 与 AgentRunner.__init__ 的默认值保持一致，供 WebUI 显示真实 Token 预算。
TOKEN_BUDGET = int(AGENT_CFG.get("context_token_budget", 24000))
SESSION_DB = Path(AGENT_CFG.get("session_store", "data/agent.db"))
if not SESSION_DB.is_absolute(): SESSION_DB = BASE / SESSION_DB
AGENT_SESSIONS = SessionStore(SESSION_DB)
# 注意：**不要**在这里调用 recover_interrupted()。
# 模块级副作用会污染任何 import cbp.bridge 的进程（单元测试、检查脚本、CLI）——
# 它们会把正在跑的会话全部标成 paused（"服务中断，可从检查点恢复"），
# 实测用户任务被反复打断就是这个原因。恢复只在服务启动时做（见 cbp/api.py lifespan）。

# ── 工作区快照：写操作前记录原像，支持"回滚整个任务的改动" ──────────
SNAPSHOT_DIR = BASE / "data" / "snapshots"
SNAPSHOTS = SnapshotStore(SNAPSHOT_DIR, int(AGENT_CFG.get("snapshot_retention", 50)))

# ── 长期记忆：跨会话累积的事实/偏好/结论（独立库，互不影响迁移与备份）──
from .memory import MemoryStore  # noqa: E402

MEMORY = MemoryStore(BASE / "data" / "memory.db")

# ── 本地 span：回合/工具调用写成 JSONL（字段对齐 OTel GenAI 语义约定）──
from .spans import SpanRecorder, set_recorder  # noqa: E402

# 单测/脚本导入 bridge 时不要写生产遥测：unittest 进程里自动关闭
import sys as _sys  # noqa: E402

_spans_on = bool(AGENT_CFG.get("spans_enabled", True)) and ("unittest" not in _sys.modules)
SPANS = SpanRecorder(BASE / "data" / "spans.jsonl",
                     enabled=_spans_on,
                     max_lines=int(AGENT_CFG.get("spans_max_lines", 20000)))
set_recorder(SPANS)
AGENT_TOOLS.memory = MEMORY


def turn_cache_decision(hit: dict | None, steps_now: int) -> tuple[str, str]:
    """回合缓存决策（纯函数，便于单测）。

    返回 (动作, 复用答案)：
      ("reuse", answer) —— 该回合已成功产出且此后无进展：直接复用，不重发；
      ("probe", "")     —— 该回合已派发但**结果未知**（中断在途）：不重发副作用，
                            让模型先做只读核对（DSH 的 "Its outcome is unknown." 思路）；
      ("send", "")      —— 没有记录或状态允许重发。
    """
    if not hit:
        return "send", ""
    status = str(hit.get("status") or "done")
    if status == "inflight":
        return "probe", ""
    if int(hit.get("steps_after") or 0) == int(steps_now):
        return "reuse", str(hit.get("answer") or "")
    return "send", ""


def memory_scopes(scope: str = "all") -> list[str]:
    """记忆作用域解析：all=全局+当前工作区；global/project 各自单取。"""
    key = str(scope or "all").strip().lower()
    if key.startswith("glob"):
        return ["global"]
    ws = memory_scope_for(Path(task_tools().root))
    if key.startswith("proj") or key.startswith("work"):
        return [ws]
    return ["global", ws]


def memory_scope_for(root: Path) -> str:
    """工作区作用域：同一目录（含不同大小写/相对写法）映射到同一作用域名。"""
    import hashlib
    key = str(Path(root).resolve()).casefold().encode("utf-8")
    return "ws:" + hashlib.sha1(key).hexdigest()[:12]


async def rollback_session_changes(sid: str, version: int | None = None) -> dict:
    """把某个 Session 期间改过的工作区文件恢复到任务开始前（或指定版本）。

    快照在写操作前捕获（同一路径保留最早状态 + 每个写操作追加一个版本），
    因此不带 version 是"任务级回滚"；带 version 则撤销该版本之后的写操作。
    会话处于运行/等待状态时拒绝，避免和正在进行的写操作打架。
    """
    state = AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    if state.get("state") in {"running", "waiting", "ready"}:
        raise HTTPException(409, "Session 正在运行或待命，请先暂停/取消后再回滚")
    root = Path(state.get("workspace") or AGENT_TOOLS.root)
    if not root.is_dir():
        raise HTTPException(400, f"工作区不存在: {root}")
    if version is not None:
        result = SNAPSHOTS.restore_rev(sid, root, int(version))
        why = f"回滚到版本 {version}：还原 {result['restored']} 个、删除 {result['deleted']} 个、失败 {result['failed']} 个"
    else:
        result = SNAPSHOTS.restore(sid, root)
        why = f"回滚任务改动：还原 {result['restored']} 个、删除 {result['deleted']} 个、失败 {result['failed']} 个"
    step = {"step": int(state.get("total_steps", 0)), "status": "rollback",
            "reason": why,
            "result": {"restored": result["restored"], "deleted": result["deleted"],
                       "failed": result["failed"], "root": result.get("root"),
                       "rev": result.get("rev"), "results": result["results"][:50]}}
    AGENT_SESSIONS.add_step(sid, step["step"], int(state.get("conversation_index", 0)), step)
    AGENT_SESSIONS.patch(sid, last_error=None if not result["failed"] else "回滚存在失败项")
    return result


# ── 自升级（修改服务自身代码）─────────────────────────────────────
SELF_UPGRADE_MARKER = BASE / "data" / ".self_upgrade_pending"


def self_scope(sid: str) -> str:
    """自升级改动的快照作用域：与工作区改动分开，可独立回滚。"""
    return f"_self_{sid}"


def mark_self_upgrade_applied() -> list[str]:
    """服务启动时调用：上一轮自升级的代码已被加载，把待重启标记归档。"""
    if not SELF_UPGRADE_MARKER.is_file():
        return []
    try:
        lines = [x for x in SELF_UPGRADE_MARKER.read_text(encoding="utf-8").splitlines() if x.strip()]
    except Exception:
        lines = []
    try:
        SELF_UPGRADE_MARKER.replace(SELF_UPGRADE_MARKER.with_suffix(".applied"))
    except Exception:
        pass
    return lines


def self_snapshot_hook(sid: str):
    """快照钩子：按目标路径自动分派到工作区作用域或自升级作用域。"""
    def hook(tool: str, rel: str, target: Path) -> None:
        t = Path(target)
        # 用**当前任务**的工作区根：并行时会话工作区不同，全局 root 会把别的会话
        # 的文件写进错误的作用域（回滚时也找不到）。
        task_root = Path(task_tools().root)
        try:
            t.relative_to(task_root)
            SNAPSHOTS.capture(sid, rel, t, tool, root=task_root)      # 工作区内
        except ValueError:
            SNAPSHOTS.capture(self_scope(sid), rel, t, tool, root=BASE)      # 服务代码
    return hook


async def rollback_self_upgrade(sid: str | None = None, use_last: bool = False) -> dict:
    """回滚服务自身代码的自升级改动（不依赖 WebUI 的离线通道见 scripts/rollback.py）。"""
    scope = self_scope(sid) if sid else ""
    if use_last or not scope:
        # 取最近修改的自升级快照作用域（只认有 manifest 的，且容忍被并发 prune）
        cands = SNAPSHOTS.scopes("_self_")
        if not cands:
            return {"restored": 0, "deleted": 0, "failed": 0, "results": [],
                    "message": "没有自升级快照记录"}

        def _mtime(name: str) -> float:
            try:
                return SNAPSHOTS.manifest_path(name).stat().st_mtime
            except OSError:
                return 0.0

        scope = max(cands, key=_mtime)
    result = SNAPSHOTS.restore(scope, BASE)
    if sid and AGENT_SESSIONS.get(sid):
        state = AGENT_SESSIONS.get(sid) or {}
        step = {"step": int(state.get("total_steps", 0)), "status": "self_rollback",
                "reason": f"回滚服务自升级：还原 {result['restored']} 个、删除 {result['deleted']} 个"
                          f"、失败 {result['failed']} 个",
                "result": {"scope": scope, "restored": result["restored"],
                           "deleted": result["deleted"], "failed": result["failed"],
                           "results": result["results"][:50]}}
        AGENT_SESSIONS.add_step(sid, step["step"], int(state.get("conversation_index", 0)), step)
    if result.get("restored") or result.get("deleted"):
        # 只在全部成功时清标记：否则部分文件仍是坏代码，清掉标记会让重启后
        # 误报"已加载上一轮自升级改动"，掩盖未回滚的失败项。
        if not result.get("failed"):
            try:
                SELF_UPGRADE_MARKER.unlink(missing_ok=True)
            except Exception:
                pass
    return result


def self_upgrade_status() -> dict:
    pending = []
    if SELF_UPGRADE_MARKER.is_file():
        try:
            pending = [x for x in SELF_UPGRADE_MARKER.read_text(encoding="utf-8").splitlines() if x.strip()]
        except Exception:
            pending = []
    scopes = SNAPSHOTS.scopes("_self_")
    return {"enabled": bool(getattr(AGENT_TOOLS, "allow_self_upgrade", False)),
            "base": str(BASE), "restart_pending": bool(pending),
            "pending_entries": pending[-20:], "snapshot_scopes": scopes,
            "whitelist": list(getattr(AGENT_TOOLS, "self_whitelist", ()) or ())}


# 注意：归档"待重启"标记必须由服务真正启动时执行（api.lifespan 调用
# mark_self_upgrade_applied()），不能在模块导入时执行——测试、脚本、
# 任何 import cbp.bridge 的进程都会因此误清掉"服务需重启"的标记。


# ── Outlook 网页版（OWA）自动化后端 ─────────────────────────────
# New Outlook 无 COM 接口、EWS 正在全球禁用、Graph 需要管理员授权。
# OWA 自动化复用本桥接的浏览器与 SSO 登录态，是当前唯一可行路径。














async def _copilot_attach(path: str, question: str = "请分析这个文件的内容并总结要点") -> dict:
    """Agent 工具 copilot_attach：上传工作区文件给 Copilot 网页版分析。

    适合图片/扫描件/复杂版式文档等本地工具无法解析的文件。回答作为
    工具结果返回，进入下一步的工具结果上下文。
    """
    target = AGENT_TOOLS._path(path)  # 复用工作区路径沙箱
    if not target.is_file():
        raise ToolError(f"文件不存在: {path}")
    if target.stat().st_size > 50 * 1024 * 1024:
        raise ToolError("文件超过 50MB，Copilot 网页上传可能失败")
    # 跟随当前会话的模型而不是默认模型：否则每次上传都把会话切成 Auto，
    # 下一回合又切回去，形成模型乒乓（每条消息都开一次菜单）。
    model = DEFAULT_MODEL
    if current_session():
        st = AGENT_SESSIONS.get(current_session())
        if st and st.get("model"):
            model = st["model"]
    await attach_file_to_copilot(target)
    answer = await agent_chat_once(question, model, False)
    return {"path": path, "question": question, "answer": answer}


AGENT_TOOLS.register(
    "copilot_attach",
    _copilot_attach,
    "把文件上传到 Copilot 网页版分析并返回回答（用于图片/复杂文档等本地工具无法解析的文件）",
    {"path": "相对路径", "question": "给 Copilot 的分析指令，可选"},
)


# ── copilot_file：让 Copilot 直接生成可下载文件，桥接下载进工作区 ──────

DOWNLOAD_HINT_RE = re.compile(r"download|下载|保存文件|save file", re.I)


async def _answer_area_html() -> str:
    """取最近一条回答所在区域的外层 HTML，用于提取下载入口与失败诊断。"""
    try:
        nodes = page.locator('[data-content="ai-message"], [class*="ai-message" i], '
                             '[class*="assistant" i], article')
        count = await nodes.count()
        if count:
            return await nodes.nth(count - 1).evaluate("el => el.outerHTML")
    except Exception:
        pass
    try:
        return await page.locator("body").evaluate("el => el.outerHTML")
    except Exception:
        return ""


async def _download_candidates():
    """当前页面里可能的下载入口，新的排在前面。

    实测形态：a[download]、blob:/data: 链接、文件卡片里的链接、带
    "下载/Download" 文案或 aria-label 的按钮。
    """
    candidates = []
    selectors = [
        'a[download]',
        'a[href^="blob:"]',
        'a[href^="data:"]',
        '[role="link"][href^="blob:"]',
        '[class*="file" i] a[href]',
        'button[aria-label*="Download" i]',
        'button[aria-label*="下载"]',
        'a[aria-label*="Download" i]',
        'a[aria-label*="下载"]',
        '[role="button"][aria-label*="Download" i]',
        '[role="button"][aria-label*="下载"]',
    ]
    for sel in selectors:
        loc = page.locator(sel)
        try:
            for i in range(await loc.count()):
                el = loc.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    href = await el.get_attribute("href") or ""
                    # 排除误报：会话/引用链接（实测 [class*=file i] a[href] 会匹配到
                    # 引用角标，把 /chat/conversation/... 当成下载入口；若被选中会把
                    # 对话页 HTML 写进目标文件，比报错更糟）。
                    if "/chat/conversation" in href or href in ("#", "/"):
                        continue
                    is_dl_href = href.startswith(("blob:", "data:"))
                    dl_attr = await el.get_attribute("download")
                    if not is_dl_href and not dl_attr and sel.startswith('[class*="file" i]'):
                        continue          # 文件卡片里的普通链接：没有下载语义，跳过
                    name = ((dl_attr or "")
                            + " " + ((await el.inner_text()) or "")[:60]
                            + " " + ((await el.get_attribute("aria-label")) or ""))
                    candidates.append({"el": el, "href": href, "text": name.strip()})
                except Exception:
                    continue
        except Exception:
            continue
    return candidates


def _resolve_href(href: str) -> str:
    """把下载候选的 href 规范成可 fetch 的绝对 URL；不可用的返回空串。

    实测故障：`[class*="file" i] a[href]` 会匹配到 `<a href="#">` / 相对路径，
    直接 `ctx.request.get()` 会抛 "Invalid URL"（错误信息完全看不出是这个原因）。
    """
    href = (href or "").strip()
    if not href or href in ("#", "/"):
        return ""
    if href.startswith(("blob:", "data:", "http://", "https://")):
        return href
    # 相对 URL：用当前页面地址补全
    try:
        from urllib.parse import urljoin
        base = page.url or ""
        if base.startswith(("http://", "https://")):
            joined = urljoin(base, href)
            return joined if joined.startswith(("http://", "https://")) else ""
    except Exception:
        pass
    return ""


def pathlib_basename(text: str) -> str:
    """从文案里取出文件名（去掉路径分隔与多余字符），用于诊断落盘。"""
    import re as _re
    m = _re.search(r"([\w\-\u4e00-\u9fff]+\.[A-Za-z0-9]{1,8})", str(text or ""))
    return m.group(1) if m else ""


async def debug_download_now(stem_hint: str = "") -> dict:
    """诊断：把当前页面第一个 blob:/data: 下载候选立刻落盘到 data/debug-download/。

    用于验证"页面里明明有下载入口、工具却拿不到"的问题；不经过 Agent 回合。
    """
    out = {"url": getattr(page, "url", "") if page else "", "saved": None, "candidates": []}
    cands = await _download_candidates()
    out["candidates"] = [{"href": c.get("href") or "", "text": (c.get("text") or "")[:100]}
                         for c in cands]
    dl = [c for c in cands if (c.get("href") or "").startswith(("blob:", "data:"))]
    pick = None
    for c in dl:
        if stem_hint and stem_hint.lower() in (c.get("text") or "").lower():
            pick = c
            break
    pick = pick or (dl[0] if dl else None)
    if pick is None:
        out["error"] = "页面里没有 blob:/data: 下载候选"
        return out
    target = BASE / "data" / "debug-download" / (pathlib_basename(pick.get("text")) or "download.bin")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = await _save_from_href(_resolve_href(pick["href"]), target)
        out["saved"] = {"path": str(target), "bytes": size,
                        "head": target.read_text(encoding="utf-8", errors="replace")[:300]}
    except Exception as exc:
        out["error"] = f"下载失败: {exc}"
    return out


async def debug_page_snapshot() -> dict:
    """诊断：当前自动化页面截图（base64 PNG）+ 识别到的下载入口。

    只给排障用（用户手动在自动化窗口操作时，从外部看页面现场）：
    截图看 Copilot 回答里有没有"下载/保存"入口，candidates 看提取到的元素。
    """
    out = {"url": getattr(page, "url", "") if page else "", "candidates": [], "png": ""}
    try:
        if page:
            raw = await page.screenshot(full_page=False)
            out["png"] = base64.b64encode(raw).decode()
    except Exception as exc:
        out["error"] = f"screenshot: {exc}"
    try:
        for c in await _download_candidates():
            href = c.get("href") or ""
            out["candidates"].append({"href": href, "resolved": _resolve_href(href),
                                      "text": (c.get("text") or "")[:120]})
    except Exception as exc:
        out["error_candidates"] = str(exc)
    return out


async def _save_from_href(href: str, target: Path) -> int:
    """把 blob:/data:/http(s) 内容写入目标文件，返回字节数。

    blob: 走页内 fetch + FileReader（页面上下文，天然带登录态）；
    http(s) 走浏览器上下文的 request（复用 Cookie）。
    """
    if href.startswith("blob:") or href.startswith("data:"):
        b64 = await page.evaluate(
            """async (url) => {
                const resp = await fetch(url);
                const buf = await resp.arrayBuffer();
                let bin = '';
                const bytes = new Uint8Array(buf);
                const CH = 0x8000;
                for (let i = 0; i < bytes.length; i += CH) {
                    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
                }
                return btoa(bin);
            }""", href)
        data = base64.b64decode(b64)
    else:
        resp = await ctx.request.get(href, timeout=120000)
        if not resp.ok:
            raise ToolError(f"下载失败 HTTP {resp.status}: {href[:120]}")
        data = await resp.body()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return len(data)


async def _copilot_file(path: str, instruction: str) -> dict:
    """Agent 工具 copilot_file：让 Copilot 生成文件并下载到工作区。

    与 write_text_b64 分块写的根本区别：文件内容不经过聊天消息，因此不受
    Copilot 单条输出上限约束——一个大文件一个回合就能拿到，不再截断。
    代价是内容由 Copilot 侧生成，本地无法逐块校验；写完后仍按普通写入
    走验证闭环（.py/.json/.html 需要 validate_python + run_unittest）。
    """
    if not AGENT_CFG.get("allow_copilot_file", True):
        raise ToolError("copilot_file 已在 config.json 中禁用（allow_copilot_file=false）")
    instruction = str(instruction or "").strip()
    if not instruction:
        raise ToolError("instruction 不能为空")
    target = AGENT_TOOLS._path(path)          # 复用工作区路径沙箱
    filename = target.name
    model = DEFAULT_MODEL
    if current_session():
        st = AGENT_SESSIONS.get(current_session())
        if st and st.get("model"):
            model = st["model"]
    # 生成可下载文件需要**非 Auto** 模型：实测 Auto 只在聊天里回答、不提供下载入口。
    # 会话模型是 auto 时，改用 agent.copilot_file_model 指定的具体模型（没配则不换，
    # 失败时下文会明确提示原因）。
    _file_model = str(AGENT_CFG.get("copilot_file_model") or "").strip()
    _auto_models = ("copilot-auto", "copilot-chat-basic-web")
    if _file_model and model in _auto_models:
        model = _file_model
    prompt = (f"请把结果生成为一个可下载的文件，文件名：{filename}\n"
              f"要求：只生成文件并提供下载入口，不要在回答里粘贴文件正文或大段内容。\n"
              f"内容要求：{instruction}")
    await agent_chat_once(prompt, model, False)

    # 找下载入口：优先文件名匹配，其次取最新的一个
    candidates = await _download_candidates()
    if not candidates:
        dump = await _answer_area_html()
        try:
            (BASE / "last_answer.html").write_text(dump[:400000], encoding="utf-8")
        except Exception:
            pass
        _hint = ""
        if model in _auto_models:
            _hint = ("当前模型是 Auto：它通常只在聊天里回答、不生成下载入口。"
                     "请选择具体模型（如 GPT-5.6 Sol Think deeper），"
                     "或配置 agent.copilot_file_model。")
        raise ToolError("Copilot 回答里没有找到下载入口（已存 last_answer.html 供排查）；"
                        + _hint
                        + "可改用 write_text_b64/append_text_b64 分块写入")

    chosen = None
    stem = Path(filename).stem.lower()
    # 优先级：① href 是 blob:/data: 且文案含文件名 → ② blob:/data: →
    #         ③ 文案含文件名 → ④ 第一个候选
    _dl = [c for c in candidates if (c.get("href") or "").startswith(("blob:", "data:"))]
    for cand in _dl:
        if stem and stem in (cand["text"] + " " + cand["href"]).lower():
            chosen = cand
            break
    chosen = chosen or (_dl[0] if _dl else None)
    if chosen is None:
        for cand in candidates:
            if stem and stem in (cand["text"] + " " + cand["href"]).lower():
                chosen = cand
                break
    chosen = chosen or candidates[0]

    # 诊断：把本次识别到的下载入口记下来（href 是否可用、文案是否匹配），
    # 排查"Invalid URL / 下载到 0 字节"时对照真实页面结构。
    try:
        (BASE / "data" / "copilot_file_candidates.json").write_text(
            json.dumps([{"href": c.get("href") or "", "resolved": _resolve_href(c.get("href") or ""),
                         "text": c.get("text") or ""} for c in candidates],
                       ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    AGENT_TOOLS._snap("copilot_file", path, target)   # 覆盖前存原像，可回滚
    before = AGENT_TOOLS._hash(target)
    size = 0
    _href = _resolve_href(chosen.get("href") or "")
    if _href:
        size = await _save_from_href(_href, target)
    else:
        # 按钮型入口，或 href 无效（#/相对路径等）：点它并把浏览器下载事件接住
        try:
            async with page.expect_download(timeout=120000) as dl_info:
                await chosen["el"].click(timeout=8000)
            download = await dl_info.value
            await download.save_as(str(target))
            size = target.stat().st_size
        except Exception as exc:
            raise ToolError(f"点击下载入口失败: {exc}") from exc
    if size <= 0:
        raise ToolError("下载到 0 字节，可能不是有效文件；请让 Copilot 重新生成或改用分块写入")
    return {"path": path, "bytes": size, "filename": filename,
            "source": "copilot-download",
            "file_event": {"tool": "copilot_file", "path": path, "before_hash": before,
                           "after_hash": AGENT_TOOLS._hash(target), "result": "ok"}}


if AGENT_CFG.get("allow_copilot_file", True):
    AGENT_TOOLS.register(
        "copilot_file",
        _copilot_file,
        "让 Copilot 直接生成文件并下载到工作区（内容不走聊天消息，不受输出长度上限约束，"
        "一个大文件一个回合即可拿到；适合代码文件、Office 文档等；写完后仍需验证）",
        {"path": "工作区相对路径（决定文件名与落盘位置）", "instruction": "要生成的内容要求"},
    )
async def periodic_session_cleanup():
    """每小时执行一次会话维护，使 session_timeout_seconds /
    completed_session_retention_days / max_sessions 三项配置生效。"""
    await asyncio.sleep(60)  # 启动后先稳定一分钟
    while True:
        try:
            result = AGENT_SESSIONS.run_maintenance(
                AGENT_CFG.get("session_timeout_seconds"),
                AGENT_CFG.get("completed_session_retention_days"),
                AGENT_CFG.get("max_sessions"))
            if any(result.values()):
                log(f"会话维护完成: {result}")
        except Exception as exc:
            log(f"会话维护失败: {exc}")
        await asyncio.sleep(3600)


# 由 api 层注册：按需启动浏览器（lazy 模式下首个 Copilot 请求会触发）。
# 用钩子而不是直接 import，避免 api ↔ bridge 循环导入。
ENSURE_BROWSER = None


def page_url_safe() -> str:
    """安全地拿当前页面 URL。

    浏览器是**按需启动**的：恢复任务/续跑时 `page` 可能还是 None，
    直接读 `page.url` 会抛 `'NoneType' object has no attribute 'url'`
    （实测：resume 一启动就 failed，任务又被卡住）。
    """
    try:
        return page.url or ""
    except Exception:
        return ""


async def _new_copilot_page(index: int):
    """在**同一个持久化上下文**里开一个新标签页（登录态共享），并导航到 Copilot。

    多窗口并行的关键：不需要第二个浏览器进程，也不需要重新登录；
    每个标签页有自己的一套 Copilot 会话/模型选择。
    """
    global ctx
    # 先确保上下文存在（窗口 0 的页由 _start_browser 创建）
    if ctx is None:
        await ensure_browser_ready()
    if ctx is None:
        raise RuntimeError("浏览器上下文不可用，无法开新窗口")
    new_page = await ctx.new_page()
    try:
        await new_page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    except Exception as exc:
        log(f"新窗口 #{index} 打开 Copilot 失败: {exc}")
    # 新标签页要**等它真的可用**再交给任务：SSO/ADFS 回跳可能要几十秒，
    # 早交出去会导致"消息发出去没反应 → 180 秒超时"。
    tok = _PAGE_CTX.set(new_page)
    try:
        ready = await wait_copilot_ready(timeout=90)
        if ready:
            # 首页可能残留上次的对话状态：开一个新对话再做任务
            try:
                await new_chat()
            except Exception as exc:
                log(f"新窗口 #{index} 新建对话失败（继续用当前页）: {exc}")
        else:
            log(f"新窗口 #{index} 未就绪（可能仍在登录/跳转）：关闭该窗口，任务改用其它窗口")
            try:
                await new_page.close()
            except Exception:
                pass
            raise RuntimeError(f"新窗口 #{index} 未就绪（需要登录 Copilot）")
    finally:
        _PAGE_CTX.reset(tok)
    log(f"已开第 {index} 个自动化窗口（多窗口并行，就绪={ready}）")
    return new_page


def _max_windows_cfg() -> int:
    try:
        return max(1, min(int(AGENT_CFG.get("copilot_windows", 1) or 1), 8))
    except Exception:
        return 1


from .window_pool import WindowBusy, WindowPool  # noqa: E402  （放在此处避免循环导入顺序问题）

WINDOWS = WindowPool(_new_copilot_page, max_windows=_max_windows_cfg())


# 提供商模型的"逻辑槽"：不需要浏览器页，但同样要独立工具上下文，可以真并行。
# 上限跟 `agent.subagent_parallel` 对齐（否则扇出 8 个只有 4 个能跑，其余白白等 30s 超时）；
# 至少 4，保证普通提供商对话不会被子代理挤没。
SLOTS = WindowPool(lambda index: _no_page(), max_windows=max(4, subagent_parallel()))


async def _no_page():
    return None


@contextlib.asynccontextmanager
async def _window(sid: str, wait_timeout: float = 30.0, need_page: bool = True):
    """取一个自动化窗口并把**任务上下文**绑好：页面 + 工具注册表 + 窗口。

    - 网页 Copilot 通道（need_page=True）：从 `WINDOWS` 取一个真实标签页；
    - API 提供商通道（need_page=False）：从 `SLOTS` 取一个无页面的槽
      （提供商本来就不需要浏览器，但同样要工具上下文隔离）。

    任务结束时无论成功/异常/取消都会归还；工具注册表是**克隆**，
    所以两个并行任务的 root / sandbox / plan / memory 互不干扰。
    """
    pool = WINDOWS if need_page else SLOTS
    if need_page:
        # 先确保浏览器上下文存在（懒启动）；否则池子创建页面会失败
        try:
            await ensure_browser_ready()
        except Exception as exc:
            log(f"启动浏览器失败（窗口池）: {exc}")
    try:
        win = await pool.acquire(sid, wait_timeout=wait_timeout)
    except WindowBusy as exc:
        raise CopilotBusy(str(exc)) from exc
    tok_page = _PAGE_CTX.set(win.page) if win.page is not None else None
    tok_win = _WIN_CTX.set(win)
    tok_sess = _SESSION_CTX.set(sid or None)
    tools = AGENT_TOOLS.clone()
    tok_tools = _TOOLS_CTX.set(tools)
    try:
        yield win, tools
    finally:
        try:
            _TOOLS_CTX.reset(tok_tools)
            _SESSION_CTX.reset(tok_sess)
            _WIN_CTX.reset(tok_win)
            if tok_page is not None:
                _PAGE_CTX.reset(tok_page)
        finally:
            pool.release(win)


def session_running(sid: str) -> bool:
    """该会话是否正有 runner 在跑（网页窗口 与 提供商逻辑槽 都算）。

    以前判定用全局 `ACTIVE_SESSION_ID == sid` 或只看 `WINDOWS`：
    - 前者只是"最近活跃"，并行时会误判（A 在跑、B 刚发过，A 就被当成没在跑）；
    - 后者漏掉提供商会话（它们占的是 `SLOTS`，不占浏览器窗口）。
    漏判的后果很具体：提供商任务的会话里插入指令会被当成"空闲"排进队列、
    恢复会被重复拉起第二个 runner、删除会放行后让在跑的 runner 落库失败。
    """
    if not sid:
        return False
    return WINDOWS.is_running(sid) or SLOTS.is_running(sid)


def any_window_busy() -> bool:
    """是否有任务正占用**浏览器窗口**（提供商的逻辑槽不算：它们不需要页面）。"""
    return bool(WINDOWS.running())


def any_task_busy() -> bool:
    """是否有任何任务在跑（网页窗口 + 提供商逻辑槽）。

    用于"改全局状态"的管理操作：切工作区、回滚服务代码等，有任何任务在跑都应拒绝。
    """
    return bool(WINDOWS.running()) or bool(SLOTS.running())


async def ensure_browser_ready() -> bool:
    """确保浏览器可用（未注册钩子时视为已就绪，保持测试与无浏览器场景可用）。"""
    fn = ENSURE_BROWSER
    if fn is None:
        return True
    try:
        return bool(await fn())
    except Exception as exc:
        log(f"浏览器按需启动失败: {exc}")
        return False


_RESUMING: set = set()
# 恢复时给模型的一句话补充（目前用于"用户已授权"）：一次性消费，用完即清。
_RESUME_HINTS: dict = {}


def set_resume_hint(sid: str, hint: str) -> None:
    """登记一次性的恢复提示（例如"用户已授权 delete_path（仅这一次）"）。"""
    if sid and str(hint or "").strip():
        _RESUME_HINTS[sid] = str(hint).strip()


def discard_resume_hint(sid: str) -> None:
    _RESUME_HINTS.pop(sid, None)
# 已排队但还没拿到锁的 resume 任务：同一会话来新消息时应当被取代，
# 否则 resume 用的旧任务文本会和用户新消息各跑一遍（顺序双跑、重复动作）。
_PENDING_RESUME: dict = {}


def _bg_task_done(sid: str):
    def cb(t):
        _RESUMING.discard(sid)
        if _PENDING_RESUME.get(sid) is t:
            _PENDING_RESUME.pop(sid, None)
        if not t.cancelled() and t.exception():
            log(f"后台任务 {sid} 异常结束: {t.exception()}")
    return cb


async def resume_session_task(sid: str) -> bool:
    """拉起后台 runner 续跑会话。已在运行或已被并发认领则返回 False。

    供 actions/resume 与 auto_resume 使用：恢复消息与原任务一致，
    避免被当作"用户追加任务"重复进入 pending。

    ⚠ 无论是否新建 runner，**都必须先把状态改回 ready**：AgentRunner.run()
    里有一段"暂停停等"是**持有全局锁**的（while state==paused: sleep），
    唯一唤醒方式就是把状态改成非 paused。若像以前那样在"锁被本会话持有"
    时直接 return False，停等的 runner 永远醒不过来、锁也一直不释放——
    表现为"点恢复弹已恢复，会话却一直是已暂停"（用户实测）。
    """
    state = AGENT_SESSIONS.get(sid)
    if not state:
        return False
    wake = {"state": "ready", "cancel_requested": False, "rollover_requested": False}
    # 情况一：会话的 runner 还活着（正停在暂停循环里）→ 只唤醒，不新建
    # 注意：网页会话占窗口，提供商会话占逻辑槽，两边都要查（漏查会拉起第二个 runner）
    if session_running(sid):
        AGENT_SESSIONS.patch(sid, **wake)
        log(f"会话 {sid} 的 runner 仍在运行，已解除暂停状态（不新建 runner）")
        return False
    # 情况二：已有恢复任务在排队等锁 → 同样先唤醒，避免重复拉起
    if sid in _RESUMING:
        AGENT_SESSIONS.patch(sid, **wake)
        return False
    _RESUMING.add(sid)
    try:
        AGENT_SESSIONS.patch(sid, state="ready", last_error=None)
        # 一次性授权后的恢复要**明确告诉模型"已授权"**：历史里留着"需要用户授权"
        # 的拦截消息，若恢复时只重发原任务，模型很可能据此认定该操作被拒而放弃
        # （实测：授权后工具根本不重试，时间线只剩那条"未获授权"）。
        _task = state.get("original_task") or "继续"
        _hint = _RESUME_HINTS.pop(sid, "")
        if _hint:
            _task = _task + "\n\n" + _hint
        req = AgentRequest(
            model=normalize_model_soft(state.get("model") or DEFAULT_MODEL),
            messages=[Message(role="user", content=_task)],
            stream=False, session_id=sid, workspace=state.get("workspace"))
        task = asyncio.create_task(run_agent(req))
        _PENDING_RESUME[sid] = task
        task.add_done_callback(_bg_task_done(sid))
    except Exception:
        _RESUMING.discard(sid)
        _PENDING_RESUME.pop(sid, None)
        raise
    return True


async def auto_resume_interrupted():
    """auto_resume=true 时，自动恢复上次服务中断（而非用户主动暂停）的会话。

    串行恢复：run_agent 内部持有全局锁，多个会话会自然排队执行。
    """
    await wait_copilot_ready()  # 等企业 SSO/ADFS 重定向链走完
    sessions = AGENT_SESSIONS.interrupted_sessions()
    if not sessions:
        return
    log(f"自动恢复 {len(sessions)} 个被中断的会话")
    for s in sessions:
        sid = s["id"]
        try:
            # 置 ready 让 AgentRunner 进入 running；恢复消息与原任务一致，
            # 避免被当作"用户追加任务"重复进入 pending。
            AGENT_SESSIONS.patch(sid, state="ready", last_error=None)
            req = AgentRequest(
                model=s.get("model") or DEFAULT_MODEL,
                messages=[Message(role="user", content=s.get("original_task") or "继续")],
                stream=False, session_id=sid, workspace=s.get("workspace"))
            log(f"会话 {sid} 已自动恢复")
            await run_agent(req)
        except Exception as exc:
            log(f"会话 {sid} 自动恢复失败: {exc}")


# ── 跟进消息队列：入队即返回，后台按序消费（DSH 式串行队列）──────────
# 此前 continue_session_message 同步阻塞到整轮跑完才返回：任务正在跑时，
# 用户发跟进消息会一直卡"发送中"。改为每会话一个 FIFO 队列，入队立即响应，
# 由后台 worker 逐条取用；run_agent/plain_chat_turn 内部仍抢全局锁，
# 因此跨会话也天然串行。
_MSG_QUEUES: dict[str, "collections.deque"] = {}
_MSG_WAKE: dict[str, asyncio.Event] = {}
_MSG_WORKERS: dict[str, asyncio.Task] = {}
_MSG_INFLIGHT: dict[str, dict] = {}


def queue_depth(sid: str) -> int:
    dq = _MSG_QUEUES.get(sid)
    return len(dq) if dq else 0


def queue_items(sid: str) -> list[dict]:
    """队列条目（含正在处理的那条）：处理中的标 processing 且位置 0，其余按 FIFO。

    只有"等待中"的条目（deque 内）可编辑/删除；处理中的只展示。
    """
    out = []
    inflight = _MSG_INFLIGHT.get(sid)
    if inflight:
        out.append({"id": inflight["id"], "position": 0, "processing": True,
                    "content": inflight["content"],
                    "mode": "agent" if inflight.get("agent_mode") else "chat",
                    "model_id": inflight.get("model_id")})
    dq = _MSG_QUEUES.get(sid)
    if dq:
        for i, item in enumerate(dq):
            out.append({"id": item["id"], "position": i + 1, "content": item["content"],
                        "mode": "agent" if item.get("agent_mode") else "chat",
                        "model_id": item.get("model_id")})
    return out


async def _message_worker(sid: str):
    dq = _MSG_QUEUES[sid]
    wake = _MSG_WAKE[sid]
    while True:
        while dq:
            item = dq.popleft()
            _MSG_INFLIGHT[sid] = item
            try:
                await _process_queued_message(sid, item)
            except Exception as exc:  # 单条失败不影响队列继续
                log(f"会话 {sid} 队列消息处理失败: {exc}")
            finally:
                _MSG_INFLIGHT.pop(sid, None)
        wake.clear()
        try:
            await wake.wait()        # 无待处理任务时挂起，等 enqueue 唤醒
        except asyncio.CancelledError:
            raise


def cancel_message_queue(sid: str) -> None:
    """删除会话时清理其队列与 worker（避免等待中的 worker 永久挂起）。"""
    task = _MSG_WORKERS.pop(sid, None)
    if task and not task.done():
        task.cancel()
    _MSG_QUEUES.pop(sid, None)
    _MSG_WAKE.pop(sid, None)
    _MSG_INFLIGHT.pop(sid, None)


async def _process_queued_message(sid: str, item: dict):
    content, model_id = item["content"], item["model_id"]
    attach_paths = item.get("attach_paths") or []
    state = AGENT_SESSIONS.get(sid)
    conv_idx = int(state.get("conversation_index", 0)) if state else 0
    runtime_update(sid, phase="followup_processing", detail=f"正在处理跟进消息：{content[:80]}")
    if item.get("agent_mode"):
        req = AgentRequest(
            model=model_id,
            messages=[Message(role="user", content=content)],
            stream=False, session_id=sid, reuse_session=True,
            workspace=state.get("workspace") if state else None)
        try:
            result, _ = await run_agent(req, attach_paths=attach_paths)
            answer = result.content
            # run_agent 收尾时**已经**把最终回答落库了（幂等）。这里以前也存一次，
            # 于是同一条回答出现两条（实测 message id 181/182，同一秒同内容）。
            # 与 L2510 那次修复同类问题，只是当年 agent 路径不走 run_agent 的落库。
        except Exception as exc:
            answer = f"[错误] {exc}"
            AGENT_SESSIONS.add_message(sid, "assistant", answer, conv_idx)
        runtime_update(sid, phase="followup_completed", answer_preview=answer[-12000:],
                       detail="跟进消息处理完成")
    else:
        pm = providers_mod.split_provider_model(model_id)
        try:
            if pm and pm[0] in PROVIDERS:
                answer = (await provider_session_chat(sid, content, model_id))["content"]
            else:
                answer = (await plain_chat_turn(sid, content, model_id, attach_paths=attach_paths))["content"]
            # 内层（plain_chat_turn / provider_session_chat）已存消息并设了 answer_preview
            # ——且对话模式存的是 markdown 版——这里**不再重复存**、也不覆盖 answer_preview。
            # 此前外层 L2017 又存了一次：同一回复出现两条（一条 markdown、一条纯文本）。
            runtime_update(sid, phase="followup_completed", detail="跟进消息处理完成")
        except Exception as exc:
            answer = f"[错误] {exc}"
            AGENT_SESSIONS.add_message(sid, "assistant", answer, conv_idx)
            runtime_update(sid, phase="followup_completed", answer_preview=answer[-12000:],
                           detail="跟进消息处理完成")
    publish_event(sid, "messages")     # 新回复：通知前端立即刷新对话


async def enqueue_session_message(sid: str, content: str, model_id: str,
                                  agent_mode: bool, attach_paths: list | None = None,
                                  message_id: int | None = None) -> dict:
    """把跟进消息入队并确保 worker 在跑。返回排队位置（1 表示马上处理）。"""
    dq = _MSG_QUEUES.get(sid)
    if dq is None:
        dq = collections.deque()
        _MSG_QUEUES[sid] = dq
        _MSG_WAKE[sid] = asyncio.Event()
        _MSG_WORKERS[sid] = asyncio.create_task(_message_worker(sid))
    ahead = len(dq)
    item_id = uuid.uuid4().hex[:12]
    dq.append({"id": item_id, "content": content, "model_id": model_id,
               "agent_mode": agent_mode, "attach_paths": attach_paths or [],
               "message_id": message_id})
    _MSG_WAKE[sid].set()
    publish_event(sid, "queue")
    publish_event(sid, "messages")
    return {"session_id": sid, "queued": True, "id": item_id, "position": ahead + 1,
            "mode": "agent" if agent_mode else "chat"}


def find_queue_item(sid: str, item_id: str) -> dict | None:
    dq = _MSG_QUEUES.get(sid)
    if not dq:
        return None
    for item in dq:
        if item.get("id") == item_id:
            return item
    return None


def remove_queue_item(sid: str, item_id: str) -> bool:
    """从等待队列移除一条；同步删除其已落库的 user 消息。返回是否移除。"""
    dq = _MSG_QUEUES.get(sid)
    if not dq:
        return False
    for i, item in enumerate(list(dq)):
        if item.get("id") == item_id:
            del dq[i]
            if item.get("message_id"):
                AGENT_SESSIONS.delete_message(sid, item["message_id"])
            publish_event(sid, "queue")
            publish_event(sid, "messages")
            return True
    return False


def update_queue_item(sid: str, item_id: str, content: str | None = None,
                      model_id: str | None = None) -> bool:
    """修改等待队列里的一条；同步更新其已落库的 user 消息。返回是否更新。"""
    item = find_queue_item(sid, item_id)
    if not item:
        return False
    if content is not None and content.strip():
        item["content"] = content.strip()
        if item.get("message_id"):
            AGENT_SESSIONS.update_message(sid, item["message_id"], content.strip())
    if model_id:
        item["model_id"] = normalize_model_soft(model_id)
    return True


def _has_complete_agent_action(text: str) -> bool:
    try:
        # 用模块级已导入的 AgentRunner：函数体内 `from agent import ...`
        # 在包结构下必然 ModuleNotFoundError，会被下面裸 except 吞掉，
        # 使本函数恒返回 False（完整动作被当畸形、合法 final 被当限流）。
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
    # 日文
    r"しばらく(?:お待ちください|してからもう一度)",
    r"現在(?:応答|利用)(?:できません|できない)",
    r"リクエストが(?:多すぎます|多すぎる)",
    r"サービスは現在利用できません",
    r"問題が発生しました",
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


# Copilot 对话长度达到上限的典型提示（英文/中文/日文）。命中后的正确处理是
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
    # 日文
    r"会話(?:が)?(?:上限|長さ制限)(?:に達しました|を超えました)",
    r"新しい会話を開始して(?:ください|続行)",
    r"会話が長くなりすぎました",
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


_ANSWER_NOISE = (
    "Press Tab to access the More options button.",
    "按 Tab 访问“更多选项”按钮。",
    "Press Tab to access the More options",
)

# Copilot 在真正回答前会先在回答区渲染一条"思考中"占位文本，随后**整段替换**成答案。
# 实测（2026-10，英文租户）：Checking that now… / Lining things up… / Gathering details…
# / Just a sec… / Give me a moment…。它们不是答案，但会被 extract_latest_answer 当成
# 回答正文 → 流式通道先把占位文本发出去，真答案出现时前缀对不上、再也发不出去，
# 调用方拿到的就是 "Checking that now…"。所以在这里整行剔除。
_THINKING_PHRASES = (
    "checking that now", "lining things up", "gathering details", "just a sec",
    "give me a moment", "working on it", "one moment", "hold on",
    "正在查找", "正在整理", "正在准备", "正在思考", "稍等一下", "请稍等",
)


def _strip_thinking(text: str) -> str:
    """剔除"思考中"占位行（只匹配**整行且很短**的提示，避免误删正文）。"""
    lines = []
    for line in str(text or "").splitlines():
        s = line.strip().lower().rstrip(".…。 ")
        if s and any(s == p or (s.startswith(p) and len(s) <= len(p) + 12)
                     for p in _THINKING_PHRASES):
            continue
        lines.append(line)
    return "\n".join(lines).strip()



def _clean_answer(text: str) -> str:
    """去掉答案里的 CBP 标记与 Copilot 的无障碍提示（它们会混进正文）。

    这些噪声以前会一路混到模型手里（实测：模型收到的"回答"其实是自己的问题
    加一句 Press Tab…），导致 JSON 解析失败、任务空转。
    """
    t = str(text or "")
    # 标记是 `[CBP-AGENT][sid][C000]`（连续多个方括号），要把整串都去掉，
    # 只去第一个会留下 `[sid][C000]` 混进正文。
    t = re.sub(r"\[CBP-(?:AGENT|TURN)\](?:\[[^\]\n]*\])*", "", t)
    t = re.sub(r"\[CBP-TURN:[^\]]*\]\s*", "", t)
    for junk in _ANSWER_NOISE:
        t = t.replace(junk, "")
    t = _strip_thinking(t)
    # 收尾可能有零宽/多余空白
    return t.strip()


def _echo_only(value: str, sent: str) -> bool:
    """清洗后的内容是否只是"我刚发出去的那句"（含标记残留），不含真正回答。"""
    v = _clean_answer(value)
    q = _clean_answer(sent)
    if not v:
        return True
    if not q:
        return False
    short = q.splitlines()[0].strip()[:40]
    if not short:
        return False
    return short in v and len(v) <= len(short) + 80


async def wait_agent_answer(previous: str = "", pause_check=None, interrupt_check=None,
                            sent: str = "") -> str:
    """Wait using inactivity timeout, not a fixed per-turn wall-clock timeout.

    TIMEOUT now means: fail only when no answer progress is observed for this
    many seconds. Every text change and every visible generation cycle refreshes
    the idle deadline. A separate hard deadline prevents an infinite wait.
    Malformed but finished output is returned to AgentRunner for repair.

    interrupt_check：用户"强行插入指令"时返回 True。**半中断**——仅当本回合还
    什么都没产出（文本没变、也没在生成）时才中止等待；一旦 Copilot 已开始生成就
    让它答完，避免丢掉已生成的回答、以及"被放弃的回答"与下一轮应答错位。
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
        value = _clean_answer(await current_answer())

        # 插入指令的半中断点：还没产出任何内容 -> 放弃等待不丢东西
        if (interrupt_check and interrupt_check() and not still_generating
                and not generation_seen and value == previous):
            await diagnostics("agent_answer_interrupted_before_output")
            raise AgentInterrupted("已插入新指令，本回合尚未产出，已中止等待")

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

        # 只有"我的问题回声 + UI 提示"不算答案：继续等真正的回答出现
        _echo = bool(sent) and _echo_only(value, sent)
        is_candidate = bool(value) and (value != previous or generation_seen) and not _echo
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
                raise CopilotRateLimited(unavailable)
            # 无法解析的回答有可能是"暂歇"而非写完：Copilot 生成 JSON 中途停顿
            # （渲染文件链接、网络抖动）时，3 秒静默就取快照会拿到半截文本，上层
            # 只能报 invalid_action——实测就发生过：界面上答案完整（final JSON），
            # 我们却抓到 "...sharepoint_uploader_ui.p"。因此畸形回答要等更久，
            # 且认定结束前再读一次页面确认（仍在增长就继续等）。
            malformed_grace = max(STABLE * 2.0, 8.0)
            if generation_seen and quiet_for >= malformed_grace:
                confirm = (await current_answer()).strip()
                if confirm != current:
                    current = confirm
                    last_change = time.monotonic()
                    idle_deadline = last_change + TIMEOUT
                    continue
                if _has_complete_agent_action(confirm):
                    await diagnostics("agent_answer_ready_late")
                    return confirm
                await diagnostics("agent_answer_needs_repair")
                return confirm

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
                raise CopilotRateLimited(unavailable)
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
        raise CopilotRateLimited(unavailable)
    if final and (final != previous or generation_seen or candidate_seen):
        await diagnostics("agent_answer_hard_return")
        return final
    await diagnostics("agent_answer_hard_timeout")
    raise TimeoutError(
        f"Copilot 单步等待超过硬上限 {int(hard_timeout)} 秒，请查看 last_page.txt"
    )

def strip_suggestion_tail(text: str) -> str:
    """去掉 Copilot 回答尾部的"后续建议"文字。

    实测（task-afa55d82ff7a）：回答正文是一个完整 JSON 动作，后面紧跟三行建议——
    「继续读取匹配代码 / 搜索菜单初始化函数 / 查看快捷键相关测试」。
    那些是页面上的建议按钮，被 `inner_text` 一起读进来了。JSON 动作本身不受影响
    （解析器只取第一个完整对象），但存库/展示的正文会被污染。
    """
    raw = text or ""
    body = raw.strip()
    if not body.startswith("{"):
        return raw
    try:
        _obj, end = json.JSONDecoder().raw_decode(body)
    except Exception:
        return raw                      # 解析不了就原样返回，交给后面的解析容错
    tail = body[end:].strip()
    if not tail:
        return body
    return body[:end].rstrip()


async def agent_chat_once(prompt: str, model_id: str, make_new: bool = False, pause_check=None,
                          interrupt_check=None) -> str:
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
    _answer = await wait_agent_answer(previous, pause_check=pause_check, sent=prompt,
                                      interrupt_check=interrupt_check)
    return strip_suggestion_tail(_answer)

# API 提供商的协议强化：网页 Copilot 对 JSON 协议遵循度高，但 DeepSeek/Kimi
# 等聊天模型爱在动作外面加解释或输出两段 JSON（撞严格防线烧回合）。
# 用独立 system 消息下达硬性输出约束，实测可显著降低解析失败率。
_PROVIDER_PROTOCOL = """你在与一个自动化执行器对话。你的整个回复必须严格遵守输出协议：
1. 只输出一个 JSON 对象：第一个字符必须是 {，最后一个字符必须是 }。
2. 禁止任何解释、思考过程、Markdown、代码块围栏（```），禁止输出第二个 JSON 对象。
3. JSON 只能是两种之一：
   {"type":"tool","tool":"工具名","arguments":{},"reason":"原因"}
   {"type":"final","content":"最终答复"}
4. 创建/追加代码、JSON、HTML、Markdown 内容必须用 write_text_b64 / append_text_b64，
   内容放 content_b64 字段（UTF-8 的 Base64），单次解码后不超过 8000 字符。
5. 工具名只能从用户消息末尾的可用工具清单里逐字照抄（如 list_directory、read_text），
   禁止猜测或创造工具名（如 list_dir、list_files、bash 都不存在）。
违反任何一条都会被系统拒绝执行并浪费回合。"""


# provider 普通对话的按会话锁：同 sid 并发请求基于相同历史交错写回的防护
_PROVIDER_LOCKS: dict[str, asyncio.Lock] = {}

# API 提供商 Agent 模式的按会话对话历史。
#
# 为什么必须留历史：AgentRunner 只在首回合（total==0）把系统提示词（含可用
# 工具清单）放进 prompt，后续回合只发"继续同一任务。上一动作实际结果：…"。
# 浏览器版 Copilot 自己保留对话上下文所以没问题；提供商是无状态 HTTP 调用，
# 若不把历史一起发过去，第二回合起模型既看不到工具清单也看不到任务目标
# （实测报错："本轮消息仍未附带可用工具清单，也没有给出任务目标…"直接卡死）。
_PROVIDER_AGENT_HISTORY: dict[str, list[dict]] = {}
_PROVIDER_HISTORY_MAX_CHARS = 160000


def _trim_provider_history(hist: list[dict], max_chars: int | None = None) -> None:
    """按字符上限回收历史：保留 system、**首条任务/工具清单消息**与最近一轮。

    首条 user 消息里带着完整的系统提示词（含可用工具清单），必须保住：
    实测 493 步的真实任务里，历史被裁剪后模型忘记了工具名，开始瞎编
    run_tests / check_syntax / execute_command，最后卡在验证门上判 failed。
    因此裁剪从 index 2 开始（跳过 system 与首条消息）。

    max_chars 取调用时的模块常量（作为函数默认参数会在定义时固定，
    运行期改常量就失效了）。至少留 system + 首条 + 最近一轮。
    """
    cap = _PROVIDER_HISTORY_MAX_CHARS if max_chars is None else max_chars

    def total() -> int:
        return sum(len(m.get("content") or "") for m in hist)
    while total() > cap and len(hist) > 4:
        hist.pop(2)                                   # 保留 system 与首条（工具清单）
        if len(hist) > 2 and hist[2].get("role") == "assistant":
            hist.pop(2)                               # 它的回复一起丢，保持配对


def _repair_provider_history(hist: list[dict]) -> int:
    """按**真实 API 契约**校验并自愈提供商对话序列，返回修掉的条数。

    契约（实测 OpenAI 兼容行为，不是我当初想当然的"严格交替"）：
      · system 必须在首位（缺失则补回）；
      · 其后**不能以 assistant 打头**（没有对应用户输入的回答）；
      · 连续的 user 消息是**合法的**（服务端会顺次理解），不能为了"交替"去删；
      · 连续 assistant 才会被合并（保留较新的一条）。

    这里踩过两次坑，都由既有测试当场抓到：
      1. 先写"严格交替"→ 把锚点（首条含工具清单的 prompt）与最近一轮 user 拆开，
         既有测试断言"最近一轮仍在"立刻失败；
      2. 再写"与锚点冲突就丢新来的"→ 又把最近一轮 user 丢了，仍然失败。
    教训：契约要按**真实 API 行为**定，不要按"看起来更整齐"定。
    """
    fixed = 0
    if not hist:
        return fixed

    head = hist[0] if hist[0].get("role") == "system" else {"role": "system", "content": _PROVIDER_PROTOCOL}
    if head is not hist[0]:
        fixed += 1
    rest = list(hist[1:]) if hist[0].get("role") == "system" else list(hist)

    out: list[dict] = [head]
    for msg in rest:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            fixed += 1
            continue
        if role == "assistant" and len(out) == 1:
            fixed += 1                     # 不能以 assistant 打头（其 user 已丢失）
            continue
        if role == "assistant" and out[-1].get("role") == "assistant":
            out[-1] = msg                  # 连续 assistant：保留较新
            fixed += 1
            continue
        out.append(msg)                    # 连续 user 合法，原样保留

    if fixed:
        hist[:] = out
        log(f"提供商对话历史结构已自愈：修正 {fixed} 条（system 首位、不以 assistant 打头）")
    return fixed


def discard_provider_agent_history(sid: str) -> None:
    """会话删除时清理提供商对话历史（避免长期运行内存里堆积）。"""
    _PROVIDER_AGENT_HISTORY.pop(sid, None)


def sync_provider_history_message(sid: str, old: str, new: str | None) -> bool:
    """把"编辑/删除了一条消息"同步到提供商的内存对话历史。

    为什么必须做：提供商路径的上下文来自 `_PROVIDER_AGENT_HISTORY`（增量累积），
    不是每回合从 messages 表重建——只改库不同步内存，模型**下一回合仍会看到旧文本**。

    匹配策略：从后往前找第一条 content 与 old 相同的条目（最近的更可能是它）。
    找不到就返回 False（例如该回合已被上下文裁剪掉），由调用方如实告知用户。
    """
    hist = _PROVIDER_AGENT_HISTORY.get(sid)
    if not hist:
        return False
    for i in range(len(hist) - 1, -1, -1):
        item = hist[i]
        if not isinstance(item, dict) or item.get("role") == "system":
            continue
        if str(item.get("content") or "") == str(old or ""):
            if new is None:
                hist.pop(i)
            else:
                item["content"] = new
            return True
    return False


# ── 强行插入指令的"立即中断"信号 ────────────────────────────────────
# 用户 Ctrl+J 插入指令时置位。提供商路径用它取消底层 HTTP 调用（取消即"这轮
# 没发生"）；网页路径只在"本回合还没产出内容"时中止等待（见 wait_agent_answer）。
_INTERRUPTS: dict[str, asyncio.Event] = {}


def _interrupt_event(sid: str) -> asyncio.Event:
    ev = _INTERRUPTS.get(sid)
    if ev is None:
        ev = asyncio.Event()
        _INTERRUPTS[sid] = ev
    return ev


def request_interrupt(sid: str) -> None:
    """请求立即中断该会话当前回合（由 /inject 在运行中调用）。"""
    _interrupt_event(sid).set()


def interrupt_pending(sid: str) -> bool:
    """只窥探不清除：等待循环每 POLL 都会问一次，若在这里清掉，
    "已开始生成所以不中断"的那次询问就会把信号吃掉、再也不会生效。"""
    ev = _INTERRUPTS.get(sid)
    return bool(ev and ev.is_set())


def clear_interrupt(sid: str) -> None:
    ev = _INTERRUPTS.get(sid)
    if ev is not None:
        ev.clear()


def discard_interrupts(sid: str) -> None:
    _INTERRUPTS.pop(sid, None)


async def _await_interruptible(sid: str, coro, what: str):
    """等待 coro；若期间该会话被要求"立即插入"，取消它并抛 AgentInterrupted。

    注意这里把底层协程包成 task 再取消，因此**外层看到的是 AgentInterrupted 而不是
    CancelledError**——provider_turn 的 `except Exception` 清理逻辑照常生效，
    历史里那条 user 消息不会残留（否则下一回合会重复追加）。
    """
    ev = _interrupt_event(sid)
    task = asyncio.ensure_future(coro)
    waiter = asyncio.ensure_future(ev.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        task.cancel(); waiter.cancel()
        raise
    if task in done:
        waiter.cancel()
        return await task
    task.cancel()
    try:
        await task
    except BaseException:
        pass
    clear_interrupt(sid)
    raise AgentInterrupted(f"已插入新指令，{what}被中断，改为下一回合按新指令执行")


def remember_initial_prompt(sid: str, prompt: str) -> None:
    """首个回合发给模型的提示词落库，供事后查看看。

    里头是完整系统提示词（工具清单 + 项目约定 + 验证闭环规则 + 任务），
    是排查"Agent 为什么这么做"的第一现场。此前只写在 RUNTIME.prompt_preview，
    每个回合都被覆盖，初始提示词随即丢失。只记第一次，异常静默（不影响回合）。
    """
    if not prompt:
        return
    try:
        st = AGENT_SESSIONS.get(sid)
        if not st or (st.get("initial_prompt") or "").strip():
            return
        AGENT_SESSIONS.patch(sid, initial_prompt=prompt[:200000])
    except Exception:
        pass


def rebuild_initial_prompt(state: dict) -> str:
    """按会话的工作区与任务重建"初始提示词"（旧会话没存过时用）。

    用独立的 ToolRegistry 重建，避免动到正在运行任务共享的那个实例
    （改 AGENT_TOOLS.root 会让跑着的任务串台）。内容可能与当初略有差异
    （AGENT.md / skills / 工具开关后来改过），因此界面会标注"近似重建"。
    """
    from .tools import ToolRegistry
    ws = Path(state.get("workspace") or DEFAULT_WORKSPACE)
    tools = ToolRegistry(ws, AGENT_CFG)
    tools.set_global_root(BASE)
    tools.current_session_id = str(state.get("id") or "")
    try:
        tools.memory_scope = memory_scope_for(ws)
    except Exception:
        pass
    tools.set_web_copilot_tools(True)
    task = str(state.get("original_task") or "")
    runner = AgentRunner(tools, None, AGENT_CFG, None, None, None, None)
    runner._task_text = task          # 供工具按任务裁剪
    try:
        runner._agent_md, runner._skills = runner._context_values()
    except Exception:
        pass
    return runner._system_prompt() + "\n\n用户任务：\n" + task


async def provider_turn(sid: str, model_id: str, prompt: str, force_new: bool = False) -> str:
    """API 提供商的 Agent 回合：带会话历史的 system 协议约束 + 累积对话。

    与浏览器版的关键差异：提供商每回合都是独立 HTTP 调用，必须把历史一起
    发过去（否则模型看不到工具清单与任务目标）。force_new（换会话/滚动摘要）
    时重置历史，从新的摘要提示重新开始。

    限流/5xx/超时映射为 CopilotRateLimited，复用 AgentRunner 的指数退避重试；
    其他错误原样抛出计入解析失败/失败处理。失败回合会把刚追加的 user 消息
    回滚掉，避免重试时同一提示在历史里堆叠。
    """
    pm = providers_mod.split_provider_model(model_id)
    provider = PROVIDERS[pm[0]]
    if force_new or sid not in _PROVIDER_AGENT_HISTORY:
        _PROVIDER_AGENT_HISTORY[sid] = [{"role": "system", "content": _PROVIDER_PROTOCOL}]
    hist = _PROVIDER_AGENT_HISTORY[sid]
    hist.append({"role": "user", "content": prompt})
    _trim_provider_history(hist)
    _repair_provider_history(hist)
    remember_initial_prompt(sid, prompt)
    # 提供商 Agent 回合原先完全不写 runtime，导致 WebUI 的"最近发送内容/
    # Copilot 当前回答/当前执行状态"三个面板全程空白——这里补齐。
    clear_interrupt(sid)   # 新回合开始：上一轮的插入信号已由 pending 承接，别误伤本回合
    runtime_update(sid, phase="api_sending", detail=f"正在调用 {pm[0]} API（{pm[1]}）",
                   prompt_preview=prompt[-12000:], answer_preview="")

    def _rollback_user() -> None:
        """回滚本次追加的 user 消息（含取消/中断），避免重试时重复追加。"""
        if hist and hist[-1].get("role") == "user":
            hist.pop()

    async def _call() -> str:
        """调用提供商：优先流式（边收边写 answer_preview，界面就能"逐字"看到），
        流式不可用（服务不支持 response_format+stream 等）则退回非流式 complete()。

        只在**一个增量都没收到**时才退回：已经收到内容再重发会重复计费、
        也可能拿到两份不同的回答。
        """
        if not bool(CFG.get("stream_provider_agent", True)) or not hasattr(provider, "stream"):
            return await provider.complete(pm[1], hist, response_schema=AGENT_ACTION_SCHEMA)
        chunks: list[str] = []
        last_push = 0.0
        try:
            async for piece in provider.stream(pm[1], hist,
                                               response_schema=AGENT_ACTION_SCHEMA):
                if not piece:
                    continue
                chunks.append(piece)
                now = time.monotonic()
                # 节流：最多每 150ms 写一次 runtime，避免高频 DB 写入
                if now - last_push >= 0.15:
                    last_push = now
                    runtime_update(sid, phase="api_streaming",
                                   detail=f"{pm[0]} API 正在流式返回…",
                                   answer_preview="".join(chunks)[-12000:])
        except (providers_mod.ProviderError, providers_mod.ProviderUnavailable) as exc:
            if chunks:
                raise
            log(f"提供商流式不可用，退回非流式：{exc}")
            return await provider.complete(pm[1], hist, response_schema=AGENT_ACTION_SCHEMA)
        except AttributeError as exc:
            # 自定义 provider 对象没实现 stream（保留兼容）
            log(f"提供商不支持流式（{exc}），改用非流式")
            return await provider.complete(pm[1], hist, response_schema=AGENT_ACTION_SCHEMA)
        text = "".join(chunks)
        if not text.strip():
            log("提供商流式返回为空，退回非流式")
            return await provider.complete(pm[1], hist, response_schema=AGENT_ACTION_SCHEMA)
        # 收尾必推完整文本：节流可能让最后一次增量没发出去，预览会停在半截
        runtime_update(sid, phase="api_streaming",
                       detail=f"{pm[0]} API 流式返回完成，正在解析动作",
                       answer_preview=text[-12000:])
        return text

    try:
        # 强行插入指令时立即取消这次 HTTP 调用：提供商是无状态请求，取消即
        # "这轮没发生"，下一回合直接带上新指令重发，不用等它跑完。
        answer = await _await_interruptible(sid, _call(), "调用提供商 API")
    except providers_mod.ProviderUnavailable as exc:
        _rollback_user()
        # window_seconds=0：提供商走"下次重试"倒计时，不用 Copilot 网页实测的
        # 1 小时固定窗口（提供商的 429/5xx/网络抖动通常几秒~几分钟就恢复）。
        raise CopilotRateLimited(str(exc), window_seconds=0) from exc
    except BaseException:
        # BaseException：包含 CancelledError——外部取消整个任务时也要回滚，
        # 否则下次运行会看到一条残留的 user 消息（重复追加）。
        _rollback_user()
        raise
    hist.append({"role": "assistant", "content": answer})
    _trim_provider_history(hist)
    _repair_provider_history(hist)
    runtime_update(sid, phase="api_received", detail=f"{pm[0]} API 已返回，正在解析动作",
                   answer_preview=answer[-12000:])
    return answer


async def provider_session_chat(sid: str, content: str, model_id: str) -> dict:
    """API 提供商的普通对话：用 Session 的消息记录作上下文，写回双方消息。

    调用方（continue_session_message）已写入本次 user 消息；新任务由
    plain_completions 先建 Session 再调本函数。
    """
    async with _PROVIDER_LOCKS.setdefault(sid, asyncio.Lock()):
        pm = providers_mod.split_provider_model(model_id)
        provider = PROVIDERS[pm[0]]
        history = [{"role": m["role"], "content": m["content"]}
                   for m in AGENT_SESSIONS.messages(sid)]
        runtime_update(sid, phase="api_sending", detail=f"正在调用 {pm[0]} API",
                       prompt_preview=content, answer_preview="")
        try:
            answer = await provider.complete(pm[1], history)
        except providers_mod.ProviderUnavailable as exc:
            raise CopilotRateLimited(str(exc), window_seconds=0) from exc
        current = AGENT_SESSIONS.get(sid)
        if not current:
            raise RuntimeError("会话在执行期间被删除")
        AGENT_SESSIONS.add_message(sid, "assistant", answer, int(current.get("conversation_index", 0)))
        AGENT_SESSIONS.patch(sid, model=model_id)
        runtime_update(sid, phase="api_completed", answer_preview=answer[-12000:],
                       detail=f"{pm[0]} API 调用完成")
        return {"session_id": sid, "role": "assistant", "content": answer,
                "url": None, "mode": "chat", "provider": pm[0]}


def _conv_index(sid: str) -> int:
    """当前对话序号（换会话后 step_no 会复用，检查点靠它区分代次）。"""
    try:
        return int((AGENT_SESSIONS.get(sid) or {}).get("conversation_index", 0))
    except Exception:
        return 0


def event_source() -> str:
    """读取路径来源（P2-10 第三期开关）：columns（默认，读数据库列）/ projection（读事件投影）。

    优先级：config.json 的 agent.event_source → 环境变量 CBP_EVENT_SOURCE → columns。
    默认必须是 columns：投影虽已被测试与巡检证明一致，但"切换读取路径"应当是显式动作，
    且任何时候都能一条命令回退（这也是分期推进第三期的前提）。
    """
    try:
        val = str(AGENT_CFG.get("event_source") or "").strip().lower()
    except Exception:
        val = ""
    if not val:
        val = (os.environ.get("CBP_EVENT_SOURCE") or "").strip().lower()
    return val if val in ("columns", "projection") else "columns"


# 距上次 todo 更新走了多少步（sid -> 步数）。超过阈值就提醒模型更一次。
_TODO_NUDGE: dict[str, int] = {}
def _todo_nudge_every() -> int:
    """每多少步提醒模型更新待办（0=关闭）。可在设置页/ config.json 的
    `agent.todo_nudge_every` 里改。"""
    try:
        return int(AGENT_CFG.get("todo_nudge_every", 12))
    except Exception:
        return 12


def nudge_todo_update(sid: str, step_no: int) -> None:
    """每 N 步没更新待办，就往 next-step 队列塞一句提醒。

    模型普遍"列一次清单就再也不管"（实测整改 15 步只调 1 次 todo_write），
    面板因此永远看不出进度。这里用既有的 pending_steps 注入通道软提醒，
    不打断执行、也不改它的规划，只是让它把状态勾一下。
    """
    try:
        st = AGENT_SESSIONS.get(sid) or {}
        todos = st.get("todos") or []
        if not todos or _todo_nudge_every() <= 0:
            return
        last = _TODO_NUDGE.get(sid)
        if last is None:
            _TODO_NUDGE[sid] = int(step_no)
            return
        if int(step_no) - last < _todo_nudge_every():
            return
        _TODO_NUDGE[sid] = int(step_no)
        pend = [str(x) for x in (st.get("pending_steps") or []) if str(x).strip()]
        msg = ("【待办】请顺手调用一次 todo_write：把已完成的项置 completed、"
               "当前正在做的项置 in_progress（整表重写）。这是用户看进度的唯一途径。")
        if msg in pend:
            return
        AGENT_SESSIONS.patch(sid, pending_steps=pend + [msg])
    except Exception:
        pass


def sync_session_todos(sid: str) -> bool:
    """按需把工作区 TODO 文件同步进会话待办（解析失败/没变化时安静返回）。

    用户诉求："todo 应该自己分析 todo.md 之类的来更新"——文件里写的是计划，
    以前 CBP 完全不看它，面板只能靠模型自觉调 todo_write（实测整改阶段只调了 1 次）。
    另外：同步后若一条 `in_progress` 都没有，就把第一条 pending 置为 in_progress，
    否则面板上永远看不出"现在在做哪一项"（用户："都没见更新做到哪的"）。
    """
    try:
        from .todo_sync import sync_todos_from_files

        changed = bool(sync_todos_from_files(AGENT_SESSIONS, sid))
        st = AGENT_SESSIONS.get(sid) or {}
        todos = list(st.get("todos") or [])
        if todos and str(st.get("state")) in ("running", "waiting") \
                and not any(str(t.get("status")) == "in_progress" for t in todos):
            for i, t in enumerate(todos):
                if str(t.get("status")) == "pending":
                    todos[i] = dict(t, status="in_progress")
                    AGENT_SESSIONS.patch(sid, todos=todos)
                    changed = True
                    break
        return changed
    except Exception:
        return False


def pending_steps_via(sid: str, source: str | None = None) -> list[dict]:
    """"结果未知"的步骤（未收尾）——可按 columns 或 projection 读取。

    第三期的做法是**同一份事实两条读取路径**：默认读列，切到投影后若两者不一致，
    记一条告警并**回退到列**（安全优先）。这样切换可以小步进行、随时可回退。
    """
    src = source or event_source()
    try:
        via_columns = AGENT_SESSIONS.pending_steps(sid)
    except Exception:
        via_columns = []
    if src != "projection":
        return via_columns
    try:
        from .projection import project

        proj = project(AGENT_SESSIONS.events(sid))
        via_proj = [{"step_no": s.get("step_no"), "tool": s.get("tool"),
                     "conversation_index": s.get("conversation_index")}
                    for s in proj.get("steps") or [] if s.get("status") == "inflight"]
    except Exception as exc:
        log(f"投影读取失败，回退到列：{exc}")
        return via_columns
    key = lambda xs: sorted((int(x.get("conversation_index") or 0), int(x.get("step_no") or 0),
                             str(x.get("tool") or "")) for x in xs)
    if key(via_proj) != key(via_columns):
        log(f"事件投影与数据库列不一致（会话 {sid}）：投影 {key(via_proj)} vs 列 {key(via_columns)}；"
            f"本次回退到列，请用 scripts/health.py 排查")
        return via_columns
    return via_proj


def turn_cache_key(prompt: str, attach_paths=None) -> str:
    """回合缓存键：**必须包含附件指纹**。

    同一句"看看这张图"配不同截图时，只用 prompt 哈希会命中上一次的缓存答案，
    模型根本没看到新图 —— 用户侧表现就是"贴图后识别失败"。
    """
    import hashlib as _hl
    key = _hl.sha1((prompt or "").encode("utf-8")).hexdigest()[:20]
    if attach_paths:
        ah = _hl.sha1()
        for p in attach_paths:
            try:
                ah.update(Path(p).read_bytes())
            except Exception:
                ah.update(str(p).encode("utf-8", "replace"))
        key += ":att-" + ah.hexdigest()[:12]
    return key


def warn_provider_cannot_attach(sid: str, model: str, attach_paths) -> None:
    """API 提供商分支收到图片：明确记录 + 提示，而不是静默丢弃。

    提供商走 HTTP API，不经过 Copilot 网页输入框，没有地方附加图片。
    以前这里直接忽略附件，于是模型只看到"看看这张图"这句话，答得像没看图。
    """
    names = [Path(getattr(x, "name", str(x))).name for x in (attach_paths or [])]
    if not names:
        return
    log(f"提供商模型 {model} 不能接收图片，已跳过 {len(names)} 张（{', '.join(names)}）")
    try:
        cur = AGENT_SESSIONS.get(sid) or {}
        sn = int(cur.get("total_steps", 0)) + 1
        AGENT_SESSIONS.add_step(sid, sn, int(cur.get("conversation_index", 0)), {
            "step": sn, "tool": "attach_image", "status": "error",
            "reason": f"当前模型 {model} 是 API 提供商，无法接收图片附件",
            "result": {"skipped": names,
                       "hint": "想让它看图，请把模型切换成 Copilot（copilot-*）再发一次"}})
    except Exception:
        pass
    runtime_update(sid, phase="running",
                   detail=f"⚠ 当前模型不支持图片：已跳过 {len(names)} 张附件；想看图请切到 Copilot 模型")


async def run_agent(req: AgentRequest, attach_paths: list | None = None):
    global ACTIVE_SESSION_ID
    if not AGENT_CFG.get("enabled", True): raise HTTPException(503, "Agent 功能已禁用")
    # 软归一：历史会话可能保存了已失效的模型 ID，回退默认模型而非 400
    req.model=normalize_model_soft(req.model)
    task="\n".join(f"[{m.role}] {m.content.strip()}" for m in req.messages if m.content.strip())
    if not task: raise HTTPException(400,"至少需要一条消息")
    sid=req.session_id or ("task-"+uuid.uuid4().hex[:12])
    runtime_update(sid, phase="queued", detail="任务已进入执行队列", answer_preview="", prompt_preview="")
    # 提供商模型（"前缀/模型"）不需要浏览器页；网页通道才占一个自动化窗口
    _need_page = providers_mod.split_provider_model(req.model) is None
    async with _window(sid, need_page=_need_page) as (win, T):
        # ACTIVE_SESSION_ID 必须在锁内声明：等锁的请求若提前覆盖，
        # 正在运行会话的删除保护/busy 判定/resume 守卫会整体失效。
        prev_active = ACTIVE_SESSION_ID
        ACTIVE_SESSION_ID = sid
        # 同一会话若有排队中的 resume 任务，被本次新消息取代：否则旧任务文本
        # 与本次消息会各跑一遍（顺序双跑，重复动作如 append/下载）。
        _pend = _PENDING_RESUME.pop(sid, None)
        if _pend is not None and not _pend.done() and _pend is not asyncio.current_task():
            _pend.cancel()
            log(f"会话 {sid} 的排队恢复任务已被新消息取代")
        existing=AGENT_SESSIONS.get(sid)
        workspace=(req.workspace.strip() if req.workspace else None) or (existing or {}).get("workspace") or DEFAULT_WORKSPACE
        T.set_root(workspace,False)
        # 沙箱档位 / 计划模式按**本次请求**生效（配置是默认值）：
        # 计划模式 = 强制只读，模型只探索并产出方案，调用 exit_plan_mode 后解除。
        _mode = str(getattr(req, "sandbox_mode", None) or AGENT_CFG.get("sandbox_mode")
                    or "workspace-write").lower()
        T.sandbox_mode = _mode if _mode in SANDBOX_MODES else "workspace-write"
        T.plan_mode = bool(getattr(req, "plan_mode", False))
        if T.plan_mode:
            log(f"会话 {sid} 进入计划模式（只读）")
            plan_hint = (
                "\n\n[计划模式] 本轮**强制只读**：任何写文件/执行命令/改外部系统的工具都会被拒绝。"
                "请先只读探索（读代码、搜索、看目录），然后产出可执行方案，"
                "并用 exit_plan_mode(plan=<方案 Markdown>) 提交。"
                "提交后**立即返回 final** 把方案呈现给用户，不要在同一轮开始执行——"
                "用户确认（说\"继续\"）后才按方案动手。")
            task = f"{task}{plan_hint}"
            if existing:
                AGENT_SESSIONS.patch(sid, plan_mode=1)
        # 记忆作用域随工作区切换：工作区记忆不跨项目泄露；来源会话用于溯源
        T.memory_scope = memory_scope_for(T.root)
        T.current_session_id = sid
        if not existing:
            existing=AGENT_SESSIONS.create(sid,task,str(T.root),req.model)
            effective_task = task
        else:
            effective_task = existing.get("original_task") or task
            if task.strip() and task.strip() != effective_task.strip():
                effective_task += "\n\n用户追加任务：\n" + task
                pending = list(existing.get("pending") or [])
                pending.append(task)
                # 追加指令不再强制新建会话：Agent 会在当前会话直接下达，保留完整上下文。
                AGENT_SESSIONS.patch(sid, pending=pending, state="ready")
        AGENT_SESSIONS.patch(sid,workspace=str(T.root),model=req.model)
        def pause_check():
            st = AGENT_SESSIONS.get(sid) or {}
            return st.get("state") == "paused"
        _pm = providers_mod.split_provider_model(req.model)
        _is_provider = bool(_pm and _pm[0] in PROVIDERS)
        # provider 会话禁用"需要 Copilot 网页会话"的工具（copilot_attach/copilot_file：
        # 没有网页对话可驱动，实测连续失败 10 次白烧回合）。浏览器会话保持启用。
        T.set_web_copilot_tools(not _is_provider)
        if _is_provider and attach_paths:
            # 提供商分支没有可附加图片的输入框：明确记录 + 提示（不再静默丢弃）
            try:
                warn_provider_cannot_attach(sid, req.model, attach_paths)
            except Exception as exc:
                log(f"记录图片不可投递失败（忽略）: {exc}")
            attach_paths = None      # 明确：本轮无附件可投递
        if _is_provider:
            # API 提供商：不经过浏览器，无页面导航/会话 URL/标记确认簿记；
            # 换会话只影响本地上下文压缩。
            async def turn(prompt, model, force_new=False):
                SNAPSHOTS.next_turn(sid)   # 一次 Copilot 回合 = 一个快照版本
                # 上一次的工具调用已派发但结果未知（进程在结果落库前退出）：绝不原样重放，
                # 先让模型只读核对现状；随后把这些步骤标为 unknown，保证只提示一次。
                _pend = []
                try:
                    _pend = pending_steps_via(sid)
                except Exception:
                    _pend = []
                if _pend:
                    for _p in _pend:
                        try:
                            AGENT_SESSIONS.finish_step(sid, int(_p.get('conversation_index') or 0),
                                                       int(_p.get('step_no') or 0), 'unknown')
                        except Exception:
                            pass
                    _names = ', '.join(sorted({str(_p.get('tool') or '?') for _p in _pend}))
                    log(f'检测到 {len(_pend)} 个结果未知的工具步骤（{_names}），先只读核对不重放')
                    return json.dumps({
                        'type': 'tool', 'tool': 'list_directory', 'arguments': {'path': '.'},
                        'reason': f'上一次工具调用（{_names}）的结果未知：先只读核对当前状态，'
                                  '不要重复可能已发生的写入/删除/发送等副作用；确认后再继续原计划。'},
                        ensure_ascii=False)
                # 提供商回合同样"发出即不可回滚"：先落 inflight，成功后转 done；
                # 崩在途中（HTTP 被中断）→ 恢复时按"结果未知"处理，只做只读核对，不重发。
                import hashlib as _hl
                _ph = _hl.sha1((prompt or "").encode("utf-8")).hexdigest()[:20]
                _ci = int((AGENT_SESSIONS.get(sid) or {}).get("conversation_index", 0))
                _steps = int((AGENT_SESSIONS.get(sid) or {}).get("total_steps", 0))
                _cache_on = bool(AGENT_CFG.get("turn_cache_enabled", True))
                if _cache_on:
                    _act, _ans = turn_cache_decision(AGENT_SESSIONS.get_turn(sid, _ci, _ph), _steps)
                    if _act == "reuse":
                        log(f"提供商回合命中缓存（{_ph[:8]}），跳过重发")
                        return _ans
                    if _act == "probe":
                        log(f"提供商回合 {_ph[:8]} 结果未知（中断在途），改为只读核对")
                        return json.dumps({
                            "type": "tool", "tool": "list_directory", "arguments": {"path": "."},
                            "reason": "上一回合结果未知（连接中断）：先只读核对当前状态，"
                                      "不要重复可能已发生的写入/删除等副作用。"}, ensure_ascii=False)
                    try:
                        AGENT_SESSIONS.begin_turn(sid, _ci, _ph, _steps)
                    except Exception:
                        pass
                _ans = await provider_turn(sid, model, prompt, force_new=force_new)
                if _cache_on and _ans:
                    try:
                        AGENT_SESSIONS.save_turn(sid, _ci, _ph, _ans,
                                                 int((AGENT_SESSIONS.get(sid) or {}).get("total_steps", 0)))
                    except Exception:
                        pass
                return _ans
        else:
            attached = {"done": False}   # 附件只在首个回合附加一次
            async def turn(prompt,model,force_new=False):
                # 恢复/续跑可能发生在浏览器 idle 时：先确保它有页面，
                # 否则后面读 page.url / 点输入框都会 NoneType 崩（实测 resume 立刻 failed）。
                await ensure_browser_ready()
                SNAPSHOTS.next_turn(sid)   # 一次 Copilot 回合 = 一个快照版本
                # 上一次的工具调用已派发但结果未知（进程在结果落库前退出）：绝不原样重放，
                # 先让模型只读核对现状；随后把这些步骤标为 unknown，保证只提示一次。
                _pend = []
                try:
                    _pend = pending_steps_via(sid)
                except Exception:
                    _pend = []
                if _pend:
                    for _p in _pend:
                        try:
                            AGENT_SESSIONS.finish_step(sid, int(_p.get('conversation_index') or 0),
                                                       int(_p.get('step_no') or 0), 'unknown')
                        except Exception:
                            pass
                    _names = ', '.join(sorted({str(_p.get('tool') or '?') for _p in _pend}))
                    log(f'检测到 {len(_pend)} 个结果未知的工具步骤（{_names}），先只读核对不重放')
                    return json.dumps({
                        'type': 'tool', 'tool': 'list_directory', 'arguments': {'path': '.'},
                        'reason': f'上一次工具调用（{_names}）的结果未知：先只读核对当前状态，'
                                  '不要重复可能已发生的写入/删除/发送等副作用；确认后再继续原计划。'},
                        ensure_ascii=False)

                clear_interrupt(sid)       # 新回合：上一轮的插入信号已由 pending 承接
                st=AGENT_SESSIONS.get(sid)
                make_new = force_new or not st.get("current_url")
                if not make_new:
                    # 页面可能因服务重启/窗口切换被重置到基础地址：续聊前若当前
                    # 页面不是该会话的 Copilot 对话，先导航回已保存的会话 URL，
                    # 避免回合误入一个全新的空对话。
                    expected = st.get("current_url") or ""
                    m = CONVERSATION_URL_RE.search(expected)
                    if m and m.group(0) not in page_url_safe():
                        await page.goto(expected, wait_until="domcontentloaded", timeout=120000)
                        await asyncio.sleep(1)
                        _forget_selected_model()
                if attach_paths and not attached["done"]:
                    _ok_att, _bad_att = [], []
                    for p in attach_paths:
                        try:
                            await attach_file_to_copilot(p)
                            _ok_att.append(p.name)
                        except Exception as exc:
                            log(f"附件 {p.name} 上传失败: {exc}")
                            _bad_att.append(f"{p.name}: {exc}")
                    attached["done"] = True
                    # 写进时间线：图片没送进 Copilot 时，用户和后续排查都能看到，
                    # 而不是只躺在服务端日志里（这正是"贴图识别失败"最难查的地方）。
                    try:
                        _sn = int((AGENT_SESSIONS.get(sid) or {}).get("total_steps", 0)) + 1
                        _cix = int((AGENT_SESSIONS.get(sid) or {}).get("conversation_index", 0))
                        AGENT_SESSIONS.add_step(sid, _sn, _cix, {
                            "step": _sn, "tool": "attach_image",
                            "status": "ok" if (_ok_att and not _bad_att) else
                                      ("error" if _bad_att and not _ok_att else "warning"),
                            "reason": f"附加图片 {len(_ok_att)}/{len(attach_paths)} 张到 Copilot 输入框",
                            "result": {"attached": _ok_att, "failed": _bad_att}})
                    except Exception:
                        pass
                marker = _agent_chat_marker(sid, int(st.get("conversation_index", 0)))
                # Copilot derives the sidebar title from the beginning of the first
                # message. This is more stable than automating the Rename editor.
                if make_new:
                    # 新任务：明确要求忽略此前对话话题——实测 Copilot 新对话会
                    # 沿用上一条聊天的话题，把新任务带到别的项目上（窜任务）。
                    send_prompt = (marker + "\n【新任务】忽略此前任何对话的话题与进度，"
                                   "只按下述内容执行。\n" + prompt)
                else:
                    send_prompt = prompt
                remember_initial_prompt(sid, send_prompt)   # 与实际发送内容一致
                # ── 回合结果缓存（P1-1）：同一会话/对话/提示词的回合若已真正发出并落过步数，
                # 恢复或重试时直接复用答案，不再重发（省一次远端回合 = 省时间与额度）。
                # 键里包含附件指纹（见 turn_cache_key）：换一张新截图不会被旧缓存顶掉
                _ph = turn_cache_key(send_prompt, attach_paths)
                _ci = int((AGENT_SESSIONS.get(sid) or {}).get("conversation_index", 0))
                _cache_on = bool(AGENT_CFG.get("turn_cache_enabled", True))
                if _cache_on:
                    _hit = AGENT_SESSIONS.get_turn(sid, _ci, _ph)
                    _done = int((AGENT_SESSIONS.get(sid) or {}).get("total_steps", 0))
                    _act, _ans = turn_cache_decision(_hit, _done)
                    if _act == "reuse":
                        log(f"回合结果命中缓存（{_ph[:8]}），跳过重发")
                        return _ans
                    if _act == "probe":
                        # 已派发但结果未知：绝不重发（可能重复副作用），先只读核对
                        log(f"回合 {_ph[:8]} 结果未知（中断在途），改为只读核对后再继续")
                        return json.dumps({
                            "type": "tool", "tool": "list_directory", "arguments": {"path": "."},
                            "reason": "上一回合在执行途中被中断，其结果未知：先只读核对当前文件与状态，"
                                      "不要重复可能已发生的写入/删除等副作用；确认后再继续原计划。"},
                            ensure_ascii=False)

                if _cache_on:
                    try:
                        AGENT_SESSIONS.begin_turn(sid, _ci, _ph,
                                                  int((AGENT_SESSIONS.get(sid) or {}).get("total_steps", 0)))
                    except Exception:
                        pass
                try:
                    ans=await agent_chat_once(send_prompt,model,make_new,pause_check=pause_check,
                                              interrupt_check=lambda: interrupt_pending(sid))
                    if _cache_on and ans:
                        try:
                            AGENT_SESSIONS.save_turn(sid, _ci, _ph, ans,
                                                    int((AGENT_SESSIONS.get(sid) or {}).get("total_steps", 0)))
                        except Exception:
                            pass

                except AgentPaused:
                    # 回合已发送到 Copilot：保存会话 URL，恢复后继续同一聊天，
                    # 避免暂停-恢复后重复创建新会话。
                    if make_new and page_url_safe():
                        paused_url = await stable_conversation_url(timeout=6)
                        if not paused_url:
                            _cand = page_url_safe()
                            paused_url = _cand if CONVERSATION_URL_RE.search(_cand) else ""
                        stp=AGENT_SESSIONS.get(sid); urls=list(stp.get("conversation_urls") or [])
                        if paused_url and paused_url not in urls: urls.append(paused_url)
                        if paused_url:
                            AGENT_SESSIONS.patch(sid,current_url=paused_url,conversation_urls=urls)
                    raise
                # 等 Copilot 把会话 ID 路由进 URL 再保存；无会话 ID 的基础地址
                # 不能当会话身份保存（下回合会误入当前打开的任意会话）。
                final_url = await stable_conversation_url(timeout=15)
                if not final_url:
                    _cand = page_url_safe()
                    final_url = _cand if CONVERSATION_URL_RE.search(_cand) else ""
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
        def save(**kw):
            # 状态一旦离开 waiting，就清掉审批残留。否则会话跑完（completed/failed）后
            # approval_pending 还留着，UI 会一直显示「需要授权」横幅——实测：点「暂不授权」
            # 只是本地隐藏，下一秒 runtime 刷新又把它弹回来；残留也会让巡检/投影看到假状态。
            if "state" in kw and kw["state"] != "waiting" and "approval_pending" not in kw:
                kw["approval_pending"] = None
            return AGENT_SESSIONS.patch(sid,**kw)
        def add_step(x):
            st=get(); AGENT_SESSIONS.add_step(sid,int(x.get("step",st["total_steps"]+1)),int(st["conversation_index"]),x)
            try:
                PLUGINS.emit("step_done", session_id=sid, tool=x.get("tool"),
                             status=x.get("status"), reason=x.get("reason") or "")
            except Exception:
                pass
        def add_file(x): AGENT_SESSIONS.add_file_event(sid,x)
        def add_conv_stat(x): AGENT_SESSIONS.add_conv_stat(sid,x)
        # 写操作前捕获原像：工作区改动与自升级改动分作用域，均可整任务回滚
        T.set_snapshot_hook(self_snapshot_hook(sid))
        T.self_rollback_fn = lambda: SNAPSHOTS.restore(self_scope(sid), BASE)
        runner=AgentRunner(T,turn,AGENT_CFG,get,save,add_step,add_file,add_conv_stat,
                        step_begin=lambda no, tool, h: (
                            AGENT_SESSIONS.begin_step(sid, _conv_index(sid), no, tool, h),
                            # 让界面显示"正在执行 X"，而不是停在上一轮的 waiting
                            runtime_update(sid, phase="executing",
                                           detail=f"正在执行第 {no} 步 · {tool}",
                                           step_started=time.time()),
                            # 工作区 TODO 文件变了就同步进待办面板（mtime 未变是空操作）
                            sync_session_todos(sid),
                            nudge_todo_update(sid, no)),
                        step_finish=lambda no, st='done': (
                            AGENT_SESSIONS.finish_step(sid, _conv_index(sid), no, st),
                            runtime_update(sid, phase="thinking", detail=f"第 {no} 步已完成（{st}）")),
                        approval_check=lambda tool,h: AGENT_SESSIONS.take_approval(sid, tool, h))
        try:
            PLUGINS.emit("session_start", session_id=sid, task=effective_task, model=req.model)
            # 按 todo 自动派子代理（默认关：config.agent.auto_team_enabled）。
            # 每个子代理都是一条独立 Copilot 对话 = 成倍额度，所以必须显式打开。
            try:
                from .auto_team import maybe_dispatch

                _n = await maybe_dispatch(AGENT_SESSIONS, sid, AGENT_CFG)
                if _n:
                    log(f"按待办自动派发 { _n } 个子代理")
            except Exception as exc:      # 自动派发失败绝不影响主任务
                log(f"自动派发失败（忽略）：{exc}")
            # 用户任务先落库：**必须在跑之前**——消息与步骤在实时流里按时间穿插，
            # 跑完再写会让"用户提问"排到它自己触发的几十个步骤后面（顺序就乱了）。
            try:
                _conv = _conv_index(sid)
                _have = AGENT_SESSIONS.messages(sid) or []
                if not any(m.get("role") == "user" for m in (_have or [])):
                    AGENT_SESSIONS.add_message(sid, "user", effective_task, _conv)
            except Exception as exc:
                log(f"落用户消息失败（忽略）：{exc}")
            result = await runner.run(effective_task,req.model)
            try:
                if (result.content or "").strip():
                    AGENT_SESSIONS.add_message(sid, "assistant", result.content,
                                               _conv_index(sid))
            except Exception as exc:
                log(f"落助手消息失败（忽略）：{exc}")
            # 父任务收尾：仍在跑的子代理没人收结果，直接取消（否则面板一直显示 running）
            try:
                _n = AGENT_SESSIONS.cancel_running_children(sid, "父任务已完成")
                if _n:
                    log(f"父任务 {sid} 完成，取消 { _n } 个仍在运行的子代理")
            except Exception:
                pass
            PLUGINS.emit("session_end", session_id=sid, content=result.content,
                         steps=len(result.steps), state="completed")
            runtime_update(sid, phase="completed", detail=result.content, answer_preview=result.content)
            SNAPSHOTS.prune()
            return result, sid
        except AgentPaused:
            # 用户暂停不是失败：写 failed 会让 AgentRunner 的停放逻辑（只认
            # paused）失效、重启后的 auto_resume（只捞 paused）也捞不回来，
            # 于是"暂停"在回合等待期间等于把会话卡死。
            AGENT_SESSIONS.patch(sid, state="paused", last_error=None)
            runtime_update(sid, phase="paused", detail="用户暂停，可从检查点恢复")
            raise
        except Exception as exc:
            runtime_update(sid, phase="failed", detail=str(exc))
            AGENT_SESSIONS.patch(sid,state="failed",last_error=str(exc))
            SNAPSHOTS.prune()  # 失败/取消路径也要按保留数回收，避免只增不减
            raise
        finally:
            T.set_snapshot_hook(None)
            T.self_rollback_fn = None
            T.set_web_copilot_tools(True)   # 复位，避免影响后续会话
            # 恢复进入前的值而非置 None：排队任务接管时不会被误清
            ACTIVE_SESSION_ID = prev_active

def _agent_chat_marker(session_id: str, conversation_index: int) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]+", "-", str(session_id)).strip("-") or "session"
    return f"[CBP-AGENT][{safe[:36]}][C{int(conversation_index):03d}]"




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













async def plain_chat_turn(sid: str, content: str, model_id: str, attach_paths: list | None = None) -> dict:
    """普通对话回合：在 Session 绑定的 Copilot 会话里问答，不跑工具循环。

    由 continue_session_message（跟进）和 plain_completions（新任务）共用。
    调用方负责写入 user 消息；本函数写入 assistant 回复并维护会话 URL。
    attach_paths 为已落盘的图片/截图附件，发送前附加到 Copilot 输入框。
    """
    global ACTIVE_SESSION_ID
    state = AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    async with _window(sid) as (win, T):
        prev_active = ACTIVE_SESSION_ID
        ACTIVE_SESSION_ID = sid
        runtime_update(sid, phase="manual_sending", detail="正在发送普通对话", prompt_preview=content, answer_preview="")
        url = state.get("current_url")
        make_new = not bool(url)
        if url and page.url != url:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=120000)
                await asyncio.sleep(1)
                _forget_selected_model()  # 会话切换后页面上选中的模型可能不同
            except Exception:
                make_new = True
        previous = "" if make_new else (await current_answer()).strip()
        try:
            # 粘贴的图片/截图附件：发送前附加到 Copilot 输入框（失败不阻断文本发送）
            for p in (attach_paths or []):
                try:
                    await attach_file_to_copilot(p)
                except Exception as exc:
                    log(f"附件 {p.name} 上传失败: {exc}")
            # 新会话把 CBP-AGENT 标记放进首条消息：Copilot 用它做侧栏标题，
            # 也是日后远程删除时的同属校验依据（与 Agent 模式一致）。
            marker = _agent_chat_marker(sid, int(state.get("conversation_index", 0)))
            send_content = (marker + "\n" + content) if make_new else content
            # 追加唯一回合标记：虚拟化列表里"You said"计数不可靠，
            # sent_successfully 用标记确认送达，避免误判重发。
            send_content = send_content.rstrip() + f"\n\n[CBP-TURN:{uuid.uuid4().hex[:12]}]"
            await send(send_content, make_new, model_id)
            answer = (await wait_agent_answer(previous, sent=content)).strip()
            # 显示/存储用 markdown（保留代码块/列表/标题/粗体）；纯文本只用于回合判定。
            # 失败时回退纯文本，保证对话总能落库。
            # 先等 DOM 稳定：否则代码块还没渲染出来，取到的 markdown 缺代码块
            await _wait_dom_stable()
            display = (await answer_markdown(answer)) or answer
            current = AGENT_SESSIONS.get(sid)
            if not current:
                raise RuntimeError("会话在执行期间被删除")
            chat_url = await stable_conversation_url(timeout=10)
            if not chat_url:
                _cand = page_url_safe()
                chat_url = _cand if CONVERSATION_URL_RE.search(_cand) else ""
            urls = list(current.get("conversation_urls") or [])
            if chat_url and chat_url not in urls: urls.append(chat_url)
            AGENT_SESSIONS.add_message(sid, "assistant", display, int(current.get("conversation_index", 0)))
            if chat_url:
                AGENT_SESSIONS.patch(sid, current_url=chat_url, conversation_urls=urls, model=model_id)
            else:
                AGENT_SESSIONS.patch(sid, conversation_urls=urls, model=model_id)
            runtime_update(sid, phase="manual_completed", answer_preview=display[-12000:], detail="普通对话完成")
            return {"session_id":sid,"role":"assistant","content":answer,"url":page.url,"mode":"chat"}
        finally:
            ACTIVE_SESSION_ID = prev_active


async def plain_completions(req: AgentRequest):
    """普通对话模式发起新任务：创建 Session 但不启动 Agent 工具循环。"""
    if not AGENT_CFG.get("enabled", True): raise HTTPException(503, "Agent 功能已禁用")
    model_id = normalize_model(req.model)
    contents = [m.content.strip() for m in req.messages if m.role == "user" and m.content.strip()]
    if not contents: raise HTTPException(400, "至少需要一条 user 消息")
    content = contents[-1]
    attach_paths = save_image_attachments(req.attachments)
    sid = req.session_id or ("task-" + uuid.uuid4().hex[:12])
    if not AGENT_SESSIONS.get(sid):
        workspace = (req.workspace.strip() if req.workspace else None) or str(AGENT_WORKSPACE)
        AGENT_SESSIONS.create(sid, content, workspace, model_id)
    AGENT_SESSIONS.add_message(sid, "user", content, 0)
    pm = providers_mod.split_provider_model(model_id)
    if pm and pm[0] in PROVIDERS:
        result = await provider_session_chat(sid, content, model_id)
    else:
        result = await plain_chat_turn(sid, content, model_id, attach_paths=attach_paths)
    return {"id": "chat-" + uuid.uuid4().hex, "object": "chat.completion",
            "created": int(time.time()), "model": model_id,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": result["content"]},
                         "finish_reason": "stop"}],
            "session_id": sid, "mode": "chat"}


def save_workspace_file(rel: str, data: bytes) -> str:
    """把二进制写进工作区（插件用；复用路径沙箱），返回相对路径。"""
    target = AGENT_TOOLS.sandbox_path(str(rel or ""))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    try:
        return str(target.relative_to(AGENT_TOOLS.root))
    except ValueError:
        return str(target)


def create_directory(parent: str, name: str) -> dict:
    """在 parent 下创建子目录（工作目录选择器的「新建目录」）。

    只允许**单层目录名**（拒绝分隔符/..），避免借选择器在任意位置递归建树。
    """
    parent_path = str(parent or "").strip() or str(AGENT_TOOLS.root)
    name = str(name or "").strip()
    if not name:
        raise ToolError("目录名不能为空")
    if any(ch in name for ch in ("/", "\\", ":", "*", "?", '"', "<", ">", "|")):
        raise ToolError("目录名不能包含路径分隔符或非法字符")
    if name in (".", ".."):
        raise ToolError("目录名非法")
    base = Path(parent_path)
    if not base.is_dir():
        raise ToolError(f"父目录不存在: {parent_path}")
    target = base / name
    if target.exists():
        raise ToolError(f"目录已存在: {target}")
    try:
        target.mkdir()
    except OSError as exc:
        raise ToolError(f"创建目录失败: {exc}") from exc
    return {"path": str(target), "name": name, "parent": str(base)}


# ── 工作目录选择器：系统原生对话框（对应 DSH 的 native 后端）──────────
async def pick_directory_native(start: str | None = None) -> dict:
    """弹系统文件夹对话框（Windows），返回用户选中的路径。

    对应 DSH 的 `dsh-host-directory-picker-native`（它用 COM IFileOpenDialog；
    这里用 PowerShell 的 FolderBrowserDialog，效果等价且无需 ctypes 手写 COM）。
    对话框在服务所在桌面弹出：本地用户能看到；非交互会话会超时并让前端回退到
    browse 模式（与 DSH 的 auto 行为一致）。
    """
    import subprocess as _sp

    base = str(start or "").strip() or str(AGENT_TOOLS.root)
    safe = base.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog;"
        f"$f.Description = '选择工作目录';"
        f"$f.SelectedPath = '{safe}';"
        "$f.ShowNewFolderButton = $true;"
        "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK)"
        " { Write-Output ('CBP_PICK=' + $f.SelectedPath) }")
    exe = shutil.which("powershell") or "powershell"
    loop = asyncio.get_running_loop()

    def _run() -> tuple[int, str]:
        proc = _sp.run([exe, "-NoProfile", "-STA", "-Command", script],
                       capture_output=True, text=True, timeout=15,
                       creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    try:
        rc, out = await loop.run_in_executor(None, _run)
    except Exception as exc:
        raise ToolError(f"无法打开系统目录对话框（{exc}）；可改用应用内浏览") from exc
    m = re.search(r"CBP_PICK=(.+)", out or "")
    if not m:
        return {"path": "", "cancelled": True}
    chosen = m.group(1).strip()
    if not chosen or not Path(chosen).is_dir():
        return {"path": "", "cancelled": True}
    return {"path": str(Path(chosen)), "cancelled": False}


# ── 工作目录选择器：目录浏览（只列子目录，供 WebUI 选择器）──────────
def browse_directories(path: str | None = None) -> dict:
    """列出目录下的**子目录**，供 WebUI 的工作目录选择器。

    不传 path 时从当前工作区开始（默认 workspace）；不列隐藏/依赖目录，
    单层最多 300 项（防止 huge 目录把界面撑爆）。
    """
    start = str(path or "").strip()
    base = Path(start).expanduser() if start else Path(AGENT_TOOLS.root)
    try:
        base = base.resolve()
    except Exception:
        base = Path(AGENT_TOOLS.root).resolve()
    if not base.is_dir():
        base = Path(AGENT_TOOLS.root).resolve()
    skip = {"__pycache__", "node_modules", "edgeprofile", ".git", "site-packages"}
    entries: list[dict] = []
    try:
        for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            try:
                if not child.is_dir():
                    continue
                name = child.name
                if name.startswith((".", "$")) or name.lower() in skip:
                    continue
                entries.append({"name": name, "path": str(child)})
            except OSError:
                continue
            if len(entries) >= 300:
                break
    except (PermissionError, OSError):
        pass
    parent = base.parent if base.parent != base else None
    return {"path": str(base), "parent": str(parent) if parent else "",
            "entries": entries, "default": str(AGENT_TOOLS.root),
            "is_default": str(base) == str(AGENT_TOOLS.root)}


# ── 深研一键任务（WebUI「深研」按钮）──────────────────────────────────
async def deep_research_task(query: str, model: str = "", angles: int = 3,
                             fetch_top: int = 3, lang: str = "zh") -> dict:
    """一键深研：插件收集证据包 → **一次直接调用模型**综合成带来源的报告。

    为什么不走 Agent 循环：实测走循环时模型会把"写报告"变成**分块写文件**
    （16 步、11 分钟）。深研的综合是一次性文本生成，直连模型即可（提供商走
    complete，Copilot 网页走单个聊天回合），并把结果落成会话 + 工作区 md 文件。
    """
    query = str(query or "").strip()
    if not query:
        raise ToolError("研究问题不能为空")
    fn = PLUGINS.plugin_function("research", "deep_research")
    if fn is None:
        raise ToolError("research 插件未启用：WebUI → 更多 → 插件管理 里启用它")
    try:
        pack = await fn(query, angles=max(1, min(int(angles or 3), 5)),
                        fetch_top=max(0, min(int(fetch_top or 3), 8)))
    except Exception as exc:
        raise ToolError(f"证据收集失败: {exc}") from exc
    sources = pack.get("sources") or []
    if not sources:
        raise ToolError("没有搜到可用来源；请换个说法或稍后重试")
    evidence = json.dumps(sources[:12], ensure_ascii=False)
    if len(evidence) > 40000:
        evidence = evidence[:40000] + "…（截断）"
    prompt = (
        "你是一位严谨的调研员。下面是 deep_research 收集到的证据包"
        "（JSON：title/url/snippet/excerpt）。\n"
        f"调研问题：{query}\n\n"
        f"证据包：\n{evidence}\n\n"
        "请直接输出一份 Markdown 调研报告：\n"
        "1) 开头 3-5 句结论摘要；\n"
        "2) 分点展开，每个要点标注来源链接；\n"
        "3) 结尾「来源」清单（标题 + URL）；\n"
        "4) 证据不足处明确说明，不要编造。\n"
        "只输出报告正文，不要写文件、不要调用工具。")

    use_model = normalize_model_soft(model or DEFAULT_MODEL)
    pm = providers_mod.split_provider_model(use_model)
    sid = "research-" + uuid.uuid4().hex[:10]
    # 系统生成的标题也要跟界面语言走（否则英文界面里冒出一行中文 "[深研] …"）
    tag = "[Research]" if str(lang).lower().startswith("en") else "[深研]"
    AGENT_SESSIONS.create(sid, f"{tag} {query}", str(AGENT_TOOLS.root), use_model)
    AGENT_SESSIONS.add_message(sid, "user", f"{tag} {query}", 0)
    AGENT_SESSIONS.patch(sid, state="running")
    t0 = time.time()
    try:
        if pm and pm[0] in PROVIDERS:
            text = await asyncio.wait_for(
                PROVIDERS[pm[0]].complete(pm[1], [{"role": "user", "content": prompt}]),
                timeout=float(AGENT_CFG.get("research_timeout_seconds", 300)))
        else:
            text = await asyncio.wait_for(
                agent_chat_once(prompt, use_model, True),
                timeout=float(AGENT_CFG.get("research_timeout_seconds", 600)))
            try:
                await _wait_dom_stable()
                md = await answer_markdown(text)
                if md:
                    text = md
            except Exception:
                pass
    except Exception as exc:
        AGENT_SESSIONS.patch(sid, state="failed", last_error=str(exc)[:500])
        raise ToolError(f"综合失败: {exc}") from exc
    report = (text or "").strip()
    if not report:
        AGENT_SESSIONS.patch(sid, state="failed", last_error="模型返回为空")
        raise ToolError("综合失败：模型返回为空")
    elapsed = round(time.time() - t0, 1)
    AGENT_SESSIONS.add_message(sid, "assistant", report, 0)
    AGENT_SESSIONS.patch(sid, state="completed",
                         summary={"research": True, "sources": len(sources),
                                  "elapsed": elapsed})
    # 同时落一份 md 到工作区，方便直接取用
    try:
        fname = f"research-{time.strftime('%Y%m%d-%H%M%S')}.md"
        save_workspace_file(fname, report.encode("utf-8"))
    except Exception:
        fname = ""
    return {"query": query, "session_id": sid, "report": report,
            "sources": sources, "queries": pack.get("queries") or [],
            "file": fname, "elapsed": elapsed, "error": None}


# ── 插件跑任务（定时任务/生图等插件用）────────────────────────────────
async def run_plugin_task(task: str, model: str = "", workspace: str = "",
                          prefix: str = "cron") -> dict:
    """跑一个独立 Agent 任务并返回结果（供插件调用；新会话，不打断当前任务）。"""
    task = str(task or "").strip()
    if not task:
        raise ToolError("task 不能为空")
    sid = f"{prefix}-{uuid.uuid4().hex[:10]}"
    use_model = normalize_model_soft(model or DEFAULT_MODEL)
    req = AgentRequest(model=use_model,
                       messages=[Message(role="user", content=task)],
                       stream=False, session_id=sid,
                       workspace=workspace or None)
    try:
        result, real_sid = await run_agent(req)
        return {"session_id": real_sid, "content": (result.content or "")[:4000],
                "steps": len(result.steps), "error": None}
    except Exception as exc:
        return {"session_id": sid, "content": "", "steps": 0, "error": str(exc)}


# ── 子代理引擎（subagent / ralph / workflow 的底座）────────────────────
# 子会话 = 独立 Copilot 对话（或 provider 独立历史），跑完父会话下一回合会自动
# 导航回自己的对话 URL（browser turn 的 expected URL 守卫），不污染父上下文。
_SUBAGENT_MAX_DEPTH = 1          # 防递归：子代理不能再开子代理
# 在跑的子代理：{child_sid: 开始时间}。**不用裸计数器**——实测一轮子代理被中断
# （浏览器上下文被关掉/回合被取消）时计数没回落，进程内后续所有子代理就永久报
# "不能再创建子代理"，只有重启才恢复。这里按"子会话是否仍在跑 + TTL"自愈。
_SUBAGENT_ACTIVE: dict[str, float] = {}
_SUBAGENT_STALE_SECONDS = 1800


def subagent_depth() -> int:
    """当前活跃子代理数（自愈：已完成/超时的记录自动清掉）。"""
    now = time.time()
    for sid, started in list(_SUBAGENT_ACTIVE.items()):
        state = (AGENT_SESSIONS.get(sid) or {}).get("state")
        if state not in ("running", "waiting") or now - started > _SUBAGENT_STALE_SECONDS:
            _SUBAGENT_ACTIVE.pop(sid, None)
    return len(_SUBAGENT_ACTIVE)


async def fanout(tasks, model: str = "", workspace: str = "",
                 max_steps: int = 0, parent_title: str = "") -> dict:
    """确定性扇出（实现在 cbp/fanout.py，延迟导入避免循环依赖）。"""
    from .fanout import fanout as _fanout
    return await _fanout(tasks, model, workspace, max_steps, parent_title)


async def run_subagent(task: str, description: str = "", model: str = "",
                       max_steps: int = 0) -> dict:
    """在独立子会话里跑一个 Agent 任务，返回结构化结果。"""
    # 必须声明 global：函数内会给 ACTIVE_SESSION_ID 赋值（切到子会话再恢复），
    # 不声明会变成局部变量，前面读取时抛 UnboundLocalError（实测踩到）。
    global ACTIVE_SESSION_ID
    task = str(task or "").strip()
    if not task:
        raise ToolError("task 不能为空")
    if subagent_chain_depth() >= _SUBAGENT_MAX_DEPTH:
        raise ToolError("子代理不能再创建子代理（防止无限递归）")
    # **必须用任务上下文**：模块全局 ACTIVE_SESSION_ID 会被并发任务覆盖，
    # 用它当父会话会把子代理挂到别人的会话下（实测：测试扇出的子代理挂到了
    # 用户正在跑的真实任务里，界面上莫名出现 19 个 Subagents）。
    parent_sid = current_session()
    # 必须用**任务级**注册表的 root：并行时全局 AGENT_TOOLS.root 可能是别的会话的
    parent_root = str(task_tools().root)
    child_sid = "sub-" + uuid.uuid4().hex[:10]
    parent_state = AGENT_SESSIONS.get(parent_sid) or {}
    child_model = normalize_model_soft((model or parent_state.get("model") or DEFAULT_MODEL).strip())
    pm = providers_mod.split_provider_model(child_model)
    is_provider = bool(pm and pm[0] in PROVIDERS)
    AGENT_SESSIONS.create(child_sid, task, parent_root, child_model)
    # 父子关系与短标签：子代理面板据此列出"谁派生的、在跑什么"
    AGENT_SESSIONS.patch(child_sid, parent_id=parent_sid,
                         description=(description or task)[:120])
    try:
        # 继承父会话的会话级授权（子代理是同一任务的一部分，不该重复授权）
        AGENT_SESSIONS.copy_session_approvals(parent_sid, child_sid)
    except Exception as exc:
        log(f"子会话继承父授权失败（不影响运行）：{exc}")
    return await _drive_child(child_sid, task, child_model, max_steps,
                              description=description, parent_sid=parent_sid,
                              parent_root=parent_root, is_provider=is_provider)


async def _drive_child(child_sid: str, task: str, model_id: str, max_steps: int,
                        description: str = "", parent_sid: str = "",
                        parent_root: str = "", is_provider: bool = False) -> dict:
    """在**已存在**的子会话上跑一轮 Agent（run_subagent 与 continue_subagent 共用）。

    注意不能走 run_agent：父回合**正持着自己的窗口**，再进 run_agent 会去抢同一把
    窗口锁而死锁（实测：subagent_send 卡了 600 秒直到客户端超时）。
    """
    global ACTIVE_SESSION_ID
    # 没有父任务的页面上下文（用户入口 /v1/agent/fanout、CLI）：子代理必须**自己**
    # 占一个窗口。否则 `page` 会退回共享页且不持锁，和正在跑的任务抢同一个标签页
    # （两边的输入框/回答互相覆盖）。有父上下文时不另开窗口：父任务正持着它等我们，
    # 再要一个窗口会把自己等死（窗口数=1 时必然 30s 超时）。
    _own_window = None
    if not is_provider and _PAGE_CTX.get() is None:
        try:
            _own_window = _window(child_sid)
            await _own_window.__aenter__()
        except Exception as exc:
            _own_window = None
            log(f"子代理 {child_sid} 未拿到自动化窗口: {exc}")
            AGENT_SESSIONS.patch(child_sid, state="failed", last_error=str(exc))
            return {"agent_id": child_sid, "description": (description or task)[:120],
                    "content": "", "steps": 0, "model": model_id, "error": str(exc)}
    _SUBAGENT_ACTIVE[child_sid] = time.time()
    # 任务级上下文：并发跑多个子代理时，会话 id / 网页工具开关 / 递归深度必须
    # 各自独立（全局单值会互相覆盖）。token 在 finally 里按任务复位。
    _tok_sid = _SESSION_CTX.set(child_sid)
    _tok_web = _WEB_TOOLS_CTX.set(not is_provider)
    _tok_depth = _SUBAGENT_DEPTH_CTX.set(subagent_chain_depth() + 1)
    _tok_tools = None
    try:
        AGENT_SESSIONS.patch(child_sid, state="running")
        # 子会话也要有对话记录：否则面板点进去只有步骤时间线、看不到"问了什么答了什么"
        try:
            AGENT_SESSIONS.add_message(child_sid, "user", task,
                                       _conv_index(child_sid))
        except Exception:
            pass
        ACTIVE_SESSION_ID = child_sid
        # 子代理也要有自己的注册表：工作区/沙箱/网页工具开关按它来。直接用全局
        # AGENT_TOOLS 会让子代理读写"默认工作区"，父任务选的 workspace 形同虚设。
        child_reg = task_tools().clone()
        try:
            child_reg.set_root(parent_root or str(task_tools().root), False)
        except Exception as exc:
            log(f"子代理 {child_sid} 切换工作区失败（沿用父工作区）: {exc}")
        child_reg.current_session_id = child_sid
        child_reg.set_web_copilot_tools(not is_provider)
        # 让扩展工具也看到子代理自己的注册表（否则 pwsh/search 仍用父/全局 root）
        _tok_tools = _TOOLS_CTX.set(child_reg)

        if is_provider:
            async def turn(prompt, model_id, force_new=False):
                return await provider_turn(child_sid, model_id, prompt, force_new=force_new)
        else:
            async def turn(prompt, model_id, force_new=False):
                st = AGENT_SESSIONS.get(child_sid) or {}
                make_new = force_new or not st.get("current_url")
                if not make_new:
                    expected = st.get("current_url") or ""
                    m = CONVERSATION_URL_RE.search(expected)
                    if m and m.group(0) not in page_url_safe():
                        await page.goto(expected, wait_until="domcontentloaded", timeout=120000)
                        await asyncio.sleep(1)
                        _forget_selected_model()
                marker = _agent_chat_marker(child_sid, int(st.get("conversation_index", 0)))
                send_prompt = (marker + "\n" + prompt) if make_new else prompt
                ans = await agent_chat_once(send_prompt, model_id, make_new)
                final_url = await stable_conversation_url(timeout=15)
                if final_url:
                    st2 = AGENT_SESSIONS.get(child_sid) or {}
                    urls = list(st2.get("conversation_urls") or [])
                    if final_url not in urls:
                        urls.append(final_url)
                    AGENT_SESSIONS.patch(child_sid, current_url=final_url, conversation_urls=urls)
                return ans

        def get():
            return AGENT_SESSIONS.get(child_sid)

        def save(**kw):
            if "state" in kw and kw["state"] != "waiting" and "approval_pending" not in kw:
                kw["approval_pending"] = None
            return AGENT_SESSIONS.patch(child_sid, **kw)

        def add_step(x):
            st = get()
            AGENT_SESSIONS.add_step(child_sid, int(x.get("step", st["total_steps"] + 1)),
                                    int(st["conversation_index"]), x)

        def add_file(x):
            AGENT_SESSIONS.add_file_event(child_sid, x)

        def add_conv_stat(x):
            AGENT_SESSIONS.add_conv_stat(child_sid, x)

        cfg_child = dict(AGENT_CFG)
        cfg_child["max_total_steps"] = max(3, min(int(max_steps or subagent_max_steps()), 200))
        runner = AgentRunner(child_reg, turn, cfg_child, get, save, add_step, add_file,
                             add_conv_stat,
                             step_begin=lambda no, tool, h: (
                                 AGENT_SESSIONS.begin_step(
                                     child_sid, _conv_index(child_sid), no, tool, h),
                                 sync_session_todos(child_sid)),
                             step_finish=lambda no, st='done': AGENT_SESSIONS.finish_step(
                                 child_sid, _conv_index(child_sid), no, st),
                             approval_check=lambda tool, h: AGENT_SESSIONS.take_approval(
                                 child_sid, tool, h))
        try:
            result = await runner.run(task, model_id)
            try:
                AGENT_SESSIONS.add_message(child_sid, "assistant",
                                           str(result.content or ""), _conv_index(child_sid))
            except Exception:
                pass
            AGENT_SESSIONS.patch(child_sid, state="completed")
            return {"agent_id": child_sid, "description": (description or task)[:120],
                    "content": result.content, "steps": len(result.steps),
                    "model": model_id, "error": None}
        except Exception as exc:
            AGENT_SESSIONS.patch(child_sid, state="failed", last_error=str(exc))
            return {"agent_id": child_sid, "description": (description or task)[:120],
                    "content": "", "steps": 0, "model": model_id, "error": str(exc)}
    finally:
        _SUBAGENT_ACTIVE.pop(child_sid, None)
        if _own_window is not None:
            try:
                await _own_window.__aexit__(None, None, None)
            except Exception:
                pass
        # 先复位任务级上下文（并行的正确性靠它），再复位全局展示值
        try:
            if _tok_tools is not None:
                _TOOLS_CTX.reset(_tok_tools)
            _SESSION_CTX.reset(_tok_sid)
            _WEB_TOOLS_CTX.reset(_tok_web)
            _SUBAGENT_DEPTH_CTX.reset(_tok_depth)
        except Exception:
            pass
        ACTIVE_SESSION_ID = parent_sid
        AGENT_TOOLS.current_session_id = parent_sid or ""
        # 不再改全局 root：子代理用的是自己的 child_reg；改全局会把并行中的
        # 其它会话/接口看到的"当前工作区"改成父任务的工作区。
        AGENT_TOOLS.set_web_copilot_tools(True)


async def continue_subagent(session_id: str, message: str, model: str = "",
                            max_steps: int = 0) -> dict:
    """继续一个已存在的子代理会话（同一上下文，追加一轮）。

    与 subagent 的区别：subagent 每次新建会话；这里**复用**子会话，模型能接着说，
    适合"先让它查，再根据结果追问"的两段式协作。
    """
    sid = str(session_id or "").strip()
    child = AGENT_SESSIONS.get(sid)
    if not child:
        raise ToolError(f"没有会话 {sid}")
    if not str(child.get("parent_id") or "").strip():
        raise ToolError(f"{sid} 不是子代理会话（parent_id 为空），请用普通任务继续它")
    msg = str(message or "").strip()
    if not msg:
        raise ToolError("message 不能为空")
    if subagent_chain_depth() >= _SUBAGENT_MAX_DEPTH:
        raise ToolError("子代理不能在子代理里继续（防止递归）")
    model_id = normalize_model_soft((model or child.get("model") or DEFAULT_MODEL).strip())
    pm = providers_mod.split_provider_model(model_id)
    is_provider = bool(pm and pm[0] in PROVIDERS)
    # 关键：**不能**调 run_agent——父回合持着全局锁，再抢锁会死锁
    # （实测 subagent_send 卡 600 秒）。走与 run_subagent 相同的 _drive_child。
    return await _drive_child(sid, msg, model_id, max_steps,
                              description=str(child.get("description") or ""),
                              parent_sid=str(child.get("parent_id") or ""),
                              parent_root=str(task_tools().root),
                              is_provider=is_provider)


async def fork_subagent(session_id: str, description: str = "",
                        keep_messages: int = 60) -> dict:
    """从某个会话派生出新的子代理（复制最近的对话，用于"另一个方案"分支）。

    新会话的 parent_id 指向源会话的父（若源本身就是顶层会话，则指向源本身），
    因此它仍出现在子代理面板里，不会污染父会话上下文。
    """
    src_sid = str(session_id or "").strip()
    src = AGENT_SESSIONS.get(src_sid)
    if not src:
        raise ToolError(f"没有会话 {src_sid}")
    new_sid = "sub-" + uuid.uuid4().hex[:10]
    keep = max(0, min(int(keep_messages or 60), 400))
    msgs = AGENT_SESSIONS.messages(src_sid, limit=keep)
    AGENT_SESSIONS.create(new_sid,
                          src.get("original_task") or f"fork of {src_sid}",
                          src.get("workspace") or str(AGENT_TOOLS.root),
                          src.get("model") or DEFAULT_MODEL)
    AGENT_SESSIONS.patch(new_sid,
                         parent_id=str(src.get("parent_id") or src_sid),
                         description=(description or f"分叉自 {src_sid}")[:120])
    copied = 0
    for item in msgs:
        role = str(item.get("role") or "")
        if role not in {"user", "assistant", "system"}:
            continue
        try:
            AGENT_SESSIONS.add_message(new_sid, role, str(item.get("content") or ""),
                                       int(item.get("conversation_index") or 0))
            copied += 1
        except Exception:
            continue
    try:
        AGENT_SESSIONS.copy_session_approvals(src_sid, new_sid)
    except Exception:
        pass
    return {"agent_id": new_sid, "source": src_sid, "messages": copied,
            "description": description or f"分叉自 {src_sid}"}


async def list_subagents(parent_id: str = "", limit: int = 50) -> dict:
    """列出子代理会话（可按父会话过滤），带状态与步数。"""
    rows = AGENT_SESSIONS.list_subagents(parent_id or None,
                                         limit=max(1, min(int(limit or 50), 200)))
    return {"subagents": [
        {"agent_id": x.get("id"), "parent_id": x.get("parent_id"),
         "description": x.get("description"), "state": x.get("state"),
         "model": x.get("model"), "total_steps": x.get("total_steps"),
         "updated": x.get("updated")}
        for x in rows]}



async def run_ralph(objective: str, max_rounds: int = 5, model: str = "",
                    max_steps_per_round: int = 0) -> dict:
    """目标迭代：每轮用**全新子代理**推进目标，直到子代理声明完成或用尽轮次。"""
    objective = str(objective or "").strip()
    if not objective:
        raise ToolError("objective 不能为空")
    rounds = max(1, min(int(max_rounds or 5), 20))
    handoff = ""
    history = []
    for i in range(rounds):
        task = (f"目标：{objective}\n\n这是第 {i + 1}/{rounds} 轮迭代。"
                + (f"上一轮交接：\n{handoff}\n\n" if handoff else "")
                + "推进目标。**如果目标已完全达成**，答复第一行写 GOAL_COMPLETE，然后给结论；"
                  "否则第一行写 GOAL_CONTINUE，并简要说明下一步交给下一轮。")
        res = await run_subagent(task, description=f"ralph 第 {i + 1} 轮",
                                 model=model, max_steps=max_steps_per_round)
        content = str(res.get("content") or "")
        history.append({"round": i + 1, "agent_id": res.get("agent_id"),
                        "status": "complete" if "GOAL_COMPLETE" in content[:200] else "continue",
                        "excerpt": content[:500], "error": res.get("error")})
        if "GOAL_COMPLETE" in content[:200]:
            return {"objective": objective, "rounds": history, "complete": True}
        handoff = content[:2000]
        if res.get("error"):
            break
    return {"objective": objective, "rounds": history, "complete": False}


async def run_workflow(spec: dict, model: str = "") -> dict:
    """按阶段编排子代理（顺序执行：浏览器通道只有一个页面，不能真并发）。"""
    if not isinstance(spec, dict):
        raise ToolError("workflow 需要对象：{name, phases:[{title, agents:[{prompt,label}]}]}")
    phases = spec.get("phases") or []
    if not isinstance(phases, list) or not phases:
        raise ToolError("workflow.phases 不能为空")
    results = []
    for phase in phases[:10]:
        if not isinstance(phase, dict):
            continue
        title = str(phase.get("title") or "")
        agents = phase.get("agents") or []
        for a in (agents or [])[:8]:
            if isinstance(a, str):
                prompt, label = a, title
            elif isinstance(a, dict):
                prompt = str(a.get("prompt") or "")
                label = str(a.get("label") or title)
            else:
                continue
            if not prompt.strip():
                continue
            res = await run_subagent(prompt, description=f"{title}｜{label}"[:120], model=model)
            results.append({"phase": title, "label": label,
                            "agent_id": res.get("agent_id"),
                            "content": res.get("content", ""), "error": res.get("error")})
    return {"workflow": str(spec.get("name") or ""), "results": results}


# ── 对抗式审计（adversarial audit）────────────────────────────────────
# 用户："安排两个或以上的 subagent 对抗性审查完成的项目，再安排一个裁判，
#        直至仅剩较小问题才算完成。"
#
# 编排：每一轮 N 名审计员（不同角度）各自出 JSON 问题清单 → 裁判核验证据
# （自己 read_text 打开文件与行号）、合并去重、重新分级 → 只剩 p2 或没有
# 问题（verdict=clean/minor）才收敛；否则可选自动修复后再审一轮。
#
# 收敛与资源保护：rounds ≤ 8、auditors ≤ 6；网页通道顺序跑（窗口数 1 时
# 排队等窗口的代价大于并行收益），提供商通道才并行（上限 agent.subagent_parallel）。

_AUDIT_PERSPECTIVES = [
    ("correctness", "正确性：逻辑错误、边界条件、空值/缺省、并发与时序、回滚完整性"),
    ("robustness", "健壮性：异常路径、超时与重试、资源释放、失败降级"),
    ("security", "安全：越权与注入、路径逃逸、凭据泄露、未校验输入"),
    ("consistency", "一致性：与项目约定/文档/测试的一致、命名、i18n、代码风格"),
]

_AUDIT_JSON_RULE = """最终输出**只输出**一个 JSON 数组（不要 markdown 围栏、不要解释）：
[{"title":"一句话问题","severity":"p0|p1|p2","evidence":{"file":"相对路径","lines":"如 120-135"},"suggestion":"怎么修"}]
没有问题就输出 []。不要在 JSON 外面写任何文字。"""


def _audit_parse(text: str):
    """容忍解析模型输出里的 JSON：剥 markdown 围栏，从第一个 { 或 [ 起 raw_decode。"""
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    dec = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch in "{[":
            try:
                return dec.raw_decode(t, i)[0]
            except Exception:
                continue
    return None


def _audit_verdict_of(issues: list) -> str:
    if not issues:
        return "clean"
    return ("minor" if all(str(i.get("severity") or "") == "p2" for i in issues) else "major")


async def adversarial_audit(target: str = "", objective: str = "", auditors: int = 3,
                            max_rounds: int = 3, auto_fix: bool = True, model: str = "",
                            session_id: str = "") -> dict:
    """对抗式审计主循环：审计员(并行) → 裁判核验 → 修复 → 再审，直至仅剩小问题。

    裁判会**亲自核验证据**（read_text 打开文件与行号），合并去重并重新分级，
    从而抑制审计员"幻觉问题"。返回结构化结果，并把每轮报告写进父会话消息。
    """
    objective = str(objective or "").strip() or "审查当前工作区的本次改动"
    target = str(target or "").strip()
    if not target:
        target = "当前工作区（" + str(task_tools().root) + "）"
    auditors = max(2, min(int(auditors or 3), 6))
    max_rounds = max(1, min(int(max_rounds or 3), 8))

    if not session_id:
        session_id = "audit-" + uuid.uuid4().hex[:10]
    sid = session_id
    if not AGENT_SESSIONS.get(sid):
        AGENT_SESSIONS.create(sid, "对抗式审计: " + objective[:80],
                              str(task_tools().root), model or DEFAULT_MODEL)
    AGENT_SESSIONS.add_message(
        sid, "user",
        f"对抗式审计：{objective}\n范围：{target}\n"
        f"配置：{auditors} 名审计员 · 最多 {max_rounds} 轮 · 自动修复={'开' if auto_fix else '关'}",
        int((AGENT_SESSIONS.get(sid) or {}).get("conversation_index", 0)))

    perspectives = list(_AUDIT_PERSPECTIVES[:auditors])
    if len(perspectives) < auditors:            # 超过预设角度数就循环使用
        perspectives = (perspectives * ((auditors // len(perspectives)) + 1))[:auditors]

    pm = providers_mod.split_provider_model(model or "")
    parallel = bool(pm and pm[0] in PROVIDERS) and auditors > 1

    history: list[dict] = []
    fix_count = 0
    final_issues: list[dict] = []

    for rnd in range(1, max_rounds + 1):
        runtime_update(sid, phase="running",
                       detail=f"对抗式审计第 {rnd}/{max_rounds} 轮：{auditors} 名审计员各自审查")
        tasks = [
            (f"你是一名对抗式代码审计员（角度：{label}（{desc}））。\n"
             f"审计目标：{objective}\n审查范围：{target}\n"
             "要求：\n"
             "1. 只读探索，绝不修改文件（read_text / find_files / list_directory / grep）。\n"
             "2. 先列出你检查了哪些文件，再逐个报告问题。\n"
             "3. 只报**有证据**的问题：必须给出文件路径与行号；不要猜、不要泛泛。\n"
             "4. severity：p0=数据损坏/安全/服务不可用；p1=明显缺陷/错误；p2=小问题/风格。\n"
             + _AUDIT_JSON_RULE)
            for label, desc in perspectives
        ]

        results: list[dict] = []
        if parallel:
            sem = asyncio.Semaphore(max(1, subagent_parallel()))

            async def _one(i: int, task: str):
                async with sem:
                    return await run_subagent(task, description=f"审计员{i}·第{rnd}轮",
                                              model=model)

            results = list(await asyncio.gather(*(_one(i + 1, t) for i, t in enumerate(tasks))))
        else:
            for i, t in enumerate(tasks):
                results.append(await run_subagent(t, description=f"审计员{i+1}·第{rnd}轮",
                                                  model=model))

        raw_issues: list[dict] = []
        for i, res in enumerate(results):
            content = str(res.get("content") or "")
            obj = _audit_parse(content)
            items = obj if isinstance(obj, list) else []
            for it in items:
                if isinstance(it, dict) and it.get("title"):
                    raw_issues.append({**it, "auditor": perspectives[i][0]})
            if res.get("error"):
                AGENT_SESSIONS.add_step(sid, int((AGENT_SESSIONS.get(sid) or {})
                                                 .get("total_steps", 0)) + 1,
                                        int((AGENT_SESSIONS.get(sid) or {})
                                            .get("conversation_index", 0)),
                                        {"step": 0, "tool": "audit_auditor", "status": "error",
                                         "reason": f"审计员 {perspectives[i][0]} 失败",
                                         "result": {"error": str(res.get("error"))}})

        runtime_update(sid, detail=f"第 {rnd} 轮：{len(raw_issues)} 个原始问题，裁判合并核验…")

        judge_prompt = (
            "你是对抗式审计的裁判。下面 JSON 是审计员的原始清单（含 auditor 字段）。\n"
            "要求：\n"
            "1. 合并去重（同一位置/同一问题合并，保留 sources）。\n"
            "2. 对每个证据核验：用 read_text 打开对应文件与行号确认；证据不实的剔除。\n"
            "3. 重新分级 severity（p0/p1/p2）。\n"
            "4. 输出**只输出**一个 JSON 对象：\n"
            '{"issues":[{"title","severity","evidence":{"file","lines"},"suggestion","sources":[...]}],'
            '"false_positives":[{"title","why"}],"verdict":"clean|minor|major"}\n'
            "verdict 判定：没有任何 p0/p1 → minor；有 p0/p1 → major；一条问题都没有 → clean。\n\n"
            f"原始清单：\n{json.dumps(raw_issues, ensure_ascii=False)[:20000]}"
        )
        judge = await run_subagent(judge_prompt, description=f"裁判·第{rnd}轮", model=model)
        verdict_obj = _audit_parse(str(judge.get("content") or ""))
        if not isinstance(verdict_obj, dict):
            verdict_obj = {"issues": raw_issues, "false_positives": [],
                           "verdict": "major",
                           "judge_error": "裁判输出无法解析，按 major 处理（保守）"}
        issues = [x for x in (verdict_obj.get("issues") or []) if isinstance(x, dict)]
        verdict = str(verdict_obj.get("verdict") or "").lower()
        if verdict not in ("clean", "minor", "major"):
            verdict = _audit_verdict_of(issues)
        history.append({"round": rnd, "auditors": auditors,
                        "raw_issues": len(raw_issues), "issues": len(issues),
                        "verdict": verdict,
                        "false_positives": len(verdict_obj.get("false_positives") or [])})
        final_issues = issues

        report = [f"### 第 {rnd} 轮审计（裁判判定：**{verdict}**）"]
        if issues:
            for it in issues:
                ev = it.get("evidence") or {}
                report.append(
                    f"- **[{it.get('severity','?')}] {it.get('title','')}**"
                    f"（{ev.get('file','?')}:{ev.get('lines','?')}）"
                    f" {it.get('suggestion','')}")
        else:
            report.append("- 裁判确认没有问题。")
        fps = verdict_obj.get("false_positives") or []
        if fps:
            report.append(f"- 剔除误报 {len(fps)} 条："
                          + "；".join(str(x.get('title', ''))[:40] for x in fps[:5]))
        AGENT_SESSIONS.add_message(sid, "assistant", "\n".join(report), _conv_index(sid))

        if verdict != "major":
            break
        if rnd >= max_rounds or not auto_fix:
            break

        # 修复轮：修完下一轮再审
        runtime_update(sid, detail=f"第 {rnd} 轮有 p0/p1，进入修复…")
        fixer_prompt = (
            "你是对抗式审计的修复工程师。以下是裁判确认的问题（JSON）。\n"
            "要求：\n"
            "1. 逐个修复，**最小改动**，不顺手重构无关代码；\n"
            "2. 改完必须跑 validate_python + run_unittest 双通过；\n"
            "3. 最终输出一个 JSON 对象 {\"fixed\":[\"标题\"],\"failed\":[\"标题\"]}\n\n"
            f"问题清单：\n{json.dumps(issues, ensure_ascii=False)[:20000]}"
        )
        fix_res = await run_subagent(fixer_prompt, description=f"修复·第{rnd}轮", model=model)
        fix_count += 1
        AGENT_SESSIONS.add_message(sid, "assistant",
                                   f"### 修复（第 {rnd} 轮）\n"
                                   + str(fix_res.get("content") or "")[:4000],
                                   _conv_index(sid))

    complete = bool(history) and history[-1]["verdict"] in ("clean", "minor")
    AGENT_SESSIONS.add_message(
        sid, "assistant",
        ("### 审计完成\n"
         f"- 轮次：{len(history)}，修复 {fix_count} 次\n"
         f"- 收敛：{'仅剩小问题，通过' if complete else '未收敛（仍有 p0/p1）'}\n"
         f"- 最终问题数：{len(final_issues)}"),
        _conv_index(sid))
    AGENT_SESSIONS.patch(sid, state="completed")
    runtime_update(sid, phase="idle", detail="对抗式审计完成")
    return {"session_id": sid, "objective": objective, "target": target,
            "rounds": history, "fixes": fix_count, "complete": complete,
            "verdict": (history[-1]["verdict"] if history else ""),
            "issues": final_issues}


async def shutdown_browser():
    """关闭浏览器上下文与 Playwright（服务退出时调用）。"""
    global ctx, pw
    try:
        if ctx is not None:
            await ctx.close()
    except Exception:
        pass
    ctx = page = None
    try:
        if pw is not None:
            await pw.stop()
    except Exception:
        pass
    pw = None

# OWA 后端注册（模块尾部导入：owa 依赖本模块已初始化完毕的 CFG/访问器）
from . import owa as _owa_mod

AGENT_TOOLS.outlook_backend = _owa_mod.OWA_BACKEND
