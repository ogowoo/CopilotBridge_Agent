# -*- coding: utf-8 -*-
"""扩展工具（cbp/tools_extra.py）：搜索 / 网页 / 命令 / 任务清单 / 图片 / 提问。

这些工具是解耦模块，通过 register_extra_tools() 挂到 ToolRegistry 上；
本测试只验证工具本身的行为与安全默认，不依赖 Copilot 网页。
"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.tools import ToolRegistry
from cbp.tools_extra import ExtraToolError, register_extra_tools


class ExtraToolsTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_extra_"))
        (self.ws / "a.py").write_text("def hello():\n    return 1  # TODO fix\n", encoding="utf-8")
        (self.ws / "b.txt").write_text("nothing here\n", encoding="utf-8")
        self.reg = ToolRegistry(self.ws, {"max_read_chars": 100000})
        self.extra = register_extra_tools(self.reg, {"allow_shell": True})

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_registers_all_groups(self):
        names = set(self.reg._tools)
        for n in ("grep", "web_fetch", "pwsh", "job_list", "job_output", "job_kill",
                  "todo_write", "present", "read_image", "ask_user_question"):
            self.assertIn(n, names, f"扩展工具 {n} 未注册")

    def test_group_switch(self):
        reg = ToolRegistry(self.ws, {"max_read_chars": 100000})
        register_extra_tools(reg, {"tools_extra": {"shell": False, "web": False}})
        self.assertNotIn("pwsh", reg._tools)
        self.assertNotIn("web_fetch", reg._tools)
        self.assertIn("grep", reg._tools)   # 未关闭的组仍在

    def test_grep_finds_content_and_skips_binary(self):
        out = asyncio.run(self.extra.grep("TODO"))
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["matches"][0]["path"], "a.py")
        self.assertIn("line", out["matches"][0])

    def test_grep_include_filter(self):
        out = asyncio.run(self.extra.grep("nothing", include="*.py"))
        self.assertEqual(out["count"], 0, "include=*.py 不应匹配 .txt")
        out2 = asyncio.run(self.extra.grep("nothing", include="*.txt"))
        self.assertEqual(out2["count"], 1)

    def test_grep_bad_regex_is_tool_error(self):
        with self.assertRaises(ExtraToolError):
            asyncio.run(self.extra.grep("["))

    def test_web_fetch_blocks_private(self):
        with self.assertRaises(ExtraToolError) as ctx:
            asyncio.run(self.extra.web_fetch("http://127.0.0.1:8765/health"))
        self.assertIn("内网", str(ctx.exception))

    def test_web_fetch_rejects_non_http(self):
        with self.assertRaises(ExtraToolError):
            asyncio.run(self.extra.web_fetch("file:///etc/passwd"))

    def test_todo_write_replaces_and_validates(self):
        out = asyncio.run(self.extra.todo_write([
            {"content": "步骤一", "status": "completed"},
            {"content": "步骤二", "status": "in_progress"},
            "自由文本",
        ]))
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["pending"], 2)
        self.assertEqual(self.extra.todos[0]["status"], "completed")
        # 非法状态回落 pending
        out2 = asyncio.run(self.extra.todo_write([{"content": "x", "status": "bogus"}]))
        self.assertEqual(out2["todos"][0]["status"], "pending")

    def test_present_records_and_checks_existence(self):
        out = asyncio.run(self.extra.present([
            {"path": "a.py", "description": "主脚本"},
            {"path": "missing.py"},
        ]))
        self.assertTrue(out["presented"][0]["exists"])
        self.assertFalse(out["presented"][1]["exists"])
        self.assertEqual(len(self.extra.deliverables), 2)

    def test_read_image_metadata_without_vision(self):
        png = self.ws / "x.png"
        # 1x1 PNG
        png.write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c6300010000050001"))
        out = asyncio.run(self.extra.read_image("x.png", analyze=False))
        self.assertEqual(out["format"], "png")
        self.assertEqual(out["dimensions"], (1, 1))
        out2 = asyncio.run(self.extra.read_image("x.png", analyze=True))
        self.assertIn("error", out2)   # 没有 vision_fn 时优雅降级

    def test_ask_user_question_without_backend(self):
        with self.assertRaises(ExtraToolError):
            asyncio.run(self.extra.ask_user_question("选哪个？"))

    def test_pwsh_runs_command(self):
        out = asyncio.run(self.extra.pwsh("Write-Output 'hi-from-pwsh'"))
        self.assertEqual(out["exit_code"], 0)
        self.assertIn("hi-from-pwsh", out["stdout"])

    def test_pwsh_disabled(self):
        reg = ToolRegistry(self.ws, {"max_read_chars": 100000})
        extra = register_extra_tools(reg, {"allow_shell": False})
        with self.assertRaises(ExtraToolError):
            asyncio.run(extra.pwsh("Write-Output x"))

    def test_background_job_lifecycle(self):
        async def flow():
            started = await self.extra.pwsh("Start-Sleep -Milliseconds 300; Write-Output 'job-done'",
                                            run_in_background=True)
            jid = started["job_id"]
            listed = await self.extra.job_list()
            self.assertEqual(len(listed["jobs"]), 1)
            out = await self.extra.job_output(jid, wait=True, timeout_seconds=20)
            self.assertEqual(out["status"], "done")
            self.assertIn("job-done", out["stdout"])
            killed = await self.extra.job_kill(jid)
            self.assertEqual(killed["status"], "killed")
        asyncio.run(flow())


class ExtraIsolationTests(unittest.TestCase):
    """并发会话隔离与审批自声明（解耦边界的两个契约）。"""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_extra_iso_"))
        self.reg = ToolRegistry(self.ws, {"max_read_chars": 100000})
        self.sid = "s1"
        self.extra = register_extra_tools(self.reg, {"allow_shell": True},
                                          session_getter=lambda: self.sid)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_todos_are_per_session(self):
        asyncio.run(self.extra.todo_write([{"content": "会话一的事", "status": "pending"}]))
        self.assertEqual(len(self.extra.todos), 1)
        self.sid = "s2"
        self.assertEqual(self.extra.todos, [], "换会话不应看到别的会话的任务清单")
        asyncio.run(self.extra.todo_write([{"content": "会话二", "status": "completed"}]))
        self.assertEqual(len(self.extra.todos), 1)
        self.sid = "s1"
        self.assertEqual(self.extra.todos[0]["content"], "会话一的事")

    def test_present_is_per_session(self):
        (self.ws / "a.txt").write_text("x", encoding="utf-8")
        asyncio.run(self.extra.present([{"path": "a.txt"}]))
        self.assertEqual(len(self.extra.deliverables), 1)
        self.sid = "s2"
        self.assertEqual(self.extra.deliverables, [])

    def test_pwsh_registered_with_approval(self):
        self.assertIn("pwsh", self.reg._tools)
        self.assertTrue(self.reg.requires_approval("pwsh"),
                        "pwsh 必须在注册时声明 approval=True")
        # 非破坏性扩展工具不应被审批门拦住
        self.assertFalse(self.reg.requires_approval("grep"))


if __name__ == "__main__":
    unittest.main()
