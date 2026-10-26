"""强行插入指令的"立即中断"信号（Ctrl+J）。

为什么单独成模块：网页通道与提供商通道都要用它——提供商路径用它取消底层
HTTP 调用（取消即"这轮没发生"）；网页路径只在"本回合还没产出内容"时中止等待
（见 bridge.wait_agent_answer）。它不依赖任何页面/会话状态，是纯信号原语。
"""

from __future__ import annotations

import asyncio

from .agent import AgentInterrupted

_INTERRUPTS: dict[str, asyncio.Event] = {}


def _interrupt_event(sid: str) -> asyncio.Event:
    ev = _INTERRUPTS.get(sid)
    if ev is None:
        ev = asyncio.Event()
        _INTERRUPTS[sid] = ev
    return ev


def request_interrupt(sid: str) -> None:
    """请求立即中断该会话当前回合（由 /inject 在运行中调用）。"""
    _interrupt_event(sid).set()


def interrupt_pending(sid: str) -> bool:
    """只窥探不清除：等待循环每 POLL 都会问一次，若在这里清掉，
    "已开始生成所以不中断"的那次询问就会把信号吃掉、再也不会生效。"""
    ev = _INTERRUPTS.get(sid)
    return bool(ev and ev.is_set())


def clear_interrupt(sid: str) -> None:
    ev = _INTERRUPTS.get(sid)
    if ev is not None:
        ev.clear()


def discard_interrupts(sid: str) -> None:
    _INTERRUPTS.pop(sid, None)


async def _await_interruptible(sid: str, coro, what: str):
    """等待 coro；若期间该会话被要求"立即插入"，取消它并抛 AgentInterrupted。

    注意这里把底层协程包成 task 再取消，因此**外层看到的是 AgentInterrupted 而不是
    CancelledError**——provider_turn 的 `except Exception` 清理逻辑照常生效，
    历史里那条 user 消息不会残留（否则下一回合会重复追加）。
    """
    ev = _interrupt_event(sid)
    task = asyncio.ensure_future(coro)
    waiter = asyncio.ensure_future(ev.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        task.cancel(); waiter.cancel()
        raise
    if task in done:
        waiter.cancel()
        return await task
    task.cancel()
    try:
        await task
    except BaseException:
        pass
    clear_interrupt(sid)
    raise AgentInterrupted(f"已插入新指令，{what}被中断，改为下一回合按新指令执行")
