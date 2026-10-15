"""事件日志（P2-10 第一期）：检查点动作会并行落事件；读取路径不变。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.session_store import SessionStore


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_ev_"))
        self.store = SessionStore(self.tmp / "a.db")
        self.store.create("s1", "任务", str(self.tmp), "m")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_checkpoints_emit_events_in_order(self):
        self.store.begin_turn("s1", 0, "ph1", 0)
        self.store.save_turn("s1", 0, "ph1", "答案", 3)
        self.store.begin_step("s1", 0, 1, "read_text", "ah1")
        self.store.finish_step("s1", 0, 1, "done")
        kinds = [e["kind"] for e in self.store.events("s1")]
        self.assertEqual(kinds, ["turn_begin", "turn_done", "step_begin", "step_done"])

    def test_payload_is_structured(self):
        self.store.begin_step("s1", 0, 7, "outlook_send", "abc123")
        ev = self.store.events("s1", kinds=("step_begin",))[0]
        self.assertEqual(ev["payload"]["tool"], "outlook_send")
        self.assertEqual(ev["payload"]["step_no"], 7)
        self.assertEqual(ev["payload"]["args_hash"], "abc123")

    def test_approval_events(self):
        self.store.grant_approval("s1", "delete_path", "h1")
        self.assertTrue(self.store.take_approval("s1", "delete_path", "h1"))
        self.assertFalse(self.store.take_approval("s1", "delete_path", "h1"))
        kinds = [e["kind"] for e in self.store.events("s1")]
        self.assertEqual(kinds, ["approval_granted", "approval_taken", "approval_denied"])

    def test_events_never_break_checkpoints(self):
        """事件是旁路：即使 payload 无法序列化，检查点也必须照常工作。"""
        class Weird:
            def __repr__(self):
                return "<weird>"
        self.store.append_event("s1", "x", {"obj": Weird()})
        self.store.begin_turn("s1", 0, "ph", 0)
        self.assertEqual((self.store.get_turn("s1", 0, "ph") or {}).get("status"), "inflight")

    def test_message_events_are_lightweight(self):
        """消息事件只记元信息（正文留在 messages 表），避免事件表膨胀。"""
        self.store.add_message("s1", "user", "很长的正文" * 500, 0)
        ev = self.store.events("s1", kinds=("message",))[0]
        self.assertEqual(ev["payload"]["role"], "user")
        self.assertEqual(ev["payload"]["chars"], len("很长的正文" * 500))
        self.assertNotIn("content", ev["payload"])

    def test_files_modified_event_records_delta(self):
        self.store.patch("s1", modified_files={"a.py": {"operation": "write"}})
        self.store.patch("s1", modified_files={"a.py": {"operation": "write"},
                                              "b.py": {"operation": "write"}})
        self.store.patch("s1", modified_files={"b.py": {"operation": "write"}})
        evs = self.store.events("s1", kinds=("files_modified",))
        self.assertEqual(len(evs), 3, "三次集合变化应各发一条事件")
        self.assertEqual(evs[0]["payload"]["added"], ["a.py"])
        self.assertEqual(evs[1]["payload"]["added"], ["b.py"])
        self.assertEqual(evs[1]["payload"]["removed"], [])
        self.assertEqual(evs[2]["payload"]["added"], [])
        self.assertEqual(evs[2]["payload"]["removed"], ["a.py"])
        self.assertEqual(evs[2]["payload"]["total"], 1)

    def test_no_event_when_files_unchanged(self):
        self.store.patch("s1", modified_files={"a.py": {"operation": "write"}})
        n = len(self.store.events("s1", kinds=("files_modified",)))
        self.store.patch("s1", modified_files={"a.py": {"operation": "write"}})
        self.assertEqual(len(self.store.events("s1", kinds=("files_modified",))), n,
                         "文件集合没变就不该再发事件")

    def test_cascade_delete(self):
        self.store.begin_turn("s1", 0, "ph", 0)
        self.assertTrue(self.store.delete("s1"))
        self.assertEqual(self.store.events("s1"), [])

    def test_kinds_filter_and_limit(self):
        for i in range(5):
            self.store.append_event("s1", "custom", {"i": i})
        self.assertEqual(len(self.store.events("s1", limit=3)), 3)
        self.assertEqual(len(self.store.events("s1", kinds=("custom",))), 5)


if __name__ == "__main__":
    unittest.main()
