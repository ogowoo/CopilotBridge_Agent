# -*- coding: utf-8 -*-
"""紧凑动作格式（无 type）+ Copilot 建议尾巴：解析器必须认。

真实事故（task-afa55d82ff7a，2026-10-20 01:31）：模型返回
    {"step":17,"tool":"grep","arguments":{...},"reason":"..."}
后面跟着 Copilot 的"后续建议"三行文字。解析器按 `type ∈ {tool,final,actions}`
过滤，把这条完整动作丢掉，整轮报"未返回完整、可解析的 Agent JSON 动作"，
连续 13 步空转、任务被卡死。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.agent import AgentRunner  # noqa: E402

REAL = (
    '{"step":17,"tool":"grep","arguments":{"pattern":"setShortcut|shortcutChanged|'
    'hotkey|register_hotkey","path":"pyshot","include":".py","max_results":100},'
    '"reason":"定位快捷键更新后未同步菜单显示的调用链"}\n'
    '\n继续读取匹配代码\n搜索菜单初始化函数\n查看快捷键相关测试'
)


class CompactActionTests(unittest.TestCase):
    def test_no_type_field_is_accepted(self):
        out = AgentRunner._parse(REAL)
        self.assertEqual(out.get("type"), "tool")
        self.assertEqual(out.get("tool"), "grep")
        self.assertEqual(out["arguments"]["path"], "pyshot")

    def test_reason_and_step_kept(self):
        out = AgentRunner._parse(REAL)
        self.assertIn("快捷键", out.get("reason") or "")
        self.assertEqual(out.get("step"), 17)

    def test_trailing_suggestion_lines_ignored(self):
        """回答后面跟着建议文字不该影响解析。"""
        out = AgentRunner._parse('{"tool":"read_text","arguments":{"path":"a.txt"}}\n\n继续阅读更多')
        self.assertEqual(out.get("type"), "tool")
        self.assertEqual(out.get("tool"), "read_text")

    def test_normal_typed_action_still_works(self):
        out = AgentRunner._parse('{"type":"tool","tool":"grep","arguments":{"pattern":"x"}}')
        self.assertEqual(out.get("tool"), "grep")

    def test_batch_of_compact_actions(self):
        text = ('{"tool":"read_text","arguments":{"path":"a"}}\n'
                '{"tool":"write_text","arguments":{"path":"b","content":"x"}}')
        out = AgentRunner._parse(text)
        self.assertEqual(out.get("type"), "actions")
        self.assertEqual(len(out.get("actions") or []), 2)

    def test_pure_garbage_still_rejected(self):
        with self.assertRaises(ValueError):
            AgentRunner._parse("这里根本没有 JSON 动作")


if __name__ == "__main__":
    unittest.main()
