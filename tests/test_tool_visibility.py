"""P0-1 工具按任务裁剪：core 常在、专业组按关键词展开、隐藏≠禁用。

注意：哪些工具"存在"由配置开关与注册来源决定（Office 默认注册、Outlook 需
allow_outlook、copilot_* 由桥接层 register 注入），断言只针对真实存在的工具。
"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.agent import AgentRunner
from gantry.tools import TOOL_GROUPS, ToolRegistry, tool_group


class ToolVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_toolvis_"))

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _runner(self, task, config=None):
        cfg = dict(config or {})
        tools = ToolRegistry(self.ws, cfg)
        runner = AgentRunner(tools, lambda *a, **k: None, {**cfg, "max_total_steps": 3},
                             state_getter=lambda: {"state": "ready"},
                             state_saver=lambda **k: None, step_saver=lambda s: None,
                             file_event_saver=lambda e: None)
        runner._task_text = task
        return runner, tools

    def _visible(self, task, config=None):
        runner, tools = self._runner(task, config)
        return {x["name"] for x in tools.descriptions(only=runner._active_tool_names())}

    def test_core_always_present(self):
        """core 组始终保留（技能/记忆类工具是否注册取决于环境，不在此断言）。"""
        names = self._visible("随便聊两句")
        for n in ("read_text", "write_text_b64", "replace_text", "validate_python",
                  "run_unittest", "tool_list"):
            self.assertIn(n, names)

    def test_office_expands_on_keyword(self):
        self.assertIn("read_excel", self._visible("帮我整理一下这个表格"))
        self.assertNotIn("read_excel", self._visible("写一个 python 函数"),
                         "无关任务不应注入 Office 工具")

    def test_outlook_group_needs_switch_and_keyword(self):
        cfg = {"allow_outlook": True}
        self.assertIn("outlook_unread", self._visible("看看我的未读邮件", cfg))
        self.assertNotIn("outlook_unread", self._visible("写一个 python 函数", cfg))

    def test_bridge_registered_tool_follows_group(self):
        """copilot_* 由桥接层注册：关键词命中才列出，但注册后执行不受影响。"""
        _, tools = self._runner("随便")
        async def fake_attach(path, question=""):
            return {"ok": True}
        tools.register("copilot_attach", fake_attach, "上传文件给 Copilot", {"path": "路径"})
        r1, _ = self._runner("随便")
        self.assertNotIn("copilot_attach", {x["name"] for x in tools.descriptions(only=r1._active_tool_names())})
        r2, _ = self._runner("把这张截图附件给 copilot 看看")
        self.assertIn("copilot_attach", {x["name"] for x in tools.descriptions(only=r2._active_tool_names())})
        self.assertTrue(asyncio.run(tools.execute("copilot_attach", {"path": "x.png"}))["ok"])

    def test_visibility_all_disables_trimming(self):
        runner, tools = self._runner("随便", {"tool_visibility": "all"})
        self.assertIsNone(runner._active_tool_names())
        self.assertEqual(len(tools.descriptions(only=None)), len(tools._descriptions_all()))

    def test_trimming_reduces_tokens_and_keeps_execution(self):
        trimmed = self._visible("写一个 python 函数")
        full = {x["name"] for x in self._runner("写一个 python 函数")[1]._descriptions_all()}
        self.assertLess(len(trimmed), len(full), "裁剪应减少注入条目")
        out = asyncio.run(self._runner("写一个 python 函数")[1].execute("list_directory", {"path": "."}))
        self.assertIn("items", out)

    def test_tool_list_reports_groups(self):
        _, tools = self._runner("随便")
        info = asyncio.run(tools.execute("tool_list", {}))
        self.assertGreaterEqual(info["total"], 20)
        self.assertIn("core", info["groups"])
        self.assertEqual(tool_group("read_excel"), "office")
        self.assertEqual(tool_group("some_external_tool"), "core")
        self.assertIn("tool_list", info["groups"]["core"])


if __name__ == "__main__":
    unittest.main()
