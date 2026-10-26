# -*- coding: utf-8 -*-
"""计划模式的循环契约：提交方案即结束本轮，不会继续动手执行。"""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.agent import AgentRunner
from gantry.tools import ToolError, ToolRegistry


class PlanModeTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_plan_"))

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _runner(self, state, replies):
        cfg = {"max_total_steps": 20}
        seen = []

        async def chat(prompt, model, make_new):
            seen.append(prompt)
            return replies.pop(0) if replies else json.dumps(
                {"type": "final", "content": "done"}, ensure_ascii=False)

        tools = ToolRegistry(self.ws, cfg)
        tools.set_root(self.ws, False)
        tools.plan_mode = True
        # 生产环境由桥接层注入；测试里同样接上，否则 exit_plan_mode 的方案落不了库
        tools.state_saver = lambda **k: state.update(k)
        runner = AgentRunner(tools, chat, cfg,
                             state_getter=lambda: state,
                             state_saver=lambda **k: state.update(k),
                             step_saver=lambda s: None,
                             file_event_saver=lambda e: None)
        runner._task_text = "改点东西"
        return runner, tools, seen

    def test_exit_plan_mode_ends_turn(self):
        state = {"state": "ready"}
        replies = [json.dumps({"type": "tool", "tool": "exit_plan_mode",
                               "arguments": {"plan": "1. 改 A\n2. 跑测试"},
                               "reason": "提交方案"}, ensure_ascii=False),
                   # 若循环没停，就会执行到这条——用来证明早退生效
                   json.dumps({"type": "tool", "tool": "write_text",
                               "arguments": {"path": "should_not_exist.txt", "content": "x"},
                               "reason": "不该执行"}, ensure_ascii=False)]
        runner, tools, seen = self._runner(state, replies)
        result = asyncio.run(runner.run("改点东西", "m"))
        self.assertIn("改 A", result.content, "本轮回答应是方案正文")
        self.assertFalse((self.ws / "should_not_exist.txt").exists(),
                         "提交方案后不应继续执行写入")
        self.assertEqual(len(seen), 1, "应该只发生一个回合")
        self.assertFalse(tools.plan_mode, "exit_plan_mode 应解除只读")
        self.assertIn("plan", state, "方案应落进会话状态")

    def test_writes_blocked_before_submit(self):
        state = {"state": "ready"}
        runner, tools, seen = self._runner(state, [
            json.dumps({"type": "tool", "tool": "write_text",
                        "arguments": {"path": "a.txt", "content": "x"},
                        "reason": "计划阶段不该写"}, ensure_ascii=False),
        ])
        asyncio.run(runner.run("改点东西", "m"))
        self.assertFalse((self.ws / "a.txt").exists(), "计划模式下写入必须被拒绝")
        steps = json.dumps(state.get("recent_steps") or [], ensure_ascii=False)
        self.assertIn("SANDBOX_PLAN_MODE", steps)


if __name__ == "__main__":
    unittest.main()
