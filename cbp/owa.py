"""Outlook 网页版（OWA）自动化后端（从 bridge.py 拆出）。

New Outlook 无 COM 接口、EWS 2026 起被微软全局禁用、Graph 需管理员授权；
OWA 复用桥接的浏览器与 SSO 登录态。ctx 经模块 __getattr__ 惰性取
bridge.ctx——headless 切换会重建上下文，直接 import 变量会拿到死引用。
"""

import asyncio
import re
import time

from . import bridge as _b
from .bridge import diagnostics


def __getattr__(name):
    if name == "ctx":
        return _b.ctx
    raise AttributeError(name)


OWA_URL = _b.CFG.get("outlook_url", "https://outlook.office.com/mail/")


owa_page = None


async def _owa_visible(page, selectors):
    ctx = _b.ctx  # 同上：headless 切换会重建上下文
    for selector in selectors:
        loc = page.locator(selector)
        for i in range(await loc.count() - 1, -1, -1):
            try:
                if await loc.nth(i).is_visible():
                    return loc.nth(i)
            except Exception:
                pass
    return None


async def owa_open() -> None:
    ctx = _b.ctx  # 同上：headless 切换会重建上下文
    """打开/复用 OWA 标签页，等待 SSO 回跳与列表渲染完成。"""
    global owa_page
    if owa_page is None or owa_page.is_closed():
        owa_page = await ctx.new_page()
    if "outlook." not in (owa_page.url or ""):
        await owa_page.goto(OWA_URL, wait_until="domcontentloaded", timeout=120000)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if "outlook." in (owa_page.url or ""):
                box = await _owa_visible(owa_page, [
                    'input[type="search"]',
                    'input[aria-label*="Search" i]',
                    'input[aria-label*="搜索"]',
                ])
                if box:
                    return
        except Exception:
            pass
        await asyncio.sleep(2)
    await diagnostics("owa_not_ready")
    raise RuntimeError("Outlook 网页版加载超时（SSO 未完成或页面结构变化）")


async def owa_search_mails(query: str, max_count: int) -> list[dict]:
    ctx = _b.ctx  # 同上：headless 切换会重建上下文
    """用 OWA 搜索框跑 KQL（isread:false / subject: 等），解析结果列表。"""
    await owa_open()
    box = await _owa_visible(owa_page, [
        'input[type="search"]', 'input[aria-label*="Search" i]', 'input[aria-label*="搜索"]',
    ])
    if not box:
        raise RuntimeError("找不到 Outlook 搜索框")
    await box.click()
    await box.fill(query)
    await box.press("Enter")
    await asyncio.sleep(2.5)  # 结果渲染
    # 邮件列表项：role=option，aria-label 未读项通常以 Unread/未读 开头
    items = []
    options = owa_page.locator('[role="option"]')
    count = await options.count()
    for i in range(min(count, max_count)):
        item = options.nth(i)
        try:
            if not await item.is_visible():
                continue
            label = (await item.get_attribute("aria-label")) or ""
            text = (await item.inner_text()).strip()
            lines = [x.strip() for x in text.splitlines() if x.strip()]
            items.append({
                "sender": lines[0] if lines else "",
                "subject": lines[1] if len(lines) > 1 else label,
                "preview": lines[2] if len(lines) > 2 else "",
                "received": lines[-1] if lines else "",
                "unread": bool(re.match(r"^(unread|未读)", label, re.I)),
                "_index": i,
            })
        except Exception:
            continue
    return items


async def owa_read_current() -> str:
    ctx = _b.ctx  # 同上：headless 切换会重建上下文
    """读取阅读窗格正文。"""
    pane = await _owa_visible(owa_page, [
        '[id*="ReadingPane"]',
        'div[role="main"]',
        '[aria-label*="reading pane" i]',
        '[aria-label*="阅读窗格"]',
    ])
    if not pane:
        return ""
    return (await pane.inner_text()).strip()


class OWABackend:
    """tools.py 的 Outlook 工具后端接口（owa 实现）。"""

    async def unread(self, max_count: int) -> dict:
        items = await owa_search_mails("isread:false", max_count)
        for it in items:
            it.pop("_index", None)
        return {"unread_returned": len(items), "items": items, "backend": "owa"}

    async def read(self, keyword: str) -> dict:
        items = await owa_search_mails(f'subject:"{keyword}"' if keyword else "isread:false", 10)
        target = None
        for it in items:
            if keyword and keyword.lower() in (it.get("subject") or "").lower():
                target = it
                break
        if target is None and items:
            target = items[0]
        if target is None:
            raise RuntimeError(f"没有找到主题包含「{keyword}」的邮件")
        options = owa_page.locator('[role="option"]')
        await options.nth(target["_index"]).click()
        await asyncio.sleep(1.2)
        body = await owa_read_current()
        target.pop("_index", None)
        target["body"] = body[:20000]
        target["truncated"] = len(body) > 20000
        return target

    async def search(self, keyword: str, max_count: int) -> dict:
        safe = str(keyword).replace('"', "")
        items = await owa_search_mails(f'subject:"{safe}"', max_count)
        for it in items:
            it.pop("_index", None)
        return {"keyword": keyword, "returned": len(items), "items": items, "backend": "owa"}

    async def send(self, to: str, subject: str, body: str, cc: str = "") -> dict:
        await owa_open()
        btn = await _owa_visible(owa_page, [
            'button[aria-label*="New mail" i]', 'button[aria-label*="新建邮件"]',
            'button[aria-label*="New message" i]', 'button[aria-label*="撰写"]',
        ])
        if not btn:
            raise RuntimeError("找不到新建邮件按钮")
        await btn.click()
        await asyncio.sleep(1.2)
        to_box = await _owa_visible(owa_page, [
            'input[aria-label*="To" i]', 'input[aria-label*="收件人"]',
        ])
        if not to_box:
            raise RuntimeError("找不到收件人输入框")
        await to_box.click()
        await to_box.press_sequentially(to)
        await asyncio.sleep(.5)
        await owa_page.keyboard.press("Enter")
        if cc:
            await owa_page.keyboard.press("Tab")
            await owa_page.keyboard.insert_text(cc)
            await owa_page.keyboard.press("Enter")
        subject_box = await _owa_visible(owa_page, [
            'input[aria-label*="subject" i]', 'input[aria-label*="主题"]',
            'input[placeholder*="subject" i]', 'input[placeholder*="主题"]',
        ])
        if subject_box:
            await subject_box.click()
            await subject_box.fill(subject)
        body_box = await _owa_visible(owa_page, [
            '[contenteditable="true"][aria-label*="body" i]',
            '[contenteditable="true"][aria-label*="正文"]',
            '[contenteditable="true"][role="textbox"]',
            '[contenteditable="true"]',
        ])
        if body_box:
            await body_box.click()
            await body_box.press_sequentially(body)
        send_btn = await _owa_visible(owa_page, [
            'button[aria-label*="Send" i]', 'button[aria-label*="发送"]',
        ])
        if not send_btn:
            raise RuntimeError("找不到发送按钮")
        await send_btn.click()
        await asyncio.sleep(1)
        return {"sent": True, "to": to, "subject": subject, "backend": "owa"}


OWA_BACKEND = OWABackend()


