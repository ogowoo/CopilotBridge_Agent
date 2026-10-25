"""page.py — Copilot 页面自动化：新对话、模型选择、发消息、取回答、等生成结束。

从 bridge.py 拆出（第三批解耦）。约定：**模块级可变状态仍放在 bridge.py**，
这里一律经 ``b.xxx`` 读写（b = bridge 模块），这样测试里的 rebind 依然生效。
本模块不定义任何 FastAPI 端点。
"""
from __future__ import annotations

import asyncio, json, re, time
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from . import bridge as b


async def diagnostics(tag):
    if not b.DIAG or b.current_page() is None: return
    try:
        (b.BASE / "last_page.txt").write_text(await b.page.locator("body").inner_text(), encoding="utf-8")
        if bool(b.CFG.get("diagnostic_html", False)):
            (b.BASE / "last_page.html").write_text(await b.page.content(), encoding="utf-8")
    except Exception: pass


async def visible(selectors):
    for selector in selectors:
        loc = b.page.locator(selector)
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
    # :has-text 会匹配“包含该文字的祖先按钮”，点祖先等于点侧栏空白处，
    # 可能误开 Notebooks 菜单。弹窗按钮一律按精确文本/角色定位。
    btn = await b.visible(
        [f'button:text-is("{t}")' for t in b.SELECTOR_TEXT["dismiss"]]
        + [f'[role="button"]:text-is("{t}")' for t in b.SELECTOR_TEXT["dismiss"]]
    )
    if btn:
        try:
            await btn.click(timeout=1000)
            await asyncio.sleep(0.2)
        except Exception:
            pass
    # 仍有菜单/浮层时按 Escape 收起（不点击任何导航项）
    try:
        await b.page.keyboard.press("Escape")
    except Exception:
        pass


_CHAT_SURFACE_HINTS = (
    "Message Copilot", "What can I help with?",
    "向 Copilot 发送消息", "传消息给 Copilot",
    "傳送訊息給 Copilot", "向 Copilot 傳送訊息",
    "Copilot にメッセージを送信する",
    "嗨，我能提供什麼協助", "何かお手伝いできることはありますか",
)
_NOTEBOOK_SURFACE_HINTS = (
    "Suggested notebooks", "No suggestions yet",
    "建議的筆記本", "尚無建議", "推薦的筆記本",
    "推奨されるノートブック", "おすすめのノートブックはありません",
)


async def _chat_surface_ready() -> bool:
    """当前主区域是不是聊天页（而不是 Notebooks / Library 等）。

    Copilot 左侧导航切到 Notebooks 后，URL 仍可能是 /chat/，且页面上没有
    任何 "You said:"，原来的 _empty() 会误判为“已在新对话”。这里同时检查
    聊天输入区文案与 Notebooks 特征，避免把任务发进笔记本页面。
    """
    try:
        text = await b.body_text()
    except Exception:
        return False
    if any(x in text for x in _NOTEBOOK_SURFACE_HINTS):
        return False
    return any(x in text for x in _CHAT_SURFACE_HINTS)


