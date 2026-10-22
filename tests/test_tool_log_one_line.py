# -*- coding: utf-8 -*-
"""调用记录：默认只有一行（最新），点它浮动展开——用源码守卫防回退。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HTML = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "webui" / "style.css").read_text(encoding="utf-8")


class ToolLogOneLineTests(unittest.TestCase):
    def test_markup_has_one_line_and_popover(self):
        self.assertIn('id="toolLogLatest"', HTML)
        self.assertIn('id="toolLogPop"', HTML)
        self.assertIn('id="toolLogBody"', HTML)
        # 顺序：todo/子代理(teamCard) → 调用记录 → 状态条
        i_team = HTML.find('id="teamCard"')
        i_log = HTML.find('id="toolLog"')
        i_status = HTML.find('id="streamStatus"')
        self.assertTrue(0 < i_team < i_log < i_status, "标记顺序不对")

    def test_default_shows_only_latest(self):
        i = JS.find("function renderToolLog(")
        self.assertGreater(i, 0)
        seg = JS[i:i + 2200]
        self.assertIn("all[all.length-1]", seg, "那一行要取最新一步")
        self.assertIn("toolLogOne", seg)

    def test_click_opens_floating_popover(self):
        self.assertIn("function toggleToolLogPop(", JS)
        self.assertIn("toggleToolLog:()=>toggleToolLogPop()", JS)
        # 浮层定位在那一行上方，不占布局
        self.assertIn("positionToolLogPop", JS)
        self.assertIn("pop.style.bottom", JS)
        self.assertIn(".toollogpop{position:fixed", CSS)
        # 只允许定义一个开合函数（历史上重复定义过，后一个把浮层覆盖掉了）
        self.assertEqual(JS.count("function toggleToolLogPop("), 1)

    def test_step_text_is_data_not_ui(self):
        # 工具名/理由/那一行是模型与工具产出，不参与语言审计
        self.assertIn('class="ss-reason" data-i18n-skip="1"', JS)
        self.assertIn('id="toolLogOne" data-i18n-skip="1"', HTML)


if __name__ == "__main__":
    unittest.main()
