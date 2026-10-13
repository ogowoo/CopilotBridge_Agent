"""Bridge 层：配置、运行状态、Playwright 浏览器自动化与 Agent 会话编排。

HTTP 路由在 api.py；本模块不定义任何 FastAPI 端点。
"""

import asyncio, json, re, time, uuid
from os.path import commonprefix
from datetime import datetime
from pathlib import Path
from typing import Literal
from fastapi import HTTPException
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field
from .agent import AgentRunner, AgentPaused, CopilotServiceUnavailable, CopilotConversationFull, validate_token_budget
from .tools import ToolRegistry, ToolError
from .session_store import SessionStore
from .snapshots import SnapshotStore
from . import providers as providers_mod

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

# API 提供商（OpenAI 兼容 HTTP API），与 Copilot 网页自动化并列的模型来源。
# 模型 ID 形如 "deepseek/deepseek-chat"，API Key 走环境变量。
PROVIDERS = providers_mod.build_providers(CFG)

# Copilot 页面可访问性文本锚点。桥接依赖这些文本定位回合与回答边界；
# 租户界面语言不同（例如中文界面）时，在 config.json 的 "markers" 中
# 追加对应文案即可——配置值会并入下方默认列表，任一命中即生效。
MARKERS = {
    "assistant_turn": ["Copilot said:"],
    "user_turn": ["You said:"],
    "answer_end": ["\nSources\n", "\nMessage Copilot\n", "AI-generated content may be incorrect"],
    "sources": ["Sources"],
    "ignored_lines": ["Copy", "Like", "Dislike", "More", "Share", "Regenerate",
                      "复制", "来源", "赞", "踩", "更多", "分享", "重新生成"],
}
for key, extra in CFG.get("markers", {}).items():
    if key in MARKERS and isinstance(extra, list):
        for value in extra:
            if isinstance(value, str) and value and value not in MARKERS[key]:
                MARKERS[key].append(value)


def _last_turn(text: str, markers: list) -> tuple:
    """Return (position, length) of the last occurring marker, or (-1, 0)."""
    best_pos, best_len = -1, 0
    for marker in markers:
        pos = text.rfind(marker)
        if pos > best_pos:
            best_pos, best_len = pos, len(marker)
    return best_pos, best_len

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
    content: str = Field(min_length=1, max_length=200000)


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
    # False 时为普通对话模式：只在该 Session 绑定的 Copilot 会话里问答，
    # 不启动 Agent 工具循环。
    agent_mode: bool = True


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


def copilot_model_available(item: dict) -> bool:
    """内置模型在菜单实况中是否可见；未成功扫描过则默认可用（兜底全显示）。"""
    if DISCOVERED_LABELS is None:
        return True
    return any(name.casefold() in DISCOVERED_LABELS for name in item["ui_names"])


def all_model_ids() -> list:
    """Copilot 网页模型 + 已配置提供商的模型。"""
    ids = list(MODELS)
    for pname, p in PROVIDERS.items():
        ids.extend(f"{pname}/{m}" for m in p.models)
    return ids


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
    # 新聊天会把模型重置为 Copilot 默认（Auto）：缓存记为 copilot-auto，
    # 注意这是页面行为而非我们的 default_model 配置。
    _forget_selected_model(reset_to="copilot-auto")


# 上次成功确认/切换的模型。新版 Copilot 的模型按钮经常没有可读文本，
# 无法廉价确认当前模型；有缓存即可跳过"开菜单→点选项"的整段耗时操作。
SELECTED_MODEL = None


def _forget_selected_model(reset_to: str | None = None):
    """Invalidate the model cache, optionally recording a known reset target.

    new_chat() resets the selector to the tenant default, so callers can pass
    DEFAULT_MODEL to keep cache hits for default-model requests.
    """
    global SELECTED_MODEL
    SELECTED_MODEL = reset_to


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


