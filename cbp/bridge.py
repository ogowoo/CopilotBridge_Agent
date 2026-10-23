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


# 上次成功确认/切换的模型。新版 Copilot 的模型按钮经常没有可读文本，
# 无法廉价确认当前模型；有缓存即可跳过"开菜单→点选项"的整段耗时操作。
SELECTED_MODEL = None


# 当前实际运行的浏览器模式（无头启动失败回退后与配置值可能不同）。
HEADLESS_CURRENT = bool(CFG.get("headless", False))


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


from .window_pool import WindowBusy, WindowPool  # noqa: E402  （放在此处避免循环导入顺序问题）
# 窗口池的工厂函数在 browser.py（第三批解耦）：这里只需要建池子用的两个
from .browser import _max_windows_cfg, _new_copilot_page  # noqa: E402

WINDOWS = WindowPool(_new_copilot_page, max_windows=_max_windows_cfg())


# 提供商模型的"逻辑槽"：不需要浏览器页，但同样要独立工具上下文，可以真并行。
# 上限跟 `agent.subagent_parallel` 对齐（否则扇出 8 个只有 4 个能跑，其余白白等 30s 超时）；
# 至少 4，保证普通提供商对话不会被子代理挤没。
SLOTS = WindowPool(lambda index: _no_page(), max_windows=max(4, subagent_parallel()))


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


async def fanout(tasks, model: str = "", workspace: str = "",
                 max_steps: int = 0, parent_title: str = "") -> dict:
    """确定性扇出（实现在 cbp/fanout.py，延迟导入避免循环依赖）。"""
    from .fanout import fanout as _fanout
    return await _fanout(tasks, model, workspace, max_steps, parent_title)


# ── 拆出去的纯解析层（answer.py）：facade 重新导出，b.xxx 调用与既有测试不变 ──
# 必须放在**文件末尾**：answer.py 顶部 `from . import bridge as b`，这里 import 它时
# bridge 主体已初始化（MARKERS 等），不存在 import 期循环。
from .answer import (  # noqa: F401
    _last_turn, _ANSWER_NOISE, _THINKING_PHRASES, _strip_thinking, _clean_answer,
    _echo_only, _has_complete_agent_action, COPILOT_UNAVAILABLE_PATTERNS,
    copilot_unavailable_reason, CONVERSATION_FULL_PATTERNS, conversation_full_reason,
    conversation_full_check, extract_latest_answer, _status_before_marker,
    strip_suggestion_tail,
)
# ── 中断信号（interrupts.py）+ 提供商通道（provider.py），同样 facade 重新导出 ──
from .interrupts import (  # noqa: F401
    _INTERRUPTS, request_interrupt, interrupt_pending, clear_interrupt,
    discard_interrupts, _await_interruptible,
)
from .provider import (  # noqa: F401
    provider_turn, provider_session_chat, discard_provider_agent_history,
    sync_provider_history_message, _PROVIDER_AGENT_HISTORY, _PROVIDER_PROTOCOL,
    _trim_provider_history, _repair_provider_history, _PROVIDER_LOCKS,
    _PROVIDER_HISTORY_MAX_CHARS,
)
# ── 消息队列（queue.py），facade 重新导出 ──
from .queue import (  # noqa: F401
    _MSG_QUEUES, _MSG_WAKE, _MSG_WORKERS, _MSG_INFLIGHT, queue_depth, queue_items,
    cancel_message_queue, _process_queued_message, enqueue_session_message,
    find_queue_item, remove_queue_item, update_queue_item, _message_worker,
)
# ── 编排层（orchestration.py）：ralph / workflow / 对抗式审计 ──
from .orchestration import (  # noqa: F401
    run_ralph, run_workflow, adversarial_audit,
    _AUDIT_PERSPECTIVES, _AUDIT_JSON_RULE, _audit_parse, _audit_verdict_of,
)
# ── 子代理（subagents.py），facade 重新导出 ──
from .subagents import (  # noqa: F401
    run_subagent, continue_subagent, fork_subagent, list_subagents, _drive_child,
    subagent_depth, _SUBAGENT_ACTIVE, _SUBAGENT_MAX_DEPTH, _SUBAGENT_STALE_SECONDS,
)

# ── 页面自动化层（page.py）：facade 重新导出，b.xxx 与既有测试不变 ──
from .page import (  # noqa: F401
    diagnostics, visible, visible_locator, dismiss, new_chat,
    get_selected_model, set_selected_model, _forget_selected_model,
    model_button, _model_id_from_label, _LABEL_NOISE, normalize_label,
    label_matches, _menu_target_locators, MENU_ITEM_ROLES, MENU_CONTAINER_SEL,
    _visible_menu_items, _submenu_hinted, _menu_label_set, _expand_submenu,
    _menu_signature, _wait_growth, _probe_submenu, dump_menu_diagnostics,
    COPILOT_URL_MARK, ensure_copilot_page, discover_models, select_model,
    attach_file_to_copilot, MAX_IMAGE_ATTACHMENTS, MAX_IMAGE_BYTES,
    save_image_attachments, input_box, wait_copilot_ready, MD_JS, body_text,
    body_text_raw, _wait_dom_stable, answer_markdown, current_answer,
    _prefer_unfolded, generating, count_user_markers, box_value,
    _body_contains_turn_marker, _wait_turn_marker, sent_successfully,
    click_send_button, send, completion_signal, updates,
)

# OWA 后端注册（模块尾部导入：owa 依赖本模块已初始化完毕的 CFG/访问器）
from . import owa as _owa_mod

AGENT_TOOLS.outlook_backend = _owa_mod.OWA_BACKEND

# ── 浏览器/窗口池层（browser.py）：facade 重新导出 ──
from .browser import (  # noqa: F401
    _launch_kwargs, _start_browser, page_url_safe, _new_copilot_page,
    _max_windows_cfg, _no_page, _window, session_running, any_window_busy,
    any_task_busy, ensure_browser_ready, shutdown_browser,
)
