# -*- coding: utf-8 -*-
"""授权之后的可见性：用户已授权，时间线不该还停在"未获一次性授权"。

用户实测反馈："已经完成了 approval_required 的，但是时间线里还显示是破坏性工具
未获一次性授权"。复现后发现是两个真问题：

1. **恢复时没告诉模型"已授权"**:历史里留着"需要用户在本会话授权该操作"的拦截
   消息，恢复只重发原任务，模型据此认定被拒而放弃 → 被拦下的工具根本不重试，
   时间线于是只剩那条 approval_required（看起来像一直没授权）。
2. **时间线没有正面记录**:授权本身不产生步骤，唯一可见的仍是拦截记录。

修法：
- `/approvals` 授权时登记一次性恢复提示（bridge.set_resume_hint），并补一条
  `approval_granted` 步骤；
- `resume_session_task` 把提示拼进恢复任务的用户消息（一次性消费）；
- 前端把"已被后续成功步骤解决"的拦截记录渲染成"已授权，已执行"（不再用失败样式）。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from cbp import bridge as b
from cbp.agent import AgentRunner
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore
from cbp.tools import ToolRegistry


class ResumeHintTests(unittest.TestCase):
    def setUp(self):
        b._RESUME_HINTS.clear()

    def tearDown(self):
        b._RESUME_HINTS.clear()

    def test_hint_is_consumed_once(self):
        b.set_resume_hint("s1", "用户已授权 delete_path（仅这一次）")
        self.assertEqual(b._RESUME_HINTS.get("s1"), "用户已授权 delete_path（仅这一次）")
        b.discard_resume_hint("s1")
        self.assertNotIn("s1", b._RESUME_HINTS)

    def test_empty_hint_ignored(self):
        b.set_resume_hint("s1", "   ")
        self.assertNotIn("s1", b._RESUME_HINTS)

    def test_resume_task_includes_hint(self):
        """恢复任务的消息里必须带上"已授权"，否则模型会放弃重试。"""
        tmp = Path(tempfile.mkdtemp(prefix="cbp_hint_"))
        ws = tmp / "ws"; ws.mkdir()
        b.AGENT_SESSIONS = SessionStore(tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(tmp / "snaps")
        b.AGENT_TOOLS.set_root(ws, False)
        b.AGENT_SESSIONS.create("s1", "删掉 gone.txt", str(ws), "copilot-auto")
        b.set_resume_hint("s1", "用户已授权对 delete_path 执行这一次操作")
        captured = {}

        async def fake_run(req):
            captured["task"] = req.messages[0].content
            return None, req.session_id

        saved = b.run_agent
        b.run_agent = fake_run
        try:
            asyncio.run(b.resume_session_task("s1"))
            for _ in range(40):
                if "task" in captured:
                    break
                asyncio.run(asyncio.sleep(0.05))
        finally:
            b.run_agent = saved
        self.assertIn("已授权", captured.get("task", ""))
        self.assertIn("delete_path", captured.get("task", ""))
        self.assertNotIn("s1", b._RESUME_HINTS, "提示是一次性的，用后即清")


class ApprovalVisibilityTests(unittest.TestCase):
    """授权接口要留下正面痕迹，且清理掉待授权状态。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_appr2_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        b.AGENT_SESSIONS.create("s1", "任务", str(self.ws), "copilot-auto")
        b.AGENT_SESSIONS.patch("s1", approval_pending={"tool": "delete_path", "args_hash": "abc123"})
        b._RESUME_HINTS.clear()
        from cbp.api import app
        self.c = TestClient(app)

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root) = self.saved
        b._RESUME_HINTS.clear()

    def test_grant_clears_pending_and_records_step(self):
        r = self.c.post("/v1/agent/sessions/s1/approvals", json={"tool": "delete_path"})
        self.assertEqual(r.status_code, 200)
        st = b.AGENT_SESSIONS.get("s1")
        self.assertIsNone(st.get("approval_pending"), "已授权就不该再挂着待授权")
        steps = b.AGENT_SESSIONS.steps("s1")
        self.assertTrue([s for s in steps if s.get("status") == "approval_granted"],
                        "时间线要留一条'已授权'的正面记录")

    def test_grant_registers_resume_hint(self):
        self.c.post("/v1/agent/sessions/s1/approvals", json={"tool": "delete_path"})
        hint = b._RESUME_HINTS.get("s1", "")
        self.assertIn("delete_path", hint)
        self.assertIn("已授权", hint)

    def test_session_scope_mentions_no_more_asking(self):
        r = self.c.post("/v1/agent/sessions/s1/approvals",
                        json={"tool": "delete_path", "scope": "session"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("不再逐次询问", b._RESUME_HINTS.get("s1", ""))


class ApprovedToolActuallyRunsTests(unittest.TestCase):
    """端到端语义：授权后恢复，被拦下的工具必须真的执行并留下 ok 步骤。"""

    def test_approved_tool_executes_on_second_run(self):
        tmp = Path(tempfile.mkdtemp(prefix="cbp_appr3_"))
        ws = tmp / "ws"; ws.mkdir()
        (ws / "gone.txt").write_text("x", encoding="utf-8")
        db = SessionStore(tmp / "agent.db")
        db.create("s1", "删掉 gone.txt", str(ws), "copilot-auto")
        cfg = {"max_total_steps": 6, "allow_delete": True}
        state = {"state": "ready", "total_steps": 0, "pending": [], "recent_steps": []}
        steps = []
        tools = ToolRegistry(ws, cfg)
        runner = AgentRunner(tools, None, cfg,
                             state_getter=lambda: state,
                             state_saver=lambda **kw: state.update(kw),
                             step_saver=lambda s: steps.append(s),
                             file_event_saver=lambda e: None,
                             approval_check=lambda tool, h: db.take_approval("s1", tool, h))
        turns = {"n": 0}

        async def chat(prompt, model, make_new):
            turns["n"] += 1
            # 关键：模拟"看到已授权提示后才继续执行"的模型行为
            if turns["n"] == 1 or "已授权" not in prompt:
                return json.dumps({"type": "tool", "tool": "delete_path",
                                   "arguments": {"path": "gone.txt"}, "reason": "删"},
                                  ensure_ascii=False)
            return json.dumps({"type": "final", "content": "已删除"}, ensure_ascii=False)

        runner.chat = chat
        # 第 1 轮：未授权 → 拦下
        asyncio.run(runner.run("删掉 gone.txt", "m"))
        self.assertTrue((ws / "gone.txt").exists())
        self.assertEqual(state.get("state"), "waiting")
        pend = state.get("approval_pending") or {}
        self.assertEqual(pend.get("tool"), "delete_path")
        # 授权并"恢复"（用带提示的任务文本，等价于 resume_session_task 的行为）
        db.grant_approval("s1", pend["tool"], pend["args_hash"])
        state["state"] = "ready"
        turns["n"] = 0
        asyncio.run(runner.run("删掉 gone.txt\n\n用户已授权对 delete_path 执行这一次操作", "m"))
        # 授权后必须真的执行：文件被删，且留下一条 ok 步骤（时间线才有正面记录）
        self.assertFalse((ws / "gone.txt").exists(), "已授权的破坏性工具必须真的执行")
        ok_steps = [s for s in steps if s.get("status") == "ok" and s.get("tool") == "delete_path"]
        self.assertTrue(ok_steps, "执行成功后要有 ok 步骤，时间线才不会只剩'未获授权'")


if __name__ == "__main__":
    unittest.main()
