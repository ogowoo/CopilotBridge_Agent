# -*- coding: utf-8 -*-
"""强行插入指令（DSH Ctrl+J 式）：运行中即时生效，不等当前这轮跑完。

机制：AgentRunner.run() 每个回合开头都 `state = self.state_getter()` 重读会话，
因此运行中往 pending 里写指令，下一回合就会被"优先执行"；与 /messages 的排队
跟进（要等当前 run 结束再起一轮）语义不同。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from gantry import bridge as b
from gantry.session_store import SessionStore
from gantry.snapshots import SnapshotStore


class InjectEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_inject_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, b.run_agent)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        b.AGENT_SESSIONS.create("s1", "任务", str(self.ws), "copilot-auto")
        b._MSG_QUEUES.clear(); b._MSG_WAKE.clear(); b._MSG_WORKERS.clear()

        async def _fake_run(req, attach_paths=None):   # 桩：队列消费时不碰浏览器
            await asyncio.sleep(3600)
            return None, req.session_id
        b.run_agent = _fake_run
        from gantry.api import app
        self.c = TestClient(app)

    def tearDown(self):
        for t in list(b._MSG_WORKERS.values()):
            t.cancel()
        (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, b.run_agent) = self.saved
        b._MSG_QUEUES.clear(); b._MSG_WAKE.clear(); b._MSG_WORKERS.clear()

    def test_running_session_goes_to_pending_not_queue(self):
        """运行中插入：写进 pending（下回合生效），不走队列。"""
        from gantry.window_pool import Window

        # 模拟"该会话正占着一个自动化窗口"（多窗口下 runner 的活跃标记就在窗口池里）
        win = Window(1, object())
        win.sid = "s1"
        b.WINDOWS._windows.append(win)
        try:
            r = self.c.post("/v1/agent/sessions/s1/inject",
                            json={"content": "别动数据库，先只改前端"})
        finally:
            try:
                b.WINDOWS._windows.remove(win)
            except ValueError:
                pass
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["running"] and d["injected"] and not d["queued"])
        self.assertTrue(d.get("interrupted"), "运行中插入应同时请求中断当前回合")
        self.assertIn("立即按新指令继续", d["note"])
        self.assertEqual(b.AGENT_SESSIONS.get("s1")["pending"], ["别动数据库，先只改前端"])
        self.assertEqual(b.queue_depth("s1"), 0)
        self.assertTrue(b.interrupt_pending("s1"), "应已置位中断信号")

    def test_idle_session_is_queued(self):
        """空闲会话：pending 没人消费 -> 改为排队执行，避免指令悬空。"""
        r = self.c.post("/v1/agent/sessions/s1/inject", json={"content": "补一段说明"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertFalse(d["running"])
        self.assertTrue(d["queued"])
        self.assertIn("空闲", d["note"])
        self.assertEqual(b.AGENT_SESSIONS.get("s1")["pending"], [])

    def test_inject_recorded_as_message_and_step(self):
        """插入要有迹可循：消息历史 + 时间线各留一条。"""
        self.c.post("/v1/agent/sessions/s1/inject", json={"content": "插入的指令"})
        msgs = b.AGENT_SESSIONS.messages("s1")
        self.assertTrue(any(m["role"] == "user" and m["content"] == "插入的指令" for m in msgs))
        steps = b.AGENT_SESSIONS.steps("s1")
        self.assertTrue([s for s in steps if s.get("status") == "user_inject"])

    def test_terminal_state_refused(self):
        b.AGENT_SESSIONS.patch("s1", state="cancelled")
        r = self.c.post("/v1/agent/sessions/s1/inject", json={"content": "x"})
        self.assertEqual(r.status_code, 409)

    def test_empty_refused_and_missing_session_404(self):
        self.assertEqual(self.c.post("/v1/agent/sessions/s1/inject",
                                     json={"content": ""}).status_code, 422)
        self.assertEqual(self.c.post("/v1/agent/sessions/nope/inject",
                                     json={"content": "x"}).status_code, 404)


def _act(tool, **kw):
    return {"type": "tool", "tool": tool, "arguments": kw}


class InjectedPendingReachesNextTurnTests(unittest.TestCase):
    """核心机制：运行中写入 pending，Agent 下一回合的提示里必须带上它。"""

    def test_pending_is_read_on_next_turn_and_cleared(self):
        from gantry.agent import AgentRunner

        ws = Path(tempfile.mkdtemp(prefix="cbp_inject_agent_"))
        (ws / "a.txt").write_text("hi", encoding="utf-8")
        turns, steps = [], []
        state = {"state": "ready", "pending": []}

        async def chat(prompt, model, make_new):
            turns.append(prompt)
            if len(turns) == 1:
                # 模拟"用户此刻点了强行插入"：直接改会话状态
                state["pending"] = ["别动数据库，先只改前端"]
                return json.dumps(_act("list_directory", path="."), ensure_ascii=False)
            return json.dumps({"type": "final", "content": "照新指令做完了"}, ensure_ascii=False)

        tools = b.ToolRegistry(ws, {})
        runner = AgentRunner(tools, chat, {}, lambda: state,
                             lambda **kw: state.update(kw), steps.append, lambda x: None)
        result = asyncio.run(runner.run("先看看目录", "m"))

        self.assertEqual(len(turns), 2)
        self.assertIn("别动数据库", turns[1], "插入的指令没有进入下一回合的提示")
        self.assertEqual(state.get("pending"), [], "送达后应清除 pending")
        self.assertIn("照新指令", result.content)


if __name__ == "__main__":
    unittest.main()
