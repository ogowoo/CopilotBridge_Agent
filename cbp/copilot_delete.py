"""Copilot 侧栏会话删除流程（从 bridge.py 拆出）。

URL 是会话的持久身份；侧栏的 CBP-AGENT 标记是同属校验。
删除后做对账式终验：以侧栏实际残留为准。
page 经模块 __getattr__ 惰性取 bridge.page——浏览器重启后 page 对象会换，
直接 import 变量会拿到死引用。
"""

import asyncio
import re
import time

from . import bridge as _b
from .bridge import (visible, visible_locator, diagnostics, log, CONVERSATION_URL_RE,
                     _agent_chat_marker, wait_copilot_ready, body_text, CFG)


def __getattr__(name):
    if name == "page":
        return _b.page
    raise AttributeError(name)


async def _open_chat_sidebar():
    page = _b.page  # 模块级缓存会拿到死引用（浏览器重启会换对象），每函数取值
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


async def _conversation_link_by_title(marker: str):
    page = _b.page  # 模块级缓存会拿到死引用（浏览器重启会换对象），每函数取值
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
    page = _b.page  # 模块级缓存会拿到死引用（浏览器重启会换对象），每函数取值
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
    page = _b.page  # 模块级缓存会拿到死引用（浏览器重启会换对象），每函数取值
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


async def _conversation_belongs_to_session(url: str, session_prefix: str) -> bool:
    """打开会话正文，确认标记仍在（标题可能已被 Copilot 自动改名）。

    发送新会话时标记 [CBP-AGENT][sid][Cnnn] 是写进首条消息体的，所以即使
    侧栏标题被改写，正文里依然能找到；这比标题匹配更可靠，也避免误删他人
    会话。找不到标记或打不开页面一律返回 False（宁可不删）。
    """
    if not url:
        return False
    page = _b.page  # 模块级缓存会拿到死引用（浏览器重启会换对象），每函数取值
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=120000)
        await asyncio.sleep(1.5)
        body = await body_text()
    except Exception:
        return False
    return session_prefix in (body or "")


