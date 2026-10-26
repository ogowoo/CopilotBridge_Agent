# -*- coding: utf-8 -*-
"""模型标签容错匹配：菜单标签与 ui_names 有细微差别时也要认出来。

真事：会话用 `copilot-gpt-5-6-sol-think-deeper`，而页面菜单/按钮上实际显示
"GPT-5.6 Sol Think"（没有 deeper）→ 锚定正则每回合都落空，界面每回合弹
"模型未在 Copilot 菜单中找到"，还白跑一次菜单展开。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.bridge import MODELS, label_matches, normalize_label  # noqa: E402


class NormalizeTests(unittest.TestCase):
    def test_strips_spaces_and_punct(self):
        self.assertEqual(normalize_label("GPT-5.6 Sol Think"), "gpt56solthink")
        self.assertEqual(normalize_label("  GPT · 5.6  "), "gpt56")

    def test_empty(self):
        self.assertEqual(normalize_label(""), "")
        self.assertEqual(normalize_label(None), "")


class LabelMatchTests(unittest.TestCase):
    NAMES = ["GPT-5.6 Sol Think deeper", "Sol Think deeper"]

    def test_exact_normalized(self):
        self.assertTrue(label_matches(self.NAMES, "GPT-5.6 Sol Think deeper"))
        self.assertTrue(label_matches(self.NAMES, "gpt5.6 sol think deeper"))

    def test_real_ui_label_without_suffix(self):
        """页面实际标签（少了 deeper）必须命中。"""
        self.assertTrue(label_matches(self.NAMES, "GPT-5.6 Sol Think"))

    def test_partial_contains(self):
        self.assertTrue(label_matches(self.NAMES, "Sol Think"))

    def test_short_token_does_not_swallow_everything(self):
        """"GPT" 这种短词不能匹配一切（否则会点错模型）。"""
        self.assertFalse(label_matches(self.NAMES, "GPT"))
        self.assertFalse(label_matches(self.NAMES, "Auto"))

    def test_unrelated(self):
        self.assertFalse(label_matches(self.NAMES, "Quick response"))
        self.assertFalse(label_matches(self.NAMES, ""))


class RegistryTests(unittest.TestCase):
    def test_real_world_labels_covered(self):
        """实测出现过的标签都要能匹配上对应模型。"""
        cases = {
            "copilot-gpt-5-6-sol-think-deeper": ["GPT-5.6 Sol Think", "GPT-5.6 Sol Think deeper"],
            "copilot-gpt-5-6-sol-quick-response": ["GPT-5.6 Sol Quick"],
            "copilot-auto": ["Auto", "自动", "自動"],
            "copilot-quick-response": ["Quick response", "快速响应", "クイック応答"],
            "copilot-think-deeper": ["Think deeper", "深度思考", "深入思考", "深く考える"],
        }
        for mid, labels in cases.items():
            names = MODELS[mid]["ui_names"]
            for label in labels:
                self.assertTrue(label_matches(names, label), f"{mid} 匹配不上标签 {label!r}")

    def test_variant_lists_disjoint_enough(self):
        """不应把一个模型误判成另一个：Quick 与 Think 不能互相匹配。"""
        quick = MODELS["copilot-gpt-5-6-sol-quick-response"]["ui_names"]
        self.assertFalse(label_matches(quick, "GPT-5.6 Sol Think"))
        think = MODELS["copilot-gpt-5-6-sol-think-deeper"]["ui_names"]
        self.assertFalse(label_matches(think, "GPT-5.6 Sol Quick"))


if __name__ == "__main__":
    unittest.main()
