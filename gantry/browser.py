"""browser.py — 浏览器生命周期与窗口池调度（多窗口并行）。

从 bridge.py 拆出（第三批解耦）。约定：**模块级可变状态仍放在 bridge.py**
（`ctx` / `pw` / `HEADLESS_CURRENT` / `WINDOWS` / `SLOTS` / `ENSURE_BROWSER`），
这里一律经 ``b.xxx`` 读写，测试里的 rebind 才有效。本模块不定义任何 FastAPI 端点。
"""
from __future__ import annotations

import asyncio, contextlib
from pathlib import Path

from . import bridge as b
from .window_pool import WindowBusy, WindowPool


def _launch_kwargs(headless: bool) -> dict:
    kwargs = dict(
        user_data_dir=str(b.PROFILE),
        channel=b.CFG.get("browser_channel", "msedge"),
        headless=headless,
        viewport={"width": 1440, "height": 1000},
    )
    # 可选：强制浏览器区域语言（如 zh-CN / zh-TW / ja-JP / en-US）。
    # Copilot 网页若按浏览器语言自动选择 UI，这能把界面切到对应语言，
    # 用于真实验证多语言回合锚点；不配则沿用系统/账号语言。
    _locale = str(b.CFG.get("browser_locale") or "").strip()
    if _locale:
        kwargs["locale"] = _locale
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
    # 已有上下文且在用的窗口：**不要关重建**（多窗口下会把用户正看的标签页全关掉，
    # 表现就是"开任务时窗口闪退"）。只有显式切换 headless 模式才需要重建。
    if b.ctx is not None:
        try:
            busy = bool(b.WINDOWS.running()) or bool(getattr(b.WINDOWS, "stats", lambda: {})()
                                                    .get("busy"))
        except Exception:
            busy = False
        if (not busy) and bool(headless) == bool(b.HEADLESS_CURRENT):
            try:
                if b.ctx.pages:
                    b.set_shared_page(b.ctx.pages[0])
                    b.WINDOWS.attach_existing(b._PAGE_RAW)   # 复用已有页面当 1 号窗口
            except Exception:
                pass
            # 复用分支也要确保页面**真的在 Copilot 上**：上一轮启动失败会留下
            # about:blank / 登录页；只返回不复位的话，点「重试浏览器」永远停在
            # needs_login（实测：ERR_TIMED_OUT 之后重试一直是 needs_login）。
            try:
                cur = b._PAGE_RAW.url if b._PAGE_RAW is not None else ""
            except Exception:
                cur = ""
            if b._PAGE_RAW is None or not b.COPILOT_URL_MARK.search(cur or ""):
                try:
                    if b._PAGE_RAW is None and b.ctx.pages:
                        b.set_shared_page(b.ctx.pages[0])
                    if b._PAGE_RAW is not None:
                        await b._PAGE_RAW.goto(b.URL, wait_until="domcontentloaded", timeout=120000)
                        b._forget_selected_model()
                except Exception as exc:
                    b.log(f"复用窗口导航到 Copilot 失败（继续，由就绪检测判定）: {exc}")
            return b.HEADLESS_CURRENT
        if busy:
            b.log("有任务正在使用浏览器，跳过重建（沿用当前窗口）")
            return b.HEADLESS_CURRENT
        try:
            await b.ctx.close()
        except Exception:
            pass
        b.ctx = None
        b.set_shared_page(None)
    launch_kwargs = b._launch_kwargs(headless)
    try:
        b.ctx = await b.pw.chromium.launch_persistent_context(**launch_kwargs)
    except Exception:
        if not headless:
            raise
        # 部分环境（旧版 Edge/安全策略）不支持无头持久化上下文，回退有窗口模式。
        b.log("无头模式启动失败，已回退为有窗口模式；请升级 Edge 或将 headless 改回 false")
        launch_kwargs["headless"] = False
        b.ctx = await b.pw.chromium.launch_persistent_context(**launch_kwargs)
    b.HEADLESS_CURRENT = launch_kwargs["headless"]
    b.log("浏览器模式：" + ("无头（不显示窗口）" if b.HEADLESS_CURRENT else "有窗口"))
    b.set_shared_page(b.ctx.pages[0] if b.ctx.pages else await b.ctx.new_page())
    # 共享页登记成 1 号窗口：windows=1 时任务复用它，不新开标签页
    try:
        b.WINDOWS.attach_existing(b._PAGE_RAW)
    except Exception:
        pass
    # ERR_ABORTED（domcontentloaded 之前就被 SSO 重定向走）不是失败：页面仍在跳，
    # 后续 wait_copilot_ready 会等它落到 Copilot 输入框可用为止。
    # 复用窗口/新窗口两处 goto 都有同样的容错，只有首次启动漏了（about:blank 实录）。
    try:
        await b.page.goto(b.URL, wait_until="domcontentloaded", timeout=120000)
    except Exception as exc:
        if "ERR_ABORTED" in str(exc):
            b.log(f"首次导航被重定向中止（正常，SSO 跳转中）：{exc}")
        else:
            raise
    b._forget_selected_model()  # 页面重载，模型选择状态未知
    return b.HEADLESS_CURRENT


