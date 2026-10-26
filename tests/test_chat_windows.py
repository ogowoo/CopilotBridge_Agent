# -*- coding: utf-8 -*-
"""普通对话通道 / 管理操作的「窗口独占」语义回归。

背景：多窗口改造后，很多地方仍按旧的"一把全局 lock + 一个共享页"写判定：
- `/v1/chat/completions` 只拿 `b.lock` 就直接驱动共享页，会和正在窗口 #1 上跑的
  Agent 任务抢同一个标签页；
- `b.lock.locked()` 永远为 False（没人再拿那把锁了），于是 /health 的 busy、
  运行中插入指令、删除会话的运行权判定全部 silently 失效。

这些用"锁"表达不出来的，必须改写成窗口池语义，并且**有测试盯着**。
"""
import asyncio
import contextlib
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException

from gantry import api, bridge as b
from gantry.window_pool import Window


class FakeWindow:
    """记录 acquire 的 sid，并提供一个可用的上下文。"""

    def __init__(self):
        self.sids = []
        self.need_page = []

    @contextlib.asynccontextmanager
    async def cm(self, sid, wait_timeout=30.0, need_page=True):
        self.sids.append(sid)
        self.need_page.append(need_page)
        yield (object(), None)


def _req(**kw):
    base = {"model": "copilot-auto", "new_conversation": False, "stream": False,
            "messages": [types.SimpleNamespace(role="user", content="你好")]}
    base.update(kw)
    return types.SimpleNamespace(**base)


class PlainChatWindowTests(unittest.TestCase):
    def test_nonstream_occupies_a_window(self):
        """普通对话必须走窗口池（以前直接驱动共享页）。"""
        fw = FakeWindow()

        async def fake_send(prompt, make_new, model):
            return "copilot-auto"

        async def fake_updates():
            yield "回答"

        saved = (b._window, b.send, b.updates)
        b._window, b.send, b.updates = fw.cm, fake_send, fake_updates
        try:
            out = asyncio.run(api.nonstream(_req(), "你好"))
        finally:
            b._window, b.send, b.updates = saved
        self.assertEqual(len(fw.sids), 1, "必须占一个窗口")
        self.assertTrue(fw.sids[0].startswith("chat-"), f"窗口簿记 id 应是临时对话 id：{fw.sids[0]}")
        self.assertTrue(fw.need_page[0], "普通对话需要真实页面")
        self.assertEqual(out["choices"][0]["message"]["content"], "回答")

    def test_stream_emits_error_when_no_window(self):
        """窗口全忙：流式要给出结构化 error，而不是把连接吊死。"""
        @contextlib.asynccontextmanager
        async def busy_window(sid, wait_timeout=30.0, need_page=True):
            raise b.CopilotBusy("所有 Copilot 窗口都在忙（1 个）")
            yield  # pragma: no cover

        saved = b._window
        b._window = busy_window
        try:
            async def collect():
                return [chunk async for chunk in api.stream_response(_req(), "你好")]
            chunks = asyncio.run(collect())
        finally:
            b._window = saved
        joined = "".join(chunks)
        self.assertIn("window_busy", joined)
        self.assertIn("[DONE]", joined)

    def test_nonstream_busy_maps_to_409(self):
        async def boom(req, prompt):
            raise b.CopilotBusy("所有 Copilot 窗口都在忙（1 个）")

        saved = api.nonstream
        api.nonstream = boom
        try:
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(api.chat(_req()))
        finally:
            api.nonstream = saved
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("窗口", ctx.exception.detail)


class BusySemanticsTests(unittest.TestCase):
    def _hold(self, sid):
        win = Window(99, object())
        win.sid = sid
        b.WINDOWS._windows.append(win)
        return win

    def _drop(self, win):
        try:
            b.WINDOWS._windows.remove(win)
        except ValueError:
            pass

    def test_health_busy_reflects_windows(self):
        win = self._hold("s-busy")
        try:
            out = asyncio.run(api.health())
        finally:
            self._drop(win)
        self.assertTrue(out["busy"], "有任务占着窗口时 /health 必须报 busy")

    def test_health_idle_without_windows(self):
        self.assertFalse(asyncio.run(api.health())["busy"])

    def test_session_running_covers_provider_slots(self):
        """提供商任务占逻辑槽（SLOTS）而不占窗口：只看 WINDOWS 会漏判。"""
        self.assertFalse(b.session_running("s-provider"))
        b.SLOTS._windows.append(Window(1, None))
        b.SLOTS._windows[-1].sid = "s-provider"
        try:
            self.assertTrue(b.session_running("s-provider"))
            self.assertFalse(b.any_window_busy(), "提供商槽不该算作浏览器忙碌")
            self.assertTrue(b.any_task_busy())
        finally:
            b.SLOTS._windows.pop()

    def test_agent_error_maps_window_busy_to_409(self):
        out = api._map_agent_error(b.CopilotBusy("窗口全忙"))
        self.assertIsInstance(out, HTTPException)
        self.assertEqual(out.status_code, 409)


if __name__ == "__main__":
    unittest.main()
