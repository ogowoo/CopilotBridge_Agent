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

BASE = Path(__file__).resolve().parent.parent  # 包在 gantry/ 内，项目根目录再上一层

# 密钥与环境变量：先读系统环境变量（最高优先），再把 .env 里**没设过的**补进来；
# config.json 里的 "${VAR}" 引用随后解析。优先级：系统环境 > .env > config.json 字面量。
from . import envfile as _envfile
_envfile.load_env_file(BASE / ".env")

CFG = _envfile.resolve_placeholders(
    json.loads((BASE / "config.json").read_text(encoding="utf-8")))
_plain_secrets = _envfile.warn_plaintext_secrets(CFG)
if _plain_secrets:
    import logging as _log
    _log.getLogger("gantry").warning(
        "config.json 里有明文敏感字段（建议搬到 .env 后用 ${VAR} 引用）：%s",
        ", ".join(_plain_secrets))
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
        "ui_names": ["Auto", "自动", "自動"],
        "description": "由 Microsoft 365 Copilot 自动选择回答模式",
        "description_en": "Let Microsoft 365 Copilot pick the response mode",
    },
    "copilot-quick-response": {
        "ui_names": ["Quick response", "Quick", "快速响应", "快速",
                     "快速回應", "クイック応答", "簡単な応答", "迅速な応答"],
        "description": "优先快速响应",
        "description_en": "Prefer fast responses",
    },
    "copilot-think-deeper": {
        # 中文界面实测是「深度思考」，不是「深入思考」；日文界面是「深く考える」。
        # 多语言变体都保留，避免换语言/换租户后模型选择静默回退到 Auto。
        "ui_names": ["Think deeper", "Think Deeper", "深度思考", "深入思考",
                     "更深入思考", "深く考える", "より深く考える",
                     "深い思考", "思考を深める"],
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
                       "Copilot からの回答:", "Copilot からの返信:",
                       "Copilot 说:", "Copilot 说：", "Copilot 回复:", "Copilot 回复：",
                       "Copilot 說:", "Copilot 說：", "Copilot 回覆:", "Copilot 回覆：",
                       "Copilot 的回覆:", "Copilot 的回覆：",
                       "Copilot 的回答:", "Copilot 的回答：", "Copilot 回应:", "Copilot 回應:"],
    "user_turn": ["You said:", "あなたの発言:", "あなた:", "ユーザー:",
                  "あなたのメッセージ:", "あなたのコメント:",
                  "你说:", "你说：", "您说:", "您说：", "用户:", "用户：",
                  "你的发言:", "你的发言：", "您的发言:", "您的发言：",
                  "你說:", "你說：", "您說:", "您說：", "使用者:", "使用者：",
                  "你的發言:", "你的發言：", "您的發言:", "您的發言："],
    "answer_end": ["\nSources\n", "\n出典\n", "\n参考資料\n", "\nMessage Copilot\n",
                   "\nCopilot へのメッセージ\n", "\nCopilot にメッセージを送信する\n",
                   "\nメッセージを入力\n",
                   "\n向 Copilot 发送消息\n", "\n向 Copilot 发送訊息\n",
                   "\n向 Copilot 傳送訊息\n", "\n发消息给 Copilot\n",
                   "\n来源\n", "\n來源\n", "\n傳送訊息給 Copilot\n", "\n訊息 Copilot\n",
                   "\n相關資訊\n", "\n相關\n",
                   "\nソース\n", "\n関連情報\n",
                   "AI-generated content may be incorrect",
                   "AI 生成的内容可能不正确", "AI 生成的内容可能不准确",
                   "AI によって生成されたコンテンツは正しくない可能性があります",
                   "AI で生成されたコンテンツは誤りを含む可能性があります",
                   "AI で生成されたコンテンツは誤りを含む可能性があります。",
                   "AI 產生的內容可能不正確", "AI 產生的內容可能不正確。"],
    "sources": ["Sources", "出典", "参考資料", "ソース", "来源", "來源", "參考資料", "相關資訊"],
    "ignored_lines": ["Copy", "Like", "Dislike", "More", "Share", "Regenerate",
                      "复制", "来源", "赞", "踩", "更多", "分享", "重新生成",
                      "コピー", "高評価", "低評価", "その他", "共有", "再生成",
                      "いいね", "よくないね",
                      "複製", "讚", "踩", "更多", "分享", "重新生成", "回覆",
                      "喜歡", "不喜歡"],
}
# 选择器里用的按钮/提示文案（日文/繁体系统实测收不到回信，就是这类锚点没命中）。
SELECTOR_TEXT = {
    "stop": ["Stop generating", "Stop", "停止生成", "生成を停止", "生成を中止",
             "停止產生", "停止生成"],
    "stop_aria": ["Stop", "停止"],
    "new_chat": ["New chat", "New conversation", "新建聊天", "新しいチャット",
                 "新規チャット", "新聊天", "新增聊天", "新的聊天"],
    "send_aria": ["Send", "发送", "送信", "傳送"],
    "dismiss": ["Accept", "接受", "Got it", "知道了", "Not now", "暂不",
                "同意する", "あとで", "OK", "閉じる",
                "接受", "知道了", "稍後", "關閉",
                # 误点删除/关闭类弹窗时，Cancel 是安全出口
                "Cancel", "取消", "キャンセル"],
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
# 自升级开关住 agent 段（`agent.allow_self_upgrade`）：注册表从这里读。
# 顶层同名键是历史误放/旧配置，不往这里并（会漏进测试环境——实测炸了 13 个测试）。
AGENT_WORKSPACE = (BASE / AGENT_CFG.get("workspace", "workspace")).resolve()
AGENT_TOOLS = ToolRegistry(AGENT_WORKSPACE, AGENT_CFG)
# 全局作用域：服务安装目录的 AGENT.md / skills 对所有工作区生效
AGENT_TOOLS.set_global_root(BASE)

# ── 扩展工具（解耦模块 gantry/tools_extra.py）：搜索/网页/命令/任务清单/图片/提问 ──
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

# ── 插件系统（gantry/plugins.py，与 DSH 插件语义兼容）────────────────────
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
# 模块级副作用会污染任何 import gantry.bridge 的进程（单元测试、检查脚本、CLI）——
# 它们会把正在跑的会话全部标成 paused（"服务中断，可从检查点恢复"），
# 实测用户任务被反复打断就是这个原因。恢复只在服务启动时做（见 gantry/api.py lifespan）。

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


def pending_self_upgrade_entries() -> list[tuple[str, str]]:
    """读待生效标记：返回 [(tool, 相对路径)]。标记格式 `时间戳\t相对路径`。"""
    if not SELF_UPGRADE_MARKER.is_file():
        return []
    try:
        lines = [x for x in SELF_UPGRADE_MARKER.read_text(encoding="utf-8").splitlines() if x.strip()]
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for line in lines:
        parts = line.split("\t")
        rel = parts[-1].strip()
        if rel:
            out.append((parts[0].strip() if len(parts) > 1 else "", rel))
    return out


def validate_pending_self_upgrade() -> list[str]:
    """检查"已写入但还没重启"的自升级改动是否真的可用。

    真事（2026-10-29 07:06）：自升级任务被取消，半成品改动留在盘上——`webui/js/settings.js`
    多了 4 行读 `setTeamScopeAmp` 等元素，而那些元素从没被加进设置页；启动时只把标记
    改名为 `.applied` 并打一行"已加载上一轮自升级改动"，等于**把坏代码无声当成成功**。
    之后任何"前端 id 引用完整性"检查都会红，但用户看到的是"自升级成功了"。

    这里只做**确定性**检查（不联网、不跑浏览器）：
      · 列出的文件必须存在（防止"记录里有、盘上没有"）；
      · .py 必须能编译（语法错=必然起不来）；
      · 前端 JS 的元素 id 引用必须完整（ui_refs 静态检查，正是上面那次事故）。
    返回问题清单；空 = 可以归档。
    """
    entries = pending_self_upgrade_entries()
    if not entries:
        return []
    problems: list[str] = []
    for _tool, rel in entries:
        target = (BASE / rel)
        if not target.is_file():
            problems.append(f"{rel}: 记录里有但文件不存在")
            continue
        if target.suffix.lower() != ".py":
            continue
        try:
            compile(target.read_text(encoding="utf-8"), str(target), "exec")
        except SyntaxError as exc:
            problems.append(f"{rel}: 语法错误 第 {exc.lineno} 行 {exc.msg}")
        except Exception as exc:                        # noqa: BLE001
            problems.append(f"{rel}: 无法读取 ({exc})")
    try:
        from .ui_refs import check_ui_id_refs
        refs = check_ui_id_refs(BASE)
        for miss in (refs.get("missing") or []):
            where = ", ".join(str(x) for x in (miss.get("lines") or [])[:2])
            problems.append(f"前端引用了不存在的元素 #{miss.get('id')}（{where}）")
    except Exception as exc:                            # noqa: BLE001
        problems.append(f"前端 id 检查自身失败: {exc}")
    return problems


def mark_self_upgrade_applied() -> dict:
    """服务启动时调用：上一轮自升级的代码已被加载，把待重启标记归档。

    返回 `{"applied": [...], "issues": [...]}`；**issues 非空就不归档**——宁可让
    `restart_pending` 一直亮着，也不要把半成品标记成"已生效"（见 validate 的注释）。
    """
    entries = pending_self_upgrade_entries()
    if not entries:
        return {"applied": [], "issues": []}
    lines = [f"{tool}\t{rel}" if tool else rel for tool, rel in entries]
    issues = validate_pending_self_upgrade()
    if issues:
        log(f"[自升级] 待生效改动未通过启动自检，保留待办标记：{issues[:3]}")
        return {"applied": [], "issues": issues}
    try:
        SELF_UPGRADE_MARKER.replace(SELF_UPGRADE_MARKER.with_suffix(".applied"))
    except Exception:
        pass
    return {"applied": lines, "issues": []}


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
        # 两套前缀都认：新名字 `_self_x`，以及旧消毒逻辑产生的 `s_self_x`
        cands = SNAPSHOTS.scopes("_self_") + SNAPSHOTS.scopes("s_self_")
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
            "pending_invalid": validate_pending_self_upgrade() if pending else [],
            "pending_entries": pending[-20:], "snapshot_scopes": scopes,
            "whitelist": list(getattr(AGENT_TOOLS, "self_whitelist", ()) or ())}