async def _visible_menu_items():
    """All currently visible model menu entries as (locator, label, is_submenu_parent)."""
    entries = []
    for role in ("option", "menuitemradio", "menuitem"):
        items = page.get_by_role(role)
        for i in range(await items.count()):
            item = items.nth(i)
            try:
                if not await item.is_visible():
                    continue
                raw_text = (await item.inner_text() or "")
                # 先取第一行再规整空白：inner_text 会把 标题/描述/已展开子项
                # 拼成多行；若先折叠空白，多行会拼成一个不存在的假模型名
                # （例如 "GPT-5.6 Sol Think deeper"），之后选择必失败。
                first_line = raw_text.splitlines()[0] if raw_text else ""
                label = re.sub(r"\s+", " ", first_line).strip()
                if not label or len(label) > 40:
                    continue
                has_popup = ((await item.get_attribute("aria-haspopup")) or "").lower() == "menu"
                entries.append((item, label, has_popup))
            except Exception:
                pass
    return entries


async def _expand_submenu(parent) -> None:
    """Open a submenu parent: hover first (Fluent UI opens on hover), click as fallback."""
    try:
        await parent.hover(timeout=3000)
        await asyncio.sleep(.4)
        return
    except Exception:
        pass
    try:
        await parent.click(timeout=3000)
        await asyncio.sleep(.4)
    except Exception:
        pass


COPILOT_URL_MARK = re.compile(r"m365\.cloud\.microsoft|/chat|copilot\.microsoft\.com", re.I)


async def ensure_copilot_page() -> str:
    """确保自动化浏览器停在 Copilot 对话页——模型菜单只在对话页存在。

    Outlook(OWA) 自动化、会话删除、普通对话都会让浏览器停在别的页面；
    不先回到对话页就直接扫模型菜单，会找不到按钮而静默失败。
    """
    if page is None:
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
    result = {"added": [], "scanned": False, "labels": [], "reason": "", "url": ""}
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
    labels = []
    expanded = set()
    leaf_labels = set()  # 叶子项（真正可选中）；父项只是容器，不算可用模型
    try:
        # 主菜单 + 最多两级子菜单；每展开一个父项立即收集新出现的选项
        for _depth in range(3):
            entries = await _visible_menu_items()
            for _item, label, has_popup in entries:
                if not has_popup:
                    leaf_labels.add(label.casefold())
                    if label not in labels:
                        labels.append(label)
            pending = [(item, label) for item, label, has_popup in entries
                       if has_popup and label not in expanded]
            if not pending:
                break
            for item, label in pending:
                expanded.add(label)
                await _expand_submenu(item)
    finally:
        for _ in range(3):  # 子菜单可能多级展开，多按几次 Escape 确保全部关闭
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(.2)
            except Exception:
                pass
    global DISCOVERED_LABELS
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
    result.update({"added": added, "scanned": True, "labels": labels})
    if not labels:
        result["reason"] = "模型菜单打开了但没有枚举到任何选项（可能是渲染延迟或前端改版）"
        await diagnostics("models_refresh_empty_menu")
    return result


async def select_model(model_id):
    global SELECTED_MODEL
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
        SELECTED_MODEL = normalized
        log(f"模型已是 {normalized} ({current_label or current_text})")
        return normalized
    other_known = {_norm(name) for mid, item in MODELS.items() if mid != normalized
                   for name in item["ui_names"]}
    if SELECTED_MODEL == normalized and not (segments & other_known):
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
        # 目标可能藏在二级子菜单（如 GPT 父项飞出更多模型）：
        # 逐个展开带 aria-haspopup 的父项后重新查找。
        entries = await _visible_menu_items()
        for parent, label, has_popup in entries:
            if not has_popup:
                continue
            await _expand_submenu(parent)
            # 子菜单展开有动画延迟，立刻找可能落空：短轮询三次
            for _try in range(3):
                option = await visible_locator(_menu_target_locators(target))
                if option:
                    break
                await asyncio.sleep(.4)
            if option:
                break
    if not option:
        try: await page.keyboard.press("Escape")
        except Exception: pass
        await diagnostics("model_option_missing")
        # 菜单结构变化或标签未收录不应打死整个回合：按当前已选模型继续，
        # 日志给出明确警告，由用户决定是否修正模型表。
        log(f"警告：模型菜单中找不到 {target_names[0]}，已按当前选中模型继续")
        return SELECTED_MODEL or normalized
    try:
        await option.click(timeout=8000)
    except Exception:
        await option.click(timeout=5000, force=True)
    await asyncio.sleep(.8)
    SELECTED_MODEL = normalized
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
    await asyncio.sleep(2)  # 等待上传与解析（附件卡片渲染）


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


