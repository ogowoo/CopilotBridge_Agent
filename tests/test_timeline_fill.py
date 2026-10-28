# -*- coding: utf-8 -*-
"""步骤时间线要铺满父卡片，不能被 `.livepane` 的高度上限卡住。

## 真事（用户）
"step timeline 的内容不是直接自适应他的父层大小，我最大化的时候他显示的范围还是很小。"

实测（1714×961）：卡片高 638px,但 `#timeline` 只有 **461px**（= 48vh），而且
`overflow-y:hidden` —— 既没铺满、也不能滚动。原因：`.livepane{max-height:38vh}`
加上几处媒体查询的 `max-height:55/60vh` 把它当"小窗格"限制住了；
而时间线是这一页的**主体内容**，应当 `flex:1` 铺满并在内部滚动。

## 修法
    #timeline.timelinepane{flex:1 1 0%;min-height:0;max-height:none;overflow:auto}
（`#answer`/`#prompt` 两个合并进来的 pre 页同样处理）

实测修后：铺满率 100%（1714/1400）、99%（1024）；时间线底边在视口内。
"""
from gantry.ui_src import ui_css
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
_RAW = ui_css()
CSS = re.sub(r"/\*[\s\S]*?\*/", " ", _RAW)


def _rule(selector: str):
    """取包含该选择器的**最后一条**规则体（层叠里生效的那条）。

    要注意 CSS 里常见 `#answer.livepane,#prompt.livepane{...}` 这种多选择器写法，
    所以按"选择器列表里含目标"来匹配，而不是要求选择器单独成条。
    """
    out = None
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", CSS):
        sels = [x.strip() for x in m.group(1).split(",")]
        if selector in sels:
            out = m.group(2).replace(" ", "")
    return out


class TimelineFillsParentTests(unittest.TestCase):
    def test_timeline_fills_and_scrolls(self):
        body = _rule("#timeline.timelinepane")
        self.assertIsNotNone(body, "缺少 #timeline 的规则")
        self.assertIn("flex:110%", body, "时间线要 flex:1（basis 0%）铺满父卡片剩余高度")
        self.assertIn("min-height:0", body, "flex 子项必须 min-height:0 才允许收缩")
        self.assertIn("max-height:none", body,
                      "必须解除 .livepane 的 max-height（否则最大化后仍只显示 48vh）")
        self.assertIn("overflow:auto", body, "内容超出时要能自己滚动")

    def test_answer_and_prompt_panes_fill_too(self):
        for sel in ("#answer.livepane", "#prompt.livepane"):
            body = _rule(sel)
            self.assertIsNotNone(body, f"缺少 {sel} 的规则")
            self.assertIn("max-height:none", body, f"{sel} 也不该被高度上限卡住")

    def test_card_is_flex_column(self):
        # flex:1 只有在父容器是 flex 列时才生效
        self.assertRegex(CSS, r"#sessionView\s*>\s*\.card:has\(> #chat\)\{[^}]*flex-direction:column",
                         "承载时间线的卡片必须是 flex 列，否则子项 flex:1 无效")


if __name__ == "__main__":
    unittest.main()
