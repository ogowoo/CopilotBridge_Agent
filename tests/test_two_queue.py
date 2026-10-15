"""两级队列（P1-8）：next-turn 重排计划；next-step 立即生效并抢占批量剩余动作。"""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.agent import AgentRunner
from cbp.session_store import SessionStore
from cbp.tools import ToolRegistry


class StepQueueRunnerTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_q2_"))
        self.state = {"state": "ready"}
        self.steps = []
        self.prompts = []

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _runner(self, script):
        tools = ToolRegistry(self.ws, {})

        async def chat(prompt, model, make_new):
            self.prompts.append(prompt)
            return json.dumps(script(len(self.prompts)), ensure_ascii=False)

        return AgentRunner(tools, chat, {"max_total_steps": 8, "max_actions_per_turn": 4},
                           state_getter=lambda: dict(self.state),
                           state_saver=lambda **kw: self.state.update(kw),
                           step_saver=lambda s: self.steps.append(s),
                           file_event_saver=lambda e: None)

    def test_step_queue_injected_into_prompt_and_cleared(self):
        def script(n):
            if n == 1:
                # 第一条回答下达后，模拟"用户在执行途中插入紧急指令"
                self.state["pending_steps"] = ["不要使用 run_python"]
                return {"type": "tool", "tool": "list_directory", "arguments": {"path": "."}, "reason": "看目录"}
            return {"type": "final", "content": "完"}
        asyncio.run(self._runner(script).run("看目录", "m"))
        self.assertTrue(any("紧急指令" in p for p in self.prompts), "紧急指令必须进提示词")
        self.assertEqual(self.state.get("pending_steps"), [], "送达后必须清空，避免反复注入")

    def test_batch_preempted_by_step_queue(self):
        """批量 3 个动作，执行完第 1 个后出现紧急指令 → 放弃剩余并记 step_preempted。"""
        def script(n):
            if n == 1:
                return {"type": "actions", "actions": [
                    {"type": "tool", "tool": "make_directory", "arguments": {"path": "a"}, "reason": "1"},
                    {"type": "tool", "tool": "make_directory", "arguments": {"path": "b"}, "reason": "2"},
                    {"type": "tool", "tool": "make_directory", "arguments": {"path": "c"}, "reason": "3"},
                ]}
            self.state["pending_steps"] = ["停"]
            return {"type": "final", "content": "完"}

        # 在第 1 个动作执行后注入指令：用 step_saver 钩子实现"执行中"注入
        tools = ToolRegistry(self.ws, {})
        calls = []

        async def chat(prompt, model, make_new):
            calls.append(prompt)
            return json.dumps(script(len(calls)), ensure_ascii=False)

        def step_saver(step):
            self.steps.append(step)
            if step.get("tool") == "make_directory" and len([s for s in self.steps if s.get("tool") == "make_directory"]) == 1:
                self.state["pending_steps"] = ["立刻停下"]

        runner = AgentRunner(tools, chat, {"max_total_steps": 8, "max_actions_per_turn": 4},
                             state_getter=lambda: dict(self.state),
                             state_saver=lambda **kw: self.state.update(kw),
                             step_saver=step_saver, file_event_saver=lambda e: None)
        asyncio.run(runner.run("建目录", "m"))
        statuses = [s.get("status") for s in self.steps]
        self.assertIn("step_preempted", statuses, f"应记录抢占步骤，实际={statuses}")
        made = [s for s in self.steps if s.get("tool") == "make_directory"]
        self.assertEqual(len(made), 1, "剩余动作必须被放弃")
        self.assertTrue((self.ws / "a").is_dir(), "已执行的第 1 个动作保留")
        self.assertFalse((self.ws / "b").exists(), "第 2 个动作不应执行")
        self.assertFalse((self.ws / "c").exists(), "第 3 个动作不应执行")


class StateKeyPersistenceTests(unittest.TestCase):
    def test_pending_steps_and_approval_pending_persist(self):
        """两个新状态键必须真的落库（patch 白名单曾经静默丢弃它们）。"""
        tmp = Path(tempfile.mkdtemp(prefix="cbp_q2db_"))
        try:
            st = SessionStore(tmp / "a.db")
            st.create("s1", "任务", str(tmp), "m")
            st.patch("s1", pending_steps=["不要用 run_python"],
                     approval_pending={"tool": "delete_path", "args_hash": "h1"})
            got = st.get("s1")
            self.assertEqual(got.get("pending_steps"), ["不要用 run_python"])
            self.assertEqual((got.get("approval_pending") or {}).get("args_hash"), "h1")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
