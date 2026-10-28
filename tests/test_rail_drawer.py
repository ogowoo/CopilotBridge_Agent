# -*- coding: utf-8 -*-
"""遮罩（.railscrim）默认必须全局隐藏，否则会把 .app 网格挤坏。

## 真事（用户截图："搞啥呢？"）
我给窄屏抽屉加遮罩时，把 `.railscrim{display:none}` 只写进了
`@media(max-width:860px)`. 窗口一放大（1714×961）,这条规则不再生效,
遮罩 `<div>` 就成了 `.app`（`display:grid`）的**第三个网格项**,
把 `grid-template-columns:318px 1fr` 挤成一条窄柱 —— 整个界面被压到左侧
约 60px 内，文字逐字竖排换行，右侧大片空白。

## 守则
- `.railscrim` 的默认隐藏必须是**全局**规则（不在任何媒体查询里）；
- 只有 ≤860px 且 `data-rail="open"` 时才显示；
- 窄屏抽屉展开时，把手 ☰ 不能压住抽屉第一行（padding-top 让位）。
"""
from gantry.ui_src import ui_js
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
_RAW = (BASE / "webui" / "style.css").read_text(encoding="utf-8")
CSS = re.sub(r"/\*[\s\S]*?\*/", " ", _RAW)
HTML = (BASE / "webui" / "index.html").read_text(encoding="utf-8")
APP = ui_js()


def _strip_media_queries(css: str) -> str:
    """去掉所有 @media 块，只留顶层规则。"""
    out, i = [], 0
    while i < len(css):
        m = re.compile(r"@media[^{]*\{").search(css, i)
        if not m:
            out.append(css[i:])
            break
        out.append(css[i:m.start()])
        depth, j = 0, m.end() - 1
        while j < len(css):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        i = j + 1
    return "".join(out)


TOP_LEVEL = _strip_media_queries(CSS)


class ScrimGlobalHideTests(unittest.TestCase):
    def test_scrim_hidden_at_top_level(self):
        self.assertRegex(
            TOP_LEVEL, r"\.railscrim\s*\{[^}]*display:none",
            "`.railscrim{display:none}` 必须是**全局**规则；只写在媒体查询里会让宽屏"
            "出现一个多出来的网格列，把主区挤成窄柱")

    def test_scrim_element_exists_once(self):
        self.assertEqual(HTML.count('id="railScrim"'), 1)

    def test_scrim_only_shown_when_drawer_open(self):
        show = [m.group(0) for m in re.finditer(r'[^{}]*\[data-rail="open"\][^{}]*\.railscrim[^{}]*\{[^}]*\}', CSS)]
        self.assertTrue(show, "遮罩的显示规则应绑定 data-rail=open")
        self.assertIn("display:block", " ".join(show).replace(" ", ""))

    def test_scrim_closes_drawer(self):
        self.assertIn('data-act="closeRail"', HTML)
        self.assertIn("closeRail:", APP)
        i = APP.index("closeRail:")
        self.assertIn("closed", APP[i:i + 260])

    def test_drawer_pads_for_handle(self):
        rules = re.findall(r'\[data-rail="open"\]\s*\.rail\{([^}]*)\}', CSS)
        self.assertTrue(rules, "缺少抽屉展开态规则")
        joined = " ".join(rules).replace(" ", "")
        # 期望：顶部让位（padding:52px ...）——不给 ☰ 留位置它就会压住「+ 新任务」
        self.assertIn("padding:52px", joined,
                      "展开时顶部要让位给 ☰，否则把手压住「+ 新任务」")


if __name__ == "__main__":
    unittest.main()
