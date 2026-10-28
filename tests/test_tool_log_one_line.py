# -*- coding: utf-8 -*-
"""调用记录：默认一行（最新），点它**行内展开**——与待办/子代理/状态同一种方式。"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gantry.ui_src import ui_css, ui_js

HTML = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
JS = ui_js()
CSS = ui_css()


class ToolLogInlineTests(unittest.TestCase):
    def test_markup_row_plus_inline_body(self):
        self.assertIn('id="toolLogLatest"', HTML)
        self.assertIn('id="toolLogBody"', HTML)
        self.assertNotIn('id="toolLogPop"', HTML, "不再有浮层弹窗")

    def test_order_todo_subagents_toollog_status(self):
        i_team = HTML.find('id="teamCard"')
        i_log = HTML.find('id="toolLog"')
        i_status = HTML.find('id="streamStatus"')
        self.assertTrue(0 < i_team < i_log < i_status, "标记顺序不对")

    def test_row_shows_latest_only(self):
        i = JS.find("function renderToolLog(")
        seg = JS[i:i + 2400]
        self.assertIn("all[all.length-1]", seg, "那一行取最新一步")
        self.assertIn("toolLogOne", seg)

    def test_expand_is_inline_not_floating(self):
        self.assertIn("function toggleToolLog()", JS)
        self.assertNotIn("positionToolLogPop", JS, "不该再有浮层定位")
        self.assertNotIn("toolLogPop", JS)
        i = CSS.find("#toolLog > #toolLogBody{")
        self.assertGreater(i, 0, "展开区要有流内样式")
        block = CSS[i:CSS.find("}", i) + 1]
        self.assertIn("max-height:34vh", block)
        self.assertIn("overflow:auto", block)
        self.assertNotIn("position:fixed", block, "行内展开不能是浮层")

    def test_step_text_is_data_not_ui(self):
        self.assertIn('class="ss-reason" data-i18n-skip="1"', JS)
        self.assertIn('id="toolLogOne" data-i18n-skip="1"', HTML)


if __name__ == "__main__":
    unittest.main()
