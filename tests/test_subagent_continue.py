# -*- coding: utf-8 -*-
"""子代理：分叉 / 继续 / 列表（不跑真实模型，只验证会话层行为与错误路径）。"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import bridge as b
from cbp.session_store import SessionStore


class SubagentForkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_subfork_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.AGENT_TOOLS.root)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.AGENT_TOOLS.set_root(self.ws, False)
        # 父会话 + 子代理
        b.AGENT_SESSIONS.create("p1", "父任务", str(self.ws), "m")
        b.AGENT_SESSIONS.create("sub-a", "子任务", str(self.ws), "m")
        b.AGENT_SESSIONS.patch("sub-a", parent_id="p1", description="子代理 A")
        b.AGENT_SESSIONS.add_message("sub-a", "user", "记住 42", 0)
        b.AGENT_SESSIONS.add_message("sub-a", "assistant", "ok", 0)

    def tearDown(self):
        b.AGENT_SESSIONS, b.AGENT_TOOLS.root = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fork_copies_messages_and_keeps_parent(self):
        out = asyncio.run(b.fork_subagent("sub-a", description="另一个方案"))
        new_sid = out["agent_id"]
        self.assertTrue(new_sid.startswith("sub-"))
        self.assertEqual(out["messages"], 2)
        row = b.AGENT_SESSIONS.get(new_sid)
        self.assertEqual(row["parent_id"], "p1", "分叉应挂在同一个父会话下")
        msgs = b.AGENT_SESSIONS.messages(new_sid)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[0]["content"], "记住 42")

    def test_fork_top_level_session_parents_to_itself(self):
        out = asyncio.run(b.fork_subagent("p1", keep_messages=10))
        row = b.AGENT_SESSIONS.get(out["agent_id"])
        self.assertEqual(row["parent_id"], "p1", "顶层会话分叉后父指针指向源会话")

    def test_delete_parent_cascades_to_children(self):
        """删父会话要把子代理一起删掉，否则子会话没有归属、只能浮在列表顶层。"""
        b.AGENT_SESSIONS.delete("p1")
        self.assertIsNone(b.AGENT_SESSIONS.get("p1"))
        self.assertIsNone(b.AGENT_SESSIONS.get("sub-a"), "子代理应随父会话级联删除")
        self.assertEqual(b.AGENT_SESSIONS.messages("sub-a"), [])

    def test_fork_unknown_session(self):
        with self.assertRaises(b.ToolError):
            asyncio.run(b.fork_subagent("nope"))

    def test_list_subagents_filters_by_parent(self):
        out = asyncio.run(b.list_subagents("p1"))
        ids = [x["agent_id"] for x in out["subagents"]]
        self.assertIn("sub-a", ids)
        self.assertNotIn("p1", ids)

    def test_continue_rejects_non_subagent(self):
        with self.assertRaises(b.ToolError) as ctx:
            asyncio.run(b.continue_subagent("p1", "继续"))
        self.assertIn("不是子代理", str(ctx.exception))

    def test_continue_rejects_empty_message(self):
        with self.assertRaises(b.ToolError):
            asyncio.run(b.continue_subagent("sub-a", "   "))


if __name__ == "__main__":
    unittest.main()
