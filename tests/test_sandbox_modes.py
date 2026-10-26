# -*- coding: utf-8 -*-
"""沙箱三档 + 计划模式：变更类工具在只读档位/计划模式下必须被拦下。"""
import asyncio
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.tools import ToolError, ToolRegistry


class SandboxModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_sbx_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.saved = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reg(self, **cfg):
        base = {"allow_delete": True, "allow_python": False}
        base.update(cfg)
        reg = ToolRegistry(self.ws, base)
        reg.set_root(self.ws, False)
        return reg

    def test_read_only_blocks_writes(self):
        reg = self._reg(sandbox_mode="read-only")
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(reg.execute("write_text", {"path": "a.txt", "content": "x"}))
        self.assertIn("SANDBOX_READ_ONLY", str(ctx.exception))
        # 只读工具仍然可用
        asyncio.run(reg.execute("write_text_b64", {"path": "keep.txt",
                                                   "content_b64": "aGk="})) if False else None
        self.assertTrue(asyncio.run(reg.execute("list_directory", {"path": "."})) is not None)

    def test_workspace_write_blocks_outside(self):
        reg = self._reg(sandbox_mode="workspace-write")
        asyncio.run(reg.execute("write_text", {"path": "ok.txt", "content": "hi"}))
        self.assertTrue((self.ws / "ok.txt").exists())
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(reg.execute("write_text",
                                    {"path": str(self.outside / "x.txt"), "content": "no"}))
        self.assertTrue("SANDBOX" in str(ctx.exception).upper())

    def test_danger_full_access_allows_outside(self):
        reg = self._reg(sandbox_mode="danger-full-access",
                        workspace_roots=[str(self.ws)])
        target = self.outside / "out.txt"
        asyncio.run(reg.execute("write_text", {"path": str(target), "content": "allowed"}))
        self.assertTrue(target.exists())

    def test_plan_mode_blocks_writes_then_exit_unlocks(self):
        state = {"plan_mode": 1}
        reg = self._reg(plan_mode=True)
        reg.state_saver = lambda **kw: state.update(kw)
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(reg.execute("write_text", {"path": "a.txt", "content": "x"}))
        self.assertIn("SANDBOX_PLAN_MODE", str(ctx.exception))
        # exit_plan_mode 解锁并落库
        res = asyncio.run(reg.execute("exit_plan_mode", {"plan": "1. 改 A\n2. 跑测试"}))
        self.assertTrue(res["ok"])
        self.assertFalse(reg.plan_mode)
        self.assertIn("plan", state)
        self.assertIn("改 A", state["plan"]["plan"])
        asyncio.run(reg.execute("write_text", {"path": "a.txt", "content": "x"}))
        self.assertTrue((self.ws / "a.txt").exists())


if __name__ == "__main__":
    unittest.main()
