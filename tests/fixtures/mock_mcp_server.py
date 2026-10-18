# -*- coding: utf-8 -*-
"""最小 MCP server（stdio），只用于测试 MCP 客户端插件。

协议：一行一条 JSON-RPC 消息；支持 initialize / notifications/initialized /
tools/list / tools/call / shutdown。工具：echo（把 text 原样返回）、add（两数相加）、
boom（返回 isError，用于测错误路径）。
"""
import json
import sys


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    tools = [
        {"name": "echo", "description": "原样返回 text",
         "inputSchema": {"type": "object", "properties": {
             "text": {"type": "string", "description": "要回显的文本"}}}},
        {"name": "add", "description": "两数相加",
         "inputSchema": {"type": "object", "properties": {
             "a": {"type": "number"}, "b": {"type": "number"}}}},
        {"name": "boom", "description": "总是失败（测错误路径）",
         "inputSchema": {"type": "object", "properties": {}}},
    ]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid = msg.get("id")
        method = msg.get("method")

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-mcp", "version": "0.0.1"}}})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                _send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": str(args.get("text", ""))}],
                    "isError": False}})
            elif name == "add":
                try:
                    total = float(args.get("a", 0)) + float(args.get("b", 0))
                except Exception:
                    total = 0
                _send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": str(total)}], "isError": False}})
            elif name == "boom":
                _send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "故意失败"}], "isError": True}})
            else:
                _send({"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32601, "message": f"unknown tool {name}"}})
        elif method == "shutdown":
            _send({"jsonrpc": "2.0", "id": mid, "result": {}})
            return 0
        elif mid is not None:
            # 真实 MCP server 对未实现的方法回 -32601；不回会让客户端干等到超时
            _send({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32601, "message": f"method not found: {method}"}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
