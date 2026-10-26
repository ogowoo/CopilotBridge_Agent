# -*- coding: utf-8 -*-
"""no-op 完成守卫（扩展版）：任务要求动手、却始终没有产出就 final 必须被拦。

用户实测缺陷：用提供商模型（DeepSeek）时"能读，没有任何写步骤"——模型先读了
一堆文件、然后直接宣布完成。原守卫条件是 `_needs_action and total == 0`,
只拦"一步工具都没调就 final",读完再 final 完全不拦（total != 0）。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry import bridge as b
from gantry.session_store import SessionStore
from gantry.snapshots import SnapshotStore


class _ScriptedProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, model, messages, response_schema=None):
        self.calls.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else '{"type":"final","content":"结束"}'


READ = '{"type":"tool","tool":"list_directory","arguments":{"path":"."},"reason":"看目录"}'
FINAL = '{"type":"final","content":"分析完成"}'


class NoopGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_noop_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        (self.ws / "a.txt").write_text("hello", encoding="utf-8")
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, dict(b.PROVIDERS))
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, _r, provs) = self.saved
        b.PROVIDERS.clear(); b.PROVIDERS.update(provs)

    def _run(self, replies, task):
        prov = _ScriptedProvider(replies)
        b.PROVIDERS["fakeprov"] = prov
        req = b.AgentRequest(model="fakeprov/m1",
                             messages=[b.Message(role="user", content=task)],
                             stream=False, session_id="s1", workspace=str(self.ws))
        result, sid = asyncio.run(b.run_agent(req))
        return prov, result, sid

    def test_read_then_final_is_blocked(self):
        """核心回归：先读、再 final（零产出）→ 守卫拦截并推它去写。"""
        prov, result, sid = self._run(
            [READ, FINAL,                                     # 读 → 想直接完成（应被拦）
             '{"type":"tool","tool":"write_text_b64","arguments":{"path":"out.txt","content_b64":"aGk="}}',
             '{"type":"final","content":"已写入 out.txt"}'],
            task="读取 a.txt 并生成 out.txt")
        steps = b.AGENT_SESSIONS.steps(sid)
        guards = [s for s in steps if s.get("status") == "noop_guard"]
        self.assertTrue(guards, "读完就 final 没有被 no-op 守卫拦截")
        self.assertIn("只做了读取", guards[0]["error"])
        self.assertTrue((self.ws / "out.txt").is_file(), "被拦后仍未产出文件")

    def test_pure_analysis_task_not_blocked(self):
        """只要求分析的任务不该被误拦（无动手动词）。"""
        prov, result, sid = self._run([FINAL], task="分析这个目录的用途")
        steps = b.AGENT_SESSIONS.steps(sid)
        self.assertFalse([s for s in steps if s.get("status") == "noop_guard"])
        self.assertIn("分析完成", result.content)

    def test_guard_allows_finish_after_two_strikes(self):
        """连续被拦两次后放行，避免小模型无限重试烧 token。"""
        prov, result, sid = self._run([FINAL, FINAL, FINAL],
                                      task="生成一份报告文件")
        steps = b.AGENT_SESSIONS.steps(sid)
        guards = [s for s in steps if s.get("status") == "noop_guard"]
        self.assertEqual(len(guards), 2)
        # 第三次 final 放行
        self.assertEqual(result.content, "分析完成")

    def test_action_tool_satisfies_guard(self):
        """只要真动手过（写出文件），就不该再拦 final。"""
        _, result, sid = self._run(
            ['{"type":"tool","tool":"write_text_b64","arguments":{"path":"r.txt","content_b64":"aGk="}}',
             FINAL],
            task="生成报告文件")
        steps = b.AGENT_SESSIONS.steps(sid)
        self.assertFalse([s for s in steps if s.get("status") == "noop_guard"])
        self.assertTrue((self.ws / "r.txt").is_file())


if __name__ == "__main__":
    unittest.main()
