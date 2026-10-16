# -*- coding: utf-8 -*-
"""会话标签：列表分组用的本地元数据（不参与 Agent 逻辑）。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from cbp import bridge as b
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore


class SessionTagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_tag_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        b.AGENT_SESSIONS.create("s1", "甲", str(self.ws), "copilot-auto")
        from cbp.api import app
        self.c = TestClient(app)

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root) = self.saved

    def test_set_and_clear_tag(self):
        r = self.c.post("/v1/agent/sessions/s1/tag", json={"tag": "工作"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["tag"], "工作")
        self.assertEqual(b.AGENT_SESSIONS.get("s1")["tag"], "工作")
        # 空串 = 取消标签
        self.c.post("/v1/agent/sessions/s1/tag", json={"tag": "  "})
        self.assertEqual(b.AGENT_SESSIONS.get("s1")["tag"], "")

    def test_tag_appears_in_session_list(self):
        self.c.post("/v1/agent/sessions/s1/tag", json={"tag": "私人"})
        rows = {s["id"]: s for s in b.AGENT_SESSIONS.list()}
        self.assertEqual(rows["s1"]["tag"], "私人")

    def test_missing_session_404(self):
        self.assertEqual(self.c.post("/v1/agent/sessions/nope/tag",
                                     json={"tag": "x"}).status_code, 404)

    def test_tag_too_long_rejected(self):
        self.assertEqual(self.c.post("/v1/agent/sessions/s1/tag",
                                     json={"tag": "x" * 41}).status_code, 422)

    def test_migration_keeps_old_sessions_readable(self):
        """老库（无 tag 列）升级后旧会话仍可读，tag 为空。"""
        import sqlite3
        old = self.tmp / "old.db"
        con = sqlite3.connect(old)
        # 与真实 schema 的差别只有"没有 tag 列"——其它列 _decode 要用，不能省
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, workspace TEXT,"
                    " model TEXT, state TEXT, created REAL, updated REAL,"
                    " cancel_requested INTEGER DEFAULT 0,"
                    " archived INTEGER DEFAULT 0, compact_requested INTEGER DEFAULT 0,"
                    " rollover_requested INTEGER DEFAULT 0)")
        con.execute("INSERT INTO sessions(id,title,workspace,model,state,created,updated)"
                    " VALUES('s9','旧会话','/tmp','m','completed',1.0,1.0)")
        con.commit(); con.close()
        store = SessionStore(old)      # 打开即迁移
        self.assertEqual(store.get("s9")["title"], "旧会话")
        self.assertIsNone(store.get("s9").get("tag"))
        store.patch("s9", tag="迁移后可用")
        self.assertEqual(store.get("s9")["tag"], "迁移后可用")


if __name__ == "__main__":
    unittest.main()