async def body_text():
    try:
        return (await page.locator("body").inner_text()).replace("\r\n", "\n")
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


async def current_answer():
    return extract_latest_answer(await body_text())


async def generating():
    return bool(await visible([
        'button[aria-label*="Stop" i]',
        'button:has-text("Stop generating")',
        'button:has-text("停止生成")',
    ]))


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
    await wait_copilot_ready(90)  # 若恰逢重启后的 SSO/ADFS 回跳，先等它就绪
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
    global ctx, page, HEADLESS_CURRENT
    if ctx is not None:
        try:
            await ctx.close()
        except Exception:
            pass
        ctx = page = None
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
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    _forget_selected_model()  # 页面重载，模型选择状态未知
    return HEADLESS_CURRENT






















AGENT_CFG = CFG.get("agent", {})
AGENT_WORKSPACE = (BASE / AGENT_CFG.get("workspace", "workspace")).resolve()
AGENT_TOOLS = ToolRegistry(AGENT_WORKSPACE, AGENT_CFG)
# 全局作用域：服务安装目录的 AGENT.md / skills 对所有工作区生效
AGENT_TOOLS.set_global_root(BASE)
# 新任务缺省工作区：POST /v1/agent/workspace 会更新它
DEFAULT_WORKSPACE = str(AGENT_WORKSPACE)
# 与 AgentRunner.__init__ 的默认值保持一致，供 WebUI 显示真实 Token 预算。
TOKEN_BUDGET = int(AGENT_CFG.get("context_token_budget", 24000))
SESSION_DB = Path(AGENT_CFG.get("session_store", "data/agent.db"))
if not SESSION_DB.is_absolute(): SESSION_DB = BASE / SESSION_DB
AGENT_SESSIONS = SessionStore(SESSION_DB)
AGENT_SESSIONS.recover_interrupted()

# ── 工作区快照：写操作前记录原像，支持"回滚整个任务的改动" ──────────
SNAPSHOT_DIR = BASE / "data" / "snapshots"
SNAPSHOTS = SnapshotStore(SNAPSHOT_DIR, int(AGENT_CFG.get("snapshot_retention", 50)))


async def rollback_session_changes(sid: str) -> dict:
    """把某个 Session 期间改过的工作区文件恢复到任务开始前的状态。

    快照在写操作前捕获（同一路径保留最早状态），因此这是"任务级回滚"：
    修改过的文件还原、任务新建的文件删除。会话处于运行/等待状态时拒绝，
    避免和正在进行的写操作打架。
    """
    state = AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    if state.get("state") in {"running", "waiting", "ready"}:
        raise HTTPException(409, "Session 正在运行或待命，请先暂停/取消后再回滚")
    root = Path(state.get("workspace") or AGENT_TOOLS.root)
    if not root.is_dir():
        raise HTTPException(400, f"工作区不存在: {root}")
    result = SNAPSHOTS.restore(sid, root)
    step = {"step": int(state.get("total_steps", 0)), "status": "rollback",
            "reason": f"回滚任务改动：还原 {result['restored']} 个、删除 {result['deleted']} 个"
                      f"、失败 {result['failed']} 个",
            "result": {"restored": result["restored"], "deleted": result["deleted"],
                       "failed": result["failed"], "root": result.get("root"),
                       "results": result["results"][:50]}}
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
        try:
            t.relative_to(AGENT_TOOLS.root)
            SNAPSHOTS.capture(sid, rel, t, tool)          # 工作区内
        except ValueError:
            SNAPSHOTS.capture(self_scope(sid), rel, t, tool)  # 服务代码
    return hook