def page_url_safe() -> str:
    """安全地拿当前页面 URL。

    浏览器是**按需启动**的：恢复任务/续跑时 `page` 可能还是 None，
    直接读 `page.url` 会抛 `'NoneType' object has no attribute 'url'`
    （实测：resume 一启动就 failed，任务又被卡住）。
    """
    try:
        return b.page.url or ""
    except Exception:
        return ""


async def _new_copilot_page(index: int):
    """在**同一个持久化上下文**里开一个新标签页（登录态共享），并导航到 Copilot。

    多窗口并行的关键：不需要第二个浏览器进程，也不需要重新登录；
    每个标签页有自己的一套 Copilot 会话/模型选择。
    """
    # 先确保上下文存在（窗口 0 的页由 _start_browser 创建）
    if b.ctx is None:
        await b.ensure_browser_ready()
    if b.ctx is None:
        raise RuntimeError("浏览器上下文不可用，无法开新窗口")
    new_page = await b.ctx.new_page()
    try:
        await new_page.goto(b.URL, wait_until="domcontentloaded", timeout=120000)
    except Exception as exc:
        b.log(f"新窗口 #{index} 打开 Copilot 失败: {exc}")
    # 新标签页要**等它真的可用**再交给任务：SSO/ADFS 回跳可能要几十秒，
    # 早交出去会导致"消息发出去没反应 → 180 秒超时"。
    tok = b._PAGE_CTX.set(new_page)
    try:
        ready = await b.wait_copilot_ready(timeout=90)
        if ready:
            # 首页可能残留上次的对话状态：开一个新对话再做任务
            try:
                await b.new_chat()
            except Exception as exc:
                b.log(f"新窗口 #{index} 新建对话失败（继续用当前页）: {exc}")
        else:
            b.log(f"新窗口 #{index} 未就绪（可能仍在登录/跳转）：关闭该窗口，任务改用其它窗口")
            try:
                await new_page.close()
            except Exception:
                pass
            raise RuntimeError(f"新窗口 #{index} 未就绪（需要登录 Copilot）")
    finally:
        b._PAGE_CTX.reset(tok)
    b.log(f"已开第 {index} 个自动化窗口（多窗口并行，就绪={ready}）")
    return new_page


def _max_windows_cfg() -> int:
    try:
        return max(1, min(int(b.AGENT_CFG.get("copilot_windows", 1) or 1), 8))
    except Exception:
        return 1


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
    pool = b.WINDOWS if need_page else b.SLOTS
    if need_page:
        # 先确保浏览器上下文存在（懒启动）；否则池子创建页面会失败
        try:
            await b.ensure_browser_ready()
        except Exception as exc:
            b.log(f"启动浏览器失败（窗口池）: {exc}")
    try:
        win = await pool.acquire(sid, wait_timeout=wait_timeout)
    except WindowBusy as exc:
        raise b.CopilotBusy(str(exc)) from exc
    tok_page = b._PAGE_CTX.set(win.page) if win.page is not None else None
    tok_win = b._WIN_CTX.set(win)
    tok_sess = b._SESSION_CTX.set(sid or None)
    tools = b.AGENT_TOOLS.clone()
    tok_tools = b._TOOLS_CTX.set(tools)
    try:
        yield win, tools
    finally:
        try:
            b._TOOLS_CTX.reset(tok_tools)
            b._SESSION_CTX.reset(tok_sess)
            b._WIN_CTX.reset(tok_win)
            if tok_page is not None:
                b._PAGE_CTX.reset(tok_page)
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
    return b.WINDOWS.is_running(sid) or b.SLOTS.is_running(sid)


def any_window_busy() -> bool:
    """是否有任务正占用**浏览器窗口**（提供商的逻辑槽不算：它们不需要页面）。"""
    return bool(b.WINDOWS.running())


def any_task_busy() -> bool:
    """是否有任何任务在跑（网页窗口 + 提供商逻辑槽）。

    用于"改全局状态"的管理操作：切工作区、回滚服务代码等，有任何任务在跑都应拒绝。
    """
    return bool(b.WINDOWS.running()) or bool(b.SLOTS.running())


async def ensure_browser_ready() -> bool:
    """确保浏览器可用（未注册钩子时视为已就绪，保持测试与无浏览器场景可用）。"""
    fn = b.ENSURE_BROWSER
    if fn is None:
        return True
    try:
        return bool(await fn())
    except Exception as exc:
        b.log(f"浏览器按需启动失败: {exc}")
        return False


async def shutdown_browser():
    """关闭浏览器上下文与 Playwright（服务退出时调用）。"""
    try:
        if b.ctx is not None:
            await b.ctx.close()
    except Exception:
        pass
    b.ctx = None
    try:
        if b.pw is not None:
            await b.pw.stop()
    except Exception:
        pass
    b.pw = None
