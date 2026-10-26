"""验证闭环旗的持久化回归测试。

背景（审查实测缺陷）：validation_required / syntax_validated / tests_passed
曾是 run() 的局部变量。用户暂停→继续、或服务重启后新建 runner，三面旗归零，
"代码已改但没验证"的记忆消失，恢复后的 Agent 直接返回 final 就能把任务标成
completed，验证门被绕过。

修复：三面旗持久化进 sessions.gate_state，run() 开头读回。
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


def act(tool, **args):
    return {"type": "tool", "tool": tool, "arguments": args, "reason": ""}


class GatePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_gate_"))
        self.steps = []
        self.state = {"state": "ready"}

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _runner(self, chat):
        return AgentRunner(ToolRegistry(self.ws, {}), chat,
                           {"max_total_steps": 20, "max_parse_failures": 3},
                           state_getter=lambda: dict(self.state),
                           state_saver=lambda **kw: self.state.update(kw),
                           step_saver=lambda s: self.steps.append(s),
                           file_event_saver=lambda e: None)

    def test_code_change_without_validation_is_remembered(self):
        """改了 .py 文件后返回 final → 记 validation_gate，且旗落库。"""
        turns = []

        async def chat(prompt, model, make_new):
            turns.append(prompt)
            if len(turns) == 1:
                return json.dumps(act("write_text_b64", path="mod.py",
                                      content_b64=base64.b64encode(b"def f(:\n").decode()),
                                 ensure_ascii=False)
            return json.dumps({"type": "final", "content": "完成"}, ensure_ascii=False)

        runner = self._runner(chat)
        asyncio.run(runner.run("改代码", "m"))
        gates = [s for s in self.steps if s.get("status") == "validation_gate"]
        self.assertTrue(gates, "未通过验证就 final 必须被拦下")
        self.assertNotEqual(self.state.get("state"), "completed")
        saved = json.loads(self.state.get("gate_state") or "{}")
        self.assertTrue(saved.get("validation_required"), "验证旗未持久化")
        self.assertFalse(saved.get("syntax_validated"))

    def test_resume_cannot_skip_validation(self):
        """模拟暂停→继续：新 runner 只给一个 final 也必须被验证门拦住。"""
        self.state["gate_state"] = json.dumps(
            {"validation_required": True, "syntax_validated": False, "tests_passed": False})
        self.state["state"] = "ready"

        async def chat(prompt, model, make_new):
            return json.dumps({"type": "final", "content": "全部完成"}, ensure_ascii=False)

        runner = self._runner(chat)
        result = asyncio.run(runner.run("继续", "m"))
        self.assertNotEqual(self.state.get("state"), "completed")
        self.assertTrue(any(s.get("status") == "validation_gate" for s in self.steps),
                        "恢复后的 runner 绕过了验证门")
        self.assertNotIn("全部完成", result.content)

    def test_gate_cleared_after_real_validation(self):
        """真正跑过 validate + unittest 后允许完成，并清掉旗。"""
        self.state["gate_state"] = json.dumps(
            {"validation_required": True, "syntax_validated": False, "tests_passed": False})
        calls = []

        async def chat(prompt, model, make_new):
            calls.append(prompt)
            n = len(calls)
            if n == 1:
                return json.dumps({"type": "actions", "actions": [
                    act("validate_python", path="mod.py"),
                    act("run_unittest", pattern="test_*.py"),
                ]}, ensure_ascii=False)
            return json.dumps({"type": "final", "content": "验证通过"}, ensure_ascii=False)

        (self.ws / "mod.py").write_text("x = 1\n", encoding="utf-8")
        (self.ws / "tests").mkdir(exist_ok=True)
        (self.ws / "tests" / "test_sample.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8")
        runner = self._runner(chat)
        result = asyncio.run(runner.run("继续", "m"))
        self.assertEqual(self.state.get("state"), "completed")
        self.assertIn("验证通过", result.content)
        saved = json.loads(self.state.get("gate_state") or "{}")
        self.assertFalse(saved.get("validation_required"), "完成后应清掉验证旗")


if __name__ == "__main__":
    unittest.main()
