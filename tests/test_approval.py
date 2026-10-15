"""破坏性工具的一次性审批：fail-closed、allowed-once、精确到参数。"""
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


class ApprovalStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_appr_"))
        self.store = SessionStore(self.tmp / "a.db")
        self.store.create("s1", "任务", str(self.tmp), "m")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_take_is_one_shot(self):
        self.assertFalse(self.store.take_approval("s1", "delete_path", "h1"), "未授权必须失败")
        self.store.grant_approval("s1", "delete_path", "h1")
        self.assertEqual(len(self.store.pending_approvals("s1")), 1)
        self.assertTrue(self.store.take_approval("s1", "delete_path", "h1"))
        self.assertFalse(self.store.take_approval("s1", "delete_path", "h1"), "用后即焚，不能复用")
        self.assertEqual(self.store.pending_approvals("s1"), [])

    def test_grant_is_argument_specific(self):
        self.store.grant_approval("s1", "delete_path", "h1")
        self.assertFalse(self.store.take_approval("s1", "delete_path", "h2"),
                         "授权必须精确到参数，不能拿 A 的授权做 B")
        self.assertFalse(self.store.take_approval("s1", "outlook_send", "h1"),
                         "也不能跨工具复用")

    def test_regrant_reopens(self):
        self.store.grant_approval("s1", "run_python", "h")
        self.assertTrue(self.store.take_approval("s1", "run_python", "h"))
        self.store.grant_approval("s1", "run_python", "h")
        self.assertTrue(self.store.take_approval("s1", "run_python", "h"), "再次授权应重新可用")

    def test_cascade_delete(self):
        self.store.grant_approval("s1", "delete_path", "h")
        self.assertTrue(self.store.delete("s1"))
        self.assertEqual(self.store.pending_approvals("s1"), [])


class ApprovalGateTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_appr_ws_"))
        (self.ws / "victim.txt").write_text("要被删掉", encoding="utf-8")
        self.state = {"state": "ready"}
        self.steps = []

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _run(self, approval_check=None):
        tools = ToolRegistry(self.ws, {"allow_delete": True})
        calls = []

        async def chat(prompt, model, make_new):
            calls.append(prompt)
            if len(calls) == 1:
                return json.dumps({"type": "tool", "tool": "delete_path",
                                   "arguments": {"path": "victim.txt"}, "reason": "删"})
            return json.dumps({"type": "final", "content": "完"})

        runner = AgentRunner(tools, chat, {"max_total_steps": 4},
                             state_getter=lambda: dict(self.state),
                             state_saver=lambda **kw: self.state.update(kw),
                             step_saver=lambda s: self.steps.append(s),
                             file_event_saver=lambda e: None,
                             approval_check=approval_check)
        return asyncio.run(runner.run("删掉 victim.txt", "m"))

    def test_fail_closed_without_grant(self):
        self._run(approval_check=lambda tool, h: False)
        self.assertTrue((self.ws / "victim.txt").exists(), "未授权时绝不能执行破坏性操作")
        statuses = [s.get("status") for s in self.steps]
        self.assertIn("approval_required", statuses)
        self.assertEqual(self.state.get("state"), "waiting")
        self.assertEqual((self.state.get("approval_pending") or {}).get("tool"), "delete_path")

    def test_no_callback_means_not_granted(self):
        self._run(approval_check=None)
        self.assertTrue((self.ws / "victim.txt").exists(), "没有回调=不放行")

    def test_granted_executes_once(self):
        seen = []

        def check(tool, h):
            seen.append((tool, h))
            return True

        self._run(approval_check=check)
        self.assertFalse((self.ws / "victim.txt").exists(), "授权后应真的执行")
        self.assertEqual(seen[0][0], "delete_path")
        self.assertEqual(len(seen[0][1]), 16)

    def test_requires_approval_matrix(self):
        t = ToolRegistry(self.ws, {"allow_delete": True, "allow_outlook_send": True})
        self.assertTrue(t.requires_approval("delete_path"))
        self.assertTrue(t.requires_approval("outlook_send"))
        self.assertFalse(t.requires_approval("read_text"))
        t2 = ToolRegistry(self.ws, {"allow_delete": True, "approval_policy": "never"})
        self.assertFalse(t2.requires_approval("delete_path"), "policy=never 时明确不要求")


if __name__ == "__main__":
    unittest.main()
