# -*- coding: utf-8 -*-
"""WebUI 版本信息（淡淡背景的小标签）。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HTML = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "webui" / "style.css").read_text(encoding="utf-8")


class VersionChipTests(unittest.TestCase):
    def test_markup_in_sidebar_footer(self):
        self.assertIn('id="appVersion"', HTML)
        self.assertIn('class="railfoot"', HTML)
        # 在侧栏（aside.rail）里面，不是别处
        aside = HTML[HTML.find('<aside class="rail">'):HTML.find("</aside>")]
        self.assertIn('id="appVersion"', aside)

    def test_fetches_version_from_root(self):
        self.assertIn("async function loadVersion(", JS)
        self.assertIn("fetch('/',{cache:'no-store'})", JS)
        self.assertIn("loadVersion();", JS, "启动时要调用")

    def test_plain_text_no_box(self):
        """用户："直接做背景，不要有框，跟 setting 那个齿轮同高"。"""
        self.assertIn("BOTTOM-VER2", CSS)
        i = CSS.find(".btmver{")
        style = CSS[i:i + 400]
        self.assertIn("background:none", style, "不能有底色块")
        self.assertIn("border:0", style, "不能有边框")
        self.assertIn("font-size:11.5px", style)
        # 与齿轮同一水平线：railfoot 高 34 + 下边距 14（齿轮 bottom:14 / 高 34）
        rail = CSS[CSS.find(".railfoot{"):CSS.find(".railfoot{") + 200]
        self.assertIn("height:34px", rail)
        self.assertIn("margin:0 0 14px", rail)

    def test_row_takes_layout_space(self):
        """占布局（flex:none）——不能再压在会话列表上。"""
        rail = CSS[CSS.find(".railfoot{"):CSS.find(".railfoot{") + 200]
        self.assertIn("flex:none", rail)

    def test_backend_exposes_version(self):
        from cbp.api import app
        self.assertTrue(app.version, "后端 app.version 应存在")


if __name__ == "__main__":
    unittest.main()
