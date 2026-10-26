"""验证门的比例原则：默认要求单测，任务声明"只做语法检查"时放宽。

背景（span 数据）：任务只要求语法检查时，门强制单测会让模型给一次性脚本造
tests/ 目录与测试文件——实测 gate-limit 因此从 4 步涨到 24 步。
"""
import asyncio
import base64
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.agent import AgentRunner
from gantry.tools import ToolRegistry


def w(path, content="x = 1\n"):
    return {"type": "tool", "tool": "write_text_b64", "arguments": {
        "path": path, "content_b64": base64.b64encode(content.encode()).decode()}, "reason": ""}


class GateProportionalTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_gateprop_"))
        self.steps = []

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _run(self, task, script, config=None):
        state = {"state": "ready"}
        calls = []

        async def chat(prompt, model, make_new):
            calls.append(prompt)
            return json.dumps(script(len(calls)), ensure_ascii=False)

        cfg = {"max_total_steps": 8}
        cfg.update(config or {})
        runner = AgentRunner(ToolRegistry(self.ws, {}), chat, cfg,
                             state_getter=lambda: dict(state),
                             state_saver=lambda **kw: state.update(kw),
                             step_saver=lambda s: self.steps.append(s),
                             file_event_saver=lambda e: None)
        return asyncio.run(runner.run(task, "m")), state, calls

    def _script(self):
        def s(n):
            if n == 1:
                return w("eval_only.py")
            if n == 2:
                return {"type": "tool", "tool": "validate_python", "arguments": {"path": "eval_only.py"}, "reason": ""}
            return {"type": "final", "content": "完成"}
        return s

    def test_unittest_not_required_when_task_says_syntax_only(self):
        result, state, calls = self._run("创建 eval_only.py，只做语法检查，然后 final", self._script())
        self.assertEqual(state.get("state"), "completed",
                         f"声明只做语法检查时不应被单测卡住（steps={[x.get('status') for x in self.steps]}）")
        self.assertNotIn("run_unittest", [x.get("tool") for x in self.steps])

    def test_unittest_not_required_when_workspace_has_no_tests(self):
        """原工作区没有 tests/：不要为一次性脚本现造测试，validate 通过即可。"""
        result, state, calls = self._run("创建 eval_only.py 并验证通过", self._script())
        self.assertEqual(state.get("state"), "completed",
                         f"没有项目测试时不应强制单测（steps={[x.get('status') for x in self.steps]}）")
        self.assertNotIn("run_unittest", [x.get("tool") for x in self.steps])

    def test_unittest_still_required_when_project_has_tests(self):
        tests = self.ws / "tests"
        tests.mkdir()
        (tests / "test_fail.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertTrue(False)\n",
            encoding="utf-8")
        result, state, calls = self._run("创建 eval_only.py 并验证通过", self._script())
        self.assertNotEqual(state.get("state"), "completed", "项目测试失败时不能允许完成")
        self.assertTrue(any(x.get("status") in ("validation_gate", "gate_blocked") for x in self.steps))

    def test_config_can_disable_requirement_globally(self):
        result, state, calls = self._run("创建 eval_only.py 并验证通过", self._script(),
                                        {"require_unittest": False})
        self.assertEqual(state.get("state"), "completed")
        self.assertFalse(any(x.get("status") == "gate_blocked" for x in self.steps))

    def test_config_can_force_unittest_without_project_tests(self):
        result, state, calls = self._run("创建 eval_only.py 并验证通过", self._script(),
                                        {"require_unittest_without_tests": True})
        self.assertNotEqual(state.get("state"), "completed")
        self.assertTrue(any(x.get("status") in ("validation_gate", "gate_blocked") for x in self.steps))


if __name__ == "__main__":
    unittest.main()
