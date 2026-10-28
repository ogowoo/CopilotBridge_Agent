# -*- coding: utf-8 -*-
"""样式拆分（`webui/style.css` → `webui/css/*.css`）的结构回归。

拆法：只在原有章节边界下刀，**连续区段、顺序不变**——11 个文件的正文拼接起来与原文件
逐字节相同（136375 字符 / 2341 行，见 scripts/css_verify.py），所以层叠行为零变化。
这里钉住"拆分没走样"：
1. 老文件已删除，css/ 下文件齐全且都在 CSS_ORDER 里；
2. index.html 按 CSS_ORDER 全部引用（少一个＝大片样式丢失）；
3. 每个文件带自己的头注释（说什么、第几块），且**不许用 CSS import 指令**（会串行阻塞首屏）；
4. 关键令牌、主题选择器、媒体查询都还在。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gantry.ui_src import CSS_ORDER, css_rule, ui_css  # noqa: E402

HTML = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
CSSDIR = ROOT / "webui" / "css"


class CssSplitTests(unittest.TestCase):
    def test_old_file_gone(self):
        self.assertFalse((ROOT / "webui" / "style.css").exists(),
                         "style.css 应已拆成 css/*.css")
        self.assertTrue(CSSDIR.is_dir())

    def test_all_files_listed_in_order(self):
        have = sorted(p.name for p in CSSDIR.glob("*.css"))
        self.assertEqual(have, sorted(CSS_ORDER), "css/ 下的文件必须都在 CSS_ORDER 里")
        self.assertGreaterEqual(len(have), 8, "拆得太少没意义，也别拆成一堆碎片")

    def test_index_links_every_file_in_order(self):
        links = re.findall(r'<link rel="stylesheet" href="css/([^"]+)">', HTML)
        self.assertEqual(links, CSS_ORDER,
                         "index.html 里 <link> 的顺序必须等于 CSS_ORDER（= 层叠顺序）")

    def test_no_css_import(self):
        for p in CSSDIR.glob("*.css"):
            self.assertNotIn("@import", p.read_text(encoding="utf-8"),
                             f"{p.name} 里不许用 CSS import（会串行阻塞首屏）")

    def test_each_file_has_header(self):
        for p in CSSDIR.glob("*.css"):
            head = p.read_text(encoding="utf-8").split("\n")[0]
            self.assertTrue(head.startswith(f"/* {p.name} —"),
                            f"{p.name} 缺文件头注释: {head[:40]}")

    def test_tokens_and_themes_survive(self):
        css = ui_css()
        self.assertIn(":root{", css, "令牌块没了")
        for var in ("--accent", "--panel", "--line", "--muted"):
            self.assertIn(var, css, f"缺少令牌 {var}")
        for theme in ('[data-theme="light"]', '[data-theme="solarized"]', '[data-theme="mint"]'):
            self.assertIn(theme, css, f"主题 {theme} 丢了")

    def test_media_queries_survive(self):
        css = ui_css()
        self.assertGreaterEqual(css.count("@media"), 20, "媒体查询明显变少（拆丢了？）")

    def test_helpers_work(self):
        self.assertTrue(css_rule(".todolist"), "css_rule 取不到 .todolist（样式被改名/删了？）")
        self.assertGreater(len(ui_css()), 100_000, "拼接后明显偏短，可能漏读文件")


if __name__ == "__main__":
    unittest.main()
