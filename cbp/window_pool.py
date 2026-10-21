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
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional


class WindowBusy(RuntimeError):
    """排队超时：所有窗口都在忙，且等待队列已满。"""


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

    def __repr__(self) -> str:  # 便于日志排查
        return f"<Window #{self.index} sid={self.sid or '-'}>"


class WindowPool:
    """窗口池。`factory(index)` 负责真正创建一个 page。"""

    def __init__(self, factory: Callable[[int], Awaitable[Any]], max_windows: int = 1):
        self._factory = factory
        self._max = max(1, int(max_windows or 1))
        self._windows: list[Window] = []
        self._next_index = 1        # 单调递增（并发创建时 len+1 会撞号）
        self._free: asyncio.Queue[Window] = asyncio.Queue()
        self._waiting = 0
        self._creating: Optional[asyncio.Task] = None
        self._guard = asyncio.Lock()

    # ── 配置 ──────────────────────────────────────────────
    @property
    def max_windows(self) -> int:
        return self._max

    async def set_max(self, n: int) -> dict[str, Any]:
        """调整上限：调大留到下次 acquire 时懒创建；调小则关闭多余的空闲窗口。"""
        n = max(1, min(int(n or 1), 8))
        self._max = n
        closed = 0
        # 只关闭**空闲**的窗口；正在跑的任务不打断
        for w in list(self._windows):
            if len(self._windows) <= n:
                break
            if w.sid or w.lock.locked():
                continue
            try:
                await w.page.close()
            except Exception:
                pass
            try:
                self._windows.remove(w)
            except ValueError:
                pass
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

    # ── 取用 / 归还 ───────────────────────────────────────
    async def acquire(self, sid: str = "", wait_timeout: float = 30.0) -> Window:
        """取一个窗口。队列满（>>上限 3 倍）时抛 WindowBusy。"""
        if self._free.empty() and len(self._windows) < self._max:
            win = await self._create()
            win.sid = sid or None
            win.last_used = time.time()
            await win.lock.acquire()
            return win
        if self._free.empty() and self._waiting >= max(4, self._max * 3):
            raise WindowBusy(f"所有 Copilot 窗口都在忙（{self._max} 个），等待队列已满")
        self._waiting += 1
        try:
            win = await asyncio.wait_for(self._free.get(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            raise WindowBusy(
                f"等待空闲 Copilot 窗口超时（{wait_timeout:.0f}s，共 {self._max} 个）") from None
        finally:
            self._waiting -= 1
        win.sid = sid or None
        win.last_used = time.time()
        await win.lock.acquire()
        return win

    def release(self, win: Window) -> None:
        win.sid = None
        win.last_used = time.time()
        if win.lock.locked():
            try:
                win.lock.release()
            except RuntimeError:
                pass
        # 超出上限的窗口顺手关掉（调小配置时）
        if len(self._windows) > self._max and not win.sid:
            try:
                self._windows.remove(win)
            except ValueError:
                pass
            try:
                asyncio.get_event_loop().create_task(win.page.close())
            except Exception:
                pass
            return
        self._free.put_nowait(win)

    # ── 状态 ──────────────────────────────────────────────
    def running(self) -> dict[str, int]:
        """正在跑的会话 → 窗口序号（用于"busy/resume"判定）。"""
        return {w.sid: w.index for w in self._windows if w.sid}

    def is_running(self, sid: str) -> bool:
        return any(w.sid == sid for w in self._windows)

    def stats(self) -> dict[str, Any]:
        busy = [w for w in self._windows if w.sid]
        return {"max": self._max, "windows": len(self._windows),
                "busy": len(busy), "waiting": self._waiting,
                "running": {w.sid: w.index for w in busy}}

    # ── 内部 ──────────────────────────────────────────────
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
        while len(self._windows) < min(count, self._max):
            win = await self._create()
            self._free.put_nowait(win)
            made += 1
        return made
