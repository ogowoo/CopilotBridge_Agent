# -*- coding: utf-8 -*-
"""暂停无法恢复的死锁回归。

真实故障（用户实测）：点「暂停」后点「恢复」，界面弹"已恢复"，但会话一直停在
"已暂停"，再也跑不起来。

机制：AgentRunner.run() 里的暂停停等是**持有全局锁**的
（while state=="paused": sleep(1)），唯一唤醒方式是把 state 改回非 paused。
而 resume_session_task 原先在"锁被本会话持有"时直接 return False，**没有改状态**
-> 停等的 runner 永远醒不过来、锁也一直不释放。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import bridge as b
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore


class ResumePausedRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_resume_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, dict(b.PROVIDERS),
                      b.run_agent)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        b.AGENT_SESSIONS.create("s1", "任务", str(self.ws), "copilot-auto")
        b._RESUMING.clear()
        b._PENDING_RESUME.clear()

        async def _fake_run(req):        # 桩：不碰浏览器，只挂住供取消
            await asyncio.sleep(3600)
            return None, req.session_id
        b.run_agent = _fake_run

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, _r, provs, b.run_agent) = self.saved
        b.PROVIDERS.clear(); b.PROVIDERS.update(provs)
        b._RESUMING.clear(); b._PENDING_RESUME.clear()

    def test_resume_while_lock_held_still_clears_paused(self):
        """核心回归：runner 活着（锁被本会话持有）时点恢复，必须把状态改回 ready。"""
        b.AGENT_SESSIONS.patch("s1", state="paused")

        async def main():
            # 模拟"停等在锁里的 runner"：持有锁并把 ACTIVE_SESSION_ID 设为该会话
            await b.lock.acquire()
            prev = b.ACTIVE_SESSION_ID
            b.ACTIVE_SESSION_ID = "s1"
            try:
                started = await b.resume_session_task("s1")
            finally:
                b.ACTIVE_SESSION_ID = prev
                b.lock.release()
            return started

        started = asyncio.run(main())
        self.assertFalse(started, "runner 还活着时不该再新建一个")
        self.assertEqual(b.AGENT_SESSIONS.get("s1")["state"], "ready",
                         "必须把状态改回 ready，否则停等的 runner 永远醒不过来")

    def test_resume_clears_cancel_flag(self):
        b.AGENT_SESSIONS.patch("s1", state="paused", cancel_requested=True)

        async def main():
            await b.lock.acquire()
            prev = b.ACTIVE_SESSION_ID
            b.ACTIVE_SESSION_ID = "s1"
            try:
                await b.resume_session_task("s1")
            finally:
                b.ACTIVE_SESSION_ID = prev
                b.lock.release()

        asyncio.run(main())
        st = b.AGENT_SESSIONS.get("s1")
        self.assertEqual(st["state"], "ready")
        self.assertFalse(st["cancel_requested"])

    def test_resume_when_idle_starts_runner(self):
        """没人跑时，恢复要真的拉起 runner。"""
        b.AGENT_SESSIONS.patch("s1", state="paused")

        async def main():
            started = await b.resume_session_task("s1")
            # 在事件循环仍活着时断言：任务已创建、已在等待锁
            return started, ("s1" in b._RESUMING), (b._PENDING_RESUME.get("s1") is not None)

        started, claimed, pending = asyncio.run(main())
        self.assertTrue(started)
        self.assertTrue(claimed, "应已原子认领")
        self.assertTrue(pending, "应已创建后台 runner 任务")
        self.assertEqual(b.AGENT_SESSIONS.get("s1")["state"], "ready")

    def test_resume_endpoint_reports_note(self):
        """端点必须给出可读的 note，避免"弹了已恢复却没跑"的误导。"""
        from cbp.api import app
        from fastapi.testclient import TestClient
        c = TestClient(app)
        b.AGENT_SESSIONS.patch("s1", state="paused")
        r = c.post("/v1/agent/sessions/s1/actions/resume").json()
        self.assertIn("note", r)
        self.assertIn(r.get("note_kind"), {"ok", "err"})
        if r.get("runner_started"):
            self.assertIn("续跑", r["note"])
        task = b._PENDING_RESUME.get("s1")
        if task:
            task.cancel()

    def test_resume_terminal_state_refused_with_reason(self):
        from cbp.api import app
        from fastapi.testclient import TestClient
        c = TestClient(app)
        b.AGENT_SESSIONS.patch("s1", state="completed")
        r = c.post("/v1/agent/sessions/s1/actions/resume").json()
        self.assertFalse(r["runner_started"])
        self.assertIn("不支持恢复", r["note"])
        self.assertEqual(r["note_kind"], "err")


if __name__ == "__main__":
    unittest.main()