async def rollback_self_upgrade(sid: str | None = None, use_last: bool = False) -> dict:
    """回滚服务自身代码的自升级改动（不依赖 WebUI 的离线通道见 scripts/rollback.py）。"""
    scope = self_scope(sid) if sid else ""
    if use_last or not scope:
        # 取最近修改的自升级快照作用域
        cands = ([d.name for d in SNAPSHOTS.base.iterdir()
                  if d.is_dir() and d.name.startswith("_self_")]
                 if SNAPSHOTS.base.is_dir() else [])
        if not cands:
            return {"restored": 0, "deleted": 0, "failed": 0, "results": [],
                    "message": "没有自升级快照记录"}
        scope = max(cands, key=lambda n: SNAPSHOTS.manifest_path(n).stat().st_mtime)
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
    scopes = ([d.name for d in SNAPSHOTS.base.iterdir()
               if d.is_dir() and d.name.startswith("_self_")]
              if SNAPSHOTS.base.is_dir() else [])
    return {"enabled": bool(getattr(AGENT_TOOLS, "allow_self_upgrade", False)),
            "base": str(BASE), "restart_pending": bool(pending),
            "pending_entries": pending[-20:], "snapshot_scopes": scopes,
            "whitelist": list(getattr(AGENT_TOOLS, "self_whitelist", ()) or ())}


# 上一轮自升级的代码在本次启动时已生效：归档待重启标记
_self_applied = mark_self_upgrade_applied()
if _self_applied:
    print(f"[自升级] 已加载上一轮自升级改动（{len(_self_applied)} 条记录），标记归档")


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
    if ACTIVE_SESSION_ID:
        st = AGENT_SESSIONS.get(ACTIVE_SESSION_ID)
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


_RESUMING: set = set()


def _bg_task_done(sid: str):
    def cb(t):
        _RESUMING.discard(sid)
        if not t.cancelled() and t.exception():
            log(f"后台任务 {sid} 异常结束: {t.exception()}")
    return cb


async def resume_session_task(sid: str) -> bool:
    """拉起后台 runner 续跑会话。已在运行或已被并发认领则返回 False。

    供 actions/resume 与 auto_resume 使用：恢复消息与原任务一致，
    避免被当作"用户追加任务"重复进入 pending。
    """
    if lock.locked() and ACTIVE_SESSION_ID == sid:
        return False
    if sid in _RESUMING:  # 原子认领：并发 resume 只允许一个通过
        return False
    state = AGENT_SESSIONS.get(sid)
    if not state:
        return False
    _RESUMING.add(sid)
    try:
        AGENT_SESSIONS.patch(sid, state="ready", last_error=None)
        req = AgentRequest(
            model=normalize_model_soft(state.get("model") or DEFAULT_MODEL),
            messages=[Message(role="user", content=state.get("original_task") or "继续")],
            stream=False, session_id=sid, workspace=state.get("workspace"))
        task = asyncio.create_task(run_agent(req))
        task.add_done_callback(_bg_task_done(sid))
    except Exception:
        _RESUMING.discard(sid)
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


async def provider_turn(model_id: str, prompt: str) -> str:
    """API 提供商的 Agent 回合：system 协议约束 + user 提示，返回回答文本。

    限流/5xx/超时映射为 CopilotServiceUnavailable，复用 AgentRunner 的
    指数退避重试；其他错误原样抛出计入解析失败/失败处理。
    """
    pm = providers_mod.split_provider_model(model_id)
    provider = PROVIDERS[pm[0]]
    messages = [{"role": "system", "content": _PROVIDER_PROTOCOL},
                {"role": "user", "content": prompt}]
    try:
        return await provider.complete(pm[1], messages)
    except providers_mod.ProviderUnavailable as exc:
        raise CopilotServiceUnavailable(str(exc)) from exc


