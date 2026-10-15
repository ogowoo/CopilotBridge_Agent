# -*- coding: utf-8 -*-
"""强行插入的"立即中断"：提供商真取消、网页版半中断。

① 提供商路径：把底层 HTTP 调用包成 task 与中断事件赛跑，插入先到就取消它。
   关键陷阱：取消抛的是 CancelledError（BaseException），若清理写在
   `except Exception` 里就不会执行 -> 历史残留一条 user 消息，下一回合重复追加。
   这里把底层协程包成 task 再取消，**外层看到的是 AgentInterrupted**，清理照常生效。
② 网页路径：只在本回合"还没产出内容"时中止等待（半中断）——一旦 Copilot 已开始
   生成就让它答完，避免丢掉已生成回答、以及"被放弃的回答"与新指令应答错位。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import bridge as b
from cbp.agent import AgentInterrupted, AgentRunner
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore


class InterruptPlumbingTests(unittest.TestCase):
    def setUp(self):
        self.saved = (b.generating, b.current_answer, b.TIMEOUT, dict(b._INTERRUPTS))
        b._INTERRUPTS.clear()

    def tearDown(self):
        (b.generating, b.current_answer, b.TIMEOUT, _old) = self.saved
        b._INTERRUPTS.clear()

    def test_request_and_peek_do_not_consume(self):
        """interrupt_pending 只窥探：等待循环每 POLL 问一次，窥探就清会把信号吃掉。"""
        self.assertFalse(b.interrupt_pending("s1"))
        b.request_interrupt("s1")
        self.assertTrue(b.interrupt_pending("s1"))
        self.assertTrue(b.interrupt_pending("s1"))   # 再问仍为真
        b.clear_interrupt("s1")
        self.assertFalse(b.interrupt_pending("s1"))
        b.discard_interrupts("s1")
        self.assertFalse(b.interrupt_pending("s1"))

    def test_await_interruptible_cancels_and_raises(self):
        """中断先到：底层协程被取消，外层收到 AgentInterrupted，信号被消费。"""
        reached = {"bye": False}

        async def slow():
            try:
                await asyncio.sleep(30)
            finally:
                reached["bye"] = True
            return "never"

        async def main():
            b.request_interrupt("s1")
            try:
                await b._await_interruptible("s1", slow(), "测试调用")
                return None
            except AgentInterrupted as exc:
                return str(exc)

        msg = asyncio.run(main())
        self.assertIsNotNone(msg, "应抛出 AgentInterrupted")
        self.assertIn("被中断", msg)
        self.assertTrue(reached["bye"], "底层协程应已被取消")
        self.assertFalse(b.interrupt_pending("s1"), "中断信号应被消费")

    def test_await_interruptible_returns_normally_without_interrupt(self):
        async def quick():
            await asyncio.sleep(0)
            return "ok"
        self.assertEqual(asyncio.run(b._await_interruptible("s1", quick(), "测试")), "ok")

    def test_web_half_interrupt_only_before_output(self):
        """网页半中断：还没产出 -> 中断；已开始生成 -> 不中断（等它答完）。"""
        # 情况一：没在生成、回答仍是上一轮的 -> 半中断生效
        async def not_gen(): return False
        async def same_answer(): return "上一轮的回答"
        b.generating, b.current_answer = not_gen, same_answer
        with self.assertRaises(AgentInterrupted):
            asyncio.run(b.wait_agent_answer("上一轮的回答", interrupt_check=lambda: True))

        # 情况二：已开始生成 -> 不中断，等它答完并正常返回回答
        seq = {"n": 0}

        async def gen_then_enough():
            seq["n"] += 1
            return seq["n"] == 1          # 第一次报告在生成 -> generation_seen=True

        async def answer_changes():
            return "上一轮的回答" if seq["n"] <= 1 else '{"type":"final","content":"新回答"}'

        b.generating, b.current_answer = gen_then_enough, answer_changes
        got = asyncio.run(b.wait_agent_answer("上一轮的回答", interrupt_check=lambda: True))
        self.assertIn("新回答", got, "已开始生成时不该中断，应等它答完")


class AgentHandlesInterruptTests(unittest.TestCase):
    """Agent 侧：中断不记步、不记失败，且下一回合执行插入的指令。"""

    def test_interrupt_step_and_next_turn_uses_injected(self):
        ws = Path(tempfile.mkdtemp(prefix="cbp_intr_"))
        (ws / "a.txt").write_text("hi", encoding="utf-8")
        turns, steps = [], []
        state = {"state": "ready", "pending": []}

        async def chat(prompt, model, make_new):
            turns.append(prompt)
            if len(turns) == 1:
                state["pending"] = ["改成先只改前端"]       # 模拟此刻 Ctrl+J
                raise AgentInterrupted("测试：插入指令，中断本回合")
            return json.dumps({"type": "final", "content": "按新指令完成"}, ensure_ascii=False)

        tools = b.ToolRegistry(ws, {})
        runner = AgentRunner(tools, chat, {}, lambda: state,
                            lambda **kw: state.update(kw), steps.append, lambda x: None)
        result = asyncio.run(runner.run("先看看目录", "m"))

        statuses = [s.get("status") for s in steps]
        self.assertIn("user_interrupt", statuses, "时间线要留一条中断记录")
        self.assertNotIn("failed", statuses)
        self.assertEqual(state.get("state"), "completed")
        self.assertEqual(len(turns), 2)
        self.assertIn("改成先只改前端", turns[1], "下一回合必须执行插入的指令")
        self.assertEqual(state.get("pending"), [], "送达后清除 pending")
        self.assertIn("按新指令完成", result.content)


class ProviderHistoryOnInterruptTests(unittest.TestCase):
    """取消/中断后，提供商历史里不能残留那条 user 消息（否则重复追加）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_intr_prov_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root,
                      dict(b.PROVIDERS), dict(b._PROVIDER_AGENT_HISTORY), dict(b.RUNTIME))
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        b._PROVIDER_AGENT_HISTORY.clear(); b.RUNTIME.clear()
        b._INTERRUPTS.clear()
        b.AGENT_SESSIONS.create("s1", "任务", str(self.ws), "fakeprov/m1")

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, _r, provs, hist, rt) = self.saved
        b.PROVIDERS.clear(); b.PROVIDERS.update(provs)
        b._PROVIDER_AGENT_HISTORY.clear(); b._PROVIDER_AGENT_HISTORY.update(hist)
        b.RUNTIME.clear(); b.RUNTIME.update(rt)
        b._INTERRUPTS.clear()

    def _slow_provider(self):
        class P:
            async def complete(self, model, messages, response_schema=None):
                await asyncio.sleep(30)
                return '{"type":"tool","tool":"list_directory","arguments":{"path":"."}}'
        b.PROVIDERS["fakeprov"] = P()

    def test_interrupt_rolls_back_user_message(self):
        self._slow_provider()

        async def main():
            task = asyncio.ensure_future(b.provider_turn("s1", "fakeprov/m1", "第一轮回合提示"))
            await asyncio.sleep(0.05)
            b.request_interrupt("s1")
            try:
                await task
                return "no-raise"
            except AgentInterrupted:
                return "interrupted"

        self.assertEqual(asyncio.run(main()), "interrupted")
        hist = b._PROVIDER_AGENT_HISTORY.get("s1") or []
        self.assertEqual([m["role"] for m in hist], ["system"],
                         "中断后历史里不该残留 user 消息（否则下回合重复追加）")

    def test_cancel_also_rolls_back(self):
        """外部取消（CancelledError）同样要回滚——清理不能只覆盖 Exception。"""
        self._slow_provider()

        async def main():
            task = asyncio.ensure_future(b.provider_turn("s1", "fakeprov/m1", "第一轮回合提示"))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            return [m["role"] for m in (b._PROVIDER_AGENT_HISTORY.get("s1") or [])]

        self.assertEqual(asyncio.run(main()), ["system"])


if __name__ == "__main__":
    unittest.main()
