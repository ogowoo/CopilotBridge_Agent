# -*- coding: utf-8 -*-
"""最小 MCP **HTTP** server（streamable HTTP），用于测试 HTTP 传输与 resources。

- POST /mcp：JSON-RPC；普通方法回 application/json，`tools/call` 故意回 SSE
  （text/event-stream），用来验证客户端的 SSE 解析路径。
- initialize 响应带 `Mcp-Session-Id` 头，客户端后续请求应带回（服务端会校验）。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {"name": "echo", "description": "原样返回 text",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "要回显的文本"}}}},
    {"name": "add", "description": "两数相加",
     "inputSchema": {"type": "object", "properties": {
         "a": {"type": "number"}, "b": {"type": "number"}}}},
]

RESOURCES = [
    {"uri": "mock://readme", "name": "readme", "mimeType": "text/plain",
     "description": "示例资源"},
]

SESSIONS: set[str] = set()


def _rpc(msg: dict) -> dict | None:
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "mock-mcp-http", "version": "0.0.1"}}}
    if method == "notifications/initialized":
        return None
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": [
            {"name": "greet", "description": "打招呼模板",
             "arguments": [{"name": "who", "description": "对象", "required": True}]},
        ]}}
    if method == "prompts/get":
        who = str(((msg.get("params") or {}).get("arguments") or {}).get("who") or "")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "description": "打招呼模板",
            "messages": [{"role": "user",
                          "content": {"type": "text", "text": f"请向 {who} 打招呼"}}]}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": RESOURCES}}
    if method == "resources/read":
        uri = (msg.get("params") or {}).get("uri")
        if uri != "mock://readme":
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown resource {uri}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": {"contents": [
            {"uri": uri, "mimeType": "text/plain", "text": "MCP 资源内容 OK"}]}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": str(args.get("text", ""))}],
                "isError": False}}
        if name == "add":
            try:
                total = float(args.get("a", 0)) + float(args.get("b", 0))
            except Exception:
                total = 0
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": str(total)}], "isError": False}}
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"unknown tool {name}"}}
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"unknown method {method}"}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):        # 静音
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_error(400, "bad json")
            return
        method = msg.get("method")

        # 会话校验：initialize 之外必须带上服务端发的 session id
        sid = self.headers.get("Mcp-Session-Id")
        if method == "initialize":
            new_sid = "sess-test-1"
            SESSIONS.add(new_sid)
        elif method == "notifications/initialized":
            # 通知没有响应体
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            if sid not in SESSIONS:
                self.send_error(400, "missing/invalid session")
                return

        payload = _rpc(msg)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else b""
        if method == "tools/call" and payload:
            # 故意用 SSE 返回，验证客户端的流式解析
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            data = b"data: " + body + b"\n\n"
            self.send_header("Content-Length", str(len(data)))
            if method == "initialize":
                self.send_header("Mcp-Session-Id", "sess-test-1")
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if method == "initialize":
            self.send_header("Mcp-Session-Id", new_sid)
        self.end_headers()
        self.wfile.write(body)


class MockMcpHttp:
    def __init__(self, port: int = 0):
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/mcp"

    def start(self) -> str:
        self.thread.start()
        return self.url

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