# provider 普通对话的按会话锁：同 sid 并发请求基于相同历史交错写回的防护
_PROVIDER_LOCKS: dict[str, asyncio.Lock] = {}


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
            raise CopilotServiceUnavailable(str(exc)) from exc
        current = AGENT_SESSIONS.get(sid)
        if not current:
            raise RuntimeError("会话在执行期间被删除")
        AGENT_SESSIONS.add_message(sid, "assistant", answer, int(current.get("conversation_index", 0)))
        AGENT_SESSIONS.patch(sid, model=model_id)
        runtime_update(sid, phase="api_completed", answer_preview=answer[-12000:],
                       detail=f"{pm[0]} API 调用完成")
        return {"session_id": sid, "role": "assistant", "content": answer,
                "url": None, "mode": "chat", "provider": pm[0]}


async def run_agent(req: AgentRequest):
    global ACTIVE_SESSION_ID
    if not AGENT_CFG.get("enabled", True): raise HTTPException(503, "Agent 功能已禁用")
    # 软归一：历史会话可能保存了已失效的模型 ID，回退默认模型而非 400
    req.model=normalize_model_soft(req.model)
    task="\n".join(f"[{m.role}] {m.content.strip()}" for m in req.messages if m.content.strip())
    if not task: raise HTTPException(400,"至少需要一条消息")
    sid=req.session_id or ("task-"+uuid.uuid4().hex[:12])
    runtime_update(sid, phase="queued", detail="任务已进入执行队列", answer_preview="", prompt_preview="")
    async with lock:
        # ACTIVE_SESSION_ID 必须在锁内声明：等锁的请求若提前覆盖，
        # 正在运行会话的删除保护/busy 判定/resume 守卫会整体失效。
        prev_active = ACTIVE_SESSION_ID
        ACTIVE_SESSION_ID = sid
        existing=AGENT_SESSIONS.get(sid)
        workspace=(req.workspace.strip() if req.workspace else None) or (existing or {}).get("workspace") or DEFAULT_WORKSPACE
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
        _pm = providers_mod.split_provider_model(req.model)
        if _pm and _pm[0] in PROVIDERS:
            # API 提供商：不经过浏览器，无页面导航/会话 URL/标记确认簿记；
            # 换会话只影响本地上下文压缩。
            async def turn(prompt, model, force_new=False):
                return await provider_turn(model, prompt)
        else:
            async def turn(prompt,model,force_new=False):
                st=AGENT_SESSIONS.get(sid)
                make_new = force_new or not st.get("current_url")
                if not make_new:
                    # 页面可能因服务重启/窗口切换被重置到基础地址：续聊前若当前
                    # 页面不是该会话的 Copilot 对话，先导航回已保存的会话 URL，
                    # 避免回合误入一个全新的空对话。
                    expected = st.get("current_url") or ""
                    m = CONVERSATION_URL_RE.search(expected)
                    if m and m.group(0) not in (page.url or ""):
                        await page.goto(expected, wait_until="domcontentloaded", timeout=120000)
                        await asyncio.sleep(1)
                        _forget_selected_model()
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
                        paused_url = await stable_conversation_url(timeout=6)
                        if not paused_url:
                            _cand = page.url or ""
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
                    _cand = page.url or ""
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
        def save(**kw): return AGENT_SESSIONS.patch(sid,**kw)
        def add_step(x):
            st=get(); AGENT_SESSIONS.add_step(sid,int(x.get("step",st["total_steps"]+1)),int(st["conversation_index"]),x)
        def add_file(x): AGENT_SESSIONS.add_file_event(sid,x)
        def add_conv_stat(x): AGENT_SESSIONS.add_conv_stat(sid,x)
        # 写操作前捕获原像：工作区改动与自升级改动分作用域，均可整任务回滚
        AGENT_TOOLS.set_snapshot_hook(self_snapshot_hook(sid))
        AGENT_TOOLS.self_rollback_fn = lambda: SNAPSHOTS.restore(self_scope(sid), BASE)
        runner=AgentRunner(AGENT_TOOLS,turn,AGENT_CFG,get,save,add_step,add_file,add_conv_stat)
        try:
            result = await runner.run(effective_task,req.model)
            runtime_update(sid, phase="completed", detail=result.content, answer_preview=result.content)
            SNAPSHOTS.prune()
            return result, sid
        except Exception as exc:
            runtime_update(sid, phase="failed", detail=str(exc))
            AGENT_SESSIONS.patch(sid,state="failed",last_error=str(exc))
            raise
        finally:
            AGENT_TOOLS.set_snapshot_hook(None)
            AGENT_TOOLS.self_rollback_fn = None
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













