# -*- coding: utf-8 -*-
"""补充测试：剥掉 Copilot 的建议尾巴。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.bridge import strip_suggestion_tail  # noqa: E402

REAL = ('{"step":17,"tool":"grep","arguments":{"pattern":"x"},"reason":"定位调用链"}\n'
        '\n继续读取匹配代码\n搜索菜单初始化函数\n查看快捷键相关测试')


class StripSuggestionTests(unittest.TestCase):
    def test_real_case(self):
        out = strip_suggestion_tail(REAL)
        self.assertTrue(out.startswith("{"))
        self.assertTrue(out.endswith("}"))
        self.assertNotIn("继续读取匹配代码", out)

    def test_plain_text_untouched(self):
        text = "这是一段普通回答，没有 JSON。"
        self.assertEqual(strip_suggestion_tail(text), text)

    def test_json_only_untouched(self):
        text = '{"type":"final","content":"ok"}'
        self.assertEqual(strip_suggestion_tail(text), text)

    def test_broken_json_falls_back(self):
        text = '{"tool":"grep","arguments":{'
        self.assertEqual(strip_suggestion_tail(text), text)

    def test_empty(self):
        self.assertEqual(strip_suggestion_tail(""), "")


if __name__ == "__main__":
    unittest.main()
