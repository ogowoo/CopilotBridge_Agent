# -*- coding: utf-8 -*-
"""设置页：配置白名单接口 + 前端入口/弹窗的单测。"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

API = (ROOT / "cbp" / "api.py").read_text(encoding="utf-8")
HTML = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
I18N = (ROOT / "webui" / "i18n.js").read_text(encoding="utf-8")


class ConfigEndpointTests(unittest.TestCase):
    def test_routes_exist(self):
        self.assertIn('@app.get("/v1/config")', API)
        self.assertIn('@app.post("/v1/config")', API)

    def test_schema_is_whitelist(self):
        """只允许改白名单里的键；未知键必须被拒绝。"""
        self.assertIn("CONFIG_SCHEMA", API)
        self.assertIn("不支持修改这些键", API)
        # 敏感项不能出现在可写白名单里
        for banned in ("api_token", "csrf_allowed_origins", "workspace"):
            self.assertNotIn(f'"{banned}": {{"kind"', API,
                             f"{banned} 不应可写")
        self.assertIn('"api_token": "已设置" if b.CFG.get("api_token") else "未设置"',
                      API, "api_token 只能只读展示且打码")

    def test_int_bounds_are_validated(self):
        self.assertIn("不能小于", API)
        self.assertIn("不能大于", API)

    def test_persist_writes_config_json(self):
        self.assertIn('cfg_path = b.BASE / "config.json"', API)
        self.assertIn("persisted", API)

    def test_todo_nudge_every_is_configurable(self):
        bridge = ((ROOT / "cbp" / "bridge.py").read_text(encoding="utf-8")
                  + (ROOT / "cbp" / "agentrun.py").read_text(encoding="utf-8"))
        self.assertIn("_todo_nudge_every", bridge)
        self.assertIn('AGENT_CFG.get("todo_nudge_every"', bridge)


class SettingsDialogTests(unittest.TestCase):
    def test_gear_button_and_dialog(self):
        self.assertIn('id="settingsBtn"', HTML, "侧栏要有齿轮入口")
        self.assertIn('data-act="openSettings"', HTML)
        self.assertIn('id="settingsMask"', HTML)
        self.assertIn('id="settingsBody"', HTML)
        self.assertIn('data-act="saveSettings"', HTML)

    def test_low_frequency_entries_moved_out_of_more_menu(self):
        """插件管理 / 重启 / 终止服务 从「More」搬到设置页。"""
        i = HTML.index('id="moreMenu"')
        menu = HTML[i:HTML.index("</div></div>", i)]
        for act in ("openPlugins", "restartService", "shutdownService"):
            self.assertNotIn(f'data-act="{act}"', menu, f"{act} 应已移出 More 菜单")

    def test_settings_js_wires_api(self):
        for needle in ("function openSettings(", "function renderSettings(",
                       "function saveSettings(", "/v1/config", "data-cfg"):
            self.assertIn(needle, APP, f"缺少 {needle}")

    def test_i18n_keys_for_settings(self):
        block = I18N[:I18N.index("const I18N")+200000] if "const I18N" in I18N else I18N
        for key in ("设置", "外观", "执行", "浏览器", "数据", "入口", "危险区",
                    "主题", "语言", "保存"):
            self.assertIn(json.dumps(key, ensure_ascii=False), block,
                          f"缺少词条：{key}")


if __name__ == "__main__":
    unittest.main()
