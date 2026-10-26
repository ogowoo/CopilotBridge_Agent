# -*- coding: utf-8 -*-
"""插件安装/升级/卸载：版本比较、备份、zip-slip 防护。"""
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.plugins import PluginHost, PluginError
from gantry.tools import ToolRegistry


def _mk_plugin(root: Path, name: str, version: str, tool: str = "") -> Path:
    d = root / f"src-{name}-{version}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps({
        "name": name, "version": version, "description": f"{name} {version}",
        "gantry": {"entry": "plugin.py", "permissions": ["tools"],
                "contributes": {"tools": [tool or f"{name}_tool"]}},
    }, ensure_ascii=False), encoding="utf-8")
    (d / "plugin.py").write_text(
        "def setup(ctx):\n"
        f"    ctx.register_tool('{tool or name + '_tool'}', lambda: {{'ok': True}},\n"
        f"                      '{name} 工具', {{}})\n", encoding="utf-8")
    return d


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_inst_"))
        self.plugdir = self.tmp / "plugins"
        self.plugdir.mkdir()
        self.registry = ToolRegistry(self.tmp / "ws", {})
        self.host = PluginHost(self.tmp, registry=self.registry)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_install_local_dir_and_load(self):
        src = _mk_plugin(self.tmp, "hello", "0.1.0", tool="hello_tool")
        out = self.host.install(str(src))
        self.assertEqual(out["name"], "hello")
        self.assertEqual(out["version"], "0.1.0")
        self.assertTrue((self.plugdir / "hello" / "plugin.py").is_file())
        self.assertIn("hello_tool", self.registry._tools)

    def test_upgrade_backs_up_old_version(self):
        v1 = _mk_plugin(self.tmp, "up", "0.1.0")
        v2 = _mk_plugin(self.tmp, "up", "0.2.0")
        self.host.install(str(v1))
        out = self.host.install(str(v2))
        self.assertEqual(out["previous"], "0.1.0")
        self.assertEqual(out["version"], "0.2.0")
        self.assertTrue(out["backup"], "覆盖安装应留备份")
        self.assertTrue(Path(out["backup"]).is_dir())
        reg = self.host.registry_view()
        row = next(p for p in reg["plugins"] if p["name"] == "up")
        self.assertEqual(row["version"], "0.2.0")
        self.assertTrue(reg.get("backups"), "注册表应列出备份")
        self.assertIn("up-", reg["backups"][0])

    def test_downgrade_refused_without_force(self):
        v1 = _mk_plugin(self.tmp, "down", "0.1.0")
        v2 = _mk_plugin(self.tmp, "down", "0.9.0")
        self.host.install(str(v2))
        with self.assertRaises(PluginError) as ctx:
            self.host.install(str(v1))
        self.assertIn("降级", str(ctx.exception))
        self.host.install(str(v1), force=True)      # 显式允许

    def test_zip_install(self):
        src = _mk_plugin(self.tmp, "zipped", "0.1.0")
        zpath = self.tmp / "zipped.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            for f in src.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(src))
        out = self.host.install(str(zpath))
        self.assertEqual(out["name"], "zipped")

    def test_zip_slip_rejected(self):
        zpath = self.tmp / "evil.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("../escape.txt", "x")
            zf.writestr("plugin.json", "{}")
        with self.assertRaises(PluginError) as ctx:
            self.host.install(str(zpath))
        self.assertIn("越界", str(ctx.exception))
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_uninstall_removes_dir_and_tool(self):
        src = _mk_plugin(self.tmp, "bye", "0.1.0", tool="bye_tool")
        self.host.install(str(src))
        self.assertIn("bye_tool", self.registry._tools)
        out = self.host.uninstall("bye")
        self.assertTrue(out["removed"])
        self.assertFalse((self.plugdir / "bye").exists())
        self.assertNotIn("bye_tool", self.registry._tools)

    def test_uninstall_unknown(self):
        with self.assertRaises(PluginError):
            self.host.uninstall("nope")

    def test_version_compare(self):
        v = PluginHost._version_tuple
        self.assertGreater(v("0.10.0"), v("0.9.9"))
        self.assertEqual(v("v1.2"), (1, 2, 0))


if __name__ == "__main__":
    unittest.main()
