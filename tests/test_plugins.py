# -*- coding: utf-8 -*-
"""插件系统（cbp/plugins.py）：清单解析、权限、加载/卸载、启停、路由分发。

与 DSH 插件语义兼容：同形清单（package.json 的 `cbp` 字段，对齐 DSH 的 `dsh`）、
同一套贡献点（tools/providers/api/events）、同样的加载→apply→卸载生命周期。
"""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.plugins import PluginError, PluginHost
from cbp.tools import ToolRegistry


def _write_plugin(root: Path, name: str, manifest: dict, code: str) -> Path:
    d = root / "plugins" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (d / "plugin.py").write_text(code, encoding="utf-8")
    return d


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_plugin_"))
        (self.tmp / "data").mkdir(parents=True, exist_ok=True)
        self.reg = ToolRegistry(self.tmp / "ws", {"max_read_chars": 1000})
        self.providers = {}
        self.host = PluginHost(self.tmp, registry=self.reg, providers=self.providers,
                               log_fn=lambda m: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_discover_and_load_registers_tool(self):
        _write_plugin(self.tmp, "demo", {
            "name": "demo", "cbp": {"entry": "plugin.py", "permissions": ["tools"],
                                     "contributes": {"tools": ["demo_tool"]}},
        }, "def setup(ctx):\n    ctx.register_tool('demo_tool', lambda x: x, '演示工具', {})\n")
        assert self.host.list()[0]["name"] == "demo"
        rec = self.host.load("demo")
        self.assertEqual(rec["status"], "loaded")
        self.assertIn("demo_tool", self.reg._tools)
        self.host.disable("demo")
        self.assertNotIn("demo_tool", self.reg._tools, "禁用应摘掉工具")

    def test_permission_is_enforced(self):
        _write_plugin(self.tmp, "bad", {
            "name": "bad", "cbp": {"permissions": [], "contributes": {}},
        }, "def setup(ctx):\n    ctx.register_tool('x', lambda: 1, 'x', {})\n")
        with self.assertRaises(PluginError):
            self.host.load("bad")

    def test_unknown_permission_rejected(self):
        _write_plugin(self.tmp, "weird", {
            "name": "weird", "cbp": {"permissions": ["root-shell"]},
        }, "def setup(ctx): pass\n")
        # 发现阶段：坏清单被跳过（不能因为一个坏插件拖垮整个服务）
        self.assertEqual(self.host.discover(), [])
        self.assertNotIn("weird", [p["name"] for p in self.host.list()])
        # 显式加载时明确报错
        with self.assertRaises(PluginError):
            self.host.load("weird")

    def test_unknown_plugin_does_not_pollute_state(self):
        """拼错的插件名不能往 plugins.json 里写状态（否则列表出现幽灵插件）。"""
        before = self.host._state.copy()
        with self.assertRaises(Exception):
            self.host.enable("no-such-plugin")
        with self.assertRaises(Exception):
            self.host.disable("no-such-plugin")
        self.assertNotIn("no-such-plugin", self.host._state)
        self.assertEqual(before.keys(), self.host._state.keys())

    def test_enable_disable_persisted(self):
        _write_plugin(self.tmp, "p1", {
            "name": "p1", "cbp": {"permissions": ["tools"], "contributes": {"tools": ["t1"]}},
        }, "def setup(ctx):\n    ctx.register_tool('t1', lambda: 1, 't', {})\n")
        self.host.load("p1")
        self.host.disable("p1")
        state = json.loads((self.tmp / "data" / "plugins.json").read_text(encoding="utf-8"))
        self.assertFalse(state["p1"]["enabled"])
        self.host.enable("p1")
        self.assertIn("t1", self.reg._tools)
        state = json.loads((self.tmp / "data" / "plugins.json").read_text(encoding="utf-8"))
        self.assertTrue(state["p1"]["enabled"])

    def test_api_route_dispatch(self):
        _write_plugin(self.tmp, "apiplug", {
            "name": "apiplug", "cbp": {"permissions": ["api"],
                                        "contributes": {"api": ["GET api/ping"]}},
        }, "def setup(ctx):\n    ctx.register_api('GET','api/ping', lambda request: {'pong': True})\n")
        self.host.load("apiplug")

        class Req:
            query_params = {}

        out = asyncio.run(self.host.dispatch_api("apiplug", "GET", "ping", Req()))
        self.assertEqual(out, {"pong": True})
        # 两种注册写法都认
        out2 = asyncio.run(self.host.dispatch_api("apiplug", "GET", "api/ping", Req()))
        self.assertEqual(out2, {"pong": True})

    def test_api_route_zero_arg_handler(self):
        """handler 可以不带参数（按签名调用，而不是靠捕获 TypeError 试错）。"""
        _write_plugin(self.tmp, "noarg", {
            "name": "noarg", "cbp": {"permissions": ["api"],
                                     "contributes": {"api": ["GET api/now"]}},
        }, "def setup(ctx):\n    ctx.register_api('GET','api/now', lambda: {'ok': 1})\n")
        self.host.load("noarg")

        class Req:
            query_params = {}

        out = asyncio.run(self.host.dispatch_api("noarg", "GET", "now", Req()))
        self.assertEqual(out, {"ok": 1})

    def test_api_handler_typeerror_is_not_retried(self):
        """handler 内部的 TypeError 必须冒出来，不能被误当成"参数不匹配"重跑一次。"""
        _write_plugin(self.tmp, "boom", {
            "name": "boom", "cbp": {"permissions": ["api"],
                                    "contributes": {"api": ["GET api/boom"]}},
        }, "def bad(request):\n    return 1 + 'x'\n"
           "def setup(ctx):\n    ctx.register_api('GET','api/boom', bad)\n")
        self.host.load("boom")

        class Req:
            query_params = {}

        with self.assertRaises(TypeError):
            asyncio.run(self.host.dispatch_api("boom", "GET", "boom", Req()))

    def test_events_and_state(self):
        seen = []
        _write_plugin(self.tmp, "ev", {
            "name": "ev", "cbp": {"permissions": ["events", "storage"]},
        }, "def setup(ctx):\n"
           "    ctx.on('step_done', lambda **kw: ctx.save_state(last=kw.get('tool')))\n")
        self.host.load("ev")
        self.host.emit("step_done", tool="read_text")
        self.assertEqual(self.host.state_for("ev").get("last"), "read_text")

    def test_package_json_with_cbp_field(self):
        d = self.tmp / "plugins" / "pkg"
        d.mkdir(parents=True)
        (d / "package.json").write_text(json.dumps({
            "name": "pkg", "version": "1.2.3",
            "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},
            "cbp": {"entry": "plugin.py", "permissions": ["tools"]},
        }), encoding="utf-8")
        (d / "plugin.py").write_text("def setup(ctx):\n    pass\n", encoding="utf-8")
        m = self.host.discover()[0]
        self.assertEqual(m.name, "pkg")
        self.assertEqual(m.version, "1.2.3")
        self.assertEqual(m.dsh, {"bundle": {"patch": "./cordis.patch.yml"}})


if __name__ == "__main__":
    unittest.main()
