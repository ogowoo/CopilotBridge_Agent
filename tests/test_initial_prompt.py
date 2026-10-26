# -*- coding: utf-8 -*-
"""会话初始提示词：落库、只记第一次、旧会话近似重建、端点可查。

用户反馈"会话的初始提示词看不到"：此前只写在 RUNTIME.prompt_preview，
每个回合都被新提示词覆盖，首回合那条（含系统提示词/工具清单/项目约定）随即丢失。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from gantry import bridge as b
from gantry.session_store import SessionStore
from gantry.snapshots import SnapshotStore


class InitialPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_prompt_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, dict(b.PROVIDERS))
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        b.AGENT_SESSIONS.create("s1", "生成一个报告文件", str(self.ws), "copilot-auto")

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, _r, provs) = self.saved
        b.PROVIDERS.clear(); b.PROVIDERS.update(provs)
        b._PROVIDER_AGENT_HISTORY.clear()

    def test_remember_only_first(self):
        b.remember_initial_prompt("s1", "首回合完整提示词")
        b.remember_initial_prompt("s1", "第二回合的短提示")
        self.assertEqual(b.AGENT_SESSIONS.get("s1")["initial_prompt"], "首回合完整提示词")

    def test_remember_survives_reopen(self):
        b.remember_initial_prompt("s1", "初始提示词 ABC")
        again = SessionStore(self.tmp / "agent.db")   # 模拟重启后重开库
        self.assertEqual(again.get("s1")["initial_prompt"], "初始提示词 ABC")

    def test_provider_turn_records_initial_prompt(self):
        class P:
            async def complete(self, model, messages, response_schema=None):
                return '{"type":"final","content":"完成"}'
        b.PROVIDERS["fakeprov"] = P()
        b._PROVIDER_AGENT_HISTORY.clear()
        asyncio.run(b.provider_turn("s1", "fakeprov/m1", "完整系统提示词+任务"))
        self.assertIn("完整系统提示词", b.AGENT_SESSIONS.get("s1")["initial_prompt"])
        asyncio.run(b.provider_turn("s1", "fakeprov/m1", "继续同一任务。上一动作结果"))
        self.assertIn("完整系统提示词", b.AGENT_SESSIONS.get("s1")["initial_prompt"])

    def test_endpoint_returns_initial_and_rebuilds_when_missing(self):
        from gantry.api import app
        c = TestClient(app)
        # 未落库 → 走近似重建
        d = c.get("/v1/agent/sessions/s1/prompt").json()
        self.assertTrue(d["rebuilt"])
        self.assertIn("用户任务", d["initial_prompt"])
        self.assertIn("生成一个报告文件", d["initial_prompt"])
        # 落库后 → 直接返回原样，不再重建
        b.remember_initial_prompt("s1", "落库的初始提示词")
        d2 = c.get("/v1/agent/sessions/s1/prompt").json()
        self.assertFalse(d2["rebuilt"])
        self.assertEqual(d2["initial_prompt"], "落库的初始提示词")

    def test_rebuild_uses_independent_registry(self):
        """重建不能改动正在运行任务共享的 AGENT_TOOLS.root（否则任务串台）。"""
        before = b.AGENT_TOOLS.root
        other = self.tmp / "other"; other.mkdir()
        b.rebuild_initial_prompt({"id": "x", "workspace": str(other), "original_task": "任务"})
        self.assertEqual(b.AGENT_TOOLS.root, before)

    def test_endpoint_404(self):
        from gantry.api import app
        c = TestClient(app)
        self.assertEqual(c.get("/v1/agent/sessions/nope/prompt").status_code, 404)


if __name__ == "__main__":
    unittest.main()