# 注意：归档"待重启"标记必须由服务真正启动时执行（api.lifespan 调用
# mark_self_upgrade_applied()），不能在模块导入时执行——测试、脚本、
# 任何 import gantry.bridge 的进程都会因此误清掉"服务需重启"的标记。


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


# 距上次 todo 更新走了多少步（sid -> 步数）。超过阈值就提醒模型更一次。
_TODO_NUDGE: dict[str, int] = {}


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


# ── 拆出去的纯解析层（answer.py）：facade 重新导出，b.xxx 调用与既有测试不变 ──
# 必须放在**文件末尾**：answer.py 顶部 `from . import bridge as b`，这里 import 它时
# bridge 主体已初始化（MARKERS 等），不存在 import 期循环。
from .answer import (  # noqa: F401
    _last_turn, _ANSWER_NOISE, _THINKING_PHRASES, _strip_thinking, _clean_answer,
    _echo_only, _has_complete_agent_action, COPILOT_UNAVAILABLE_PATTERNS,
    copilot_unavailable_reason, LICENSE_PATTERNS, license_reason,
    CONVERSATION_FULL_PATTERNS, conversation_full_reason,
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
    save_image_attachments, input_box, _focus_box, wait_copilot_ready, MD_JS, body_text,
    body_text_raw, _wait_dom_stable, answer_markdown, current_answer,
    answer_after_marker, side_panel, debug_side_panel,
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


# ── 回合编排（agentrun.py）/ 下载（downloads.py）/ 工作区文件 / 研究任务 ──
from .agentrun import (  # noqa: F401
    wait_agent_answer,
    agent_chat_once,
    run_agent,
    _agent_chat_marker,
    CONVERSATION_URL_RE,
    stable_conversation_url,
    remember_initial_prompt,
    rebuild_initial_prompt,
    _conv_index,
    event_source,
    _todo_nudge_every,
    nudge_todo_update,
    sync_session_todos,
    pending_steps_via,
    turn_cache_key,
    warn_provider_cannot_attach,
    plain_chat_turn,
    plain_completions,
)
from .downloads import (  # noqa: F401
    DOWNLOAD_HINT_RE,
    _answer_area_html,
    _download_candidates,
    _resolve_href,
    pathlib_basename,
    debug_download_now,
    debug_page_snapshot,
    _save_from_href,
    _copilot_file,
)
from .workspace_files import (  # noqa: F401
    save_workspace_file,
    create_directory,
    pick_directory_native,
    browse_directories,
)
from .research import (  # noqa: F401
    deep_research_task,
    run_plugin_task,
    fanout,
)


# copilot_file 的工具注册（实现已在 downloads.py，注册放在重导出之后）
if AGENT_CFG.get("allow_copilot_file", True):
    AGENT_TOOLS.register(
        "copilot_file",
        _copilot_file,
        "让 Copilot 直接生成文件并下载到工作区（内容不走聊天消息，不受输出长度上限约束，"
        "一个大文件一个回合即可拿到；适合代码文件、Office 文档等；写完后仍需验证）",
        {"path": "工作区相对路径（决定文件名与落盘位置）", "instruction": "要生成的内容要求"},
    )
