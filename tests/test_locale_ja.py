# -*- coding: utf-8 -*-
"""界面语言（日文）下的锚点识别。

用户："现在有使用日文系统的在测试 copilot，只能发送信息给自动化窗口，但收不到回信。"

根因：桥接靠 "Copilot said:" / "You said:" / "Stop generating" / "New chat"
这类界面文案定位回合与回答边界；日文界面上这些锚点不存在，发送后能发出去、
却识别不到回答 → 一直等 → 超时/收不到。

修法：
- MARKERS 与各选择器文案内置日文默认值；
- config.json 的 "markers" 可继续追加任意语言；
- 找不到 assistant 锚点时退到"最后一条用户消息之后"（结构兜底）。

本文件用假页面文本验证日文锚点能命中，不依赖真实浏览器。
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from cbp import bridge as b

# 页面自动化层已拆到 cbp/page.py（bridge.py 第三批解耦），源码断言两处一起读
BRIDGE = ((BASE / "cbp" / "bridge.py").read_text(encoding="utf-8")
          + (BASE / "cbp" / "page.py").read_text(encoding="utf-8"))


class JapaneseMarkersTests(unittest.TestCase):
    def test_markers_include_japanese(self):
        self.assertTrue(any("回答" in m or "応答" in m for m in b.MARKERS["assistant_turn"]))
        self.assertTrue(any("あなた" in m for m in b.MARKERS["user_turn"]))
        self.assertTrue(any("出典" in m for m in b.MARKERS["answer_end"]))
        self.assertTrue(any("出典" in m for m in b.MARKERS["sources"]))

    def test_selector_text_include_japanese(self):
        self.assertTrue(any("停止" in t or "中止" in t for t in b.SELECTOR_TEXT["stop"]))
        self.assertTrue(any("チャット" in t for t in b.SELECTOR_TEXT["new_chat"]))
        self.assertTrue(any("送信" in t for t in b.SELECTOR_TEXT["send_aria"]))
        self.assertTrue(any("同意" in t or "閉じる" in t for t in b.SELECTOR_TEXT["dismiss"]))

    def test_generating_uses_selector_text(self):
        i = BRIDGE.index("async def generating")
        seg = BRIDGE[i:i + 400]
        self.assertIn('SELECTOR_TEXT["stop_aria"]', seg)
        self.assertIn('SELECTOR_TEXT["stop"]', seg)

    def test_send_button_uses_selector_text(self):
        i = BRIDGE.index("async def click_send_button")
        seg = BRIDGE[i:i + 500]
        self.assertIn('SELECTOR_TEXT["send_aria"]', seg)

    def test_new_chat_uses_selector_text(self):
        i = BRIDGE.index("async def new_chat")
        seg = BRIDGE[i:i + 700]
        self.assertIn('SELECTOR_TEXT["new_chat"]', seg)

    def test_dismiss_uses_selector_text(self):
        i = BRIDGE.index("async def dismiss")
        seg = BRIDGE[i:i + 300]
        self.assertIn('SELECTOR_TEXT["dismiss"]', seg)


class JapaneseAnswerExtractionTests(unittest.TestCase):
    """假页面：日文锚点下也能抽出回答。"""

    def test_japanese_markers_extract(self):
        body = (
            "New chat\n検索\nライブラリ\n\n"
            "あなた:\n只回复两个字：收到\n\n"
            "Copilot の回答:\n收到\n\n"
            "出典\n"
        )
        self.assertEqual(b.extract_latest_answer(body), "收到")

    def test_japanese_alternate_marker(self):
        body = (
            "あなたの発言:\n只回复数字 42\n\n"
            "Copilot からの応答:\n42\n\n"
            "Copilot へのメッセージ\n"
        )
        self.assertEqual(b.extract_latest_answer(body), "42")

    def test_structural_fallback_without_assistant_marker(self):
        """assistant 锚点缺失时不再把用户自己的问题当成回答（宁可空）。"""
        body = (
            "何かの履歴\n"
            "あなた:\n只回复两个字：收到\n\n"
            "收到\n"
        )
        # 没有 assistant 锚点：返回空，由上层走超时/诊断，而不是把问题当答案
        self.assertEqual(b.extract_latest_answer(body), "")


class JapanesePatternTests(unittest.TestCase):
    def test_unavailable_matches_japanese(self):
        self.assertTrue(b.copilot_unavailable_reason("現在応答できません。しばらくしてからもう一度お試しください。"))
        self.assertFalse(b.copilot_unavailable_reason("これは普通の回答です。"))

    def test_conversation_full_matches_japanese(self):
        self.assertTrue(b.conversation_full_reason("会話が長くなりすぎました。新しい会話を開始してください。"))
        self.assertFalse(b.conversation_full_reason("正常な回答です。"))


if __name__ == "__main__":
    unittest.main()