async def plain_chat_turn(sid: str, content: str, model_id: str) -> dict:
    """普通对话回合：在 Session 绑定的 Copilot 会话里问答，不跑工具循环。

    由 continue_session_message（跟进）和 plain_completions（新任务）共用。
    调用方负责写入 user 消息；本函数写入 assistant 回复并维护会话 URL。
    """
    global ACTIVE_SESSION_ID
    state = AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    async with lock:
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
            # 新会话把 CBP-AGENT 标记放进首条消息：Copilot 用它做侧栏标题，
            # 也是日后远程删除时的同属校验依据（与 Agent 模式一致）。
            marker = _agent_chat_marker(sid, int(state.get("conversation_index", 0)))
            send_content = (marker + "\n" + content) if make_new else content
            # 追加唯一回合标记：虚拟化列表里"You said"计数不可靠，
            # sent_successfully 用标记确认送达，避免误判重发。
            send_content = send_content.rstrip() + f"\n\n[CBP-TURN:{uuid.uuid4().hex[:12]}]"
            await send(send_content, make_new, model_id)
            answer = (await wait_agent_answer(previous)).strip()
            current = AGENT_SESSIONS.get(sid)
            if not current:
                raise RuntimeError("会话在执行期间被删除")
            chat_url = await stable_conversation_url(timeout=10)
            if not chat_url:
                _cand = page.url or ""
                chat_url = _cand if CONVERSATION_URL_RE.search(_cand) else ""
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
            ACTIVE_SESSION_ID = prev_active


async def plain_completions(req: AgentRequest):
    """普通对话模式发起新任务：创建 Session 但不启动 Agent 工具循环。"""
    if not AGENT_CFG.get("enabled", True): raise HTTPException(503, "Agent 功能已禁用")
    model_id = normalize_model(req.model)
    contents = [m.content.strip() for m in req.messages if m.role == "user" and m.content.strip()]
    if not contents: raise HTTPException(400, "至少需要一条 user 消息")
    content = contents[-1]
    sid = req.session_id or ("task-" + uuid.uuid4().hex[:12])
    if not AGENT_SESSIONS.get(sid):
        workspace = (req.workspace.strip() if req.workspace else None) or str(AGENT_WORKSPACE)
        AGENT_SESSIONS.create(sid, content, workspace, model_id)
    AGENT_SESSIONS.add_message(sid, "user", content, 0)
    pm = providers_mod.split_provider_model(model_id)
    if pm and pm[0] in PROVIDERS:
        result = await provider_session_chat(sid, content, model_id)
    else:
        result = await plain_chat_turn(sid, content, model_id)
    return {"id": "chat-" + uuid.uuid4().hex, "object": "chat.completion",
            "created": int(time.time()), "model": model_id,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": result["content"]},
                         "finish_reason": "stop"}],
            "session_id": sid, "mode": "chat"}


async def shutdown_browser():
    """关闭浏览器上下文与 Playwright（服务退出时调用）。"""
    global ctx, page, pw
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
