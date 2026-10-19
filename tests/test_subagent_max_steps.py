# -*- coding: utf-8 -*-
"""子代理步数上限可配置（用户实测：默认 40 步在做完前就用尽）。

4 个子代理分别停在 39/40/41 步——不是任务本身有问题，而是**默认上限太紧**：
浏览器通道每回合只推进一点，"读几个大文件 + 分批写 + 验证"很容易超过 40 步。

本文件钉住：
1. `agent.subagent_max_steps` 生效（默认 80），夹在 10~200；
2. 工具显式传 `max_steps` 时优先用它；
3. `max_total_steps` 的提示里给出**可执行的下一步**（继续 / 调大 / 配哪个键），
   而不是只说"已停止"——这条消息会被父会话和用户看到。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import bridge as b


class SubagentMaxStepsTests(unittest.TestCase):
    def setUp(self):
        self.saved_cfg = b.AGENT_CFG
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root)
        b.AGENT_CFG = dict(b.AGENT_CFG)
        tmp = Path(tempfile.mkdtemp(prefix="cbp_steps_"))
        from cbp.session_store import SessionStore
        from cbp.snapshots import SnapshotStore
        b.AGENT_SESSIONS = SessionStore(tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(tmp / "snaps")
        ws = tmp / "ws"; ws.mkdir()
        b.AGENT_TOOLS.set_root(ws, False)

    def tearDown(self):
        b.AGENT_CFG = self.saved_cfg
        (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root) = self.saved

    def test_default_is_80(self):
        b.AGENT_CFG.pop("subagent_max_steps", None)
        self.assertEqual(b.subagent_max_steps(), 80)

    def test_config_value_respected(self):
        b.AGENT_CFG["subagent_max_steps"] = 150
        self.assertEqual(b.subagent_max_steps(), 150)

    def test_clamped(self):
        b.AGENT_CFG["subagent_max_steps"] = 1
        self.assertEqual(b.subagent_max_steps(), 10, "太小要夹到下限")
        b.AGENT_CFG["subagent_max_steps"] = 9999
        self.assertEqual(b.subagent_max_steps(), 200, "太大要夹到上限")
        b.AGENT_CFG["subagent_max_steps"] = "abc"
        self.assertEqual(b.subagent_max_steps(), 80, "非法值回落默认")

    def test_tool_default_uses_config(self):
        from cbp.tools_extra import register_extra_tools
        b.AGENT_CFG["subagent_max_steps"] = 120
        got = {}

        def fn(task, description="", model="", max_steps=40):
            async def run():
                got["steps"] = max_steps
                return {"agent_id": "s", "steps": 1, "content": "ok"}
            return run()

        impl = register_extra_tools(b.AGENT_TOOLS, {}, subagent_fn=fn)
        asyncio.run(impl.subagent(task="x"))          # 不传 max_steps
        self.assertEqual(got["steps"], 120, "不传时应取配置值，而不是硬编码 40")

    def test_explicit_value_wins(self):
        from cbp.tools_extra import register_extra_tools
        b.AGENT_CFG["subagent_max_steps"] = 120
        got = {}

        def fn(task, description="", model="", max_steps=40):
            async def run():
                got["steps"] = max_steps
                return {"agent_id": "s", "steps": 1, "content": "ok"}
            return run()

        impl = register_extra_tools(b.AGENT_TOOLS, {}, subagent_fn=fn)
        asyncio.run(impl.subagent(task="x", max_steps=30))
        self.assertEqual(got["steps"], 30, "显式传值优先")

    def test_step_limit_message_is_actionable(self):
        """步数用尽的消息要给出路，不能只说"已停止"。"""
        from cbp.agent import AgentRunner
        from cbp.tools import ToolRegistry
        tmp = Path(tempfile.mkdtemp(prefix="cbp_steps2_"))
        ws = tmp / "ws"; ws.mkdir()
        b.AGENT_TOOLS.set_root(ws, False)
        state = {"state": "running", "total_steps": 0, "pending": [], "recent_steps": []}

        async def chat(prompt, model, make_new):
            return '{"type":"tool","tool":"list_directory","arguments":{"path":"."},"reason":"x"}'

        runner = AgentRunner(b.AGENT_TOOLS, chat, {"max_total_steps": 2},
                             state_getter=lambda: state,
                             state_saver=lambda **kw: state.update(kw),
                             step_saver=lambda s: None,
                             file_event_saver=lambda e: None)
        res = asyncio.run(runner.run("跑", "m"))
        self.assertIn("最大总步骤数", res.content)
        self.assertIn("接着做的方法", res.content, "要告诉用户/模型下一步怎么做")
        self.assertIn("subagent_send", res.content)
        self.assertIn("subagent_max_steps", res.content, "要指出可配置的键名")


if __name__ == "__main__":
    unittest.main()
