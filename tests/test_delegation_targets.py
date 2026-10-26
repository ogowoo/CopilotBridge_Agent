# -*- coding: utf-8 -*-
"""任务可拆性识别：给模型"可考虑扇出"的提示，不能对普通任务噪声化。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.agent import delegation_targets


class DelegationTargetTests(unittest.TestCase):
    def test_path_list_detected(self):
        got = delegation_targets(
            "分别审计 workspace/core、workspace/webui、workspace/tests 这三个目录的代码风格")
        self.assertEqual(len(got), 3)
        self.assertIn("workspace/core", got)

    def test_model_list_detected(self):
        got = delegation_targets(
            "对比三个模型的输出：kimi-coding/k3、deepseek/deepseek-chat、copilot-auto")
        self.assertGreaterEqual(len(got), 3)

    def test_single_file_task_not_flagged(self):
        self.assertEqual(delegation_targets("把 workspace/x.py 里的函数名改成蛇形"), [])

    def test_small_task_not_flagged(self):
        self.assertEqual(delegation_targets("写一个 README 说明怎么启动服务"), [])

    def test_empty_task(self):
        self.assertEqual(delegation_targets(""), [])


if __name__ == "__main__":
    unittest.main()
