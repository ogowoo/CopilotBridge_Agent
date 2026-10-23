# -*- coding: utf-8 -*-
"""答案噪声：CBP 标记 / 无障碍提示 / 自己的问题回声都不能当成回答。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class CleanAnswerTests(unittest.TestCase):
    def test_strips_markers_and_ui_hint(self):
        from cbp.bridge import _clean_answer
        raw = ("[CBP-AGENT][task-x][C000]\n只回一个词：banana"
               "Press Tab to access the More options button.")
        out = _clean_answer(raw)
        self.assertNotIn("[CBP-AGENT]", out)
        self.assertNotIn("Press Tab", out)
        self.assertIn("banana", out)

    def test_keeps_real_answer(self):
        from cbp.bridge import _clean_answer
        self.assertEqual(_clean_answer('[CBP-TURN:abc]\n{"type":"final"}'), '{"type":"final"}')

    def test_echo_only_detected(self):
        from cbp.bridge import _echo_only
        sent = "只回一个词：banana"
        echo = ("[CBP-AGENT][task-x][C000]\n只回一个词：banana"
                "Press Tab to access the More options button.")
        self.assertTrue(_echo_only(echo, sent), "只是自己的问题回声")
        self.assertFalse(_echo_only("banana", sent))
        self.assertFalse(_echo_only('{"type":"final","content":"ok"}', sent))

    def test_wait_agent_answer_rejects_echo(self):
        src = ((ROOT / "cbp" / "bridge.py").read_text(encoding="utf-8")
               + (ROOT / "cbp" / "agentrun.py").read_text(encoding="utf-8"))
        self.assertIn("_echo_only(value, sent)", src)
        i = src.find("async def wait_agent_answer(")
        self.assertIn("sent: str = \"\"", src[i:i + 200])


if __name__ == "__main__":
    unittest.main()
