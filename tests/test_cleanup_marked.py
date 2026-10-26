# -*- coding: utf-8 -*-
"""cleanup-marked 端点的输入校验测试（用 TestClient，浏览器未起时也能跑 400/409 分支）。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gantry.api as api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class CleanupMarkedTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)

    def test_rejects_non_list_sids(self):
        r = self.client.post("/v1/agent/copilot/cleanup-marked", json={"sids": "not-a-list"})
        self.assertEqual(r.status_code, 400)

    def test_rejects_bad_sid_format(self):
        r = self.client.post("/v1/agent/copilot/cleanup-marked",
                             json={"sids": ["../evil", "ok-sid"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("非法 sid", r.json()["detail"])

    def test_409_when_browser_not_started(self):
        # 测试环境没起浏览器 → 409
        r = self.client.post("/v1/agent/copilot/cleanup-marked", json={"sids": ["diag-x"]})
        self.assertEqual(r.status_code, 409)

    def test_scan_only_also_validates_sids(self):
        r = self.client.post("/v1/agent/copilot/cleanup-marked",
                             json={"sids": ["a b"], "scan_only": True})
        self.assertEqual(r.status_code, 400)

    def test_endpoint_shape_exists(self):
        """确认端点已注册，并且用 marker 前缀扫描（两套标记都认）。"""
        src = (ROOT / "gantry" / "routes_sessions.py").read_text(encoding="utf-8")
        self.assertIn("cleanup-marked", src)
        self.assertIn("finalize_session_remote_deletion", src)
        cd = (ROOT / "gantry" / "copilot_delete.py").read_text(encoding="utf-8")
        self.assertIn("scan_sidebar_markers", cd)
        self.assertIn("marker_prefix_regex", cd, "扫描器要两套标记都认")


if __name__ == "__main__":
    unittest.main()
