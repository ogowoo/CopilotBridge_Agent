# -*- coding: utf-8 -*-
"""服务限流倒计时：会话字段持久化 + 锚定首次超限 + 固定窗口。

Copilot 限流是固定窗口（实测约 1 小时），且不会在文案里写明时长，因此
倒计时浮窗锚定"首次超限时间 + service_retry_window_seconds"，而不是每次
退避时长——否则用户会看到每 10 分钟跳一次的假倒数。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.session_store import SessionStore


class RetryCountdownStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_retry_"))
        self.store = SessionStore(self.tmp / "agent.db")
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.store.create("s1", "任务", str(self.ws), "copilot-auto")

    def test_retry_fields_persist(self):
        self.store.patch("s1", state="waiting", retry_resume_at=12345.0,
                         retry_attempt=3, rate_limited_since=10000.0)
        s = self.store.get("s1")
        self.assertEqual(s["retry_resume_at"], 12345.0)
        self.assertEqual(s["retry_attempt"], 3)
        self.assertEqual(s["rate_limited_since"], 10000.0)

    def test_retry_fields_clearable(self):
        self.store.patch("s1", retry_resume_at=12345.0, retry_attempt=3,
                         rate_limited_since=10000.0)
        self.store.patch("s1", retry_resume_at=None, retry_attempt=0,
                         rate_limited_since=None)
        s = self.store.get("s1")
        self.assertIsNone(s["retry_resume_at"])
        self.assertEqual(s["retry_attempt"], 0)
        self.assertIsNone(s["rate_limited_since"])


class RetryWindowAnchorTests(unittest.TestCase):
    """倒计时终点 = rate_limited_since + window，与退避次数无关。"""

    def test_window_anchor(self):
        from cbp.agent import AgentRunner
        r = AgentRunner(None, None, {"service_retry_window_seconds": 3660}, None, None, None, None)
        since = 1_700_000_000.0
        self.assertEqual(since + 3660, since + r.service_retry_window_seconds)

    def test_window_defaults_to_one_hour_plus_one_minute(self):
        from cbp.agent import AgentRunner
        r = AgentRunner(None, None, {}, None, None, None, None)
        self.assertEqual(r.service_retry_window_seconds, 3660)


if __name__ == "__main__":
    unittest.main()
