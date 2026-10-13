# -*- coding: utf-8 -*-
"""会话级模型：接口切换 + 续跑沿用（服务重启后不丢）。

会话页面要能看/改当前会话的模型；resume/auto_resume 也必须沿用会话存下来的
模型，否则服务重启后任务会悄悄跑在别的模型上。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from cbp import bridge as b
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore


class SessionModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_sessmodel_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.root = self.ws
        b.AGENT_SESSIONS.create("s1", "任务标题", str(self.ws), "copilot-think-deeper")
        from cbp.api import app
        # 本机 config.json 可能配置了 api_token（合法部署）：测试要带上它，
        # 否则会因 401 失败，把"配置正确"误报成"代码有问题"。
        headers = {}
        if b.CFG.get("api_token"):
            headers["X-CBP-Token"] = str(b.CFG["api_token"])
        self.c = TestClient(app, headers=headers)

    def tearDown(self):
        b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root = self.saved

    def test_switch_model_persists(self):
        r = self.c.post("/v1/agent/sessions/s1/model", json={"model": "copilot-auto"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["model"], "copilot-auto")
        self.assertIn("下一回合生效", r.json()["applies"])
        # 真的落库（服务重启后 resume 会读它）
        self.assertEqual(b.AGENT_SESSIONS.get("s1")["model"], "copilot-auto")

    def test_unknown_model_falls_back_with_note(self):
        r = self.c.post("/v1/agent/sessions/s1/model", json={"model": "no-such/model"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["note"], "失效模型应给出回退说明")
        self.assertEqual(b.AGENT_SESSIONS.get("s1")["model"], r.json()["model"])

    def test_missing_session_and_empty_model(self):
        self.assertEqual(self.c.post("/v1/agent/sessions/nope/model",
                                     json={"model": "copilot-auto"}).status_code, 404)
        self.assertEqual(self.c.post("/v1/agent/sessions/s1/model",
                                     json={"model": ""}).status_code, 422)

    def test_resume_request_uses_session_model(self):
        """resume/auto_resume 组装的请求必须带会话存储的模型。"""
        seen = {}

        async def fake_run_agent(req):
            seen["model"] = req.model
            return None, req.session_id

        saved = b.run_agent
        b.run_agent = fake_run_agent
        try:
            b.AGENT_SESSIONS.patch("s1", state="paused", last_error="服务中断，可从检查点恢复")
            ok = asyncio.run(b.resume_session_task("s1"))
        finally:
            b.run_agent = saved
        self.assertTrue(ok)
        self.assertEqual(seen.get("model"), "copilot-think-deeper")
        asyncio.run(asyncio.sleep(0))     # 让后台任务收尾


if __name__ == "__main__":
    unittest.main()
