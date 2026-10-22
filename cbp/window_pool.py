"""Copilot 自动化窗口池：一个窗口 = 一个独立的 Copilot 网页标签页。

为什么需要
----------
以前整个桥接只有**一个** `page` + 一把全局 `lock`，所有任务（含子代理）排成一列。
这带来两个问题：
1. 一个任务卡住（限流/等服务不可用）时，其它会话只能干等；
2. 用户想「并行几个就开几个窗口」做不到，因为页面状态（当前模型、当前会话）
   是所有任务共享的。

设计
----
- 窗口是**同一个持久化浏览器上下文里的多个 page**（登录态共享，不用重复登录）；
- 每个窗口自带一把 `asyncio.Lock`，同一窗口内串行，窗口之间并行；
- 任务通过 `acquire(sid)` 拿到一个窗口（不要就排队，队列长度有上限），
  `release()` 归还；归还时窗口保留，下次复用（省掉一次导航）；
- 页面的"当前模型"缓存、当前会话 URL 都挂在窗口上，互不干扰。

并发上限来自配置 `agent.copilot_windows`（1–4），由 `set_max()` 动态调整：
调大 = 下次需要时懒创建；调小 = 超出部分空闲时关闭，正在用的任务跑完为止。

并发与竞态
----------
`acquire()` 会在几十个并发任务里同时调用。建窗（`ctx.new_page()` + 等 SSO 就绪）
很慢，因此**同一时刻只允许一个慢建窗**：后来者拿到建窗锁后先看空闲队列，
避免"都判断没满 → 各自开一个 → 超出上限"。页面被用户关掉/渲染进程崩了会被
就地摘除，不会把死页面发给任务。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional


class WindowBusy(RuntimeError):
    """排队超时：所有窗口都在忙，且等待队列已满。"""


def _page_closed(page: Any) -> bool:
    """页面是否已关闭/不可用（假页面没有 is_closed 时视为可用）。"""
    if page is None:
        return False
    try:
        fn = getattr(page, "is_closed", None)
        return bool(fn()) if callable(fn) else False
    except Exception:
        return False


class Window:
    """一个自动化窗口（一个 page + 一把锁 + 该页面的状态）。"""

    def __init__(self, index: int, page: Any):
        self.index = index
        self.page = page
        self.lock = asyncio.Lock()
        self.sid: Optional[str] = None          # 正在这个窗口跑的会话
        self.created_at = time.time()
        self.last_used = time.time()
        # 页面级状态：不能放在全局，否则两个窗口会互相污染
        self.selected_model: Optional[str] = None
        self.current_url: Optional[str] = None
        self.turn_marker: Optional[str] = None
        # 是否正躺在空闲队列里：防"重复归还"把同一个窗口发两次（两个任务抢同一页面）
        self.in_pool = False

    def __repr__(self) -> str:  # 便于日志排查
        return f"<Window #{self.index} sid={self.sid or '-'}>"


class WindowPool:
    """窗口池。`factory(index)` 负责真正创建一个 page。"""

    def __init__(self, factory: Callable[[int], Awaitable[Any]], max_windows: int = 1):
        self._factory = factory
        self._max = max(1, int(max_windows or 1))
        self._windows: list[Window] = []
        self._next_index = 1        # 单调递增（并发建窗时 len+1 会撞号）
        self._free: asyncio.Queue[Window] = asyncio.Queue()
        self._waiting = 0
        self._create_lock = asyncio.Lock()   # 串行化慢建窗，避免超过上限
        self._guard = asyncio.Lock()         # 列表/队列的结构性操作

    # ── 配置 ────────────────────────────────────────────────────────
    @property
    def max_windows(self) -> int:
        return self._max

    async def set_max(self, n: int) -> dict[str, Any]:
        """调整上限：调大留到下次 acquire 时懒创建；调小则关闭多余的空闲窗口。"""
        n = max(1, min(int(n or 1), 8))
        self._max = n
        closed = 0
        async with self._guard:
            # 只关闭**空闲**的窗口；正在跑的任务不打断
            for w in list(self._windows):
                if len(self._windows) <= n:
                    break
                if w.sid or w.lock.locked():
                    continue
                try:
                    if w.page is not None:
                        await w.page.close()
                except Exception:
                    pass
                try:
                    self._windows.remove(w)
                except ValueError:
                    pass
                w.in_pool = False
                # 从空闲队列里摘掉（队列无法按值删除，取出再放回）
                kept: list[Window] = []
                while not self._free.empty():
                    item = self._free.get_nowait()
                    if item is not w:
                        kept.append(item)
                for item in kept:
                    self._free.put_nowait(item)
                closed += 1
        return {"max": self._max, "closed": closed, "windows": len(self._windows)}

    # ── 复用已就绪的页面 ───────────────────────────────────────────
    def attach_existing(self, page: Any) -> Optional[Window]:
        """把**已经就绪**的页面登记成 1 号窗口（启动时调用）。

        这样 `windows=1` 时任务直接用它，不新开标签页（新标签要自己走一遍 SSO，
        企业租户里常常起不来 → "窗口没登录/发送失败"）。
        """
        if page is None or self._windows:
            return None
        win = Window(self._next_index, page)
        self._next_index += 1
        win.in_pool = True
        self._windows.append(win)
        self._free.put_nowait(win)
        return win

    # ── 取用 / 归还 ────────────────────────────────────────────────
    def _take_free(self) -> Optional[Window]:
        """从空闲队列取一个**活着**的窗口；坏页面就地丢弃。"""
        while not self._free.empty():
            try:
                win = self._free.get_nowait()
            except asyncio.QueueEmpty:
                return None
            win.in_pool = False
            if _page_closed(win.page):
                # 用户手动关了标签页/渲染进程崩了：从池里摘掉，继续找下一个
                try:
                    self._windows.remove(win)
                except ValueError:
                    pass
                continue
            return win
        return None

    async def acquire(self, sid: str = "", wait_timeout: float = 30.0) -> Window:
        """取一个窗口。队列满（>上限 3 倍，至少 4）时抛 WindowBusy。"""
        win = self._take_free()
        if win is None and len(self._windows) < self._max:
            # 慢建窗：同一时刻只允许一个在创建；等锁期间别人可能已经归还了窗口，
            # 所以拿到锁后**先看空闲队列**再建新窗。
            async with self._create_lock:
                win = self._take_free()
                if win is None and len(self._windows) < self._max:
                    win = await self._create()
        if win is None:
            if self._waiting >= max(4, self._max * 3):
                raise WindowBusy(f"所有 Copilot 窗口都在忙（{self._max} 个），等待队列已满")
            self._waiting += 1
            try:
                win = await asyncio.wait_for(self._free.get(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                raise WindowBusy(
                    f"等待空闲 Copilot 窗口超时（{wait_timeout:.0f}s，共 {self._max} 个）") from None
            finally:
                self._waiting -= 1
            win.in_pool = False
            if _page_closed(win.page):
                # 排队等到的是个死窗口：摘掉它，让调用方重试或明确报忙
                try:
                    self._windows.remove(win)
                except ValueError:
                    pass
                raise WindowBusy("等到的是已关闭的 Copilot 窗口（请重试或重开浏览器）")
        win.sid = sid or None
        win.last_used = time.time()
        await win.lock.acquire()
        return win

    def release(self, win: Window) -> None:
        """归还窗口：**绝不关页面**。

        教训：以前这里有一条"超出上限就 close()"的清理，而 `win.sid` 在判断前
        已被清空 → 条件恒真 → **第二个窗口一归还，用户正看着的标签页就被关掉**，
        表现为"开启任务后自动化窗口闪退"。缩容只由 set_max() 在**空闲**时做。
        """
        win.sid = None
        win.last_used = time.time()
        if win.lock.locked():
            try:
                win.lock.release()
            except RuntimeError:
                pass
        if win not in self._windows:
            return
        if _page_closed(win.page):
            try:
                self._windows.remove(win)
            except ValueError:
                pass
            win.in_pool = False
            return
        if win.in_pool:      # 重复归还：不重复入队（否则两个任务会拿到同一页面）
            return
        win.in_pool = True
        self._free.put_nowait(win)

    # ── 状态 ────────────────────────────────────────────────────────
    def running(self) -> dict[str, int]:
        """正在跑的会话 → 窗口序号（用于"busy/resume"判定）。"""
        return {w.sid: w.index for w in self._windows if w.sid}

    def is_running(self, sid: str) -> bool:
        return bool(sid) and any(w.sid == sid for w in self._windows)

    def stats(self) -> dict[str, Any]:
        busy = [w for w in self._windows if w.sid]
        return {"max": self._max, "windows": len(self._windows),
                "busy": len(busy), "waiting": self._waiting,
                "running": {w.sid: w.index for w in busy}}

    # ── 内部 ────────────────────────────────────────────────────────
    async def _create(self) -> Window:
        index = self._next_index
        self._next_index += 1
        page = await self._factory(index)
        win = Window(index, page)
        self._windows.append(win)
        return win

    async def prewarm(self, count: int = 1) -> int:
        """预先创建窗口（启动时可选）：返回实际创建数。"""
        made = 0
        async with self._create_lock:
            while len(self._windows) < min(count, self._max):
                win = await self._create()
                win.in_pool = True
                self._free.put_nowait(win)
                made += 1
        return made
