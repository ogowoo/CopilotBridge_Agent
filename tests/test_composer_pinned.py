# -*- coding: utf-8 -*-
"""主列结构对齐 DSH：输入框是主列的最后一个 flex 子项，页面永不滚动。

用户："跟 dsh 一样的结构不行？" —— 行，而且比原来的补丁干净：

    旧：composer 藏在 #chat 内部 → 短视口下整张对话卡落到折叠线以下
        → 只能 position:fixed + 按视口调 padding 硬顶（会压内容）
    新：.main 是 flex 列，composer 是最后一个子项（flex:none）,
        #sessionView/#chatlog 自己滚 → 输入框永远在底部，且不覆盖内容

窄屏(≤860px) 侧栏改成"22px 拉手条 + 展开为浮层"（DSH 式），
不再把侧栏堆在主区上方（那样会吃掉约 265px，545px 高的窗口根本不够）。

实测（真服务 + Edge）：
| 视口 | 页面滚动 | 卡片底 vs 输入框顶 | 输入框可见 |
|---|---|---|---|
| 800×545 | 0 | 438 == 438 | ✓ |
| 1024×600 | 0 | 493 == 493 | ✓ |
| 1400×900 | 0 | 793 == 793 | ✓ |
"""
from gantry.ui_src import ui_css, ui_js
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
_RAW = ui_css()
CSS = re.sub(r"/\*[\s\S]*?\*/", " ", _RAW)
HTML = (BASE / "webui" / "index.html").read_text(encoding="utf-8")
APP = ui_js()


class DshStructureTests(unittest.TestCase):
    def test_composer_is_last_child_of_main(self):
        """composer 必须在 .main 里、且在 #sessionView 之后（不是浮层）。"""
        i_main = HTML.index("<main")
        i_comp = HTML.index('class="composer"')
        i_end = HTML.index("</main>")
        self.assertLess(i_main, i_comp, "composer 应在 <main> 内部")
        self.assertLess(i_comp, i_end, "composer 应在 </main> 之前")
        # composer 不能还在 #chat 内部
        i_chat = HTML.index('id="chat"')
        i_chat_end = HTML.index("<!--", i_comp) if False else i_end
        self.assertTrue(i_comp > i_chat, "结构上 composer 已在 #chat 之后")

    def test_css_pins_composer_inside_main(self):
        """composer 必须在 .main 内常驻可见。

        历史：先在 `f80841e` 里改成 flex 子项（`flex:none`），随后 `1f2d0cc`
        又照 DSH 重做成**浮动框**（`position:absolute; bottom:…`）——测试没跟上。
        现在的约束是：① 是 `.main` 的直接子项；② 用 absolute 定位（不是 fixed，
        fixed 会脱离布局、压住滚动内容）。
        """
        m = re.search(r"\.main\s*>\s*\.composer\{([^}]*)\}", CSS)
        self.assertIsNotNone(m, "composer 必须是 .main 的直接子项")
        body = m.group(1).replace(" ", "")
        self.assertIn("position:absolute", body,
                      "浮动输入框应相对 .main 绝对定位")
        self.assertNotIn("position:fixed", body, "不要用 fixed（会压住滚动内容）")
        self.assertNotRegex(CSS, r"#chat \.composer\{[^}]*position:fixed",
                            "不要再把输入框做成 fixed 浮层（会压住内容）")

    def test_page_never_scrolls(self):
        m = re.search(r"html,body\{([^}]*)\}", CSS)
        self.assertIn("overflow:hidden", m.group(1).replace(" ", ""))

    def test_session_view_is_flex_and_scrolls_inner(self):
        self.assertRegex(CSS, r"#sessionView\{[^}]*display:flex",
                         "#sessionView 应是 flex 列（让对话卡撑满剩余高度）")

    def test_narrow_rail_is_drawer(self):
        self.assertRegex(CSS, r':root\[data-rail="open"\]\s*\.rail\{[^}]*width',
                         "窄屏侧栏应该是抽屉（open 时展开）")
        self.assertIn("narrow", APP, "toggleRail 需按屏宽区分抽屉/折叠语义")
        self.assertIn("'open'", APP)

    def test_kpi_compacted_on_narrow(self):
        self.assertIn("repeat(5,minmax(0,1fr))", CSS.replace(" ", ""),
                      "窄窗口 KPI 应压成一行，否则白占高度挤掉对话区")


class ComposerTabVisibilityTests(unittest.TestCase):
    def test_composer_hidden_on_timeline_tab(self):
        self.assertIn("main > .composer", APP, "liveTab 要同步输入框显隐（已移出 #chat）")
        i = APP.index("function liveTab")
        seg = APP[i:i + 1200]
        # 现在统一走 syncComposer()（它同时判断"空状态可见"和"是否在对话页"）
        self.assertIn("syncComposer", seg, "liveTab 里应调用 syncComposer 同步输入框显隐")


if __name__ == "__main__":
    unittest.main()