async def new_chat():
    """开一个空对话。**必须验证真的空了，而且真的在聊天页**。

    实测（task-316f6502774b）：新任务第一轮 prompt 里明明是新任务（建 ok.txt），
    模型却接着另一条"pyshot 快捷键"对话的思路去读 pyshot/main.py。
    说明点"New chat"之后页面并没有变成空对话——后续 prompt 落在了旧上下文里，
    任务被"串"到上一条对话上（用户："为什么会窜到 pyshot 里的？"）。

    实测（2026-10）：左侧导航切到 Notebooks 后 URL 仍是 /chat/，且没有
    "You said:"，_empty() 误判为已在新对话 → 任务被发进笔记本页面。
    因此 _empty() 同时要求“没有用户回合”+“主区域是聊天页”。
    """
    async def _empty() -> bool:
        try:
            return (await b.count_user_markers() == 0
                    and await _chat_surface_ready())
        except Exception:
            return False

    # 只能用**精确文本**定位：:has-text 会命中“包含 New chat 的祖先按钮”，
    # 点击祖先中心会落到 Notebooks 上（2026-10 实测：侧栏整个被点开）。
    btn = await b.visible(
        [f'button[aria-label="{t}" i]' for t in b.SELECTOR_TEXT["new_chat"]]
        + [f'a[aria-label="{t}" i]' for t in b.SELECTOR_TEXT["new_chat"]]
        + [f'button:text-is("{t}")' for t in b.SELECTOR_TEXT["new_chat"]]
        + [f'a:text-is("{t}")' for t in b.SELECTOR_TEXT["new_chat"]]
        + [f'[role="link"]:text-is("{t}")' for t in b.SELECTOR_TEXT["new_chat"]]
        + [f'[role="button"]:text-is("{t}")' for t in b.SELECTOR_TEXT["new_chat"]]
    )
    if btn:
        await btn.click()
        await asyncio.sleep(1)
    else:
        await b.page.goto(b.URL, wait_until="domcontentloaded", timeout=120000)
        await asyncio.sleep(2)

    if not await _empty():
        # 点击没生效 / 页面被复用 / 仍在 Notebooks：回基础地址重试一次
        b.log("新建对话后页面仍不是空聊天页（可能在 Notebooks 或旧会话），回基础地址重试")
        try:
            await b.page.goto(b.URL, wait_until="domcontentloaded", timeout=120000)
        except Exception:
            pass
        for _ in range(20):                 # 最多等 10s
            await asyncio.sleep(0.5)
            if await _empty():
                break
    if not await _empty():
        raise b.CopilotSendFailed(
            "新对话未创建成功（页面仍在旧会话或 Notebooks）：已中止，避免把新任务接到"
            "错误页面的上下文里。请点『重试浏览器』或稍后再试。")
    # 新聊天会把模型重置为 Copilot 默认（Auto）：缓存记为 copilot-auto，
    # 注意这是页面行为而非我们的 default_model 配置。
    b._forget_selected_model(reset_to="copilot-auto")


def get_selected_model() -> str | None:
    """当前窗口缓存的模型（没有窗口时用全局值，兼容旧路径与测试）。"""
    win = b._WIN_CTX.get()
    return win.selected_model if win is not None else b.SELECTED_MODEL


def set_selected_model(value: str | None) -> None:
    b.SELECTED_MODEL = value
    win = b._WIN_CTX.get()
    if win is not None:
        win.selected_model = value


def _forget_selected_model(reset_to: str | None = None):
    """Invalidate the model cache, optionally recording a known reset target.

    new_chat() resets the selector to the tenant default, so callers can pass
    DEFAULT_MODEL to keep cache hits for default-model requests.
    """
    b.set_selected_model(reset_to)


