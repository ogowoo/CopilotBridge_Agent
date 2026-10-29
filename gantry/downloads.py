"""downloads.py — Copilot 回答区下载：候选链接解析、href 落盘、调试快照、copilot_file（生成文件并下载进工作区）

从 bridge.py 拆出（第四批解耦）。约定：模块级可变状态与配置仍在 bridge.py，
这里经 ``b.xxx`` 读写；本模块内部互相调用保持直接调用。本模块不定义 FastAPI 端点。
"""
from __future__ import annotations

import asyncio, base64, collections, contextlib, json, os, re, shutil, time, uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import HTTPException

from . import bridge as b


DOWNLOAD_HINT_RE = re.compile(r"download|下载|保存文件|save file", re.I)


async def _answer_area_html() -> str:
    """取最近一条回答所在区域的外层 HTML，用于提取下载入口与失败诊断。"""
    try:
        nodes = b.page.locator('[data-content="ai-message"], [class*="ai-message" i], '
                             '[class*="assistant" i], article')
        count = await nodes.count()
        if count:
            return await nodes.nth(count - 1).evaluate("el => el.outerHTML")
    except Exception:
        pass
    try:
        return await b.page.locator("body").evaluate("el => el.outerHTML")
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
        loc = b.page.locator(sel)
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
        base = b.page.url or ""
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
    out = {"url": getattr(b.page, "url", "") if b.page else "", "saved": None, "candidates": []}
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
    target = b.BASE / "data" / "debug-download" / (pathlib_basename(pick.get("text")) or "download.bin")
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
    out = {"url": getattr(b.page, "url", "") if b.page else "", "candidates": [], "png": ""}
    try:
        if b.page:
            raw = await b.page.screenshot(full_page=False)
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
        b64 = await b.page.evaluate(
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
        resp = await b.ctx.request.get(href, timeout=120000)
        if not resp.ok:
            raise b.ToolError(f"下载失败 HTTP {resp.status}: {href[:120]}")
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
    if not b.AGENT_CFG.get("allow_copilot_file", True):
        raise b.ToolError("copilot_file 已在 config.json 中禁用（allow_copilot_file=false）")
    instruction = str(instruction or "").strip()
    if not instruction:
        raise b.ToolError("instruction 不能为空")
    target = b.AGENT_TOOLS._path(path)          # 复用工作区路径沙箱
    filename = target.name
    model = b.DEFAULT_MODEL
    if b.current_session():
        st = b.AGENT_SESSIONS.get(b.current_session())
        if st and st.get("model"):
            model = st["model"]
    # 生成可下载文件需要**非 Auto** 模型：实测 Auto 只在聊天里回答、不提供下载入口。
    # 会话模型是 auto 时，改用 agent.copilot_file_model 指定的具体模型（没配则不换，
    # 失败时下文会明确提示原因）。
    _file_model = str(b.AGENT_CFG.get("copilot_file_model") or "").strip()
    _auto_models = ("copilot-auto", "copilot-chat-basic-web")
    if _file_model and model in _auto_models:
        model = _file_model
    prompt = (f"请把结果生成为一个可下载的文件，文件名：{filename}\n"
              f"要求：只生成文件并提供下载入口，不要在回答里粘贴文件正文或大段内容。\n"
              f"内容要求：{instruction}")
    await b.agent_chat_once(prompt, model, False)

    # 找下载入口：优先文件名匹配，其次取最新的一个
    candidates = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        candidates = await _download_candidates()
        if candidates:
            break
        await asyncio.sleep(0.5)
    if not candidates:
        dump = await _answer_area_html()
        try:
            (b.BASE / "last_answer.html").write_text(dump[:400000], encoding="utf-8")
        except Exception:
            pass
        _hint = ""
        if model in _auto_models:
            _hint = ("当前模型是 Auto：它通常只在聊天里回答、不生成下载入口。"
                     "请选择具体模型（如 GPT-5.6 Sol Think deeper），"
                     "或配置 agent.copilot_file_model。")
        raise b.ToolError("Copilot 回答里没有找到下载入口（已存 last_answer.html 供排查）；"
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
        (b.BASE / "data" / "copilot_file_candidates.json").write_text(
            json.dumps([{"href": c.get("href") or "", "resolved": _resolve_href(c.get("href") or ""),
                         "text": c.get("text") or ""} for c in candidates],
                       ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    b.AGENT_TOOLS._snap("copilot_file", path, target)   # 覆盖前存原像，可回滚
    before = b.AGENT_TOOLS._hash(target)
    size = 0
    _href = _resolve_href(chosen.get("href") or "")
    if _href:
        size = await _save_from_href(_href, target)
    else:
        # 按钮型入口，或 href 无效（#/相对路径等）：点它并把浏览器下载事件接住
        try:
            async with b.page.expect_download(timeout=120000) as dl_info:
                await chosen["el"].click(timeout=8000)
            download = await dl_info.value
            await download.save_as(str(target))
            size = target.stat().st_size
        except Exception as exc:
            raise b.ToolError(f"点击下载入口失败: {exc}") from exc
    if size <= 0:
        raise b.ToolError("下载到 0 字节，可能不是有效文件；请让 Copilot 重新生成或改用分块写入")
    return {"path": path, "bytes": size, "filename": filename,
            "source": "copilot-download",
            "file_event": {"tool": "copilot_file", "path": path, "before_hash": before,
                           "after_hash": b.AGENT_TOOLS._hash(target), "result": "ok"}}
