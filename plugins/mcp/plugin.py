# -*- coding: utf-8 -*-
"""MCP 客户端插件（stdio + streamable HTTP/SSE 两种传输，含 resources）。

把外部 MCP server 的工具注册成 Agent 工具，命名 `mcp__<server>__<tool>`；
资源（resources）额外提供一个只读工具 `mcp__<server>__read_resource`。

协议要点（2024-11-05 / 2025-03-26 streamable HTTP）：
- stdio：一行一条 JSON-RPC 消息
- HTTP：POST 单端点，响应可以是 application/json，也可以是 text/event-stream（SSE）
- 握手 initialize → notifications/initialized → tools/list → resources/list
- 结果：tools/call 返回 {content:[{type:'text',text}], isError}
        resources/read 返回 {contents:[{uri,mimeType,text|blob}]}
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time

PROTOCOL = "2024-11-05"
_CLIENT_INFO = {"name": "gantry-mcp", "version": "0.2.0"}
_REQ_TIMEOUT = 30.0


class McpError(RuntimeError):
    pass


def _safe(name: str) -> str:
    """工具名只能是 [A-Za-z0-9_]：MCP 工具名可能带 - . / 等字符。"""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name or "")).strip("_") or "tool"


def _schema_to_args(schema: dict | None) -> dict[str, str]:
    """MCP 的 inputSchema（JSON Schema）→ 我们注册表用的 {参数名: 说明}。"""
    props = (schema or {}).get("properties") or {}
    out: dict[str, str] = {}
    for key, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        desc = str(spec.get("description") or "").strip()
        typ = str(spec.get("type") or "").strip()
        if not desc:
            desc = f"{typ} 参数" if typ else "参数"
        out[str(key)] = desc[:200]
    return out


def _content_text(result: dict) -> str:
    """把 tools/call 的 content 数组拼成文本。"""
    parts: list[str] = []
    for item in (result or {}).get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        elif item.get("type") == "resource" and isinstance(item.get("resource"), dict):
            parts.append(str(item["resource"].get("text") or ""))
    return "\n".join(p for p in parts if p)


def _resource_text(result: dict) -> str:
    """把 resources/read 的 contents 数组拼成文本（blob 只给出 mime/长度）。"""
    parts: list[str] = []
    for item in (result or {}).get("contents") or []:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri") or ""
        if item.get("text") is not None:
            parts.append(f"# {uri}\n{item.get('text')}")
        elif item.get("blob") is not None:
            raw = str(item.get("blob"))
            parts.append(f"# {uri}\n（二进制 {item.get('mimeType') or 'application/octet-stream'}，"
                         f"base64 {len(raw)} 字符）")
        else:
            parts.append(f"# {uri}\n（空）")
    return "\n\n".join(parts)


# ────────────────────────── stdio 传输 ──────────────────────────

class McpStdioClient:
    """一个 stdio MCP server 的连接（按需启动、按需关闭）。"""

    transport = "stdio"

    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, cwd: str | None = None,
                 timeout: float = _REQ_TIMEOUT):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self.timeout = float(timeout or _REQ_TIMEOUT)
        self.proc: asyncio.subprocess.Process | None = None
        self.tools: list[dict] = []
        self.resources: list[dict] = []
        self.prompts: list[dict] = []
        self.error: str | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._pump_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self.stderr_tail: list[str] = []

    async def start(self) -> None:
        exe = shutil.which(self.command) or self.command
        env = {**os.environ, **self.env}
        env.setdefault("PYTHONIOENCODING", "utf-8")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = await asyncio.create_subprocess_exec(
            exe, *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env, cwd=self.cwd, **kwargs)
        self._pump_task = asyncio.create_task(self._pump())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._handshake()

    async def close(self) -> None:
        tasks = [t for t in (self._pump_task, self._stderr_task) if t]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pump_task = self._stderr_task = None
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=1)
        except Exception:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=1)
            except Exception:
                pass
        # Windows 的 proactor 管道不显式关会在 GC 时刷 "unclosed transport" 警告。
        try:
            st = getattr(proc, "_transport", None)
            if st is not None:
                st.close()
        except Exception:
            pass

    async def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            try:
                line = await self.proc.stdout.readline()
            except Exception:
                break
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None and mid in self._pending:
                fut = self._pending.pop(mid)
                if not fut.done():
                    fut.set_result(msg)

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            try:
                line = await self.proc.stderr.readline()
            except Exception:
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self.stderr_tail.append(text[-300:])
                del self.stderr_tail[:-20]

    async def _write(self, payload: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise McpError(f"{self.name}: 进程未启动")
        self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def _notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)

    async def request(self, method: str, params: dict | None = None,
                      timeout: float | None = None) -> dict:
        self._id += 1
        rid = self._id
        limit = float(timeout or self.timeout)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            await self._write(payload)
            msg = await asyncio.wait_for(fut, timeout=limit)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            tail = ("；stderr: " + " | ".join(self.stderr_tail[-3:])) if self.stderr_tail else ""
            raise McpError(f"{self.name}: {method} 超时 {limit:.0f}s{tail}")
        except Exception as exc:
            self._pending.pop(rid, None)
            raise McpError(f"{self.name}: {method} 发送失败: {exc}") from exc
        if isinstance(msg, dict) and msg.get("error"):
            err = msg["error"]
            raise McpError(f"{self.name}: {method} 返回错误: "
                           f"{err.get('message') if isinstance(err, dict) else err}")
        return msg.get("result") or {}

    # ── 公共业务（两种传输共用）──
    async def _handshake(self) -> None:
        await self.request("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        })
        try:
            await self._notify("notifications/initialized")
        except Exception:
            pass
        await self._load_tools()

    async def _load_tools(self) -> None:
        data = await self.request("tools/list")
        tools = (data or {}).get("tools") or []
        self.tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        try:
            # 可选能力：没实现 resources 的 server 可能回 -32601，也可能干脆不回。
            # 用短超时探测，避免连接阶段被一个可选能力拖住 30 秒。
            rdata = await self.request("resources/list", timeout=min(self.timeout, 8.0))
        except Exception:
            self.resources = []          # server 没实现 resources 是正常的
        else:
            items = (rdata or {}).get("resources") or []
            self.resources = [r for r in items if isinstance(r, dict) and r.get("uri")]
        try:
            pdata = await self.request("prompts/list", timeout=min(self.timeout, 8.0))
        except Exception:
            self.prompts = []            # 同上：prompts 也是可选能力
        else:
            pitems = (pdata or {}).get("prompts") or []
            self.prompts = [p for p in pitems if isinstance(p, dict) and p.get("name")]
        self.error = None

    async def list_resources(self) -> list[dict]:
        return self.resources

    async def get_prompt(self, name: str, arguments: dict | None = None) -> dict:
        """取 prompt 模板并渲染成消息序列。"""
        result = await self.request("prompts/get", {"name": str(name),
                                                    "arguments": arguments or {}})
        messages = (result or {}).get("messages") or []
        parts: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            text = ""
            if isinstance(content, dict):
                text = str(content.get("text") or "")
            elif isinstance(content, list):
                text = "\n".join(str(c.get("text") or "") for c in content
                                 if isinstance(c, dict))
            elif content is not None:
                text = str(content)
            if text:
                parts.append(f"[{msg.get('role') or 'user'}] {text}")
        if not parts:
            raise McpError(f"{self.name}: prompt {name} 没有内容")
        return {"name": str(name),
                "description": str((result or {}).get("description") or ""),
                "text": "\n\n".join(parts)}

    async def call_tool(self, tool: str, arguments: dict | None = None) -> dict:
        result = await self.request("tools/call", {"name": tool,
                                                   "arguments": arguments or {}})
        text = _content_text(result)
        if result.get("isError"):
            raise McpError(text or f"{self.name}:{tool} 返回 isError")
        return {"text": text, "raw": result, "isError": False}

    async def read_resource(self, uri: str) -> dict:
        result = await self.request("resources/read", {"uri": str(uri)})
        text = _resource_text(result)
        if not text:
            raise McpError(f"{self.name}: 资源 {uri} 内容为空")
        return {"uri": str(uri), "text": text}


# ────────────────────── streamable HTTP 传输 ──────────────────────

class McpHttpClient(McpStdioClient):
    """MCP over HTTP（streamable HTTP；兼容把响应写成 text/event-stream）。

    复用 stdio 客户端的握手/业务方法，只覆盖底层收发。
    """

    transport = "http"

    def __init__(self, name: str, url: str, headers: dict[str, str] | None = None,
                 timeout: float = _REQ_TIMEOUT):
        super().__init__(name, command="", args=[], env={}, cwd=None, timeout=timeout)
        self.url = str(url or "").strip()
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self.session_id: str | None = None
        self._http = None

    async def start(self) -> None:
        import httpx
        if not self.url:
            raise McpError(f"{self.name}: 需要 url")
        self._http = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        await self._handshake()

    async def close(self) -> None:
        http, self._http = self._http, None
        if http is not None:
            try:
                await http.aclose()
            except Exception:
                pass

    async def _notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._post(payload, expect_id=None)

    async def request(self, method: str, params: dict | None = None,
                      timeout: float | None = None) -> dict:
        self._id += 1
        rid = self._id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        msg = await self._post(payload, expect_id=rid, timeout=timeout)
        if isinstance(msg, dict) and msg.get("error"):
            err = msg["error"]
            raise McpError(f"{self.name}: {method} 返回错误: "
                           f"{err.get('message') if isinstance(err, dict) else err}")
        return (msg or {}).get("result") or {}

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream",
             "Content-Type": "application/json",
             "MCP-Protocol-Version": PROTOCOL}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        h.update(self.headers)
        return h

    async def _post(self, payload: dict, expect_id: int | None,
                    timeout: float | None = None) -> dict | None:
        if self._http is None:
            raise McpError(f"{self.name}: HTTP 客户端未初始化")
        kwargs = {"timeout": float(timeout)} if timeout else {}
        try:
            resp = await self._http.post(self.url, json=payload, headers=self._headers(),
                                         **kwargs)
        except Exception as exc:
            raise McpError(f"{self.name}: HTTP 请求失败: {exc}") from exc
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if resp.status_code >= 400:
            raise McpError(f"{self.name}: HTTP {resp.status_code}: {resp.text[:200]}")
        ctype = (resp.headers.get("content-type") or "").lower()
        if expect_id is None:
            return None
        if "text/event-stream" in ctype:
            return await self._read_sse(resp, expect_id)
        try:
            data = resp.json()
        except Exception:
            # 有些实现把 SSE 写成普通文本
            return self._parse_sse_text(resp.text, expect_id)
        if isinstance(data, list):            # 批量响应
            for item in data:
                if isinstance(item, dict) and item.get("id") == expect_id:
                    return item
            return (data[0] if data else {})
        return data

    async def _read_sse(self, resp, expect_id: int) -> dict:
        async for line in resp.aiter_lines():
            msg = self._sse_line(line)
            if msg is not None and msg.get("id") == expect_id:
                return msg
        raise McpError(f"{self.name}: SSE 流里没有 id={expect_id} 的响应")

    @staticmethod
    def _sse_line(line: str) -> dict | None:
        if not line or not line.startswith("data:"):
            return None
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            return None
        try:
            msg = json.loads(raw)
        except Exception:
            return None
        return msg if isinstance(msg, dict) else None

    @classmethod
    def _parse_sse_text(cls, text: str, expect_id: int) -> dict | None:
        for line in (text or "").splitlines():
            msg = cls._sse_line(line)
            if msg is not None and msg.get("id") == expect_id:
                return msg
        raise McpError("HTTP 响应既不是 JSON 也不是可解析的 SSE")


# ────────────────────────── 管理器 ──────────────────────────

class McpManager:
    """管理若干 MCP server 连接（不含事件循环假设，便于单测）。"""

    def __init__(self, servers: dict | None = None):
        self.servers: dict[str, dict] = dict(servers or {})
        self.clients: dict[str, McpStdioClient] = {}
        self.errors: dict[str, str] = {}

    def add(self, name: str, spec: dict) -> dict:
        name = str(name or "").strip()
        if not name:
            raise McpError("server 名不能为空")
        spec = spec or {}
        url = str(spec.get("url") or "").strip()
        cmd = str(spec.get("command") or "").strip()
        if not url and not cmd:
            raise McpError(f"{name}: 需要 command（stdio）或 url（HTTP）")
        entry = {
            "transport": "http" if url else "stdio",
            "command": cmd,
            "args": [str(a) for a in (spec.get("args") or [])],
            "env": {str(k): str(v) for k, v in (spec.get("env") or {}).items()},
            "cwd": (spec.get("cwd") or None),
            "url": url,
            "headers": {str(k): str(v) for k, v in (spec.get("headers") or {}).items()},
            "timeout": float(spec.get("timeout") or _REQ_TIMEOUT),
            "enabled": bool(spec.get("enabled", True)),
        }
        self.servers[name] = entry
        return {"name": name, **entry}

    def remove(self, name: str) -> bool:
        existed = name in self.servers
        self.servers.pop(name, None)
        return existed

    async def connect(self, name: str) -> McpStdioClient:
        spec = self.servers.get(name)
        if not spec:
            raise McpError(f"没有名为 {name} 的 MCP server")
        old = self.clients.pop(name, None)
        if old is not None:
            await old.close()
        if spec.get("transport") == "http":
            client: McpStdioClient = McpHttpClient(
                name, spec.get("url") or "", spec.get("headers"),
                spec.get("timeout", _REQ_TIMEOUT))
        else:
            client = McpStdioClient(name, spec["command"], spec.get("args"),
                                    spec.get("env"), spec.get("cwd"),
                                    spec.get("timeout", _REQ_TIMEOUT))
        try:
            await client.start()
        except Exception as exc:
            await client.close()
            self.errors[name] = str(exc)
            raise
        self.errors.pop(name, None)
        self.clients[name] = client
        return client

    async def connect_all(self) -> dict[str, str]:
        for name, spec in self.servers.items():
            if not spec.get("enabled", True):
                continue
            try:
                await self.connect(name)
            except Exception:
                pass          # 单个 server 失败不影响其它；错误留在 self.errors
        return dict(self.errors)

    async def close_all(self) -> None:
        for client in list(self.clients.values()):
            await client.close()
        self.clients.clear()

    def tool_specs(self) -> list[dict]:
        """展平所有 server 的工具（含每个 server 一个只读资源工具）。"""
        out = []
        for name, client in self.clients.items():
            for tool in client.tools:
                tname = _safe(tool.get("name"))
                out.append({
                    "server": name,
                    "tool": str(tool.get("name")),
                    "registered_name": f"mcp__{_safe(name)}__{tname}",
                    "description": str(tool.get("description") or "").strip()
                                   or f"MCP {name} 的 {tool.get('name')} 工具",
                    "arguments": _schema_to_args(tool.get("inputSchema")),
                })
            if client.prompts:
                names = ", ".join(str(x.get("name")) for x in client.prompts[:8])
                out.append({
                    "server": name,
                    "tool": "use_prompt",
                    "registered_name": f"mcp__{_safe(name)}__use_prompt",
                    "description": (f"用 MCP server {name} 的 prompt 模板生成提示词。"
                                    f"可用模板: {names}"),
                    "arguments": {"name": "模板名（见说明里列出的可用项）",
                                  "arguments": "模板参数对象（按模板声明传）"},
                })
            if client.resources:
                uris = ", ".join(str(r.get("uri")) for r in client.resources[:8])
                out.append({
                    "server": name,
                    "tool": "read_resource",
                    "registered_name": f"mcp__{_safe(name)}__read_resource",
                    "description": (f"读取 MCP server {name} 的资源（只读）。"
                                    f"可用 uri: {uris}"),
                    "arguments": {"uri": "资源 URI（见工具说明里列出的可用项）"},
                })
        return out

    def prompt_specs(self) -> list[dict]:
        out = []
        for name, client in self.clients.items():
            for pr in client.prompts:
                out.append({
                    "server": name,
                    "name": str(pr.get("name")),
                    "description": str(pr.get("description") or "").strip(),
                    "arguments": [{"name": str(a.get("name")),
                                   "description": str(a.get("description") or ""),
                                   "required": bool(a.get("required"))}
                                  for a in (pr.get("arguments") or [])
                                  if isinstance(a, dict) and a.get("name")],
                })
        return out

    async def get_prompt(self, server: str, name: str,
                         arguments: dict | None = None) -> dict:
        client = self.clients.get(server)
        if client is None:
            raise McpError(f"MCP server 未连接: {server}")
        return await client.get_prompt(name, arguments)

    async def call(self, server: str, tool: str, arguments: dict | None = None) -> dict:
        client = self.clients.get(server)
        if client is None:
            raise McpError(f"MCP server 未连接: {server}")
        if tool == "read_resource":
            uri = str((arguments or {}).get("uri") or "").strip()
            if not uri:
                raise McpError("read_resource 需要 uri")
            return await client.read_resource(uri)
        if tool == "use_prompt":
            name = str((arguments or {}).get("name") or "").strip()
            if not name:
                raise McpError("use_prompt 需要 name")
            args = (arguments or {}).get("arguments")
            return await client.get_prompt(name, args if isinstance(args, dict) else {})
        return await client.call_tool(tool, arguments)

    def resource_specs(self) -> list[dict]:
        out = []
        for name, client in self.clients.items():
            for res in client.resources:
                out.append({"server": name,
                            "uri": str(res.get("uri")),
                            "name": str(res.get("name") or ""),
                            "mimeType": str(res.get("mimeType") or ""),
                            "description": str(res.get("description") or "")})
        return out


# ────────────────────────── 插件入口 ──────────────────────────

def setup(ctx):
    manager = McpManager(ctx.state.get("servers") or {})
    ctx._mcp = manager
    ctx._mcp_tools = set()

    def _snapshot() -> dict:
        return {
            "servers": [
                {"name": name,
                 "transport": spec.get("transport"),
                 "command": spec.get("command") or None,
                 "args": spec.get("args") or [],
                 "url": spec.get("url") or None,
                 "enabled": spec.get("enabled", True),
                 "connected": name in manager.clients,
                 "error": manager.errors.get(name),
                 # 注意：client.tools 是 MCP 原始工具字典（name/description/inputSchema），
                 # 不是 tool_specs() 的展平结构——这里曾取 t["tool"] 直接 KeyError → 500
                 "tools": [str(t.get("name")) for t in (manager.clients.get(name).tools
                                                        if name in manager.clients else [])],
                 "resources": [str(r.get("uri")) for r in (manager.clients.get(name).resources
                                                           if name in manager.clients else [])],
                 "prompts": [str(x.get("name")) for x in (manager.clients.get(name).prompts
                                                          if name in manager.clients else [])]}
                for name, spec in manager.servers.items()
            ],
            "tools": manager.tool_specs(),
            "resources": manager.resource_specs(),
            "prompts": manager.prompt_specs(),
        }

    async def _connect_and_register():
        await manager.connect_all()
        _register_dynamic(ctx, manager)
        return _snapshot()

    async def _list():
        return _snapshot()

    async def _call(server: str, tool: str, arguments: dict | None = None):
        return await manager.call(server, tool, arguments or {})

    async def _read_resource(server: str, uri: str):
        return await manager.call(server, "read_resource", {"uri": uri})

    ctx.register_tool("mcp_list", _list,
                      "列出已配置的 MCP server、其工具与资源（含连接状态与错误）", {})
    ctx.register_tool("mcp_call", _call,
                      "调用 MCP server 的工具（server/tool/arguments）",
                      {"server": "MCP server 名", "tool": "工具名",
                       "arguments": "参数对象"})
    ctx.register_tool("mcp_read_resource", _read_resource,
                      "读取 MCP server 的资源（只读）",
                      {"server": "MCP server 名", "uri": "资源 URI"})
    async def _get_prompt(server: str, name: str, arguments: dict | None = None):
        return await manager.get_prompt(server, name, arguments or {})

    ctx.register_tool("mcp_get_prompt", _get_prompt,
                      "用 MCP server 的 prompt 模板生成提示词（server/name/arguments）",
                      {"server": "MCP server 名", "name": "模板名",
                       "arguments": "模板参数对象"})

    # ── 管理接口（加/删/重连/直调/看资源）──
    async def _api_list(request):
        return _snapshot()

    async def _api_add(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = str((body or {}).get("name") or "")
        spec = (body or {}).get("spec") or {}
        manager.add(name, spec)
        ctx.save_state(servers=manager.servers)
        try:
            await manager.connect(name)
        except Exception as exc:
            return {"name": name, "connected": False, "error": str(exc)}
        _register_dynamic(ctx, manager)
        return {"name": name, "connected": True, "tools": _snapshot()["tools"]}

    async def _api_remove(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = str((body or {}).get("name") or "")
        client = manager.clients.pop(name, None)
        if client is not None:
            await client.close()
        removed = manager.remove(name)
        ctx.save_state(servers=manager.servers)
        _register_dynamic(ctx, manager)      # 摘掉该 server 的工具
        return {"removed": removed, "name": name}

    async def _api_reload(request):
        await manager.close_all()
        return await _connect_and_register()

    async def _api_call(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return await manager.call(str(body.get("server") or ""),
                                      str(body.get("tool") or ""),
                                      body.get("arguments") or {})
        except Exception as exc:
            return {"error": str(exc)}

    async def _api_resources(request):
        server = str(request.query_params.get("server") or "")
        client = manager.clients.get(server)
        if client is None:
            return {"error": f"MCP server 未连接: {server}"}
        uri = str(request.query_params.get("uri") or "").strip()
        try:
            if uri:
                return await client.read_resource(uri)
            return {"resources": client.resources}
        except Exception as exc:
            return {"error": str(exc)}

    ctx.register_panel({"id": "servers", "title": "MCP 服务器", "view": "json",
                        "api": {"method": "GET", "path": "api/list"},
                        "hint": "连接状态、工具与资源"})
    ctx.register_api("GET", "api/list", _api_list)
    ctx.register_api("POST", "api/add", _api_add)
    ctx.register_api("POST", "api/remove", _api_remove)
    ctx.register_api("POST", "api/reload", _api_reload)
    ctx.register_api("POST", "api/call", _api_call)
    ctx.register_api("GET", "api/resources", _api_resources)

    async def _api_prompts(request):
        """列出全部 prompt 模板；带 server+name+arguments 时直接渲染。"""
        name = str(request.query_params.get("name") or "").strip()
        server = str(request.query_params.get("server") or "").strip()
        if name and server:
            raw = request.query_params.get("arguments") or ""
            try:
                args = json.loads(raw) if raw else {}
            except Exception:
                args = {}
            try:
                return await manager.get_prompt(server, name, args if isinstance(args, dict) else {})
            except Exception as exc:
                return {"error": str(exc)}
        return {"prompts": manager.prompt_specs()}

    async def _api_prompt(request):
        """渲染指定 prompt：{server, name, arguments} → {text}。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return await manager.get_prompt(str(body.get("server") or ""),
                                            str(body.get("name") or ""),
                                            body.get("arguments") or {})
        except Exception as exc:
            return {"error": str(exc)}

    ctx.register_api("GET", "api/prompts", _api_prompts)
    ctx.register_api("POST", "api/prompt", _api_prompt)

    # 启动即连接（需要事件循环；无循环时只记日志，工具仍可手动调 api/reload）
    try:
        ctx._mcp_task = asyncio.ensure_future(_connect_and_register())
    except RuntimeError:
        ctx._mcp_task = None
        ctx.log("mcp: 无事件循环，未自动连接（可用 POST api/reload）")
    ctx.log(f"mcp 已就绪（{len(manager.servers)} 个 server 配置）")


