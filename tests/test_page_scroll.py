# -*- coding: utf-8 -*-
"""页面级滚动必须为 0：顶部横幅不能让整页可滚（否则顶部一行被切）。

用户截图（800×545）：**顶部那一行被切掉了**（最上沿露出半个 "AA" 和图标），
而且整个页面能滚。

## 实测根因
`body.bootbar-on .app{margin-top:28px}` + `html,body{height:100%}` 会发生
**上外边距折叠**：margin 穿透 body 折到 html 上，文档高度 = 视口 + 28px
→ `documentElement.scrollHeight - clientHeight = 28` → 页面可滚 28px
→ 一滚，`.app` 顶部那 28px 被推出视口，工具栏/模型那一行被切。

（同一条规则还被写了两份：`calc(100vh - 26px)` 与 `calc(100vh - 28px)`,
后者胜出，所以实测差值是 28。）

## 修法
- `html,body{overflow:hidden}`：页面本身永不滚动，滚动只发生在内部面板；
- 横幅让位改用 **body 的 padding-top**（padding 不参与外边距折叠），
  `.app` 高度 `calc(100vh - 28px)`;
- 合并重复的 26px / 28px 两条规则。
"""
from gantry.ui_src import ui_css
import re
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
_RAW_CSS = ui_css()
# 先剥掉注释再匹配：注释里引用过旧写法，直接正则会误判
CSS = re.sub(r"/\*[\s\S]*?\*/", " ", _RAW_CSS)


class PageScrollTests(unittest.TestCase):
    def test_html_body_cannot_scroll(self):
        m = re.search(r"html,body\{([^}]*)\}", CSS)
        self.assertIsNotNone(m, "缺少 html,body 基础规则")
        self.assertIn("overflow:hidden", m.group(1).replace(" ", ""),
                      "页面本身必须禁止滚动（否则顶部横幅会让整页可滚，切掉工具栏）")

    def test_banner_offset_uses_padding_not_margin(self):
        # 找 bootbar-on 的 .app 规则：不允许再用 margin-top（会与 body 高度折叠）
        rules = re.findall(r"body\.bootbar-on[^{]*\{[^}]*\}", CSS)
        self.assertTrue(rules, "缺少 body.bootbar-on 规则")
        for r in rules:
            if ".app" in r:
                self.assertNotIn("margin-top", r,
                                 "横幅让位不能用 .app{margin-top}：会与 html/body 的 "
                                 "height:100% 折叠，把文档撑高、页面可滚")
        joined = "\n".join(rules)
        self.assertIn("padding-top", joined, "横幅让位应改用 body 的 padding-top")
        self.assertIn("calc(100vh-28px)", joined.replace(" ", ""),
                      "横幅高 28px，.app 高度要相应减掉")

    def test_no_conflicting_bootbar_app_rules(self):
        rules = [r for r in re.findall(r"body\.bootbar-on[^{]*\{[^}]*\}", CSS) if ".app" in r]
        heights = set()
        for r in rules:
            for h in re.findall(r"height:calc\(100vh\s*-\s*(\d+)px\)", r):
                heights.add(h)
        self.assertLessEqual(len(heights), 1,
                             f"bootbar-on 的 .app 高度写了多个互相冲突的值: {sorted(heights)}")

    def test_inner_scrollers_untouched(self):
        # 内部面板仍要有自己的滚动，别为了"页面不滚"把内容藏死
        for sel in (".chatlog", ".steps", ".livepane"):
            m = re.search(re.escape(sel) + r"\{([^}]*)\}", CSS)
            if not m:
                continue
            self.assertIn("overflow:auto", m.group(1).replace(" ", ""),
                          f"{sel} 必须可内部滚动，否则短视口下内容够不到")


if __name__ == "__main__":
    unittest.main()
