# -*- coding: utf-8 -*-
"""目标 / 计划在会话里的持久化（goal 模式曾因"列不存在"静默丢数据）。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.session_store import SessionStore


class GoalPersistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_goal_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.store = SessionStore(self.tmp / "agent.db")
        self.store.create("s1", "目标会话", str(self.ws), "copilot-auto")

    def test_goal_roundtrip(self):
        """patch(goal=...) 必须真的落库并能读回来（此前 sessions 表没有该列）。"""
        goal = {"objective": "把覆盖率提到 80%", "rounds": 2, "max_rounds": 10,
                "status": "active"}
        self.store.patch("s1", goal=goal)
        got = self.store.get("s1")
        self.assertIsInstance(got.get("goal"), dict, "goal 应被解码成 dict 而不是字符串")
        self.assertEqual(got["goal"]["objective"], "把覆盖率提到 80%")
        self.assertEqual(got["goal"]["rounds"], 2)

    def test_todos_roundtrip(self):
        todos = [{"content": "写测试", "status": "completed"},
                 {"content": "跑审计", "status": "pending"}]
        self.store.patch("s1", todos=todos)
        got = self.store.get("s1")
        self.assertEqual(got.get("todos"), todos)

    def test_unknown_column_still_rejected(self):
        """白名单外的列仍应被忽略（不能因为加了 goal 就把校验放宽）。"""
        self.store.patch("s1", not_a_column="x")
        self.assertNotIn("not_a_column", self.store.get("s1"))


if __name__ == "__main__":
    unittest.main()
