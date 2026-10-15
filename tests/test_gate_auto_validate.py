# -*- coding: utf-8 -*-
"""验证门自动补跑：模型忘记工具名时，系统代跑真验证，别让长任务白干。

真实时间线（task-mv7zkarh，493 步，deepseek-v4-flash，最终 failed）尾部：
  step484 write_text_b64 ok / step485 replace_text ok
  step487 validation_gate（第 2 次被拒）
  step488 run_python error（已禁用）
  step489 execute_python / 490 run_tests / 491 execute_command / 492 check_syntax
        —— 全是模型**瞎编的工具名**（它已忘记 validate_python/run_unittest）
  step493 gate_blocked → 整任务判 failed
"""
import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import bridge as b
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode()


class _ScriptedProvider:
    def __init__(self, replies):
        self.replies = list(replies)

    async def complete(self, model, messages, response_schema=None):
        return self.replies.pop(0) if self.replies else '{"type":"final","content":"完成"}'


class GateAutoValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_gate_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        (self.ws / "tests").mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, dict(b.PROVIDERS))
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, _r, provs) = self.saved
        b.PROVIDERS.clear(); b.PROVIDERS.update(provs)

    def _run(self, replies, task="创建 m.py 并保证测试通过"):
        b.PROVIDERS["fakeprov"] = _ScriptedProvider(replies)
        req = b.AgentRequest(model="fakeprov/m1",
                             messages=[b.Message(role="user", content=task)],
                             stream=False, session_id="s1", workspace=str(self.ws))
        return asyncio.run(b.run_agent(req))

    def test_auto_validate_completes_when_code_is_fine(self):
        """代码没问题、模型只想直接 final → 系统补跑验证通过后正常完成（不再判 failed）。"""
        _, sid = self._run([
            '{"type":"tool","tool":"write_text_b64","arguments":{"path":"m.py","content_b64":"%s"}}' % _b64("X = 1\n"),
            '{"type":"tool","tool":"write_text_b64","arguments":{"path":"tests/test_m.py","content_b64":"%s"}}'
            % _b64("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"),
            '{"type":"final","content":"已完成"}',      # 直接被验证门拦下 → 系统补跑
        ])
        steps = b.AGENT_SESSIONS.steps(sid)
        auto = [s for s in steps if s.get("reason") == "验证门补跑（模型未自行验证）"]
        self.assertEqual([s.get("tool") for s in auto], ["validate_python", "run_unittest"])
        self.assertTrue(all(s.get("status") == "ok" for s in auto))
        # 补跑通过 → 会话正常 completed，而不是 failed
        self.assertEqual(b.AGENT_SESSIONS.get(sid)["state"], "completed")
        self.assertFalse([s for s in steps if s.get("status") == "gate_blocked"])

    def test_gate_message_names_exact_tools(self):
        """拒绝消息必须给出可逐字照抄的工具名与参数（防模型瞎编工具名）。"""
        _, sid = self._run([
            '{"type":"tool","tool":"write_text_b64","arguments":{"path":"bad.py","content_b64":"%s"}}'
            % _b64("def broken(:\n"),                       # 语法错误 → 补跑必失败
            '{"type":"final","content":"完成"}',
            '{"type":"final","content":"完成"}',
            '{"type":"final","content":"完成"}',
        ])
        steps = b.AGENT_SESSIONS.steps(sid)
        gate = [s for s in steps if s.get("status") in ("validation_gate", "gate_blocked")]
        self.assertTrue(gate)
        msg = gate[0]["error"]
        self.assertIn("validate_python", msg)      # 明确工具名
        self.assertIn("run_unittest", msg)
        self.assertIn("run_tests", msg)            # 明确点名不要用哪些假名字
        self.assertIn("系统已代你补跑过一次", msg)   # 附真实失败原因

    def test_auto_validate_failure_reports_real_reason(self):
        """补跑失败时要把真实原因（语法错误）回给模型。"""
        _, sid = self._run([
            '{"type":"tool","tool":"write_text_b64","arguments":{"path":"bad2.py","content_b64":"%s"}}'
            % _b64("def broken(:\n"),
            '{"type":"final","content":"完成"}',
            '{"type":"final","content":"完成"}',
            '{"type":"final","content":"完成"}',
        ])
        steps = b.AGENT_SESSIONS.steps(sid)
        gate = [s for s in steps if s.get("status") == "validation_gate"]
        self.assertIn("bad2.py", gate[0]["error"])


if __name__ == "__main__":
    unittest.main()
