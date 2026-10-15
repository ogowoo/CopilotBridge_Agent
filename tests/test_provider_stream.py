# -*- coding: utf-8 -*-
"""提供商流式回合：边收边写 answer_preview（界面"逐字"看到的来源）。

原则：
- 优先流式（保留 response_schema，结构化动作协议的解析率不能牺牲）；
- 只在**一个增量都没收到**时才退回非流式（已收到内容再重发会重复计费、
  也可能拿到两份不同的回答）；
- provider 对象没有 stream（自定义/旧实现）时不报错，直接用非流式；
- CFG["stream_provider_agent"]=false 可整体关掉流式。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import bridge as b
from cbp import providers as P
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore


class FakeStreamProvider:
    """按块吐字的假提供商；可配置"收到几块后报错"。"""

    def __init__(self, pieces, fail_after=None, empty=False):
        self.pieces = pieces
        self.fail_after = fail_after
        self.empty = empty
        self.complete_calls = 0
        self.stream_schema = "unset"

    async def stream(self, model, messages, response_schema=None):
        self.stream_schema = response_schema
        for i, piece in enumerate(self.pieces):
            if self.fail_after is not None and i >= self.fail_after:
                raise P.ProviderError("流式中断")
            await asyncio.sleep(0)
            yield piece
        if self.empty:
            return

    async def complete(self, model, messages, response_schema=None):
        self.complete_calls += 1
        return '{"type":"final","content":"来自非流式"}'


class StreamProviderTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_stream_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root,
                      dict(b.PROVIDERS), dict(b._PROVIDER_AGENT_HISTORY),
                      dict(b.CFG), dict(b.RUNTIME))
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        b._PROVIDER_AGENT_HISTORY.clear(); b.RUNTIME.clear(); b._INTERRUPTS.clear()
        b.AGENT_SESSIONS.create("s1", "任务", str(self.ws), "fakeprov/m1")

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, _r, provs, hist, cfg, rt) = self.saved
        b.PROVIDERS.clear(); b.PROVIDERS.update(provs)
        b._PROVIDER_AGENT_HISTORY.clear(); b._PROVIDER_AGENT_HISTORY.update(hist)
        b.CFG.clear(); b.CFG.update(cfg)
        b.RUNTIME.clear(); b.RUNTIME.update(rt)
        b._INTERRUPTS.clear()

    def _run(self, prov, prompt="回合提示"):
        b.PROVIDERS["fakeprov"] = prov
        return asyncio.run(b.provider_turn("s1", "fakeprov/m1", prompt))

    def test_streamed_answer_is_returned_and_preview_grows(self):
        pieces = ['{"type":', '"final",', '"content":"流式拼接"}']
        prov = FakeStreamProvider(pieces)
        seen = []
        orig = b.runtime_update

        def spy(sid, **kw):
            if kw.get("phase") == "api_streaming":
                seen.append(kw.get("answer_preview", ""))
            return orig(sid, **kw)

        b.runtime_update = spy
        try:
            answer = self._run(prov)
        finally:
            b.runtime_update = orig
        self.assertEqual(answer, "".join(pieces))
        self.assertTrue(seen, "应有流式的 answer_preview 更新")
        self.assertEqual(seen[-1], "".join(pieces), "最后一次预览应是完整内容")
        self.assertIsNotNone(prov.stream_schema, "流式也要带结构化输出（动作协议）")

    def test_falls_back_when_stream_fails_before_any_chunk(self):
        prov = FakeStreamProvider(["x"], fail_after=0)
        answer = self._run(prov)
        self.assertIn("来自非流式", answer)
        self.assertEqual(prov.complete_calls, 1, "未收到任何增量 -> 退回非流式一次")

    def test_does_not_fall_back_after_partial_content(self):
        """已经收到内容再退回重发会导致重复计费与两份回答——必须直接报错。"""
        prov = FakeStreamProvider(["part1", "part2", "part3"], fail_after=2)
        with self.assertRaises(P.ProviderError):
            self._run(prov)
        self.assertEqual(prov.complete_calls, 0)

    def test_empty_stream_falls_back(self):
        prov = FakeStreamProvider([], empty=True)
        answer = self._run(prov)
        self.assertIn("来自非流式", answer)
        self.assertEqual(prov.complete_calls, 1)

    def test_provider_without_stream_uses_complete(self):
        """没有 stream 方法的 provider（自定义/旧实现）不该报错。"""
        class NoStream:
            def __init__(self):
                self.calls = 0

            async def complete(self, model, messages, response_schema=None):
                self.calls += 1
                return '{"type":"final","content":"只有非流式"}'
        prov = NoStream()
        answer = self._run(prov)
        self.assertIn("只有非流式", answer)
        self.assertEqual(prov.calls, 1)

    def test_config_switch_disables_streaming(self):
        prov = FakeStreamProvider(['{"type":"final","content":"不该走流式"}'])
        b.CFG["stream_provider_agent"] = False
        answer = self._run(prov)
        self.assertIn("来自非流式", answer)
        self.assertEqual(prov.complete_calls, 1)


if __name__ == "__main__":
    unittest.main()