async def delete_copilot_conversation(url: str, expected_title: str) -> dict:
    page = _b.page  # 模块级缓存会拿到死引用（浏览器重启会换对象），每函数取值
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

        # 删除成功/已被删除后 Copilot 常弹 "This chat is no longer available"
        # 提示框。该弹窗常由 Fluent UI 渲染在 shadow DOM 里，body 的
        # inner_text 读不到其中文本，必须改用可穿透 shadow root 的
        # Playwright locator 探测；这也是删除成功的强证据。
        _target_id_m = CONVERSATION_URL_RE.search(url or "")
        _target_id = _target_id_m.group(0) if _target_id_m else ""

        GONE_DIALOG_RE = re.compile(r"no longer available|recently deleted|不再可用|已被删除|已删除", re.I)

        async def _gone_dialog_present() -> bool:
            # 先在对话框容器内找（中英文），容器选择器可穿透 shadow DOM。
            for sel in ('[role="alertdialog"]', '[role="dialog"]', '.fui-DialogSurface'):
                loc = page.locator(sel)
                for i in range(min(await loc.count(), 5)):
                    try:
                        if await loc.nth(i).get_by_text(GONE_DIALOG_RE).count():
                            return True
                    except Exception:
                        pass
            # 兜底一：整页 light DOM 文本。
            try:
                if GONE_DIALOG_RE.search(await body_text()):
                    return True
            except Exception:
                pass
            # 兜底二：整页 shadow DOM 探测，仅限英文特征串——对话内容几乎
            # 不可能含 "no longer available"，中文特征串可能出现在文件内容里，
            # 全页匹配会有误报，故仅限容器内使用。
            try:
                if await page.get_by_text(re.compile(r"no longer available|recently deleted", re.I)).count():
                    return True
            except Exception:
                pass
            return False

        async def _dismiss_gone_dialog() -> bool:
            try:
                if not await _gone_dialog_present():
                    return False
            except Exception:
                return False
            btn = await visible([
                'button:has-text("Close")',
                'button:has-text("关闭")',
                '[role="alertdialog"] button',
                '[role="dialog"] button',
            ])
            if btn:
                try:
                    await btn.click(timeout=2000)
                except Exception:
                    pass
            else:
                # 找不到关闭按钮时 Escape 兜底，避免弹窗残留遮挡侧栏校验
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
            await asyncio.sleep(.5)
            return True

        has_conv_id = bool(CONVERSATION_URL_RE.search(url or ""))
        if has_conv_id:
            await page.goto(url, wait_until="domcontentloaded", timeout=120000)
            await asyncio.sleep(1.5)
            # 重试删除已不存在的会话时，打开 URL 即弹"不再可用"：直接认定已删除。
            # 刚完成目标 URL 的导航，弹窗说的必然是该会话；删除后 Copilot 常会
            # 立即跳走（URL 不再含会话 ID），不能再用 _on_target_page() 附加条件。
            if await _dismiss_gone_dialog():
                result["deleted"] = True
                result["method"] = "already-gone"
                return result
        # 清理残留弹窗（无会话 ID 的兜底路径不会触发上面的导航清洗），
        # 保证后续"点击确认后出现的弹窗 = 本次删除的强证据"这一推理成立。
        await _dismiss_gone_dialog()
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
            # Copilot 现在会自动为会话生成标题（例如"OneDrive文件上传SharePoint工具"），
            # 我们写在首条消息里的 [CBP-AGENT][sid][Cnnn] 会被标题改写掉，标题不再
            # 可靠。改成打开会话正文确认标记仍在——标记是发送时写进消息体的，
            # 证据更强，也不会因为改名而误判。
            if not await _conversation_belongs_to_session(url, session_prefix):
                raise RuntimeError(
                    f"URL 对应会话不属于当前 Session（标题与正文均无标记），拒绝删除；"
                    f"需要前缀: {session_prefix}"
                )
            # 验证时导航离开了原页面：重新定位那一行再继续删除
            await _open_chat_sidebar()
            link = await _conversation_sidebar_link(url) if has_conv_id else None
            if not link:
                link = await _conversation_link_by_title(expected_title)
            if not link:
                raise RuntimeError("正文验证通过，但侧栏中找不到该会话行")
            row = await _chat_row_from_link(link)
            await row.scroll_into_view_if_needed(timeout=5000)

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

        # 菜单有入场动画/渲染延迟，只查一次会误判"找不到 Delete"：
        # 轮询等待 Delete 出现；若菜单根本没打开（点击落空），重试点 More。
        delete_item = None
        delete_deadline = time.monotonic() + 8
        while time.monotonic() < delete_deadline and delete_item is None:
            delete_item = await visible_locator([
                page.get_by_role("menuitem", name=re.compile(r"Delete|删除", re.I)),
                page.get_by_role("menuitemradio", name=re.compile(r"Delete|删除", re.I)),
                page.get_by_role("button", name=re.compile(r"^Delete$|^删除$", re.I)),
                page.get_by_text(re.compile(r"^Delete$|^删除$", re.I), exact=True),
            ])
            if delete_item:
                break
            any_menu = await visible_locator([
                page.get_by_role("menuitem"),
                page.get_by_role("menuitemradio"),
            ])
            if not any_menu:
                try:
                    await more.click(timeout=2000)
                except Exception:
                    pass
            await asyncio.sleep(0.4)
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

        # 点击确认后的校验：先识别"不再可用"弹窗（删除成功强证据），
        # 再做侧栏列表轮询。入口阶段已清过残留弹窗，此刻出现的弹窗
        # 必为本次删除所致，无需再校验当前 URL。
        if await _dismiss_gone_dialog():
            result["deleted"] = True
            return result

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
            if await _dismiss_gone_dialog():
                result["deleted"] = True
                return result
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
        if await _dismiss_gone_dialog():
            result["deleted"] = True
            return result
        if await _still_listed():
            raise RuntimeError("点击删除后，目标 URL 仍存在于 Chats 列表")
        result["deleted"] = True
    except Exception as exc:
        result["error"] = str(exc)
        await diagnostics("delete_agent_url_marker_failed")
    return result


