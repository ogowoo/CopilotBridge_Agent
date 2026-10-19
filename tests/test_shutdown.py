# -*- coding: utf-8 -*-
"""终止服务 / 退出：UI 按钮背后要真的能停掉服务。

要点：
- 优雅停机：入口（start.py）把"置 uvicorn Server.should_exit"注入 lifecycle；
- 拿不到注入时退回 SIGINT（Windows 上会硬杀，所以注入路径优先）；
- **始终有硬退出兜底**：按钮不能出现"点了没反应"；
- 幂等：按钮可能被连点；
- 端点是"先回响应再停"（直接停会让前端拿到连接重置，看着像失败）。
"""
import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from cbp import bridge as b
from cbp import lifecycle
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        lifecycle.disarm()                # 清状态 + 取消排队中的延时触发
        self.calls = []
        self.saved_hook = lifecycle._hook
        self.saved_flag = lifecycle._shutting_down
        self.saved_force = lifecycle._force_seconds
        lifecycle._hook = lambda: self.calls.append("hook")
        lifecycle._shutting_down = False
        lifecycle._force_seconds = 30          # 别在测试里真的硬退出

    def tearDown(self):
        lifecycle.disarm()           # 作废排队中的兜底定时器
        lifecycle._hook = self.saved_hook
        lifecycle._shutting_down = self.saved_flag
        lifecycle._force_seconds = self.saved_force

    def test_request_shutdown_uses_hook(self):
        self.assertEqual(lifecycle.request_shutdown(), "uvicorn")
        self.assertEqual(self.calls, ["hook"])
        self.assertTrue(lifecycle.shutdown_pending())

    def test_request_shutdown_is_idempotent(self):
        lifecycle.request_shutdown()
        self.assertEqual(lifecycle.request_shutdown(), "already")
        self.assertEqual(self.calls, ["hook"], "连点不应重复触发")

    def test_falls_back_to_signal_when_hook_missing(self):
        """拿不到 uvicorn server（例如别人直接 uvicorn app:app）也要能停。"""
        lifecycle._hook = None
        sent = []
        import os as _os
        import signal as _signal
        real_kill = _os.kill
        _os.kill = lambda pid, sig: sent.append(sig)
        try:
            self.assertEqual(lifecycle.request_shutdown(), "signal")
        finally:
            _os.kill = real_kill
        self.assertEqual(sent, [_signal.SIGINT])

    def test_broken_hook_still_shuts_down(self):
        """钩子抛异常不能导致"点了没反应"。"""
        def boom():
            raise RuntimeError("hook broken")
        lifecycle._hook = boom
        import os as _os
        real_kill = _os.kill
        _os.kill = lambda pid, sig: None
        try:
            self.assertEqual(lifecycle.request_shutdown(), "signal")
        finally:
            _os.kill = real_kill

    def test_force_exit_thread_is_armed(self):
        """硬退出兜底必须被武装（宽限期到点即退）。"""
        armed = []
        real_force = lifecycle._force_exit_later
        lifecycle._force_exit_later = lambda s: armed.append(s)
        try:
            lifecycle.request_shutdown(force_after=1.5)
        finally:
            lifecycle._force_exit_later = real_force
        self.assertEqual(armed, [1.5])


class ShutdownEndpointTests(unittest.TestCase):
    def setUp(self):
        lifecycle.disarm()                # 清状态 + 取消排队中的延时触发
        self.tmp = Path(tempfile_dir())
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        self.calls = []
        self.saved_hook = lifecycle._hook
        self.saved_flag = lifecycle._shutting_down
        self.saved_force = lifecycle._force_seconds
        lifecycle._hook = lambda: self.calls.append("hook")
        lifecycle._shutting_down = False
        lifecycle._force_seconds = 30
        from cbp.api import app
        self.c = TestClient(app)

    def tearDown(self):
        lifecycle.disarm()           # 作废排队中的兜底定时器
        (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root) = self.saved
        lifecycle._hook = self.saved_hook
        lifecycle._shutting_down = self.saved_flag
        lifecycle._force_seconds = self.saved_force

    def test_endpoint_responds_before_shutting_down(self):
        """先回响应再停：否则前端看到的是连接被重置。"""
        r = self.c.post("/v1/shutdown")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["stopping"])
        self.assertIn("中断", body["note"])
        self.assertEqual(self.calls, [], "响应返回时还不该已经停机")
        # 到点后（0.4s）真正触发
        for _ in range(40):
            if self.calls:
                break
            time.sleep(0.05)
        self.assertEqual(self.calls, ["hook"], "延迟后应触发优雅停机")

    def test_second_call_reports_already(self):
        lifecycle._shutting_down = True
        body = self.c.post("/v1/shutdown").json()
        self.assertTrue(body.get("already"))


def tempfile_dir() -> str:
    import tempfile
    return tempfile.mkdtemp(prefix="cbp_shutdown_")


if __name__ == "__main__":
    unittest.main()