def _register_dynamic(ctx, manager: McpManager) -> None:
    """把 MCP 工具/资源**对账**进工具表：缺的补上，多余的摘掉。

    之前只加不减：移除 server 后它的工具还留在表里，指向已关闭的连接，
    模型调用只会得到"未连接"（实测遗留过 4 个 mcp__mock__* 幽灵工具）。
    """
    desired = {item["registered_name"] for item in manager.tool_specs()}
    known = getattr(ctx, "_mcp_tools", set()) or set()
    for stale in known - desired:
        try:
            reg = getattr(getattr(ctx, "host", None), "registry", None)
            if reg is not None and hasattr(reg, "unregister"):
                reg.unregister(stale)
        except Exception:
            pass
        try:
            ctx.registered["tools"].remove(stale)
        except Exception:
            pass
    ctx._mcp_tools = set(desired)

    for item in manager.tool_specs():
        rname, server, tool = item["registered_name"], item["server"], item["tool"]

        async def _fn(arguments=None, _s=server, _t=tool, **kwargs):
            payload = arguments if isinstance(arguments, dict) else dict(kwargs)
            try:
                res = await manager.call(_s, _t, payload)
            except Exception as exc:
                return {"error": str(exc)}
            return {"text": res.get("text", ""), "isError": res.get("isError", False)}

        ctx.register_tool(rname, _fn, item["description"], item["arguments"])


def teardown(ctx):
    manager = getattr(ctx, "_mcp", None)
    task = getattr(ctx, "_mcp_task", None)
    if task is not None:
        try:
            task.cancel()
        except Exception:
            pass
    if manager is not None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(manager.close_all())
        except Exception:
            pass
    ctx.log("mcp 已卸载")
