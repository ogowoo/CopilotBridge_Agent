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

    def test_plain_chinese_suggestion_tail_stripped(self):
        text = "收到\n\n改成更正式的语气\n换一种更亲切的说法\n再提供几个两字版本"
        self.assertEqual(strip_suggestion_tail(text), "收到")

    def test_plain_traditional_suggestion_tail_stripped(self):
        text = "收到\n\n改成更正式的語氣\n提供三種不同說法\n改成適合工作場合"
        self.assertEqual(strip_suggestion_tail(text), "收到")

    def test_plain_japanese_suggestion_tail_stripped(self):
        text = ("收到\n\nもっと丁寧な表現にして\nビジネス向けに言い換えて\n"
                "英語に翻訳して")
        self.assertEqual(strip_suggestion_tail(text), "收到")

    def test_plain_english_suggestion_tail_stripped(self):
        text = ("It is 42.\n\nMake it more formal\nTranslate this to Japanese\n"
                "Give me three alternatives")
        self.assertEqual(strip_suggestion_tail(text), "It is 42.")

    def test_plain_numbered_list_untouched(self):
        text = "步骤如下：\n\n1. 读取文件\n2. 修改配置\n3. 运行测试"
        self.assertEqual(strip_suggestion_tail(text), text)

    def test_plain_non_suggestion_short_block_untouched(self):
        text = "结论如下：\n\n端口是 8080\n日志在 data 目录"
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
