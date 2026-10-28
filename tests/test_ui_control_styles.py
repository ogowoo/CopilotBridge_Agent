# -*- coding: utf-8 -*-
"""控件样式回归：「强行插入」对话框的两个单选项被撑变形。

根因（真浏览器实测）：全局规则 `input,textarea,select{...width:100%}` 把
`input[type=radio]` 也算进去了 —— 单选框被撑到 **422px**，把后面的标签文字挤成
**0 宽**，中文只能逐字换行，于是整个选项卡高 400px、文字竖排（用户截图）。

所以本文件守两件事：
1. 全局输入框样式**必须排除** radio/checkbox/range/file（纯文本类才铺满）；
2. 选择类控件保持原生尺寸（宽度小、不再被套上文本框边框）。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.ui_src import ui_css  # noqa: E402

CSS = ui_css()          # 样式已拆成 webui/css/*.css，这里拿拼接后的全文


class InputStylingTests(unittest.TestCase):
    def test_global_rule_excludes_choice_inputs(self):
        # 找到那条给文本输入框铺满宽度的规则（选择器可能跨行，用 [^{}] 限定在一条规则内）
        m = re.search(r"([^{}]*textarea[^{}]*)\{([^{}]*width:100%[^{}]*)\}", CSS)
        self.assertIsNotNone(m, "找不到全局输入框规则")
        selector = m.group(1)
        for kind in ("radio", "checkbox"):
            self.assertIn(f":not([type={kind}])", selector,
                          f"全局输入框样式必须排除 {kind}，否则它会被撑成整行宽")

    def test_choice_inputs_keep_native_size(self):
        m = re.search(r"input\[type=radio\][^{]*\{([^}]*)\}", CSS)
        self.assertIsNotNone(m, "缺少 radio/checkbox 的原生尺寸覆盖规则")
        body = m.group(1)
        self.assertIn("width:auto", body)
        self.assertIn("border:0", body, "单选框不该被套上文本框边框")
        self.assertIn("flex:none", body)

    def test_inject_dialog_option_allows_label_to_shrink_wrap(self):
        """`.gmopt span` 必须能撑开：0 宽会让中文逐字换行。"""
        m = re.search(r"\.gmopt span\{([^}]*)\}", CSS)
        self.assertIsNotNone(m)
        self.assertIn("min-width:0", m.group(1))
        # 单选框不被撑宽 + span 可伸展 => 文字才排得开
        self.assertIn("flex:none", re.search(r"\.gmopt input\{([^}]*)\}", CSS).group(1))

    def test_dialog_markup_has_two_modes(self):
        html = (Path(__file__).resolve().parent.parent / "webui" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('name="gmMode"'), 2, "插入方式应有两个单选项")
        self.assertIn('value="step"', html)
        self.assertIn('value="turn"', html)


if __name__ == "__main__":
    unittest.main()
