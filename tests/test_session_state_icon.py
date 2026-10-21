# -*- coding: utf-8 -*-
"""会话行的状态用 SVG 图标表示，不再用文字块。

用户："侧边会话栏里的会话状态标识直接用 SVG 图标来显示，现在的 completed 这种太占位置了。"

## 改法
- 原来一行有**两处**状态指示：左侧 8px 圆点（只有颜色）+ 右侧 `.stag` 文字块
  （中文两字还行，英文 "completed" 很宽，把标题挤没了）。现在合并成**一个**
  14×14 描边图标：
  | 状态 | 形状 | 颜色 |
  |---|---|---|
  | running | 虚线圆弧（旋转） | 绿 |
  | completed | 圆圈 + 勾 | 蓝 |
  | failed | 圆圈 + ✕ | 红 |
  | paused | 圆圈 + 两竖 | 琥珀 |
  | waiting | 圆圈 + 指针 | 琥珀 |
  | ready | 空心圆 | 灰 |
  | cancelled / expired | 圆圈 + 斜杠 | 灰 |
  | archived | 圆圈 + 方块 | 灰 |
- 完整文案放 `title`/`aria-label`（悬停可见、读屏可读），不占视觉宽度。
- 尊重 `prefers-reduced-motion`：关掉旋转动画。
"""
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
APP = (BASE / "webui" / "app.js").read_text(encoding="utf-8")
_RAW = (BASE / "webui" / "style.css").read_text(encoding="utf-8")
CSS = re.sub(r"/\*[\s\S]*?\*/", " ", _RAW)

STATES = ("running", "completed", "failed", "paused", "waiting",
          "ready", "cancelled", "expired", "archived")


class SessionStateIconTests(unittest.TestCase):
    def test_every_state_has_an_icon(self):
        block = APP[APP.index("const STATE_ICON="):]
        block = block[:block.index("};")]
        for st in STATES:
            self.assertRegex(block, rf"\b{st}\s*:", f"状态 {st} 缺少图标定义")

    def test_row_uses_icon_not_text_pill(self):
        i = APP.index("e.innerHTML=`<span class=\"sstat")
        row = APP[i:i + 900]
        self.assertIn("stateIcon(s.state)", row, "会话行要用 SVG 图标")
        # 文案现在走 stateLabel(s)：父会话已结束但子代理仍在跑时要说清楚
        self.assertIn("stateLabel(s)", row, "图标要带 title/aria-label 文案")
        self.assertNotIn('class="stag"', row, "不再使用文字块")

    def test_old_dot_removed_from_row(self):
        i = APP.index("e.innerHTML=`<span class=\"sstat")
        row = APP[i:i + 400]
        self.assertNotIn('class="dot ', row,
                         "左侧小圆点与图标重复，应去掉（只留一个指示位）")

    def test_icon_is_small_and_accessible(self):
        i = APP.index("function stateIcon(")
        seg = APP[i:i + 500]
        self.assertIn('width="14"', seg)
        self.assertIn('aria-hidden="true"', seg, "装饰性 svg 要对读屏隐藏，文案交给外层")

    def test_css_colors_and_motion(self):
        for cls in ("run", "warn", "bad", "done"):
            self.assertRegex(CSS, rf"\.sess \.sstat\.{cls}\{{[^}}]*color:",
                             f".sstat.{cls} 需要颜色规则")
        self.assertIn(".sstat svg .spin", CSS)
        self.assertIn("prefers-reduced-motion", CSS,
                      "旋转动画要尊重 reduced-motion")


if __name__ == "__main__":
    unittest.main()
