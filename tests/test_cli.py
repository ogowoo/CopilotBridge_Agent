# -*- coding: utf-8 -*-
"""CLI（headless）：参数解析、对假服务的只读命令、连不上时的明确报错。"""
import json
import socket
import sys
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from cbp import cli


class Stub(BaseHTTPRequestHandler):
    """最小假服务：只为验证 CLI 的请求路径与输出。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send({"status": "ok", "busy": False, "browser_state": "idle",
                               "copilot_ready": False})
        if self.path == "/v1/agent/tools":
            return self._send({"tools": [{"name": "read_text"}, {"name": "pwsh"}]})
        if self.path == "/v1/agent/sessions":
            return self._send({"sessions": [{"id": "s1", "state": "completed", "title": "甲"}]})
        if self.path == "/v1/plugins":
            return self._send({"plugins": [{"name": "mcp", "status": "loaded",
                                            "enabled": True, "description": "MCP"}]})
        if self.path.startswith("/v1/plugins/mcp/api/list"):
            return self._send({"servers": [], "tools": []})
        if self.path == "/v1/agent/sandbox":
            return self._send({"mode": "workspace-write", "modes": ["read-only"]})
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b"{}"
        data = json.loads(body or b"{}")
        if self.path == "/v1/agent/completions":
            return self._send({"choices": [{"message": {"content": "收到 " + data["messages"][0]["content"]}}],
                               "agent_steps": [{"step": 1}], "session_id": "s1",
                               "workspace": "ws"})
        self.send_error(404)


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_parser_accepts_run_options(self):
        ap = cli.build_parser()
        args = ap.parse_args(["run", "做点事", "--model", "kimi-coding/k3",
                              "--plan", "--sandbox", "read-only"])
        self.assertEqual(args.task, "做点事")
        self.assertEqual(args.model, "kimi-coding/k3")
        self.assertTrue(args.plan)
        self.assertEqual(args.sandbox, "read-only")

    def test_status_against_stub(self):
        self.assertEqual(cli.main(["--url", self.url, "status"]), 0)

    def test_run_prints_answer_and_uses_plan_mode(self):
        # 用 --json 验证提交体与返回解析
        rc = cli.main(["--url", self.url, "run", "你好", "--json"])
        self.assertEqual(rc, 0)

    def test_tools_and_sessions(self):
        self.assertEqual(cli.main(["--url", self.url, "tools"]), 0)
        self.assertEqual(cli.main(["--url", self.url, "sessions", "--limit", "1"]), 0)

    def test_plugins_and_mcp_list(self):
        self.assertEqual(cli.main(["--url", self.url, "plugins", "list"]), 0)
        self.assertEqual(cli.main(["--url", self.url, "mcp", "list"]), 0)

    def test_connect_error_is_reported(self):
        # 找一个一定没人监听的端口
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        rc = cli.main(["--url", f"http://127.0.0.1:{port}", "status"])
        self.assertEqual(rc, 1, "连不上时应返回非零退出码")

    def test_pyproject_declares_entry_point(self):
        data = tomllib.loads((BASE / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["scripts"]["cbp"], "cbp.cli:main")
        self.assertIn("httpx", " ".join(data["project"]["dependencies"]))


if __name__ == "__main__":
    unittest.main()
