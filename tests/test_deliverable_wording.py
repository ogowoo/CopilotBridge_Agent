# -*- coding: utf-8 -*-
"""交付物措辞：产出文件就在本地工作区，回答里不能说"可下载"。

真事（用户截图）：任务生成了 `pyshot_todo.md`，回答却说"并提供可下载文件"——
文件本来就在用户本机工作区，再让他下载一次自相矛盾。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry import prompt  # noqa: E402
from gantry.tools_extra import _DESCRIPTIONS  # noqa: E402


class DeliverableRuleTests(unittest.TestCase):
    def test_rule_exists_and_says_local(self):
        rule = prompt.DELIVERABLE_RULE
        self.assertIn("工作区", rule)
        self.assertIn("相对路径", rule)
        self.assertIn("不要", rule)

    def test_rule_gives_positive_example(self):
        """要给出正确说法，不能只是一句禁令。"""
        self.assertIn("已生成", prompt.DELIVERABLE_RULE)

    def test_registered_in_agent_prompt(self):
        src = (Path(__file__).resolve().parent.parent / "gantry" / "agent_prompt.py").read_text(encoding="utf-8")
        # 现在注册的是**函数**（按提示词语言取译本），不再是常量：
        # 语言放在 contextvar 里，import 时还不知道请求语言，必须渲染时求值。
        self.assertIn('reg.add("deliverable", deliverable_rule', src)

    def test_present_description_warns_against_download_wording(self):
        desc = str(_DESCRIPTIONS.get("present") or "")
        self.assertTrue(desc, "present 必须有描述")
        self.assertIn("工作区", desc)
        self.assertIn("可下载", desc, "描述里要明确『不要说可下载』")


if __name__ == "__main__":
    unittest.main()
