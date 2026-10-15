"""工具派发检查点：结果未知的副作用步骤可被识别。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.session_store import SessionStore


class StepDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_stepdisp_"))
        self.store = SessionStore(self.tmp / "agent.db")
        self.store.create("s1", "任务", str(self.tmp), "copilot-auto")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inflight_then_finish(self):
        self.store.begin_step("s1", 0, 3, "outlook_send", "hash-a")
        pend = self.store.pending_steps("s1")
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["tool"], "outlook_send")
        self.store.finish_step("s1", 0, 3)
        self.assertEqual(self.store.pending_steps("s1"), [])

    def test_only_unfinished_reported(self):
        self.store.begin_step("s1", 0, 1, "read_text", "h1")
        self.store.finish_step("s1", 0, 1)
        self.store.begin_step("s1", 0, 2, "delete_path", "h2")
        pend = self.store.pending_steps("s1")
        self.assertEqual([p["step_no"] for p in pend], [2])
        self.assertEqual(pend[0]["status"], "inflight")

    def test_conv_index_isolation_and_upsert(self):
        self.store.begin_step("s1", 0, 5, "write_text", "h")
        self.store.begin_step("s1", 1, 5, "write_text", "h")   # 换会话后 step_no 复用
        self.assertEqual(len(self.store.pending_steps("s1")), 2, "不同对话序号应各自独立")
        self.store.finish_step("s1", 0, 5)
        self.assertEqual([p["conversation_index"] for p in self.store.pending_steps("s1")], [1])

    def test_cascade_delete(self):
        self.store.begin_step("s1", 0, 1, "outlook_send", "h")
        self.assertTrue(self.store.delete("s1"))
        self.assertEqual(self.store.pending_steps("s1"), [])


if __name__ == "__main__":
    unittest.main()
