# -*- coding: utf-8 -*-
"""会话工作区必须真正生效（用户："工作区的选择好像不生效，看我截图"）。

截图里会话头部写的工作区是 `...\\wgime-py-pure\\plugins`，Copilot 却回答
"当前工作目录是 `...\\workspace`"。

## 根因
多窗口改造后每个任务拿的是**克隆**的 `ToolRegistry`，`run_agent` 调
`T.set_root(会话工作区)` 改的是克隆；但 `_tools` 里存的是 `self.read_text` 这类
**bound method**，浅拷贝后仍指向原始注册表 → 工具执行用的是全局 root。
扩展工具更直接：单个全局 `ExtraTools` 实例持有 `self.reg = AGENT_TOOLS`。

## 修法
- `ToolRegistry.clone()` 把 `_tools` 里 `__self__ is self` 的函数重新绑定到克隆；
- 扩展工具用 `_active_reg()`（优先当前任务的 `_TOOLS_CTX`），不再写死 `self.reg`；
- 快照钩子/子代理 root 也改用任务级注册表。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from gantry.ui_src import ui_js

from gantry.tools import ToolRegistry

APP = ui_js()
BRIDGE = (BASE / "gantry" / "bridge.py").read_text(encoding="utf-8")
SUBAGENTS = (BASE / "gantry" / "subagents.py").read_text(encoding="utf-8")
EXTRA = (BASE / "gantry" / "tools_extra.py").read_text(encoding="utf-8")


class WindowIntegrationTests(unittest.TestCase):
    """走 `_window` 的真实路径：run_agent 就是在这里 set_root 后调用工具的。"""

    def test_window_clone_applies_workspace_to_tools(self):
        import gantry.bridge as b

        tmp = Path(tempfile.mkdtemp(prefix="cbp_win_ws_"))
        ws = tmp / "chosen"
        ws.mkdir()
        global_root = Path(b.AGENT_TOOLS.root)

        async def main():
            async with b._window("t-ws", need_page=False) as (_win, T):
                T.set_root(ws, False)
                await b.task_tools().write_text("deep/a.txt", "hello")

        asyncio.run(main())
        self.assertTrue((ws / "deep" / "a.txt").is_file(),
                        "工具没写进会话工作区（修前会写进全局 root）")
        self.assertFalse((global_root / "deep" / "a.txt").exists())
        self.assertEqual(Path(b.AGENT_TOOLS.root), global_root, "全局 root 不能被任务改掉")

    def test_extra_tools_in_window_follow_workspace(self):
        import gantry.bridge as b

        tmp = Path(tempfile.mkdtemp(prefix="cbp_win_extra_"))
        ws = tmp / "chosen"
        ws.mkdir()
        seen = {}

        async def main():
            async with b._window("t-ws2", need_page=False) as (_win, T):
                T.set_root(ws, False)
                seen["root"] = Path(b.TOOLS_EXTRA._active_reg().root)

        asyncio.run(main())
        self.assertEqual(seen["root"], ws.resolve(), "扩展工具没跟随任务工作区")


class CloneRebindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_ws_"))
        self.ws1 = self.tmp / "one"
        self.ws2 = self.tmp / "two"
        self.ws1.mkdir()
        self.ws2.mkdir()
        self.reg = ToolRegistry(self.ws1, {})

    def test_tools_rebound_to_clone(self):
        clone = self.reg.clone()
        for name in ("read_text", "write_text", "list_directory", "run_unittest"):
            fn = clone._tools.get(name)
            self.assertIsNotNone(fn, f"缺工具 {name}")
            self.assertIs(fn.__self__, clone, f"{name} 仍绑定在原始注册表上")
        # 原注册表不受影响
        self.assertIs(self.reg._tools["read_text"].__self__, self.reg)

    def test_clone_root_controls_writes(self):
        clone = self.reg.clone()
        clone.set_root(self.ws2, False)
        asyncio.run(clone.write_text("a.txt", "hello"))
        self.assertTrue((self.ws2 / "a.txt").is_file(), "写到了错误的工作区（修前会写进 ws1）")
        self.assertFalse((self.ws1 / "a.txt").exists())

    def test_clone_sandbox_path_follows_root(self):
        clone = self.reg.clone()
        clone.set_root(self.ws2, False)
        self.assertEqual(clone.sandbox_path("x/y.txt").parent.resolve(),
                         (self.ws2 / "x").resolve())

    def test_clone_root_isolated_from_original(self):
        clone = self.reg.clone()
        clone.set_root(self.ws2, False)
        self.assertEqual(Path(self.reg.root), self.ws1.resolve())
        self.assertEqual(Path(clone.root), self.ws2.resolve())


class ExtraToolsActiveRegTests(unittest.TestCase):
    def test_extra_tools_use_task_registry(self):
        from gantry import bridge as b
        from gantry.tools_extra import register_extra_tools

        tmp = Path(tempfile.mkdtemp(prefix="cbp_extra_ws_"))
        one, two = tmp / "one", tmp / "two"
        one.mkdir(); two.mkdir()
        reg = ToolRegistry(one, {"allow_shell": False})
        impl = register_extra_tools(reg, {"allow_shell": False})
        clone = reg.clone()
        clone.set_root(two, False)
        tok = b._TOOLS_CTX.set(clone)
        try:
            self.assertEqual(Path(impl._active_reg().root), two.resolve(),
                             "扩展工具没跟随当前任务的注册表")
        finally:
            b._TOOLS_CTX.reset(tok)
        self.assertEqual(Path(impl._active_reg().root), one.resolve(), "无任务上下文时回落全局")


class SubagentRootTests(unittest.TestCase):
    def test_run_subagent_uses_task_root(self):
        # 子代理代码在 gantry/subagents.py（bridge.py 解耦后）
        i = SUBAGENTS.index("async def run_subagent")
        seg = SUBAGENTS[i:i + 1200]
        self.assertIn("b.task_tools().root", seg)
        self.assertNotIn("parent_root = str(AGENT_TOOLS.root)", seg)

    def test_drive_child_gets_own_registry(self):
        i = SUBAGENTS.index("async def _drive_child")
        seg = SUBAGENTS[i:i + 6000]
        self.assertIn("child_reg = b.task_tools().clone()", seg)
        self.assertIn("AgentRunner(child_reg", seg)
        self.assertIn("child_reg.set_root(", seg)

    def test_drive_child_does_not_reset_global_root(self):
        i = SUBAGENTS.index("async def _drive_child")
        seg = SUBAGENTS[i:i + 6000]
        self.assertNotIn("AGENT_TOOLS.set_root(parent_root", seg)

    def test_snapshot_hook_uses_task_root(self):
        i = BRIDGE.index("def self_snapshot_hook")
        seg = BRIDGE[i:i + 900]
        self.assertIn("task_tools().root", seg)

    def test_extra_tools_have_active_reg(self):
        self.assertIn("def _active_reg(self)", EXTRA)
        self.assertIn("_TOOLS_CTX.get()", EXTRA)


class FrontendWorkspaceTests(unittest.TestCase):
    def test_workspace_sent_with_task(self):
        # 任务请求里必须带当前选择的工作区（否则后端只能沿用会话旧值/默认值）
        i = APP.index("async function runTask")
        seg = APP[i:i + 900]
        self.assertIn("workspace:", seg)

    def test_workspace_selector_updates_ui_state(self):
        # 选择工作目录只更新前端状态，真正的生效点是随任务请求体发给后端
        self.assertIn("wsCurrent", APP)
        self.assertIn("function newSessionInWorkspace", APP)
        self.assertIn("id=\"workspace\"", (BASE / "webui" / "index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
