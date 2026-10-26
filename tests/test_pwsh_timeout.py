# -*- coding: utf-8 -*-
"""单测：pwsh 超时要杀**进程树**（旧实现只杀直接子进程，孙进程持管道导致永不返回）。"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.tools import ToolError, ToolRegistry
from gantry.tools_extra import register_extra_tools


class PwshTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_pwsh_"))
        self.reg = ToolRegistry(self.tmp, {"allow_shell": True, "approval_policy": "never",
                                           "shell_timeout_seconds": 2})
        self.extra = register_extra_tools(self.reg, {"allow_shell": True,
                                                     "shell_timeout_seconds": 2})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_timeout_raises_with_hint(self):
        """超时必须**返回**（旧实现会挂死），并给出换策略提示。"""
        async def run():
            return await self.extra.pwsh(
                "Start-Sleep -Seconds 30", timeout_seconds=1)

        with self.assertRaises(Exception) as ctx:
            asyncio.run(run())
        msg = str(ctx.exception)
        self.assertIn("超时", msg)
        self.assertIn("run_in_background", msg, "超时提示要给出可行替代策略")

    def test_timeout_does_not_hang_with_child_process(self):
        """孙进程持有 stdout 时也必须返回：这是旧实现挂 8 分钟的真实场景。"""
        async def run():
            return await self.extra.pwsh(
                "powershell -NoProfile -Command \"Start-Sleep -Seconds 30\"",
                timeout_seconds=1)

        with self.assertRaises(Exception):
            asyncio.run(run())
        # 只要没卡住到这里即算通过（用例本身有 60s 超时保护）

    def test_kill_tree_helper_exists(self):
        self.assertTrue(callable(getattr(self.extra, "_kill_tree", None)))

    def test_normal_command_still_works(self):
        async def run():
            return await self.extra.pwsh("Write-Output ok", timeout_seconds=30)

        out = asyncio.run(run())
        self.assertIn("ok", out["stdout"])
        self.assertEqual(out["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
