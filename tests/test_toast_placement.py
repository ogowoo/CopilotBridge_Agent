# -*- coding: utf-8 -*-
"""提示信息（toast）统一位置：顶部居中一列，不再散落在左下角。"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HTML = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "webui" / "style.css").read_text(encoding="utf-8")


def blocks(selector: str) -> list[str]:
    out = []
    for m in re.finditer(re.escape(selector), CSS):
        end = CSS.find("}", m.start())
        out.append(CSS[m.start():end + 1])
    return out


def toast_rule() -> str:
    for b in blocks(".toasts{"):
        if "position:fixed" in b:
            return b
    return ""


class ToastPlacementTests(unittest.TestCase):
    def test_single_position_top_center(self):
        block = toast_rule()
        self.assertTrue(block, "找不到 .toasts 定位规则")
        self.assertIn("top:", block)
        self.assertIn("left:50%", block)
        self.assertIn("translateX(-50%)", block)
        self.assertIn("bottom:auto", block, "不能还贴底")
        self.assertNotIn("bottom:28px", block, "旧左下定位必须清掉")
        self.assertIn("flex-direction:column", block, "多条向下堆叠")

    def test_avoids_top_banners(self):
        block = toast_rule()
        self.assertIn("--boot-h", block, "要让开浏览器提示细条")
        self.assertIn("--banner-h", block, "要让开连接异常横幅")

    def test_no_bottom_reserve(self):
        """提示跑到顶部后，底部不能再为它留白。"""
        self.assertIn("rs.setProperty('--toast-h', '0px')", JS)
        self.assertFalse([b for b in blocks(".main{") if "var(--toast-h" in b],
                         "底部内边距不该再叠加提示高度")

    def test_aria_live(self):
        self.assertIn('id="toasts" role="status" aria-live="polite"', HTML)


if __name__ == "__main__":
    unittest.main()
