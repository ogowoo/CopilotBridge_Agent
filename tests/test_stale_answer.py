# -*- coding: utf-8 -*-
"""回归：追加指令后必须等**新一轮回答**，不能把上一次的最终答复当成新回答。

用户实测：在已完成的会话里追加"把 docs/xx.md 转成中文"，界面立刻出现与上一轮
完全相同的 "Task completed…"，而且没有任何新步骤（5 秒就"完成"）。
根因是 `wait_agent_answer` 里 `(value != previous or generation_seen)` 给了旧文本后门。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AGENTRUN = (ROOT / "gantry" / "agentrun.py").read_text(encoding="utf-8")
PAGE = (ROOT / "gantry" / "page.py").read_text(encoding="utf-8")
BRIDGE = (ROOT / "gantry" / "bridge.py").read_text(encoding="utf-8")


class StaleAnswerTests(unittest.TestCase):
    def test_no_generation_seen_backdoor(self):
        i = AGENTRUN.index("def wait_agent_answer(")
        seg = AGENTRUN[i:i + 6000]
        self.assertNotIn("(value != previous or generation_seen)", seg,
                         "旧文本后门：generation_seen 会把没变化的文本当答案")
        self.assertIn("is_candidate = bool(value) and (value != previous", seg)

    def test_marker_threaded_through(self):
        i = AGENTRUN.index("def wait_agent_answer(")
        seg = AGENTRUN[i:i + 6000]
        self.assertIn("marker: str = \"\"", seg)
        self.assertIn("answer_after_marker(marker)", seg)
        # 两条发送路径都要传 marker
        self.assertIn("marker=marker", AGENTRUN)
        self.assertIn("marker=turn_marker", AGENTRUN)

    def test_helper_exists_and_exported(self):
        self.assertIn("async def answer_after_marker(", PAGE)
        self.assertIn("answer_after_marker", BRIDGE, "bridge 要 re-export 才能 b.answer_after_marker")
        i = PAGE.index("async def answer_after_marker(")
        seg = PAGE[i:i + 1200]
        self.assertIn("raw.rfind(marker)", seg, "只取标记之后的文本")
        self.assertIn('return ""', seg, "标记还没出现时应返回空串（继续等）")


if __name__ == "__main__":
    unittest.main()
