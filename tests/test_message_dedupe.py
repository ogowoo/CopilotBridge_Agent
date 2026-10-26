# -*- coding: utf-8 -*-
"""测试：同一条 assistant 回答不会被写成两条（队列 worker + run_agent 各写一次的旧事故）。"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.session_store import SessionStore  # noqa: E402

ANSWER = "已重新从 GitHub 拉取并重置项目：pyshot 当前位于 origin/main 提交 6779109。"


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.store = SessionStore(Path(tempfile.mkdtemp(prefix="cbp_dup_")) / "a.db")
        self.store.create("t1", "任务", str(Path(tempfile.gettempdir())), "copilot-auto")

    def test_identical_assistant_within_window_is_skipped(self):
        a = self.store.add_message("t1", "assistant", ANSWER, 0)
        b = self.store.add_message("t1", "assistant", ANSWER, 0)
        self.assertIsNotNone(a)
        self.assertIsNone(b, "同内容 assistant 在窗口内应被跳过")
        rows = [m for m in self.store.messages("t1") if m["role"] == "assistant"]
        self.assertEqual(len(rows), 1)

    def test_different_content_allowed(self):
        self.store.add_message("t1", "assistant", ANSWER, 0)
        self.store.add_message("t1", "assistant", ANSWER + "补一句", 0)
        rows = [m for m in self.store.messages("t1") if m["role"] == "assistant"]
        self.assertEqual(len(rows), 2)

    def test_user_duplicates_allowed(self):
        """用户真的可能连发同样的话两次，不能吞。"""
        self.store.add_message("t1", "user", "继续", 0)
        self.store.add_message("t1", "user", "继续", 0)
        rows = [m for m in self.store.messages("t1") if m["role"] == "user"]
        self.assertEqual(len(rows), 2)

    def test_outside_window_allowed(self):
        self.store.add_message("t1", "assistant", ANSWER, 0)
        # 把时间戳改早，模拟超过窗口
        with self.store.lock, self.store._connect() as con:
            con.execute("UPDATE messages SET created=? WHERE session_id=?",
                        (time.time() - 60, "t1"))
        self.store.add_message("t1", "assistant", ANSWER, 0)
        rows = [m for m in self.store.messages("t1") if m["role"] == "assistant"]
        self.assertEqual(len(rows), 2, "超过窗口的重复内容应允许（可能是新回合）")


if __name__ == "__main__":
    unittest.main()
