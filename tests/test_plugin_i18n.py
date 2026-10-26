# -*- coding: utf-8 -*-
"""插件自带译文：清单解析、只合并已加载插件、接口可用。"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.plugins import PluginHost, _read_manifest
from gantry.tools import ToolRegistry


def _write(root: Path, name: str, i18n: dict) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps({
        "name": name, "version": "0.1.0", "description": name,
        "gantry": {"entry": "plugin.py", "permissions": ["tools"],
                "i18n": i18n},
    }, ensure_ascii=False), encoding="utf-8")
    (d / "plugin.py").write_text(
        "def setup(ctx):\n    ctx.register_tool('%s_t', lambda: {}, '演示', {})\n" % name,
        encoding="utf-8")
    return d


class PluginI18nTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_pi18n_"))
        self.plug = self.tmp / "plugins"
        self.plug.mkdir()
        self.host = PluginHost(self.tmp, registry=ToolRegistry(self.tmp / "ws", {}))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_manifest_parses_i18n(self):
        _write(self.plug, "p1", {"en": {"定时任务": "Scheduled tasks"}, "tw": {"文件": "檔案"}})
        man = _read_manifest(self.plug / "p1")
        self.assertEqual(man.i18n["en"]["定时任务"], "Scheduled tasks")
        self.assertEqual(man.i18n["tw"]["文件"], "檔案")

    def test_only_loaded_plugins_contribute(self):
        _write(self.plug, "p1", {"en": {"甲": "A"}})
        _write(self.plug, "p2", {"en": {"乙": "B"}})
        self.host.load("p1")
        merged = self.host.i18n()
        self.assertEqual(merged["en"], {"甲": "A"}, "未加载插件的译文不该混进来")
        self.host.load("p2")
        self.assertEqual(self.host.i18n()["en"], {"甲": "A", "乙": "B"})

    def test_ignores_unknown_lang_keys(self):
        _write(self.plug, "p3", {"fr": {"甲": "A"}, "en": {"乙": "B"}})
        self.host.load("p3")
        self.assertEqual(set(self.host.i18n()), {"en", "tw"})

    def test_builtin_plugins_declare_translations(self):
        """内置插件（schedule/mcp）应当自带英文，作为第三方插件的样板。"""
        root = Path(__file__).resolve().parent.parent / "plugins"
        for name in ("schedule", "mcp"):
            man = _read_manifest(root / name)
            self.assertTrue(man.i18n.get("en"), f"{name} 缺少 i18n.en")


if __name__ == "__main__":
    unittest.main()
