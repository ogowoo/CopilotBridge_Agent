# -*- coding: utf-8 -*-
"""MCP 客户端插件：连上 mock server，工具能注册、能调用、错误能透出。"""
import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PLUGIN = BASE / "plugins" / "mcp" / "plugin.py"
MOCK = BASE / "tests" / "fixtures" / "mock_mcp_server.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("cbp_plugin_mcp_unit", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class McpClientTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_plugin()
        self.mgr = self.mod.McpManager()

    def tearDown(self):
        self.mgr = self.mod.McpManager()

    def test_connect_lists_tools(self):
        async def scenario():
            self.mgr.add("mock", {"command": sys.executable, "args": [str(MOCK)]})
            await self.mgr.connect_all()
            self.assertNotIn("mock", self.mgr.errors)
            names = [t["tool"] for t in self.mgr.tool_specs()]
            self.assertEqual(sorted(names), ["add", "boom", "echo"])
            await self.mgr.close_all()

        asyncio.run(scenario())

    def test_call_tool_returns_text(self):
        async def scenario():
            self.mgr.add("mock", {"command": sys.executable, "args": [str(MOCK)]})
            await self.mgr.connect_all()
            res = await self.mgr.call("mock", "echo", {"text": "你好 MCP"})
            self.assertEqual(res["text"], "你好 MCP")
            self.assertFalse(res["isError"])
            res2 = await self.mgr.call("mock", "add", {"a": 2, "b": 3})
            self.assertEqual(res2["text"], "5.0")
            await self.mgr.close_all()

        asyncio.run(scenario())

    def test_tool_error_surfaces(self):
        async def scenario():
            self.mgr.add("mock", {"command": sys.executable, "args": [str(MOCK)]})
            await self.mgr.connect_all()
            with self.assertRaises(self.mod.McpError):
                await self.mgr.call("mock", "boom", {})
            await self.mgr.close_all()

        asyncio.run(scenario())

    def test_registered_name_is_sanitized(self):
        async def scenario():
            self.mgr.add("my-server.v2", {"command": sys.executable, "args": [str(MOCK)]})
            await self.mgr.connect_all()
            names = [t["registered_name"] for t in self.mgr.tool_specs()]
            self.assertIn("mcp__my_server_v2__echo", names)
            await self.mgr.close_all()

        asyncio.run(scenario())

    def test_bad_server_reports_error_without_breaking_others(self):
        async def scenario():
            self.mgr.add("good", {"command": sys.executable, "args": [str(MOCK)]})
            self.mgr.add("bad", {"command": "definitely-not-a-real-binary-xyz"})
            await self.mgr.connect_all()
            self.assertIn("bad", self.mgr.errors)
            self.assertNotIn("good", self.mgr.errors)
            self.assertTrue([t for t in self.mgr.tool_specs() if t["server"] == "good"])
            await self.mgr.close_all()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
