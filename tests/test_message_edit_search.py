# -*- coding: utf-8 -*-
"""消息编辑/删除 + 跨会话搜索。

编辑的诚实语义：只改本地历史。网页 Copilot 那边已发出的内容无法撤回；提供商
会话则必须**同步内存里的对话历史**（`_PROVIDER_AGENT_HISTORY` 是增量累积的，
不是每回合从 messages 表重建）——只改库不同步，模型下一回合仍会看到旧文本。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from cbp import bridge as b
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore


class MessageEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_edit_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root,
                      dict(b._PROVIDER_AGENT_HISTORY))
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        b._PROVIDER_AGENT_HISTORY.clear()
        b.AGENT_SESSIONS.create("s1", "会话", str(self.ws), "copilot-auto")
        self.mid = b.AGENT_SESSIONS.add_message("s1", "user", "原始内容", 0)
        from cbp.api import app
        self.c = TestClient(app)

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, hist) = self.saved
        b._PROVIDER_AGENT_HISTORY.clear(); b._PROVIDER_AGENT_HISTORY.update(hist)

    def test_edit_updates_row(self):
        r = self.c.patch(f"/v1/agent/sessions/s1/messages/{self.mid}",
                         json={"content": "改过的内容"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["updated"])
        msgs = b.AGENT_SESSIONS.messages("s1")
        self.assertEqual(msgs[0]["content"], "改过的内容")

    def test_edit_rejects_empty_and_missing(self):
        # 空串被 pydantic 拦（min_length=1）→ 422；纯空白过模型校验，由 handler 拦 → 400
        self.assertEqual(self.c.patch(f"/v1/agent/sessions/s1/messages/{self.mid}",
                                      json={"content": ""}).status_code, 422)
        self.assertEqual(self.c.patch(f"/v1/agent/sessions/s1/messages/{self.mid}",
                                      json={"content": "  "}).status_code, 400)
        self.assertEqual(self.c.patch("/v1/agent/sessions/s1/messages/99999",
                                      json={"content": "x"}).status_code, 404)
        self.assertEqual(self.c.patch("/v1/agent/sessions/nope/messages/1",
                                      json={"content": "x"}).status_code, 404)

    def test_delete_removes_row(self):
        r = self.c.delete(f"/v1/agent/sessions/s1/messages/{self.mid}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["deleted"])
        self.assertEqual(b.AGENT_SESSIONS.messages("s1"), [])
        self.assertEqual(self.c.delete(f"/v1/agent/sessions/s1/messages/{self.mid}").status_code, 404)

    def test_edit_syncs_provider_memory_history(self):
        """只改库不同步内存 -> 模型下一回合仍看到旧文本。"""
        b._PROVIDER_AGENT_HISTORY["s1"] = [
            {"role": "system", "content": "协议"},
            {"role": "user", "content": "原始内容"},
            {"role": "assistant", "content": "回答"}]
        d = self.c.patch(f"/v1/agent/sessions/s1/messages/{self.mid}",
                         json={"content": "改过的内容"}).json()
        self.assertTrue(d["history_synced"])
        hist = b._PROVIDER_AGENT_HISTORY["s1"]
        self.assertEqual([x["content"] for x in hist],
                         ["协议", "改过的内容", "回答"])
        self.assertIn("上下文也已同步", d["note"])

    def test_delete_syncs_provider_memory_history(self):
        b._PROVIDER_AGENT_HISTORY["s1"] = [
            {"role": "system", "content": "协议"},
            {"role": "user", "content": "原始内容"}]
        d = self.c.delete(f"/v1/agent/sessions/s1/messages/{self.mid}").json()
        self.assertTrue(d["history_synced"])
        self.assertEqual([x["content"] for x in b._PROVIDER_AGENT_HISTORY["s1"]], ["协议"])

    def test_history_sync_reports_false_when_not_found(self):
        """内存里没有对应条目（已被裁剪）时如实告知，不假装成功。"""
        self.assertFalse(b.sync_provider_history_message("s1", "不存在的文本", "x"))
        self.assertFalse(b.sync_provider_history_message("没有这个会话", "a", "b"))

    def test_system_message_never_edited_in_memory(self):
        """协议/system 条目不该被消息编辑连带改掉。"""
        b._PROVIDER_AGENT_HISTORY["s1"] = [{"role": "system", "content": "原始内容"}]
        self.assertFalse(b.sync_provider_history_message("s1", "原始内容", "改"))
        self.assertEqual(b._PROVIDER_AGENT_HISTORY["s1"][0]["content"], "原始内容")


class CrossSessionSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_gs_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        b.AGENT_SESSIONS.create("s1", "进度任务", str(self.ws), "m")
        b.AGENT_SESSIONS.create("s2", "数据库任务", str(self.ws), "m")
        b.AGENT_SESSIONS.create("s3", "已归档", str(self.ws), "m")
        b.AGENT_SESSIONS.patch("s3", archived=True)
        b.AGENT_SESSIONS.add_message("s1", "user", "把进度改成 100% 完成", 0)
        b.AGENT_SESSIONS.add_message("s2", "user", "SQLite 的 LIKE 要转义下划线 _ 吗", 0)
        b.AGENT_SESSIONS.add_message("s3", "user", "这条在归档会话里 100% 不该出现", 0)
        from cbp.api import app
        self.c = TestClient(app)

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root) = self.saved

    def test_search_across_sessions_with_session_info(self):
        d = self.c.get("/v1/search", params={"q": "完成"}).json()
        self.assertEqual(d["count"], 1)
        hit = d["results"][0]
        self.assertEqual(hit["session_id"], "s1")
        self.assertEqual(hit["title"], "进度任务")
        self.assertIn("完成", hit["snippet"])
        self.assertIn("message_id", hit)

    def test_wildcards_are_escaped(self):
        """搜 100% 不能变成通配；搜 _ 不能匹配所有内容。"""
        d = self.c.get("/v1/search", params={"q": "100%"}).json()
        self.assertEqual([h["session_id"] for h in d["results"]], ["s1"],
                         "归档会话不该出现，且 % 必须是字面量")
        self.assertEqual(self.c.get("/v1/search", params={"q": "_"}).json()["count"], 1)

    def test_archived_sessions_excluded(self):
        d = self.c.get("/v1/search", params={"q": "归档"}).json()
        self.assertEqual(d["count"], 0)

    def test_empty_query_rejected_and_limit_clamped(self):
        self.assertEqual(self.c.get("/v1/search", params={"q": ""}).status_code, 400)
        d = self.c.get("/v1/search", params={"q": "。", "limit": 9999}).json()
        self.assertLessEqual(len(d["results"]), 100)


if __name__ == "__main__":
    unittest.main()
