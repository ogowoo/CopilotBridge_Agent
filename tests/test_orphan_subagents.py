# -*- coding: utf-8 -*-
"""单测：父任务结束时取消仍在跑的子会话。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.session_store import SessionStore


class CancelChildrenTests(unittest.TestCase):
    def setUp(self):
        self.store = SessionStore(Path(tempfile.mkdtemp(prefix="cbp_ch_")) / "a.db")
        self.store.create("task-1", "父任务", str(Path(tempfile.gettempdir())), "copilot-auto")
        for i, st in enumerate(("running", "waiting", "completed"), 1):
            self.store.create(f"sub-{i}", f"子{i}", str(Path(tempfile.gettempdir())),
                              "copilot-auto")
            self.store.patch(f"sub-{i}", state=st, parent_id="task-1")

    def test_cancels_only_running_children(self):
        n = self.store.cancel_running_children("task-1")
        self.assertEqual(n, 2, "只该取消 running/waiting 的子会话")
        self.assertEqual(self.store.get("sub-1")["state"], "cancelled")
        self.assertEqual(self.store.get("sub-2")["state"], "cancelled")
        self.assertEqual(self.store.get("sub-3")["state"], "completed", "已完成的不能动")
        self.assertTrue(self.store.get("sub-1")["cancel_requested"])

    def test_keeps_other_parents_alone(self):
        self.store.create("task-2", "别的父任务", str(Path(tempfile.gettempdir())), "copilot-auto")
        self.store.create("sub-x", "别人的子", str(Path(tempfile.gettempdir())),
                          "copilot-auto")
        self.store.patch("sub-x", state="running", parent_id="task-2")
        self.store.cancel_running_children("task-1")
        self.assertEqual(self.store.get("sub-x")["state"], "running")

    def test_note_recorded(self):
        self.store.cancel_running_children("task-1", "父任务已完成")
        self.assertIn("父任务已完成", self.store.get("sub-1")["last_error"] or "")

    def test_empty_parent_is_noop(self):
        self.assertEqual(self.store.cancel_running_children(""), 0)


if __name__ == "__main__":
    unittest.main()
