# -*- coding: utf-8 -*-
"""改名（CBP → Gantry）回归测试。

这些用例来自一次**多 agent 交叉审核**的实际发现，逐条钉住，防止再犯：
1. 前端类名/动画名只改了一边（`mark.cbp-hit` vs `class="gantry-hit"`）→ 高亮丢样式；
2. 旧会话删不掉（删除时只认新标记 `[GANTRY-AGENT]`，旧的 `[CBP-AGENT]` 被判"不属于本会话"）；
3. `GANTRY_*` 环境变量是死代码（`brand.env` 无人调用，各处仍直读 `CBP_*`）；
4. `localStorage` 只读新键，升级后 token/语言/主题全丢；
5. 界面还有旧名（`Copilot Local Bridge Agent` / `CB` 徽标）；
6. 有 `tr()` 用到的中文 key 在英文表里没有译文（英文界面露中文）。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gantry import brand  # noqa: E402

APP = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "webui" / "style.css").read_text(encoding="utf-8")
BOOT = (ROOT / "webui" / "boot.js").read_text(encoding="utf-8")
HTML = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
I18N = (ROOT / "webui" / "i18n.js").read_text(encoding="utf-8")
DELETE = (ROOT / "gantry" / "copilot_delete.py").read_text(encoding="utf-8")
API = (ROOT / "gantry" / "api.py").read_text(encoding="utf-8")
ROUTES_CORE = (ROOT / "gantry" / "routes_core.py").read_text(encoding="utf-8")


class FrontendParityTests(unittest.TestCase):
    def test_search_highlight_class_matches_css(self):
        """app.js 生成的类名必须在 CSS 里有对应规则（改名时曾经只改了一边）。"""
        self.assertIn("'gantry-hit'", APP, "app.js 应生成 gantry-hit")
        self.assertIn("mark.gantry-hit", CSS, "style.css 缺少 mark.gantry-hit 规则")
        self.assertNotIn("mark.cbp-hit", CSS, "旧的 mark.cbp-hit 残留会与 JS 不一致")

    def test_animation_names_gantry(self):
        for name in ("gantry-flash", "gantry-reveal"):
            self.assertIn("@" + "keyframes " + name, CSS)
            self.assertIn("animation:" + name, CSS.replace("animation: ", "animation:"))

    def test_title_fallback_is_new_brand(self):
        self.assertIn("function applyTitleVersion(", APP)
        # 注意：不要把整份 app.js 放进断言消息（失败时会刷屏），只报命中的行
        bad = [ln for ln in APP.splitlines()
               if "Copilot Local Bridge Agent" in ln or "'CopilotBridge Agent'" in ln]
        self.assertEqual(bad[:3], [], "app.js 仍有旧品牌兜底")

    def test_empty_state_monogram(self):
        m = re.search(r'<div class="nc-head"><span class="big">([^<]+)</span></div>', HTML)
        self.assertIsNotNone(m, "找不到空态徽标")
        self.assertEqual(m.group(1).strip(), "G", "首页徽标应为 G（旧的是 CB）")

    def test_localstorage_migration_and_fallback(self):
        self.assertIn("gantry-migrated", BOOT, "boot.js 缺少一次性迁移标记")
        self.assertIn("'cbp-'", BOOT, "boot.js 应迁移旧的 cbp-* 键")
        self.assertIn("localStorage.getItem('cbp-token')", APP, "apiToken 要回落旧 token 键")


class LegacyMarkerTests(unittest.TestCase):
    def test_prefixes_cover_both_names(self):
        pre = brand.marker_prefixes("task-x")
        self.assertIn("[GANTRY-AGENT][task-x]", pre)
        self.assertIn("[CBP-AGENT][task-x]", pre, "旧标记必须仍被认")

    def test_prefix_regex_matches_old_and_new(self):
        rx = brand.marker_prefix_regex("task-x")
        self.assertTrue(rx.search("[CBP-AGENT][task-x][C000] 旧会话"))
        self.assertTrue(rx.search("[GANTRY-AGENT][task-x][C001] 新会话"))
        self.assertFalse(rx.search("[GANTRY-AGENT][task-y][C000]"))

    def test_delete_uses_candidate_prefixes(self):
        """删除的归属校验必须用候选集，而不是单一新前缀。"""
        self.assertIn("session_prefixes", DELETE)
        self.assertIn("any(p in row_text for p in session_prefixes)", DELETE)
        self.assertIn("marker_prefixes(", DELETE)
        self.assertIn("prefix_re", DELETE, "对账终验要用正则同时匹配两套前缀")


class EnvCompatTests(unittest.TestCase):
    def test_brand_env_prefers_new_name(self):
        import os
        old = os.environ.get("GANTRY_PORT")
        try:
            os.environ["GANTRY_PORT"] = "9999"
            os.environ["CBP_PORT"] = "1234"
            self.assertEqual(brand.env("PORT"), "9999")
            del os.environ["GANTRY_PORT"]
            self.assertEqual(brand.env("PORT"), "1234", "旧名仍应回落生效")
        finally:
            os.environ.pop("GANTRY_PORT", None)
            if old is not None:
                os.environ["GANTRY_PORT"] = old
            os.environ.pop("CBP_PORT", None)

    def test_code_does_not_bypass_brand_env(self):
        for rel in ("gantry/cli.py", "gantry/agentrun.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            # 只抓读取：写入 `os.environ["CBP_PORT"] = x`（给旧名留兼容别名）不算
            bad = re.findall(r'os\.environ\.get\(["\']CBP_[A-Z_]+', src)
            bad += [m for m in re.findall(r'os\.environ\[["\']CBP_[A-Z_]+["\']\](?!\s*=)', src)]
            self.assertEqual(bad, [], f"{rel} 仍直读 CBP_*：{bad}（应走 brand.env）")


class ServerBrandTests(unittest.TestCase):
    def test_root_endpoint_reports_brand(self):
        self.assertIn('"name": _APP_NAME', ROUTES_CORE)
        self.assertIn("from .brand import APP_NAME", ROUTES_CORE)

    def test_startup_banner_has_brand_and_version(self):
        self.assertIn('_APP_NAME} v{_APP_VERSION} 已启动', API)

    def test_lang_header_accepts_legacy(self):
        self.assertIn('_LANG_HEADER = "X-Gantry-Lang"', API)
        self.assertIn('_LANG_HEADER_LEGACY = "X-CBP-Lang"', API)


class I18nCoverageTests(unittest.TestCase):
    def test_every_cjk_tr_key_has_english(self):
        """`tr('中文')` 用到的 key 必须在英文表里有非中文译文（审核实测漏了 19 条）。"""
        keys = set(re.findall(r"tr\(\s*'((?:[^'\\]|\\.)*?)'\s*[,)]", APP))
        keys |= set(re.findall(r'tr\(\s*"((?:[^"\\]|\\.)*?)"\s*[,)]', APP))
        cjk = [k for k in keys if re.search(r"[\u4e00-\u9fff]", k)]
        self.assertGreater(len(cjk), 200, "关键词数量异常，检查正则")
        missing = []
        for k in cjk:
            m = re.search(r'"' + re.escape(k) + r'"\s*:\s*"([^"]*)"', I18N)
            if not m or re.search(r"[\u4e00-\u9fff]", m.group(1)):
                missing.append(k)
        self.assertEqual(missing, [], f"这些 key 缺英文或译文仍是中文：{missing}")


if __name__ == "__main__":
    unittest.main()