async def model_button():
    all_names = [name for item in b.MODELS.values() for name in item["ui_names"]]
    exact = re.compile(
        r"^(?:" + "|".join(re.escape(name) for name in all_names) + r")$",
        re.I,
    )
    button = await b.visible_locator([
        b.page.get_by_role("button", name=exact),
        b.page.locator('button[aria-haspopup="menu"]').filter(has_text=exact),
        b.page.locator('button[aria-haspopup="listbox"]').filter(has_text=exact),
        b.page.locator('[role="button"][aria-haspopup="menu"]').filter(has_text=exact),
        b.page.locator('[role="button"][aria-haspopup="listbox"]').filter(has_text=exact),
    ])
    if not button:
        # 兜底：当前选中的模型可能是菜单里新出现、尚未收录的模型，
        # 按钮文本不匹配任何已知名字，改按结构特征定位模型选择器。
        button = await b.visible_locator([
            b.page.locator('button[aria-label*="Model Selector" i]'),
            b.page.locator('button[aria-label*="模型"][aria-haspopup]'),
            b.page.locator('button[aria-label*="モデル"][aria-haspopup]'),
            b.page.locator('button[aria-label*="model" i][aria-haspopup="menu"]'),
            b.page.locator('button[aria-label*="model" i][aria-haspopup="listbox"]'),
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
    return b._LABEL_NOISE.sub("", str(text or "").lower())


def label_matches(target_names, label: str) -> bool:
    """该菜单标签是否对应目标模型（归一后相等，或一方包含另一方）。"""
    got = b.normalize_label(label)
    if not got:
        return False
    for name in target_names:
        want = b.normalize_label(name)
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
        b.page.get_by_role("option", name=target),
        b.page.get_by_role("menuitem", name=target),
        b.page.get_by_role("menuitemradio", name=target),
        b.page.get_by_text(target, exact=True),
        b.page.get_by_role("option", name=target, exact=False),
        b.page.get_by_role("menuitem", name=target, exact=False),
        b.page.get_by_role("menuitemradio", name=target, exact=False),
        b.page.get_by_text(target, exact=False),
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
            entries.append((item, label, await b._submenu_hinted(item)))
        except Exception:
            pass

    for role in b.MENU_ITEM_ROLES:
        items = b.page.get_by_role(role)
        for i in range(await items.count()):
            await add(items.nth(i))
    # 兜底：可见菜单容器内的直接子项（role 命名不统一时靠结构识别）
    for sel in b.MENU_CONTAINER_SEL:
        containers = b.page.locator(sel)
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
    return {label for _item, label, _hint in await b._visible_menu_items()}


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
        items = await b.page.evaluate(
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
        if set(await b._menu_signature()) - base_labels:
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
    await b._expand_submenu(item, allow_click=False)
    if await b._wait_growth(base_labels, tries=6 if hinted else 2):
        return True
    if not hinted:
        return False
    try:
        await item.focus(timeout=800)
        await b.page.keyboard.press("ArrowRight")
        if await b._wait_growth(base_labels, tries=4):
            return True
    except Exception:
        pass
    await b._expand_submenu(item, allow_click=True)
    return await b._wait_growth(base_labels)


async def dump_menu_diagnostics() -> str:
    """把当前可见的菜单 DOM/文本落盘（last_menu.html / last_menu.txt）。

    模型菜单扫不到（尤其二级子菜单）时，这两份快照是唯一能看到真实
    角色与结构的现场：AI/维护者据此改选择器，无需盲猜。
    """
    try:
        parts, texts = [], []
        for sel in b.MENU_CONTAINER_SEL:
            loc = b.page.locator(sel)
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
            (b.BASE / "last_menu.html").write_text(
                "\n\n<!-- ===== menu ===== -->\n\n".join(parts)[:400000], encoding="utf-8")
            (b.BASE / "last_menu.txt").write_text("\n\n-----\n\n".join(texts)[:80000], encoding="utf-8")
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
    if b.current_page() is None:
        raise RuntimeError("自动化浏览器尚未启动")
    current = ""
    try:
        current = b.page.url or ""
    except Exception:
        current = ""
    if b.COPILOT_URL_MARK.search(current):
        return current
    base = str(b.CFG.get("copilot_url") or "https://m365.cloud.microsoft/chat/")
    await b.page.goto(base, wait_until="domcontentloaded", timeout=120000)
    await b.wait_copilot_ready(90)
    await asyncio.sleep(1.0)
    try:
        return b.page.url or base
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
        result["url"] = await b.ensure_copilot_page()
    except Exception as exc:
        result["reason"] = f"无法打开 Copilot 对话页: {exc}"
        return result
    button = await b.model_button()
    if not button:
        result["reason"] = "未找到模型菜单按钮：页面可能未就绪/未登录，或 Copilot 前端改版"
        await b.diagnostics("models_refresh_no_button")
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
            entries = await b._visible_menu_items()
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
                if await b._probe_submenu(item, await b._submenu_hinted(item), base_labels):
                    parents.add(label)
                    grew = True
                    break   # 菜单结构已变，重新收集后再继续探测
            if not grew:
                break
        # 收尾再累积一次（此刻可能仍开着某个子菜单），并把菜单现场落盘
        for _i, lbl, _h in await b._visible_menu_items():
            if lbl not in seen_labels:
                seen_labels.add(lbl)
                labels_order.append(lbl)
        result_menu_dump = await b.dump_menu_diagnostics()
    finally:
        for _ in range(3):  # 子菜单可能多级展开，多按几次 Escape 确保全部关闭
            try:
                await b.page.keyboard.press("Escape")
                await asyncio.sleep(.2)
            except Exception:
                pass
    labels = [lbl for lbl in labels_order if lbl not in parents]
    leaf_labels = {lbl.casefold() for lbl in labels}
    # 半渲染/子菜单未飞出时只枚举到 1 个叶子：这种"扫描结果"会把其它模型
    # 全部标成不可用（WebUI 下拉大面积消失），且在下一次成功扫描前不自愈。
    # 因此只在扫到 ≥2 个叶子时更新可见性集合；空结果保留上次快照。
    if len(leaf_labels) >= 2 or not b.DISCOVERED_LABELS:
        b.DISCOVERED_LABELS = leaf_labels  # 只记叶子：GPT 这类子菜单父项不是可选模型
    added = []
    known_names = {name.casefold() for item in b.MODELS.values() for name in item["ui_names"]}
    for label in labels:
        if label.casefold() in known_names:
            continue
        model_id = b._model_id_from_label(label)
        if not model_id or model_id in b.MODELS:
            continue
        b.MODELS[model_id] = {
            "ui_names": [label],
            "description": f"从 Copilot 菜单自动发现的模型（{label}）",
        }
        known_names.add(label.casefold())
        added.append(model_id)
    # 命中数决定可用性判定的严格程度：0 命中（菜单整体改名）时放行全部内置模型
    b.DISCOVERY_MATCHED = sum(
        1 for item in b.MODELS.values()
        if any(name.casefold() in leaf_labels for name in item["ui_names"])
    )
    result.update({"added": added, "scanned": True, "labels": labels,
                   "menu_dump": result_menu_dump, "parents": sorted(parents),
                   "matched_builtins": b.DISCOVERY_MATCHED})
    if not labels:
        result["reason"] = "模型菜单打开了但没有枚举到任何选项（可能是渲染延迟或前端改版）"
        await b.diagnostics("models_refresh_empty_menu")
    elif not parents:
        result["reason"] = ("菜单已枚举但未探测到子菜单父项；若列表缺少二级模型，"
                            "请把服务目录的 last_menu.html 发给维护者")
    return result


async def select_model(model_id):
    normalized = b.normalize_model(model_id)
    target_names = b.MODELS[normalized]["ui_names"]
    button = await b.model_button()
    if not button:
        await b.diagnostics("no_model_button")
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
        b.set_selected_model(normalized)
        b.log(f"模型已是 {normalized} ({current_label or current_text})")
        return normalized
    other_known = {_norm(name) for mid, item in b.MODELS.items() if mid != normalized
                   for name in item["ui_names"]}
    if b.get_selected_model() == normalized and not (segments & other_known):
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
    option = await b.visible_locator(b._menu_target_locators(target))
    if not option:
        # 目标可能藏在二级子菜单（如 GPT 父项飞出更多模型）：逐个探测展开。
        # 不再依赖 aria-haspopup 判定父项——悬停安全，按效果确认是否父项。
        entries = await b._visible_menu_items()
        base_labels = {lbl for _i, lbl, _h in entries}
        for parent, label, _hint in entries:
            if not await b._probe_submenu(parent, await b._submenu_hinted(parent), base_labels):
                continue
            # 子菜单展开有动画延迟，立刻找可能落空：短轮询三次
            for _try in range(3):
                option = await b.visible_locator(b._menu_target_locators(target))
                if option:
                    break
                await asyncio.sleep(.4)
            if option:
                break
            base_labels = await b._menu_label_set()
    if not option:
        # 归一化兜底：把可见菜单项按 normalize_label 比较一次，命中就直接点它。
        # （精确正则用的是整串锚定，标签少一个后缀就落空）
        try:
            for item, label, _hint in await b._visible_menu_items():
                if b.label_matches(target_names, label):
                    option = item
                    b.log(f"模型标签按归一化匹配命中：{label!r} → {normalized}")
                    break
        except Exception as exc:
            b.log(f"归一化匹配失败（忽略）：{exc}")
    if not option:
        try: await b.page.keyboard.press("Escape")
        except Exception: pass
        await b.diagnostics("model_option_missing")
        # 菜单结构变化或标签未收录不应打死整个回合：按当前已选模型继续，
        # 但必须让用户看见——否则重启后"模型悄悄变回 Auto"完全无从察觉。
        fallback = b.get_selected_model() or normalized
        b.log(f"警告：模型菜单中找不到 {target_names[0]}，已按当前选中模型继续")
        if b.current_session():
            b.runtime_update(b.current_session(), model_warning=(
                f"模型 {target_names[0]} 未在 Copilot 菜单中找到，本回合沿用 "
                f"{b.MODELS.get(fallback, {}).get('ui_names', [fallback])[0]}"))
        return fallback
    try:
        await option.click(timeout=8000)
    except Exception:
        await option.click(timeout=5000, force=True)
    await asyncio.sleep(.8)
    b.set_selected_model(normalized)
    b.log(f"模型已切换为 {normalized}")
    return normalized


async def attach_file_to_copilot(file_path: Path) -> None:
    """把本地文件附加到 Copilot 输入框（不发送）。

    Copilot 的 input[type=file] 通常是隐藏的：先尝试点开附件按钮使其出现，
    再用 set_input_files 直接注入（对隐藏控件同样有效）。
    """
    file_input = b.page.locator('input[type="file"]')
    if not await file_input.count():
        btn = await b.visible([
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
        await b.diagnostics("no_file_input")
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
                _found += await b.page.locator(_sel).count()
            except Exception:
                pass
        if _found:
            break
        await asyncio.sleep(0.5)
        _waited += 0.5
    if _waited:
        b.log(f"等待附件 {file_path.name} 上传完成 {_waited:.1f}s")


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
    upload_dir = b.BASE / "data" / "uploads"
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return []
    paths: list[Path] = []
    for i, item in enumerate(list(images)[:b.MAX_IMAGE_ATTACHMENTS]):
        if not item or not isinstance(item, str):
            continue
        m = re.match(r"^data:image/([A-Za-z0-9+.-]+);base64,(.*)$", item, re.S)
        ext, b64 = "png", item
        if m:
            ext = (m.group(1).lower().replace("jpeg", "jpg") or "png")
            b64 = m.group(2)
        try:
            data = b.base64.b64decode(b64, validate=False)
        except Exception:
            continue
        if not data or len(data) > b.MAX_IMAGE_BYTES:
            continue
        p = upload_dir / f"{b.uuid.uuid4().hex[:12]}_{i}.{ext}"
        try:
            p.write_bytes(data)
            paths.append(p)
        except Exception:
            continue
    return paths


async def input_box():
    x = await b.visible([
        'textarea[placeholder]',
        'textarea',
        '[contenteditable="true"][role="textbox"]',
        '[contenteditable="true"]',
        '[role="textbox"]',
    ])
    if not x:
        await b.diagnostics("no_input")
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
    host = urlparse(b.URL).netloc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            url = b.page.url or ""
            if host and host in url:
                box = await b.visible([
                    'textarea[placeholder]', 'textarea',
                    '[contenteditable="true"][role="textbox"]',
                    '[contenteditable="true"]', '[role="textbox"]',
                ])
                if box:
                    return True
        except Exception:
            pass
        await asyncio.sleep(2)
    b.log("等待 Copilot 就绪超时（仍在登录/跳转中），跳过启动期自动化")
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
        return (await b.page.locator("body").inner_text()).replace("\r\n", "\n")
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
        return (await b.page.evaluate(
            "() => (document.querySelector('main') || document.body).textContent || ''"
        ) or "").replace("\r\n", "\n")
    except Exception:
        return ""


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
            n = await b.page.locator("body").evaluate("el => el.innerHTML.length")
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
        await b.page.evaluate(b.MD_JS)
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
        res = await b.page.evaluate(js, [first, last])
        if not res or not isinstance(res, dict):
            return ""
        md = (res.get("md") or "").strip()
        if res.get("dom"):
            try:
                (b.BASE / "data" / "answer_dom.html").write_text(res["dom"], encoding="utf-8")
            except Exception:
                pass
        return md
    except Exception:
        return ""


async def current_answer():
    # Agent 模式必须保持纯文本：回复是 JSON 动作，markdown 转换可能破坏它。
    # 聊天显示的 markdown 版本由 plain_chat_turn 单独取（见 answer_markdown）。
    raw = await b.body_text()
    text = b.extract_latest_answer(raw)
    # 思考占位必须在这里就剔掉：流式收集按"公共前缀增长"输出，占位文本一旦被当成
    # 答案发出去，真答案替换整段时前缀为空、后续内容永远发不出去。
    if text and len(text) <= 80 and text == b._status_before_marker(raw):
        return ""
    return b._strip_thinking(await b._prefer_unfolded(text))


_SIDE_PANEL_JS = r"""
() => {
  const W = window.innerWidth, H = window.innerHeight;
  const out = {candidates: []};
  const sels = [
    'aside', '[role="complementary"]', '[role="dialog"]',
    '[data-testid*="canvas" i]', '[data-testid*="panel" i]', '[data-testid*="preview" i]',
    '[class*="SidePanel" i]', '[class*="side-panel" i]', '[class*="RightPane" i]',
    '[class*="rightRail" i]', '[class*="Canvas" i]', '[class*="Preview" i]',
    '[class*="Pane" i]', '[class*="Panel" i]', '[aria-label*="文件"]', '[aria-label*="File"]',
    '[aria-label*="预览"]', '[aria-label*="Preview"]'
  ];
  const seen = new Set();
  const boxes = [];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      if (seen.has(el)) continue;
      seen.add(el);
      const r = el.getBoundingClientRect();
      const txt = (el.innerText || '').trim();
      if (r.width < 200 || r.height < 160) continue;
      // 只看"右侧"的块：面板通常在窗口右半边
      if (r.left < W * 0.35) continue;
      // 排除聊天区自己
      if (el.closest('main [class*="chat" i]') && !el.matches('aside,[role="complementary"]')) continue;
      boxes.push({el: el, r: r, txt: txt, sel: sel});
    }
  }
  if (!boxes.length) return out;
  boxes.sort((a, b) => (b.txt.length - a.txt.length));
  const best = boxes[0];
  let md = '';
  try {
    // 面板里若已经是 markdown 渲染结果，直接用 innerText 即可（保留段落换行）
    md = (best.el.innerText || '').trim();
  } catch (e) { md = ''; }
  const head = best.el.querySelector('h1,h2,[class*="title" i],[aria-label]');
  out.title = head ? (head.innerText || head.getAttribute('aria-label') || '').trim().slice(0, 120) : '';
  out.text = md;
  out.len = md.length;
  out.sel = best.sel;
  out.rect = [Math.round(best.r.left), Math.round(best.r.top),
              Math.round(best.r.width), Math.round(best.r.height)];
  out.candidates = boxes.slice(0, 12).map(b => ({
    sel: b.sel, cls: (b.el.className || '').toString().slice(0, 60),
    len: b.txt.length, rect: [Math.round(b.r.left), Math.round(b.r.top),
                              Math.round(b.r.width), Math.round(b.r.height)]
  }));
  return out;
}
"""


async def side_panel() -> dict:
    """右侧面板（文件预览/侧栏）里的正文；没有面板时返回 {}。"""
    try:
        r = await b.page.evaluate(_SIDE_PANEL_JS)
    except Exception:
        return {}
    if not r or not isinstance(r, dict):
        return {}
    text = (r.get("text") or "").strip()
    # 太短的多半是工具栏/标题，不算内容
    if len(text) < 120:
        return {}
    return {"title": r.get("title") or "", "text": text, "selector": r.get("sel") or ""}


async def debug_side_panel() -> dict:
    """排障：面板候选块一览（尺寸/类名/文本长度）。"""
    try:
        r = await b.page.evaluate(_SIDE_PANEL_JS)
    except Exception as exc:
        return {"error": str(exc)[:200]}
    if not r:
        return {"error": "no result"}
    r = dict(r)
    if r.get("text"):
        r["head"] = r["text"][:300]
        r["text"] = f"（{len(r['text'])} 字符，已省略）"
    return r


async def answer_after_marker(marker: str) -> str:
    """只取**本次回合标记之后**的最新回答。

    虚拟化列表里旧答复仍在页面上，靠"和上一次不同"判断不可靠；
    标记是每次请求唯一的，出现在我发出的消息里，因此标记之后的文本一定是新回合的。
    """
    if not marker:
        return ""
    raw = await b.body_text()
    idx = raw.rfind(marker)
    if idx < 0:
        return ""
    tail = raw[idx + len(marker):]
    text = b.extract_latest_answer(tail)
    if text and len(text) <= 80 and text == b._status_before_marker(tail):
        return ""
    return b._strip_thinking(await b._prefer_unfolded(text))


async def _prefer_unfolded(text: str) -> str:
    """若渲染文本把 JSON 参数里的连续空格折叠了，改用 textContent 版本。

    只做**保守替换**：两份里都找得到动作、且原文版本确实保留了更多连续空白，
    才采用原文版本（否则保持原行为，避免 textContent 缺换行带来的副作用）。
    """
    try:
        if not text or '"tool"' not in text:
            return text
        raw = b.extract_latest_answer(await b.body_text_raw())
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
    return bool(await b.visible(
        [f'button[aria-label*="{t}" i]' for t in b.SELECTOR_TEXT["stop_aria"]]
        + [f'button:has-text("{t}")' for t in b.SELECTOR_TEXT["stop"]]
    ))


async def count_user_markers():
    text = await b.body_text()
    return sum(text.count(m) for m in b.MARKERS["user_turn"])


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
        return marker in await b.body_text()
    except Exception:
        return False


async def _wait_turn_marker(marker: str, timeout: float = 30) -> bool:
    """Confirm the exact submitted turn, independent of virtualized message counts."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await b._body_contains_turn_marker(marker):
            return True
        await asyncio.sleep(b.POLL)
    return False


async def sent_successfully(box, before_count, prompt):
    marker_match = re.search(r"\[CBP-TURN:[0-9a-f]{12}\]", prompt)
    marker = marker_match.group(0) if marker_match else ""
    for _ in range(20):
        await asyncio.sleep(.25)
        value = await b.box_value(box)
        if marker:
            if not value and await b._body_contains_turn_marker(marker):
                return True
            continue
        now_count = await b.count_user_markers()
        if now_count > before_count:
            return True
        if not value and prompt in await b.body_text():
            return True
    return False


async def click_send_button():
    button = await b.visible(
        [f'button[aria-label="{t}"]' for t in b.SELECTOR_TEXT["send_aria"]]
        + [f'button[aria-label*="{t}" i]' for t in b.SELECTOR_TEXT["send_aria"]]
        + [f'button[title*="{t}" i]' for t in b.SELECTOR_TEXT["send_aria"]]
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
    if not await b.ensure_browser_ready():
        raise b.CopilotSendFailed(
            "Copilot 浏览器未就绪（未登录或启动失败）：可改用提供商模型，"
            "或在界面点『重试浏览器』后重试")
    # 若恰逢重启后的 SSO/ADFS 回跳，先等它就绪；**等不到就明确失败**，
    # 否则会一直挂在 phase=sending（实测挂了 2 分钟以上，任务像是卡死）。
    if not await b.wait_copilot_ready(90):
        raise b.CopilotSendFailed(
            "Copilot 未就绪（未登录或页面未加载完）：本轮无法发送。"
            "请在自动化浏览器窗口登录 Copilot，或改用提供商模型后重试。")
    try:
        _license = b.license_reason(await b.body_text())
    except Exception:
        _license = ""
    if _license:
        raise RuntimeError(
            "Microsoft 365 Copilot 许可证不可用：" + _license +
            "。请确认自动化浏览器当前登录的账号已分配 Copilot 许可证"
            "（不是 Copilot Chat Basic）；换有许可证的账号后重试。")
    await b.dismiss()
    if make_new:
        await b.new_chat()
    # Select after New chat because opening a new conversation can reset Auto.
    selected_model = await b.select_model(model_id)

    box = await b.input_box()
    await box.click()
    try:
        await box.fill(prompt)
    except Exception:
        await box.press("Control+A")
        await box.press_sequentially(prompt)
    before_count = await b.count_user_markers()

    await box.press("Enter")
    if await b.sent_successfully(box, before_count, prompt):
        await b.diagnostics("sent")
        b.log("问题已发送")
        return selected_model

    await box.click()
    if await b.click_send_button() and await b.sent_successfully(box, before_count, prompt):
        await b.diagnostics("sent_button")
        b.log("问题已通过发送按钮提交")
        return selected_model

    await box.click()
    await box.press("Control+Enter")
    if await b.sent_successfully(box, before_count, prompt):
        await b.diagnostics("sent_ctrl_enter")
        b.log("问题已通过 Ctrl+Enter 提交")
        return selected_model

    full = await b.conversation_full_check()
    if full:
        await b.diagnostics("send_failed_conversation_full")
        raise b.CopilotConversationFull(full)
    await b.diagnostics("send_failed")
    raise b.CopilotSendFailed("问题停留在输入框，Enter、发送按钮和 Ctrl+Enter 均未触发发送")


async def completion_signal():
    text = await b.body_text()
    pos, marker_len = b._last_turn(text, b.MARKERS["assistant_turn"])
    answer = text[pos + marker_len:] if pos >= 0 else ""
    stripped = answer.rstrip()
    has_sources = any(("\n" + s + "\n") in answer or stripped.endswith(s)
                      for s in b.MARKERS["sources"])
    stop_button = await b.visible([
        'button[aria-label*="Stop" i]',
        'button:has-text("Stop generating")',
        'button:has-text("停止生成")',
    ])
    return has_sources or stop_button is None


async def updates():
    deadline = time.monotonic() + b.TIMEOUT
    previous = ""
    emitted = 0
    unchanged_since = None
    started = False
    while time.monotonic() < deadline:
        await asyncio.sleep(b.POLL)
        current = await b.current_answer()
        if not current:
            continue
        if current == previous:
            unchanged_since = unchanged_since or time.monotonic()
        else:
            unchanged_since = None
        stable_prefix = b.commonprefix([previous, current]) if previous else ""
        if len(stable_prefix) > emitted:
            part = stable_prefix[emitted:]
            emitted = len(stable_prefix)
            if part:
                if not started:
                    started = True
                    b.log("已识别 Copilot said 区域，开始流式返回")
                yield part
        previous = current
        settled = unchanged_since is not None and time.monotonic() - unchanged_since >= b.STABLE
        if current and settled and await b.completion_signal():
            if len(current) > emitted:
                yield current[emitted:]
                emitted = len(current)
            await b.diagnostics("success")
            b.log(f"回答完成，共 {len(current)} 字符")
            return
    final = await b.current_answer()
    if final and await b.completion_signal():
        if len(final) > emitted:
            yield final[emitted:]
        await b.diagnostics("success_late")
        b.log(f"回答延迟完成，共 {len(final)} 字符")
        return
    await b.diagnostics("timeout")
    raise TimeoutError("等待回答超时，请查看 last_page.txt")
