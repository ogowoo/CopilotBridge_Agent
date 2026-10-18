# -*- coding: utf-8 -*-
"""目标续轮（create_goal 的循环契约）。

目标处于 active 时，模型返回 final 不算结束，Agent 自动续轮；轮次用尽标记 blocked。
状态存在会话 state["goal"]，由扩展工具 create_goal/update_goal 维护。
"""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.agent import AgentRunner
from cbp.tools import ToolRegistry


class GoalContinueTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_goal_"))

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _runner(self, state, prompts):
        cfg = {"max_total_steps": 20}

        async def chat(prompt, model, make_new):
            prompts.append(prompt)
            return json.dumps({"type": "final", "content": "done"}, ensure_ascii=False)

        tools = ToolRegistry(self.ws, cfg)
        return AgentRunner(tools, chat, cfg,
                           state_getter=lambda: state,
                           state_saver=lambda **k: state.update(k),
                           step_saver=lambda s: None,
                           file_event_saver=lambda e: None)

    def test_goal_continues_until_complete(self):
        state = {"state": "ready",
                 "goal": {"objective": "完成 X", "rounds": 0, "max_rounds": 2,
                          "status": "active"}}
        prompts = []
        r = self._runner(state, prompts)
        r._task_text = "解释一下这个概念"      # 非动作任务，避免 noop_guard 干扰
        asyncio.run(r.run("解释一下这个概念", "m"))
        # 首轮 + 2 次续轮 = 3 个回合
        self.assertEqual(len(prompts), 3, "目标 active 时应自动续轮")
        self.assertIn("goal_round", json.dumps(state.get("recent_steps") or [], ensure_ascii=False))
        self.assertEqual(state["goal"]["status"], "blocked", "轮次用尽后应标记 blocked")

    def test_no_goal_single_turn(self):
        state = {"state": "ready"}
        prompts = []
        r = self._runner(state, prompts)
        r._task_text = "解释一下这个概念"
        asyncio.run(r.run("解释一下这个概念", "m"))
        self.assertEqual(len(prompts), 1)

    def test_completed_goal_no_continue(self):
        state = {"state": "ready",
                 "goal": {"objective": "完成 X", "rounds": 0, "max_rounds": 5,
                          "status": "complete"}}
        prompts = []
        r = self._runner(state, prompts)
        r._task_text = "解释一下这个概念"
        asyncio.run(r.run("解释一下这个概念", "m"))
        self.assertEqual(len(prompts), 1, "目标已完成时不应续轮")


if __name__ == "__main__":
    unittest.main()
