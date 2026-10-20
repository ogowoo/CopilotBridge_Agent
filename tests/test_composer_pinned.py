# -*- coding: utf-8 -*-
"""输入框必须常驻可见（窄/矮窗口），且不能压住消息。

用户要求（选项 1）：把输入框钉在可视区底部，滚动内容时输入框不动。

## 结构与难点（实测）
- `.card > #chat > #chatlog + .composer`：composer 在 #chat 内部本来就是
  `flex:none`（钉在面板底部），问题在**整张对话卡落到折叠线以下**；
- 800×545 时≤860px 的响应式把侧栏**堆到主区上方**（吃掉 265px），`.main` 只剩
  280px，对话卡（含 89px 输入框）必然在折叠线以下 → 只靠 flex/sticky 补不回来
  （卡片比可视区还高，sticky 也拉不上来）。

## 做法
窄(≤860px)或矮(≤660px)窗口把 `.composer` **固定到视口底部**，并给对话区/会话区
留出等量底部内边距（否则最后一条消息会被压住——实测 104px 不够，170px 才干净）。

## 踩过的两个坑（本文件守着）
1. 不能在矮窗口媒体查询里写 `#chat.livepane{max-height:none}` —— 那会让对话区
   不再内部滚动，消息直接顶到固定输入框下面（实测重叠 900px）；
2. 不能对所有屏宽都设 `html,body{overflow:hidden}` —— ≤860px 本来就是"自然文档流 +
   页面滚动"的设计，禁掉会把窄屏内容永久藏住。宽屏禁滚、窄屏放开。
"""
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
_RAW = (BASE / "webui" / "style.css").read_text(encoding="utf-8")
CSS = re.sub(r"/\*[\s\S]*?\*/", " ", _RAW)


def _media_block(*keywords: str) -> str:
    """取出含指定关键字的 @media 块的正文（粗略但够用）。"""
    out = []
    for m in re.finditer(r"@media[^{]*\{", CSS):
        start = m.start()
        depth, i = 0, m.end() - 1
        while i < len(CSS):
            if CSS[i] == "{":
                depth += 1
            elif CSS[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        block = CSS[start:i + 1]
        if all(k in block for k in keywords):
            out.append(block)
    return "\n".join(out)


class ComposerPinnedTests(unittest.TestCase):
    def test_composer_fixed_on_narrow_or_short(self):
        block = _media_block("max-width: 860px", "max-height: 660px", "#chat .composer")
        self.assertTrue(block, "缺少窄/矮窗口的输入框固定规则")
        self.assertIn("position:fixed", block.replace(" ", ""),
                      "窄/矮窗口里输入框要固定在视口底部，否则滚内容时够不到")

    def test_bottom_padding_reserves_space(self):
        block = _media_block("max-width: 860px", "max-height: 660px", "#chat .composer")
        m = re.search(r"#chatlog\{[^}]*padding-bottom:\s*(\d+)px", block)
        self.assertIsNotNone(m, "对话区必须留出底部内边距，否则固定输入框压住最后一条消息")
        self.assertGreaterEqual(int(m.group(1)), 140,
                                "实测 104px 不够（会压住消息），至少要 140px")

    def test_wide_screen_still_forbids_page_scroll(self):
        m = re.search(r"html,body\{([^}]*)\}", CSS)
        self.assertIn("overflow:hidden", m.group(1).replace(" ", ""),
                      "宽屏整屏布局必须禁止页面滚动（否则顶部横幅会把整页撑高 28px）")

    def test_narrow_screen_restores_page_scroll(self):
        block = _media_block("max-width:860px", "overflow:auto")
        self.assertTrue(block, "≤860px 是自然文档流设计，必须把页面滚动放回来")

    def test_chat_keeps_internal_scroll(self):
        # 不许出现"矮窗口把 #chat max-height 改成 none"这种写法（实测导致重叠）
        self.assertNotIn("max-height:none", CSS.replace(" ", ""),
                         "#chat 不能取消 max-height：对话区会失去内部滚动、消息顶到输入框下面")


if __name__ == "__main__":
    unittest.main()
