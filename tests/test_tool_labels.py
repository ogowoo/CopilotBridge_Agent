# -*- coding: utf-8 -*-
"""工具在界面里的显示名（read_text → 读取文本 / Read text），title 保留原始 id。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JS = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
I18N = (ROOT / "webui" / "i18n.js").read_text(encoding="utf-8")


class ToolLabelTests(unittest.TestCase):
    def test_map_has_three_languages(self):
        self.assertIn("const TOOL_LABELS=", JS)
        i = JS.find("read_text:{")
        self.assertGreater(i, 0)
        block = JS[i:i + 120]
        for lang in ("zh:", "tw:", "en:"):
            self.assertIn(lang, block, f"read_text 缺 {lang}")

    def test_fallback_to_raw_id(self):
        """没收录的工具回落到原始 id（绝不显示空白）。"""
        i = JS.find("function toolLabel(")
        self.assertGreater(i, 0)
        body = JS[i:i + 400]
        self.assertIn("return key", body)
        self.assertIn("TOOL_LABELS[key]", body)

    def test_used_in_step_row_and_log(self):
        self.assertIn("toolLabel(tool)", JS)
        self.assertIn("toolLabel(tool)+' · '", JS)
        self.assertIn("title=", JS, "步骤行要给原始 id 留 title")

    def test_summary_key_translated(self):
        self.assertIn('"汇总": "Summary"', I18N)


if __name__ == "__main__":
    unittest.main()