async def finalize_session_remote_deletion(sid: str, remote_results: list) -> list:
    page = _b.page  # 模块级缓存会拿到死引用（浏览器重启会换对象），每函数取值
    """对账式终验：以 [CBP-AGENT][sid] 侧栏标记为准，而不是逐个 URL 的弹窗识别。

    逐 URL 删除的判定（"no longer available" 弹窗、删除后轮询）在不同租户
    实现差异大，会误判。对账阶段回到基准地址清点侧栏：
    1. 把所有仍带本 Session 标记的会话（含 conversation_urls 未记录到的漏网）
       补删一遍；
    2. 刷新后清点残留，报失败但已不在侧栏的条目改判删除成功（verified-absent）。
    最终仍以侧栏实际残留为准，避免误识别双向出错（不会漏报，也不会误报成功）。
    """
    prefix = _agent_chat_marker(sid, 0)[:-6]  # [CBP-AGENT][<sid>]，与侧栏标题前缀一致

    def _conv_id(url: str) -> str:
        m = CONVERSATION_URL_RE.search(url or "")
        return m.group(0).rsplit("/", 1)[-1] if m else (url or "")

    def _already_ok(url: str) -> bool:
        cid = _conv_id(url)
        return any(r.get("deleted") and _conv_id(r.get("url") or "") == cid for r in remote_results)

    async def _rows_with_marker() -> dict:
        """侧栏中所有带本 Session 标记的会话：href -> conversation_index（未知为 None）。"""
        found: dict = {}
        hits = page.get_by_text(re.compile(re.escape(prefix)))
        for i in range(min(await hits.count(), 60)):
            try:
                hit = hits.nth(i)
                if not await hit.is_visible():
                    continue
                row = await _chat_row_from_link(hit)
                text = (await row.inner_text()).strip()
                if prefix not in text:
                    continue
                link = row.locator('a[href*="/conversation/"]').first
                if not await link.count():
                    continue
                href = await link.get_attribute("href")
                if not href:
                    continue
                if href.startswith("/"):
                    href = "https://m365.cloud.microsoft" + href
                m = re.search(r"\[C(\d+)\]", text)
                if href not in found:
                    found[href] = int(m.group(1)) if m else None
            except Exception:
                pass
        return found

    async def _scroll_sidebar():
        """虚拟滚动列表只渲染可视行：悬停到列表上滚动一轮，触发更多行渲染。"""
        try:
            anchor = page.locator('a[href*="/conversation/"]').first
            if await anchor.count():
                await anchor.hover(timeout=3000)
                await page.mouse.wheel(0, 1500)
                await asyncio.sleep(1.0)
        except Exception:
            pass

    try:
        base = str(CFG.get("copilot_url") or "https://m365.cloud.microsoft/chat/")
        await page.goto(base, wait_until="domcontentloaded", timeout=120000)
        await wait_copilot_ready(90)
        await _open_chat_sidebar()
        await asyncio.sleep(1.0)
    except Exception:
        return remote_results  # 基准页都打不开时维持原结果，不误判

    # 第一遍清点（先滚动再收，尽量覆盖虚拟列表）
    await _scroll_sidebar()
    for _round in range(2):
        rows = await _rows_with_marker()
        pending = {u: c for u, c in rows.items() if not _already_ok(u)}
        if not pending:
            break
        for url, cidx in pending.items():
            marker = _agent_chat_marker(sid, cidx if cidx is not None else 0)
            try:
                remote_results.append(await delete_copilot_conversation(url, marker))
            except Exception as exc:  # 单条失败不中断对账
                remote_results.append({"url": url, "expected_title": marker, "deleted": False,
                                       "error": str(exc), "method": "url-plus-session-marker"})
        await asyncio.sleep(1.0)
        await _scroll_sidebar()

    # 终验：刷新列表（虚拟列表残留不可靠），以最终残留为准改判
    try:
        await page.reload(wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2.0)
        await _open_chat_sidebar()
        await _scroll_sidebar()
    except Exception:
        pass
    remaining = await _rows_with_marker()
    remaining_ids = {_conv_id(u) for u in remaining}

    async def _visible_conversation_rows() -> int:
        """页面里可见的会话行数（不限本 Session）。

        用于判断"侧栏到底读出来了没有"：只有列表确实渲染了内容时，
        "找不到目标会话"才算删除成功的证据；列表根本没渲染（未登录、
        面板没打开、前端改版）时找不到是因为看不见，不能据此改判。
        """
        n = 0
        try:
            links = page.locator('a[href*="/conversation/"]')
            for i in range(min(await links.count(), 300)):
                try:
                    if await links.nth(i).is_visible():
                        n += 1
                except Exception:
                    pass
        except Exception:
            pass
        return n

    rows_readable = await _visible_conversation_rows() > 0
    for r in remote_results:
        if r.get("deleted"):
            continue
        cid = _conv_id(r.get("url") or "")
        if not cid or cid in remaining_ids:
            continue                      # 明确还在侧栏 → 保持失败
        if not rows_readable:
            # 列表读不出来时不得改判：否则会把"看不见"当成"已删除"（假成功，
            # 会话其实还留在 Copilot 侧栏里）
            note = "侧栏未能读出会话列表，无法确认是否已删除"
            r["error"] = f"{r['error']}；{note}" if r.get("error") else note
            continue
        r["deleted"] = True
        r["method"] = "verified-absent"
        r["error"] = None

    # 侧栏兜底仍失败的条目：逐个再导航一次目标 URL。已删除的会话会弹
    # "no longer available"，delete_copilot_conversation 的 already-gone
    # 分支直接改判成功——侧栏虚拟列表刷新后仍可能残留，URL 级证据最权威。
    for r in remote_results:
        if r.get("deleted"):
            continue
        url = r.get("url") or ""
        if not CONVERSATION_URL_RE.search(url):
            continue
        try:
            retry = await delete_copilot_conversation(url, r.get("expected_title") or "")
            if retry.get("deleted"):
                r["deleted"] = True
                r["method"] = retry.get("method", "verified-gone-page")
                r["error"] = None
        except Exception:
            pass
    return remote_results


