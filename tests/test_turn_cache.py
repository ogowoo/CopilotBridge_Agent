"""回合结果表：同一提示词重放时复用答案、步数门槛、级联删除。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.bridge import turn_cache_decision
from gantry.session_store import SessionStore


class TurnCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_turn_"))
        self.store = SessionStore(self.tmp / "agent.db")
        self.store.create("s1", "任务", str(self.tmp), "copilot-auto")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_get(self):
        self.store.save_turn("s1", 0, "hash-a", "答案是 A", 3)
        got = self.store.get_turn("s1", 0, "hash-a")
        self.assertIsNotNone(got)
        self.assertEqual(got["answer"], "答案是 A")
        self.assertEqual(got["steps_after"], 3)
        self.assertIsNone(self.store.get_turn("s1", 1, "hash-a"), "对话序号不同不应命中")
        self.assertIsNone(self.store.get_turn("s1", 0, "hash-b"), "提示词不同不应命中")

    def test_upsert_same_turn(self):
        self.store.save_turn("s1", 0, "h", "旧", 1)
        self.store.save_turn("s1", 0, "h", "新", 5)
        got = self.store.get_turn("s1", 0, "h")
        self.assertEqual(got["answer"], "新")
        self.assertEqual(got["steps_after"], 5)

    def test_answer_is_capped(self):
        self.store.save_turn("s1", 0, "h", "x" * 9000, 1)
        self.assertLessEqual(len(self.store.get_turn("s1", 0, "h")["answer"]), 4000)

    def test_inflight_then_done(self):
        """发请求前落 inflight；成功后转 done。"""
        self.store.begin_turn("s1", 0, "h", 2)
        got = self.store.get_turn("s1", 0, "h")
        self.assertEqual(got["status"], "inflight")
        self.assertEqual(got["answer"], "")
        self.store.save_turn("s1", 0, "h", "答案", 3)
        got = self.store.get_turn("s1", 0, "h")
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["answer"], "答案")

    def test_decision_reuse_probe_send(self):
        """决策三态：已成功且无进展→复用；在途→只读核对；其它→正常发送。"""
        self.assertEqual(turn_cache_decision(None, 5), ("send", ""))
        self.assertEqual(turn_cache_decision({"status": "done", "steps_after": 5, "answer": "A"}, 5),
                         ("reuse", "A"))
        # 步数已推进（此后有进展）→ 视为新回合，正常发送
        self.assertEqual(turn_cache_decision({"status": "done", "steps_after": 5, "answer": "A"}, 7)[0],
                         "send")
        # 在途：绝不重发
        self.assertEqual(turn_cache_decision({"status": "inflight", "steps_after": 5, "answer": ""}, 5)[0],
                         "probe")

    def test_cascade_delete(self):
        self.store.save_turn("s1", 0, "h", "a", 1)
        self.assertTrue(self.store.delete("s1"))
        self.assertIsNone(self.store.get_turn("s1", 0, "h"), "会话删除后回合缓存也应清理")


if __name__ == "__main__":
    unittest.main()
