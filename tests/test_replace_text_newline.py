# -*- coding: utf-8 -*-
"""真实时间线（task-mv7zkarh，493 步）暴露的两个缺陷：

1) replace_text 在 Windows CRLF 文件上大量失败——模型从 JSON 给出的多行片段
   是 LF，而工具用 newline="" 原样读 CRLF，精确匹配必然为 0（该会话失败 91 次）。
2) no-op 守卫在换会话（rollover）后误判"没有任何产出"——它只看会被截断的
   history；而该会话已成功 replace_text 99 次、换过 6 次会话。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.tools import ToolRegistry
from gantry import bridge as b
from gantry.session_store import SessionStore
from gantry.snapshots import SnapshotStore


class ReplaceTextNewlineTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_crlf_"))
        self.tools = ToolRegistry(self.ws, {})

    def _run(self, coro):
        return asyncio.run(coro)

    def test_lf_snippet_matches_crlf_file(self):
        """核心回归：CRLF 文件 + 模型给的 LF 片段必须能替换，且保持 CRLF。"""
        f = self.ws / "mod.py"
        f.write_bytes(b"def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 2\r\n")
        self._run(self.tools.replace_text("mod.py", "def a():\n    return 1",
                                          "def a():\n    return 100"))
        self.assertEqual(f.read_bytes(),
                         b"def a():\r\n    return 100\r\n\r\ndef b():\r\n    return 2\r\n")

    def test_crlf_snippet_matches_lf_file(self):
        f = self.ws / "lf.txt"
        f.write_bytes(b"x\ny\n")
        self._run(self.tools.replace_text("lf.txt", "x\r\ny", "X\r\nY"))
        self.assertEqual(f.read_bytes(), b"X\nY\n")

    def test_exact_match_still_preferred(self):
        """CRLF 文件里本就存在的 CRLF 片段走原路径，行为不变。"""
        f = self.ws / "a.txt"
        f.write_bytes(b"one\r\ntwo\r\n")
        self._run(self.tools.replace_text("a.txt", "one\r\ntwo", "1\r\n2"))
        self.assertEqual(f.read_bytes(), b"1\r\n2\r\n")

    def test_missing_snippet_error_shows_real_content(self):
        """真不匹配时把文件里的真实内容摘给模型，便于一轮内纠正。"""
        f = self.ws / "x.py"
        f.write_text("def go():\n    return 42\n", encoding="utf-8")
        with self.assertRaises(Exception) as cm:
            self._run(self.tools.replace_text("x.py", "def go():\n        return 43", "nope"))
        msg = str(cm.exception)
        self.assertIn("未找到要替换的原文本", msg)
        self.assertIn("return 42", msg)      # 原文摘录
        self.assertIn("第 1 行", msg)


class NoopGuardAfterRolloverTests(unittest.TestCase):
    """换会话后仍应记得"本次运行已经产出过"，不得误弹 noop_guard。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_rollover_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, dict(b.PROVIDERS))
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, _r, provs) = self.saved
        b.PROVIDERS.clear(); b.PROVIDERS.update(provs)

    def test_write_before_rollover_does_not_trigger_guard(self):
        """先写文件，再强制换会话（history 被截断），最后 final 不该被拦。"""
        import base64
        payload = base64.b64encode(b"x = 1\n").decode()
        replies = [
            '{"type":"tool","tool":"write_text_b64","arguments":{"path":"a.py","content_b64":"%s"}}' % payload,
            '{"type":"tool","tool":"validate_python","arguments":{"path":"a.py"}}',
            '{"type":"tool","tool":"run_unittest","arguments":{"path":"tests"}}',
            '{"type":"final","content":"完成"}',
        ]

        class P:
            async def complete(self, model, messages, response_schema=None):
                return replies.pop(0) if replies else '{"type":"final","content":"完成"}'
        b.PROVIDERS["fakeprov"] = P()
        req = b.AgentRequest(model="fakeprov/m1",
                             messages=[b.Message(role="user", content="创建 a.py 并验证")],
                             stream=False, session_id="s1", workspace=str(self.ws))
        result, sid = asyncio.run(b.run_agent(req))
        steps = b.AGENT_SESSIONS.steps(sid)
        self.assertFalse([s for s in steps if s.get("status") == "noop_guard"],
                         "已有成功写入，不该再弹 noop_guard")
        self.assertTrue((self.ws / "a.py").is_file())

    def test_failed_writes_only_get_distinct_message(self):
        """只尝试过写但全失败 → 提示要区别于"完全没尝试"。"""
        ws_file = self.ws / "b.txt"; ws_file.write_text("real\n", encoding="utf-8")
        replies = [
            '{"type":"tool","tool":"replace_text","arguments":{"path":"b.txt","old":"NOT-THERE","new":"x"}}',
            '{"type":"final","content":"完成"}',
            '{"type":"final","content":"完成"}',
            '{"type":"final","content":"完成"}',
        ]

        class P:
            async def complete(self, model, messages, response_schema=None):
                return replies.pop(0) if replies else '{"type":"final","content":"完成"}'
        b.PROVIDERS["fakeprov"] = P()
        req = b.AgentRequest(model="fakeprov/m1",
                             messages=[b.Message(role="user", content="修改 b.txt 并保存")],
                             stream=False, session_id="s2", workspace=str(self.ws))
        asyncio.run(b.run_agent(req))
        steps = b.AGENT_SESSIONS.steps("s2")
        guards = [s for s in steps if s.get("status") == "noop_guard"]
        self.assertTrue(guards)
        self.assertIn("尝试过修改但每次都失败", guards[0]["error"])


if __name__ == "__main__":
    unittest.main()
