# -*- coding: utf-8 -*-
"""界面语言（繁体中文）下的锚点识别。

用户反馈繁体中文 Copilot 网页无法定位回合边界。本文件用假页面文本验证
繁体中文锚点能命中，不依赖真实浏览器。
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from gantry import bridge as b


class TraditionalChineseMarkersTests(unittest.TestCase):
    def test_markers_include_traditional_chinese(self):
        self.assertTrue(any("說" in m or "回覆" in m or "回答" in m
                            for m in b.MARKERS["assistant_turn"]))
        self.assertTrue(any("說" in m or "使用者" in m
                            for m in b.MARKERS["user_turn"]))
        self.assertTrue(any("來源" in m or "傳送" in m
                            for m in b.MARKERS["answer_end"]))
        self.assertTrue(any("來源" in m or "參考資料" in m
                            for m in b.MARKERS["sources"]))

    def test_selector_text_include_traditional_chinese(self):
        self.assertTrue(any("停止" in t or "中止" in t
                            for t in b.SELECTOR_TEXT["stop"]))
        self.assertTrue(any("聊天" in t or "新增" in t
                            for t in b.SELECTOR_TEXT["new_chat"]))
        self.assertTrue(any("傳送" in t
                            for t in b.SELECTOR_TEXT["send_aria"]))
        self.assertTrue(any("接受" in t or "關閉" in t
                            for t in b.SELECTOR_TEXT["dismiss"]))


class TraditionalChineseAnswerExtractionTests(unittest.TestCase):
    """假页面：繁体中文锚点下也能抽出回答。"""

    def test_traditional_markers_extract(self):
        body = (
            "新增聊天\n搜尋\n\n"
            "你說:\n只回覆兩個字：收到\n\n"
            "Copilot 的回覆:\n收到\n\n"
            "來源\n"
        )
        self.assertEqual(b.extract_latest_answer(body), "收到")

    def test_traditional_alternate_marker(self):
        body = (
            "你的發言:\n只回覆數字 42\n\n"
            "Copilot 說:\n42\n\n"
            "傳送訊息給 Copilot\n"
        )
        self.assertEqual(b.extract_latest_answer(body), "42")

    def test_structural_fallback_without_assistant_marker(self):
        """assistant 锚点缺失时不再把用户自己的问题当成回答（宁可空）。"""
        body = (
            "何かの履歷\n"
            "你說:\n只回覆兩個字：收到\n\n"
            "收到\n"
        )
        self.assertEqual(b.extract_latest_answer(body), "")


if __name__ == "__main__":
    unittest.main()
