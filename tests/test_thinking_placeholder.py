# -*- coding: utf-8 -*-
"""回答抽取：Copilot 的"思考中"占位文本不能当成答案。

实测（2026-10 英文租户）：提问后回答区先出现
`Checking that now…` / `Lining things up…` / `Gathering details…`，随后**整段替换**
成真正答案。流式通道按公共前缀增长输出：占位文本一旦被当成答案发出，真答案出现时
前缀为空，后续内容永远发不出去 —— 调用方只拿到 "Checking that now…"。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry import bridge as b


class ThinkingPlaceholderTests(unittest.TestCase):
    def test_placeholders_are_stripped(self):
        for text in ("Checking that now…", "Lining things up...", "Gathering details…",
                     "Just a sec…", "Give me a moment…", "正在整理…"):
            self.assertEqual(b._strip_thinking(text), "", f"{text!r} 应被剔除")

    def test_placeholder_removed_but_answer_kept(self):
        raw = "Checking that now…\n\nALPHA-1234"
        self.assertEqual(b._strip_thinking(raw), "ALPHA-1234")
        raw2 = "Give me a moment…\n个版本\n加入表情符号"
        self.assertEqual(b._strip_thinking(raw2), "个版本\n加入表情符号")

    def test_real_answer_is_untouched(self):
        answer = "Just a sec — I'll explain.\n这里说明为什么等待是正常的。"
        self.assertEqual(b._strip_thinking(answer), answer)

    def test_clean_answer_strips_placeholder_and_markers(self):
        raw = "[CBP-AGENT][task-1][C000] Checking that now…\nPress Tab to access the More options button."
        self.assertEqual(b._clean_answer(raw), "")


class StatusBubbleTests(unittest.TestCase):
    """结构信号：状态短语会轮换（Digging in… 等穷举不完），靠"上方状态行"识别。"""

    BODY = ("New chat\nChats\nYou said:\n只回复数字 42\n\nDigging in…\n\n"
            "Copilot said: Digging in…\n\nMessage Copilot\nAI-generated content may be incorrect")

    def test_status_line_found_before_marker(self):
        self.assertEqual(b._status_before_marker(self.BODY), "Digging in…")

    def test_current_answer_ignores_unknown_status_phrase(self):
        import asyncio

        async def fake_body():
            return self.BODY

        saved = b.body_text
        b.body_text = fake_body
        try:
            self.assertEqual(asyncio.run(b.current_answer()), "",
                             "未知状态短语也不能当成答案")
        finally:
            b.body_text = saved

    def test_current_answer_keeps_real_answer(self):
        import asyncio

        body = self.BODY.replace("Copilot said: Digging in…", "Copilot said: 42")

        async def fake_body():
            return body

        saved = b.body_text
        b.body_text = fake_body
        try:
            self.assertEqual(asyncio.run(b.current_answer()), "42")
        finally:
            b.body_text = saved


if __name__ == "__main__":
    unittest.main()
