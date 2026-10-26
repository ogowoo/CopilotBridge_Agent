"""后端加固：跨站写入拦截、安全响应头、文档鉴权、日志脱敏、runtime 快照一致性。"""
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from starlette.testclient import TestClient as _TC  # noqa: F401

from gantry import api as A

PROBE = "/v1/agent/__hardening_probe__"     # 不存在的路径：只验证中间件是否放行/拦截


class OriginGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(A.app, base_url="http://127.0.0.1:8765")

    def test_cross_site_write_rejected(self):
        for headers in ({"Origin": "http://evil.example"},
                        {"Sec-Fetch-Site": "cross-site"},
                        {"Referer": "http://evil.example/x"}):
            r = self.c.post(PROBE, headers=headers)
            self.assertEqual(r.status_code, 403, f"{headers} 应被拒")
            self.assertIn("拒绝跨站", r.json()["detail"])

    def test_same_origin_write_allowed(self):
        r = self.c.post(PROBE, headers={"Origin": "http://127.0.0.1:8765"})
        self.assertNotEqual(r.status_code, 403, "同源写入不该被拦")

    def test_non_browser_client_allowed(self):
        """curl / 评测脚本不带 Origin，必须放行（否则把自动化也挡了）。"""
        self.assertNotEqual(self.c.post(PROBE).status_code, 403)
        self.assertNotEqual(self.c.post(PROBE, headers={"Origin": "http://localhost:8765"}).status_code, 403)

    def test_other_local_port_rejected(self):
        r = self.c.post(PROBE, headers={"Origin": "http://127.0.0.1:9999"})
        self.assertEqual(r.status_code, 403, "其它端口的本机页面也是跨源")

    def test_reads_not_blocked(self):
        r = self.c.get(PROBE, headers={"Origin": "http://evil.example"})
        self.assertNotEqual(r.status_code, 403, "只拦写方法（读被浏览器同源策略保护）")

    def test_extra_allowed_origins_configurable(self):
        old = A.CSRF_ALLOWED_ORIGINS
        A.CSRF_ALLOWED_ORIGINS = ["http://trusted.example"]
        try:
            self.assertNotEqual(self.c.post(PROBE, headers={"Origin": "http://trusted.example"}).status_code, 403)
        finally:
            A.CSRF_ALLOWED_ORIGINS = old


class SecurityHeaderTests(unittest.TestCase):
    def test_headers_present(self):
        c = TestClient(A.app, base_url="http://127.0.0.1:8765")
        r = c.get("/health")
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(r.headers.get("referrer-policy"), "no-referrer")
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        csp = r.headers.get("content-security-policy") or ""
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)

    def test_ui_page_also_has_headers(self):
        c = TestClient(A.app, base_url="http://127.0.0.1:8765")
        r = c.get("/ui")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")


class DocsAuthTests(unittest.TestCase):
    def test_docs_gated_when_token_configured(self):
        old = A.API_TOKEN
        A.API_TOKEN = "s3cret-token"
        try:
            c = TestClient(A.app, base_url="http://127.0.0.1:8765")
            self.assertEqual(c.get("/openapi.json").status_code, 401)
            self.assertEqual(c.get("/docs").status_code, 401)
            ok = c.get("/openapi.json", params={"token": "s3cret-token"})
            self.assertEqual(ok.status_code, 200)
            ok2 = c.get("/openapi.json", headers={"X-CBP-Token": "s3cret-token"})
            self.assertEqual(ok2.status_code, 200)
            self.assertEqual(c.get("/health").status_code, 200, "健康检查不需要 token")
        finally:
            A.API_TOKEN = old

    def test_docs_open_when_no_token(self):
        old = A.API_TOKEN
        A.API_TOKEN = ""
        try:
            self.assertEqual(TestClient(A.app, base_url="http://127.0.0.1:8765").get("/docs").status_code, 200)
        finally:
            A.API_TOKEN = old


class RedactFilterTests(unittest.TestCase):
    def _record(self, with_dict_args: bool) -> logging.LogRecord:
        if with_dict_args:
            rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                                    '%(client_addr)s - "%(request_line)s"', 
                                    {"client_addr": "127.0.0.1", "request_line":
                                     "GET /v1/agent/spans?token=SUPERSECRET HTTP/1.1"}, None)
        else:
            rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                                    'GET /v1/agent/spans?token=SUPERSECRET', (), None)
        return rec

    def test_redact_dict_args(self):
        """uvicorn 默认格式走 dict args——旧实现只处理 tuple，会把 token 原样写盘。"""
        f = A._RedactTokenFilter()
        rec = self._record(True)
        f.filter(rec)
        self.assertNotIn("SUPERSECRET", rec.getMessage())
        self.assertIn("token=***", rec.getMessage())

    def test_redact_tuple_args(self):
        f = A._RedactTokenFilter()
        rec = self._record(False)
        f.filter(rec)
        self.assertNotIn("SUPERSECRET", rec.getMessage())


class RuntimeSnapshotTests(unittest.TestCase):
    def test_approval_pending_only_exposed_while_waiting(self):
        """审批残留不得外泄。

        真事：会话跑完（completed）后 `approval_pending` 仍留在状态里，UI 就会把已完成的任务
        显示成"需要授权"，而且每秒的 runtime 刷新会把它重新弹出来（点"暂不授权"也没用）。
        现在读取路径只在实际 waiting 时暴露，写入路径（bridge.save）在离开 waiting 时清理。
        """
        pend = {"tool": "delete_path", "args_hash": "abc"}
        waiting = A._runtime_snapshot("sid-x", {"state": "waiting", "approval_pending": pend})
        self.assertEqual(waiting["approval_pending"], pend, "waiting 时必须暴露，否则无法授权")
        for st in ("completed", "failed", "cancelled", "running", "ready"):
            snap = A._runtime_snapshot("sid-x", {"state": st, "approval_pending": pend})
            self.assertIsNone(snap["approval_pending"], f"{st} 不应暴露审批残留")
        snap = A._runtime_snapshot("sid-x", {"state": "completed"})
        self.assertIsNone(snap["approval_pending"])

    def test_snapshot_contains_session_fields(self):
        """SSE 与 HTTP 必须给出同一份事实（缺字段会让界面每秒抖动）。"""
        state = {"state": "running", "total_steps": 7, "conversation_steps": 3,
                 "estimated_tokens": 1234, "conversation_index": 2,
                 "retry_resume_at": 42.0, "retry_attempt": 2, "rate_limited_since": 41.0,
                 "current_url": "https://example.test/chat"}
        snap = A._runtime_snapshot("sid-not-real", state)
        for key in ("session_state", "total_steps", "conversation_steps", "estimated_tokens",
                    "conversation_index", "current_url", "token_budget", "busy",
                    "queue_pending", "retry_resume_at", "retry_attempt", "rate_limited_since"):
            self.assertIn(key, snap, f"快照必须包含 {key}")
        self.assertEqual(snap["session_state"], "running")
        self.assertEqual(snap["retry_resume_at"], 42.0)


if __name__ == "__main__":
    unittest.main()
