# -*- coding: utf-8 -*-
"""单测：删除计划能自动判断"这个任务用过 Copilot 吗"。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from cbp import bridge as b
from cbp.session_store import SessionStore


class DeletionPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_del_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.AGENT_TOOLS.root)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "a.db")
        b.AGENT_TOOLS.set_root(self.ws, False)
        from cbp import api as api_mod
        self.client = TestClient(api_mod.app)
        # 用过 Copilot 的会话
        b.AGENT_SESSIONS.create("used", "分析项目", str(self.ws), "copilot-auto")
        b.AGENT_SESSIONS.patch("used",
                               current_url="https://m365.cloud.microsoft/chat/c/aaa")
        # 只用 API 提供商模型的会话
        b.AGENT_SESSIONS.create("prov", "问答", str(self.ws), "deepseek/deepseek-chat")

    def tearDown(self):
        b.AGENT_SESSIONS, b.AGENT_TOOLS.root = self.saved

    def test_copilot_session_detected(self):
        d = self.client.get("/v1/agent/sessions/used/deletion-plan").json()
        self.assertTrue(d["used_copilot"])
        self.assertEqual(d["remote_count"], 1)
        self.assertTrue(d["suggest_delete_remote"])

    def test_provider_only_session_not_flagged(self):
        d = self.client.get("/v1/agent/sessions/prov/deletion-plan").json()
        self.assertFalse(d["used_copilot"])
        self.assertEqual(d["remote_count"], 0)
        self.assertFalse(d["suggest_delete_remote"])

    def test_unknown_session_404(self):
        self.assertEqual(self.client.get("/v1/agent/sessions/nope/deletion-plan").status_code, 404)

    def test_force_local_deletes_even_if_remote_fails(self):
        """远端删不掉时，强制清理也必须把本地记录删掉（用户："任务删不掉"）。"""
        r = self.client.delete("/v1/agent/sessions/used?delete_remote=true&force_local=true")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["deleted"])
        self.assertIsNone(b.AGENT_SESSIONS.get("used"))

    def test_without_force_keeps_local_on_remote_failure(self):
        """非 force 时保持旧语义：远端没删干净就不动本地。"""
        r = self.client.delete("/v1/agent/sessions/used?delete_remote=true")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        if body.get("deleted") is False:
            self.assertIsNotNone(b.AGENT_SESSIONS.get("used"))
        else:
            self.assertIsNone(b.AGENT_SESSIONS.get("used"))


if __name__ == "__main__":
    unittest.main()
