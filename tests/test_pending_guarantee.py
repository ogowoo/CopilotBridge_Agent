# -*- coding: utf-8 -*-
"""回归：跟进消息一定会出现在下一回合提示词里。

实测事故（task-mvn6jjrt）：用户 10:30 / 10:33 / 10:45 三次发
"现在帮我治理一下发布流水线问题"，`turn_results.prompt` 里三次都是
"继续同一任务。上一动作实际结果：…" —— 用户原话一次都没进提示词，
模型只能在旧任务（"把文件加载进来我看看"）上打转。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUEUE = (ROOT / "gantry" / "queue.py").read_text(encoding="utf-8")
AGENT = (ROOT / "gantry" / "agent.py").read_text(encoding="utf-8")


class PendingGuaranteeTests(unittest.TestCase):
    def test_queue_does_not_swallow_message(self):
        """入队处理会把 content 原样交给 run_agent（不丢消息）。"""
        i = QUEUE.index("if item.get(\"agent_mode\"):")
        seg = QUEUE[i:i + 800]
        self.assertIn("content=content", seg)
        self.assertIn("run_agent", seg)

    def test_prompt_marks_current_goal(self):
        self.assertIn("【当前目标】", AGENT, "提示词要把最新要求标成当前目标")
        self.assertEqual(AGENT.count("【当前目标】"), 2, "首轮与回合内两处都要改到")

    def test_pending_prompt_contains_user_text(self):
        # 两处提示词都用 "\n".join(pending_now) 拼用户原话
        self.assertGreaterEqual(AGENT.count('"\\n".join(pending_now)'), 2)




class PendingWrittenOnFollowupTests(unittest.TestCase):
    """缩进事故回归：`pending.append(task)` 曾被缩进到 `except` 里，正常路径永不执行。"""

    def test_pending_block_not_inside_except(self):
        """`pending.append(task)` 必须和 `except` 同级（12 空格缩进），不能在 except 块里。"""
        src = (ROOT / "gantry" / "agentrun.py").read_text(encoding="utf-8")
        lines = src.splitlines()
        idx = next(n for n, l in enumerate(lines) if l.strip().startswith("pending = list(existing.get"))
        line = lines[idx]
        indent = len(line) - len(line.lstrip())
        self.assertEqual(indent, 16, f"pending 那行的缩进是 {indent}，应为 16（else→if→语句）")
        # 往上找最近的 except：如果 first-run 就是 except，那说明还在 except 块内
        for back in range(1, 14):
            prev = lines[idx - back].strip()
            if prev.startswith("if task.strip()"):
                break
            self.assertFalse(prev.startswith("except"), "pending 落在了 except 块里（缩进事故）")
        else:
            self.fail("往上 14 行内没找到 if 分支")

    def test_followup_prompt_carries_user_text(self):
        # 回合提示词用 pending_now 拼用户原话，并且标注成【当前目标】
        i = AGENT.index("【当前目标】")
        seg = AGENT[i:i + 300]
        self.assertIn("pending_now", seg)


if __name__ == "__main__":
    unittest.main()
