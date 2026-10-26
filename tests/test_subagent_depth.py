# -*- coding: utf-8 -*-
"""子代理深度自愈：中断残留的记录不能永久堵死子代理。"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry import bridge as b


class DepthTests(unittest.TestCase):
    def setUp(self):
        self.saved = b.AGENT_SESSIONS
        from gantry.session_store import SessionStore
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_depth_"))
        b.AGENT_SESSIONS = SessionStore(self.tmp / "a.db")
        b._SUBAGENT_ACTIVE.clear()

    def tearDown(self):
        b.AGENT_SESSIONS = self.saved
        b._SUBAGENT_ACTIVE.clear()

    def test_running_child_counts(self):
        b.AGENT_SESSIONS.create("sub-x", "t", str(self.tmp), "m")
        b.AGENT_SESSIONS.patch("sub-x", state="running")
        b._SUBAGENT_ACTIVE["sub-x"] = time.time()
        self.assertEqual(b.subagent_depth(), 1)

    def test_finished_child_does_not_count(self):
        b.AGENT_SESSIONS.create("sub-y", "t", str(self.tmp), "m")
        b.AGENT_SESSIONS.patch("sub-y", state="completed")
        b._SUBAGENT_ACTIVE["sub-y"] = time.time()
        self.assertEqual(b.subagent_depth(), 0, "已完成的子代理不该继续占深度")

    def test_stale_entry_expires(self):
        b.AGENT_SESSIONS.create("sub-z", "t", str(self.tmp), "m")
        b.AGENT_SESSIONS.patch("sub-z", state="running")
        b._SUBAGENT_ACTIVE["sub-z"] = time.time() - b._SUBAGENT_STALE_SECONDS - 10
        self.assertEqual(b.subagent_depth(), 0, "超时的残留记录必须自愈")

    def test_missing_session_cleared(self):
        b._SUBAGENT_ACTIVE["sub-gone"] = time.time()
        self.assertEqual(b.subagent_depth(), 0)


if __name__ == "__main__":
    unittest.main()
