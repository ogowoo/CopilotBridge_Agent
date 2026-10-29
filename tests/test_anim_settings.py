# -*- coding: utf-8 -*-
"""「设置 → 外观 → 动画」：示波器 4 参数 + 输入框呼吸 3 参数。

这是之前 task-mvs4un2x 想做但半途而废的功能（那次只往 settings.js 塞了 4 行读元素的代码，
元素从没加进界面，ui_refs 不变量当场报红）。这次做完整：

  控件（settings.js 模板）→ 参数（animprefs.js，localStorage + 区间钳制）
    → 应用：示波器读参数绘制（scope.js）；呼吸写 CSS 变量（css/composer.css 关键帧已参数化）

数值层的东西（钳制、百分比映射）不好在 Python 里测 JS，这里守住"接线"不脱钩；
真实效果由两个浏览器探针验证（scripts/smoke_anim.py、scripts/smoke_scope.py，
实测：振幅 0.4→1.6 时墨迹跨度 4→14px；线宽 1.8→4 时厚度 3→5px；透明度 1→0.25 时
峰值 alpha 255→64；关呼吸后 :root[data-breathe=off]）。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SETTINGS = (ROOT / "webui" / "js" / "settings.js").read_text(encoding="utf-8")
PREFS = (ROOT / "webui" / "js" / "animprefs.js").read_text(encoding="utf-8")
SCOPE = (ROOT / "webui" / "js" / "scope.js").read_text(encoding="utf-8")
MAIN = (ROOT / "webui" / "js" / "main.js").read_text(encoding="utf-8")
COMPOSER_CSS = (ROOT / "webui" / "css" / "composer.css").read_text(encoding="utf-8")
SETTINGS_CSS = (ROOT / "webui" / "css" / "settings.css").read_text(encoding="utf-8")
I18N = (ROOT / "webui" / "i18n.js").read_text(encoding="utf-8")

RANGE_KEYS = ["scopeAmp", "scopeLine", "scopeOpacity", "scopeSpeed",
              "breatheDur", "breatheStrength"]
SWITCH_KEYS = ["scopeOn", "breatheOn"]


class ControlTests(unittest.TestCase):
    def test_sliders_and_switches_present(self):
        # 开关是字面量行；滑杆由 _animRow(key, …) 生成（模板里只出现参数名）
        for k in SWITCH_KEYS:
            self.assertIn(f'data-anim="{k}"', SETTINGS, f"缺少开关 {k}")
        self.assertIn('data-anim="${key}"', SETTINGS, "滑杆模板没接 data-anim")
        self.assertIn('id="set_${key}"', SETTINGS, "滑杆模板没生成 id")
        for k in RANGE_KEYS:
            self.assertIn(f"_animRow('{k}'", SETTINGS, f"缺少滑杆行 {k}")

    def test_old_heartbeat_switch_kept(self):
        """老用户的 `gantry-teamscope-heartbeat` 开关必须还在（不能因为改版丢设置）。"""
        self.assertIn("setTeamScopeHeartbeat", SETTINGS)
        self.assertIn("gantry-teamscope-heartbeat", PREFS)

    def test_live_preview_and_save(self):
        """拖动即时生效（不用等保存），保存按钮也要把滑杆值落库。"""
        self.assertIn("addEventListener('input'", SETTINGS)
        self.assertIn("_onAnimInput", SETTINGS)
        self.assertIn("applyAnimPrefs();", SETTINGS)
        self.assertRegex(SETTINGS, r"\[('scopeAmp'.*'breatheStrength')\]\.forEach")

    def test_i18n_labels(self):
        for key in ["动画", "示波器振幅", "示波器线宽", "示波器透明度", "示波器速度",
                    "输入框呼吸", "任务运行时输入框边框呼吸", "呼吸时长", "呼吸强度"]:
            self.assertIn(f'"{key}":', I18N, f"i18n.js 缺英文词条: {key}")


class PrefsModuleTests(unittest.TestCase):
    def test_defaults_and_ranges(self):
        for k in RANGE_KEYS + SWITCH_KEYS:
            self.assertIn(k, PREFS, f"animprefs.js 缺参数 {k}")
        self.assertIn("ANIM_RANGES", PREFS)
        self.assertIn("clampNum", PREFS)
        # 默认值 = 调参前的原始观感
        self.assertIn("scopeLine: 1.8", PREFS)
        self.assertIn("scopeAmp: 1.0", PREFS)
        self.assertIn("breatheDur: 1.8", PREFS)
        self.assertIn("breatheStrength: 100", PREFS)

    def test_keys_stable(self):
        for key in ["gantry-teamscope-amp", "gantry-teamscope-line",
                    "gantry-teamscope-opacity", "gantry-teamscope-speed",
                    "gantry-breathe-on", "gantry-breathe-dur", "gantry-breathe-strength"]:
            self.assertIn(key, PREFS, f"缺 localStorage 键 {key}")

    def test_applies_css_vars_and_off_switch(self):
        self.assertIn("--breathe-dur", PREFS)
        self.assertIn("--breathe-hi", PREFS)
        self.assertIn("data-breathe", PREFS)
        self.assertIn("gantry-animprefs", PREFS, "改完要派发事件让 scope 刷新缓存")


class ScopeTests(unittest.TestCase):
    def test_scope_uses_prefs(self):
        self.assertIn("import { readAnimPrefs } from './animprefs.js'", SCOPE)
        for use in ("_pf.scopeAmp", "_pf.scopeLine", "_pf.scopeOpacity", "_pf.scopeSpeed"):
            self.assertIn(use, SCOPE, f"scope.js 没用到 {use}")
        self.assertIn("gantry-animprefs", SCOPE, "缓存要能被设置页的改动失效")

    def test_scope_switch_honoured(self):
        self.assertIn("if(!scopePrefs().scopeOn)return;", SCOPE)


class CssTests(unittest.TestCase):
    def test_keyframes_parameterized(self):
        self.assertIn("var(--breathe-hi,88%)", COMPOSER_CSS)
        self.assertIn("var(--breathe-ring,3px)", COMPOSER_CSS)
        self.assertIn("var(--breathe-glow,18px)", COMPOSER_CSS)
        self.assertIn("composer-breathe var(--breathe-dur,1.8s)", COMPOSER_CSS)
        self.assertIn(':root[data-breathe="off"] .main > .composer.agent-busy{animation:none}',
                      COMPOSER_CSS)

    def test_fallbacks_equal_old_look(self):
        """变量缺失时的默认值必须等于改版前的写死值（不动设置就不该有视觉变化）。"""
        for frag in ("var(--breathe-hi,88%)", "var(--breathe-ring-a,18%)",
                     "var(--breathe-glow-a,22%)", "var(--breathe-dur,1.8s)"):
            self.assertIn(frag, COMPOSER_CSS)

    def test_slider_styles(self):
        self.assertIn('input[type="range"]', SETTINGS_CSS)
        self.assertIn(".animval", SETTINGS_CSS)

    def test_reduced_motion_still_wins(self):
        """系统开了"减少动态效果"时，呼吸与示波器都要停（原有兜底不能被我改坏）。"""
        self.assertIn("@media (prefers-reduced-motion:reduce)", COMPOSER_CSS)
        self.assertIn("prefers-reduced-motion: reduce", SCOPE)


class BootTests(unittest.TestCase):
    def test_boot_applies_prefs_and_binds(self):
        self.assertIn("applyAnimPrefs()", MAIN, "启动就该铺好参数（别等打开设置页）")
        for name in ("applyAnimPrefs", "readAnimPrefs", "setAnimPref", "animPrefLabel"):
            self.assertIn(f"window.{name} =", MAIN, f"main.js 没把 {name} 绑到 window")


class NoStrayIdRefsTest(unittest.TestCase):
    def test_every_anim_control_id_exists_in_template(self):
        """每个参数都要有真实存在的控件 id（ui_refs 不变量查的是"引用了不存在的 id"）。"""
        ids = set(re.findall(r'id="(setBreatheOn|setTeamScopeHeartbeat)"', SETTINGS))
        for want in ("setTeamScopeHeartbeat", "setBreatheOn"):
            self.assertIn(want, ids, f"开关 id 缺失: {want}")
        # 滑杆：模板里 `id="set_${key}"`，且每个参数名都真的传给了 _animRow
        self.assertIn('id="set_${key}"', SETTINGS)
        for k in RANGE_KEYS:
            self.assertIn(f"_animRow('{k}'", SETTINGS, f"滑杆参数没接上: {k}")
        # saveSettings 按 id 逐个读回，键名必须与 _animRow 生成的一致
        for k in RANGE_KEYS:
            self.assertIn(f"'{k}'", SETTINGS, f"saveSettings 没落库 {k}")


if __name__ == "__main__":
    unittest.main()
