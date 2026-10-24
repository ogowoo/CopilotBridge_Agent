# -*- coding: utf-8 -*-
"""WebUI 版本信息与侧栏底栏（DSH 式：⚙ 设置 + 版本号固定在侧栏底部）。"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HTML = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "webui" / "style.css").read_text(encoding="utf-8")


def css_block(selector: str) -> str:
    i = CSS.find(selector)
    return "" if i < 0 else CSS[i:CSS.find("}", i) + 1]


class RailFooterTests(unittest.TestCase):
    def test_footer_inside_rail(self):
        """设置 + 版本号在侧栏底部的固定行里（不是浮在页面左下角）。"""
        aside = HTML[HTML.find('<aside class="rail">'):HTML.find("</aside>")]
        self.assertIn('class="railfoot"', aside)
        self.assertIn('id="settingsBtn"', aside)
        self.assertIn('id="appVersion"', aside)

    def test_list_scrolls_above_footer(self):
        """footer 是 flex:none 的最后一个子项 → 会话列表不可能滚到它下面。"""
        rail = css_block(".rail{")
        self.assertIn("flex-direction:column", rail)
        self.assertIn("#sessions{flex:1 1 auto;min-height:0;overflow:auto}", CSS)
        footer = css_block(".railfoot{")
        self.assertIn("flex:none", footer)

    def test_divider_fades_at_both_ends(self):
        """那条分隔线：中间实、两端渐隐。"""
        self.assertIn(".railfoot::before", CSS)
        i = CSS.find(".railfoot::before")
        block = CSS[i:CSS.find("}", i) + 1]
        self.assertIn("linear-gradient(90deg", block)
        self.assertIn("transparent 0%", block)
        self.assertIn("transparent 100%", block)

    def test_version_plain_text(self):
        ver = css_block(".btmver{")
        self.assertIn("font-size:11.5px", ver)
        self.assertIn("color:var(--faint)", ver)
        self.assertNotIn("border-radius:999px", ver, "不是胶囊")

    def test_fetches_version_from_root(self):
        self.assertIn("async function loadVersion(", JS)
        self.assertIn("fetch('/',{cache:'no-store'})", JS)
        self.assertIn("loadVersion();", JS)

    def test_narrow_fallback_gear(self):
        """窄屏抽屉收起时侧栏整体隐藏 → 保留一个固定的齿轮入口。"""
        self.assertIn('@media(max-width:860px){\n  :root:not([data-rail="open"]) .railfoot{position:fixed', CSS)


class SettingsI18nTests(unittest.TestCase):
    def test_all_schema_labels_translated(self):
        from cbp.api import CONFIG_SCHEMA
        i18n = (ROOT / "webui" / "i18n.js").read_text(encoding="utf-8")
        missing = [spec["label"] for spec in CONFIG_SCHEMA.values()
                   if spec.get("label") and json.dumps(spec["label"], ensure_ascii=False) not in i18n]
        self.assertEqual(missing, [], f"这些设置标签没有英文词条: {missing}")

    def test_coverage_scan_expands_all_panes(self):
        src = (ROOT / "scripts" / "i18n_coverage.py").read_text(encoding="utf-8")
        self.assertIn("data-pane", src, "审计要把隐藏分组也展开再扫")


if __name__ == "__main__":
    unittest.main()
