# -*- coding: utf-8 -*-
"""MCP 客户端插件（stdio 传输）。

把外部 MCP server 的工具注册成 Agent 工具，命名 `mcp__<server>__<tool>`。

协议要点（2024-11-05）：
- stdio 上一条消息一行 JSON（换行分隔，消息内不能有裸换行）
- 握手 initialize → 通知 notifications/initialized → tools/list → tools/call
- 结果形如 {"content":[{"type":"text","text":"..."}],"isError":bool}

为什么用 stdio：本地 MCP server 几乎都是 stdio（npx/uvx 启动的进程），
不需要额外端口与鉴权；HTTP 传输后续可在同一 manager 上加。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time

PROTOCOL = "2024-11-05"
_CLIENT_INFO = {"name": "cbp-mcp", "version": "0.1.0"}
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


class McpStdioClient:
    """一个 stdio MCP server 的连接（按需启动、按需关闭）。"""

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
        self.error: str | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._pump_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self.stderr_tail: list[str] = []

    # ── 生命周期 ──
    async def start(self) -> None:
        exe = shutil.which(self.command) or self.command
        env = {**os.environ, **self.env}
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(
                __import__("subprocess"), "CREATE_NO_WINDOW", 0)
        self.proc = await asyncio.create_subprocess_exec(
            exe, *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env, cwd=self.cwd, **kwargs)
        self._pump_task = asyncio.create_task(self._pump())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self.request("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        })
        await self._notify("notifications/initialized")
        data = await self.request("tools/list")
        tools = (data or {}).get("tools") or []
        self.tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        self.error = None

    async def close(self) -> None:
        for task in (self._pump_task, self._stderr_task):
            if task is not None:
                task.cancel()
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
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        # Windows 的 proactor 管道不显式关会在 GC 时刷 "unclosed transport" 警告。
        # 子进程 transport 覆盖三条管道（stdin/stdout/stderr），关它最彻底。
        try:
            st = getattr(proc, "_transport", None)
            if st is not None:
                st.close()
        except Exception:
            pass

    # ── 收发 ──
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

    async def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        rid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            await self._write(payload)
            msg = await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            tail = ("；stderr: " + " | ".join(self.stderr_tail[-3:])) if self.stderr_tail else ""
            raise McpError(f"{self.name}: {method} 超时 {self.timeout:.0f}s{tail}")
        except Exception as exc:
            self._pending.pop(rid, None)
            raise McpError(f"{self.name}: {method} 发送失败: {exc}") from exc
        if isinstance(msg, dict) and msg.get("error"):
            err = msg["error"]
            raise McpError(f"{self.name}: {method} 返回错误: "
                           f"{err.get('message') if isinstance(err, dict) else err}")
        return msg.get("result") or {}

    # ── 业务 ──
    async def call_tool(self, tool: str, arguments: dict | None = None) -> dict:
        result = await self.request("tools/call", {"name": tool,
                                                   "arguments": arguments or {}})
        texts = []
        for item in (result.get("content") or []):
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
        text = "\n".join(t for t in texts if t)
        if result.get("isError"):
            raise McpError(text or f"{self.name}:{tool} 返回 isError")
        return {"text": text, "raw": result, "isError": bool(result.get("isError"))}


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
        cmd = str((spec or {}).get("command") or "").strip()
        if not cmd:
            raise McpError(f"{name}: 需要 command")
        entry = {"command": cmd,
                 "args": [str(a) for a in (spec.get("args") or [])],
                 "env": {str(k): str(v) for k, v in (spec.get("env") or {}).items()},
                 "cwd": (spec.get("cwd") or None),
                 "timeout": float(spec.get("timeout") or _REQ_TIMEOUT),
                 "enabled": bool(spec.get("enabled", True))}
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
        client = McpStdioClient(name, spec["command"], spec.get("args"), spec.get("env"),
                                spec.get("cwd"), spec.get("timeout", _REQ_TIMEOUT))
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
        """展平所有 server 的工具，供注册进工具表。"""
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
        return out

    async def call(self, server: str, tool: str, arguments: dict | None = None) -> dict:
        client = self.clients.get(server)
        if client is None:
            raise McpError(f"MCP server 未连接: {server}")
        return await client.call_tool(tool, arguments)


# ────────────────────────── 插件入口 ──────────────────────────

def setup(ctx):
    manager = McpManager(ctx.state.get("servers") or {})
    ctx._mcp = manager

    def _snapshot() -> dict:
        return {
            "servers": [
                {"name": name,
                 "command": spec.get("command"),
                 "args": spec.get("args") or [],
                 "enabled": spec.get("enabled", True),
                 "connected": name in manager.clients,
                 "error": manager.errors.get(name),
                 # 注意：client.tools 是 MCP 原始工具字典（键是 name/description/inputSchema），
                 # 不是 tool_specs() 的展平结构——这里曾取 t["tool"] 直接 KeyError → 500
                 "tools": [str(t.get("name")) for t in (manager.clients.get(name).tools
                                                        if name in manager.clients else [])]}
                for name, spec in manager.servers.items()
            ],
            "tools": manager.tool_specs(),
        }

    async def _connect_and_register():
        await manager.connect_all()
        _register_dynamic(ctx, manager)
        return _snapshot()

    async def _list():
        return _snapshot()

    async def _call(server: str, tool: str, arguments: dict | None = None):
        return await manager.call(server, tool, arguments or {})

    ctx.register_tool("mcp_list", _list,
                      "列出已配置的 MCP server 及其工具（含连接状态与错误）", {})
    ctx.register_tool("mcp_call", _call,
                      "调用 MCP server 的工具（server/tool/arguments）",
                      {"server": "MCP server 名", "tool": "工具名",
                       "arguments": "参数对象"})

    # ── 管理接口（加/删/重连/直调）──
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

    ctx.register_api("GET", "api/list", _api_list)
    ctx.register_api("POST", "api/add", _api_add)
    ctx.register_api("POST", "api/remove", _api_remove)
    ctx.register_api("POST", "api/reload", _api_reload)
    ctx.register_api("POST", "api/call", _api_call)

    # 启动即连接（需要事件循环；无循环时只记日志，工具仍可手动调 api/reload）
    try:
        ctx._mcp_task = asyncio.ensure_future(_connect_and_register())
    except RuntimeError:
        ctx._mcp_task = None
        ctx.log("mcp: 无事件循环，未自动连接（可用 POST api/reload）")
    ctx.log(f"mcp 已就绪（{len(manager.servers)} 个 server 配置）")


def _register_dynamic(ctx, manager: McpManager) -> None:
    """把 MCP 工具注册进工具表；重连后旧名字先摘掉，避免指向已关闭的连接。"""
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
