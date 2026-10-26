# -*- coding: utf-8 -*-
"""命令行入口（headless）：把服务当作本地 API 用，无需打开 WebUI。

设计取舍：CLI **不**在进程内初始化浏览器/桥接（那会把 Playwright 与 Edge 配置
拖进每次调用，且与服务里的会话状态不一致），而是走 HTTP 调同一个服务——
和 WebUI 看到的是同一份事实。

    cbp run "把 docs 里的死链列出来" --model kimi-coding/k3
    cbp run "重构 X" --plan          # 计划模式（只读，产出方案后停）
    cbp serve --port 8765
    cbp sessions / cbp tools / cbp plugins list / cbp mcp list / cbp sandbox
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

DEFAULT_URL = os.environ.get("CBP_URL", "http://127.0.0.1:8765")
DEFAULT_TOKEN = os.environ.get("CBP_TOKEN", "")


def _client(args) -> httpx.Client:
    headers = {}
    token = getattr(args, "token", "") or DEFAULT_TOKEN
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=getattr(args, "url", "") or DEFAULT_URL,
                        headers=headers, timeout=getattr(args, "timeout", 900))


def _fail(msg: str, code: int = 1) -> int:
    print(f"cbp: {msg}", file=sys.stderr)
    return code


def _get(client: httpx.Client, path: str) -> Any:
    r = client.get(path)
    r.raise_for_status()
    return r.json()


# ────────────────────────── 子命令 ──────────────────────────

def cmd_run(args) -> int:
    """跑一个 Agent 任务（headless），打印结果。"""
    if not args.task.strip():
        return _fail("任务内容不能为空")
    body = {
        "model": args.model,
        "stream": False,
        "agent_mode": True,
        "messages": [{"role": "user", "content": args.task}],
    }
    if args.session:
        body["session_id"] = args.session
    if args.workspace:
        body["workspace"] = args.workspace
    if args.plan:
        body["plan_mode"] = True
    if args.sandbox:
        body["sandbox_mode"] = args.sandbox
    with _client(args) as c:
        try:
            r = c.post("/v1/agent/completions", json=body)
        except httpx.ConnectError:
            return _fail(f"连不上服务 {args.url or DEFAULT_URL}（先运行 cbp serve）")
        if r.status_code >= 400:
            return _fail(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    print(content.strip())
    if not args.quiet:
        steps = data.get("agent_steps") or []
        print(f"\n— {len(steps)} 步 · 会话 {data.get('session_id')} · "
              f"工作区 {data.get('workspace')}", file=sys.stderr)
    return 0


def cmd_serve(args) -> int:
    """启动本地服务（等价于 python start.py）。"""
    server = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "start.py")
    if not os.path.isfile(server):
        return _fail(f"找不到服务入口: {server}")
    argv = [sys.executable, server]
    if args.port:
        os.environ["CBP_PORT"] = str(args.port)
    try:
        import uvicorn  # noqa: F401
    except Exception:
        return _fail("缺少依赖：pip install -r requirements.txt")
    os.execv(sys.executable, argv)
    return 0


def cmd_fanout(args) -> int:
    """确定性扇出：每个 --task 派一个子代理，结果写回一个父会话。"""
    tasks = [t for t in (args.task or []) if t.strip()]
    if len(tasks) < 2:
        return _fail("至少给两个 --task（每个任务一个子代理）")
    body = {"tasks": tasks, "model": args.model or "", "max_steps": args.max_steps}
    if args.workspace:
        body["workspace"] = args.workspace
    with _client(args) as c:
        try:
            d = c.post("/v1/agent/fanout", json=body)
        except httpx.ConnectError:
            return _fail(f"连不上服务 {args.url or DEFAULT_URL}")
        if d.status_code >= 400:
            return _fail(f"HTTP {d.status_code}: {d.text[:300]}")
        data = d.json()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"父会话 {data.get('session_id')} · 成功 {data.get('succeeded')} · "
              f"失败 {data.get('failed')} · {data.get('seconds')}s")
        for r in data.get("results") or []:
            head = f"[{r['index']}] {'ok ' if r.get('ok') else 'ERR'}"
            print(f"{head} {str(r.get('content') or r.get('error') or '')[:200]}")
    return 0 if not data.get("failed") else 1


def cmd_status(args) -> int:
    with _client(args) as c:
        try:
            d = _get(c, "/health")
        except httpx.ConnectError:
            return _fail(f"连不上服务 {args.url or DEFAULT_URL}")
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(f"服务: {d.get('status')} · 忙碌: {d.get('busy')} · "
              f"Copilot: {d.get('browser_state')}（ready={d.get('copilot_ready')}）")
    return 0


def cmd_sessions(args) -> int:
    with _client(args) as c:
        d = _get(c, "/v1/agent/sessions")
    rows = (d.get("sessions") or [])[:args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for s in rows:
        print(f"{(s.get('id') or ''):<24} {(s.get('state') or ''):<10} "
              f"{str(s.get('title') or '')[:60]}")
    return 0


def cmd_tools(args) -> int:
    with _client(args) as c:
        d = _get(c, "/v1/agent/tools")
    items = d.get("tools") if isinstance(d, dict) else d
    names = [x.get("name") for x in items] if items and isinstance(items[0], dict) else items
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        print(f"{len(names)} 个工具：")
        print("  " + " ".join(sorted(str(n) for n in names)))
    return 0


def cmd_sandbox(args) -> int:
    with _client(args) as c:
        if args.mode:
            r = c.post("/v1/agent/sandbox", json={"mode": args.mode,
                                                  "persist": args.persist})
            if r.status_code >= 400:
                return _fail(r.text[:300])
            print(json.dumps(r.json(), ensure_ascii=False))
        else:
            print(json.dumps(_get(c, "/v1/agent/sandbox"), ensure_ascii=False))
    return 0


def cmd_plugins(args) -> int:
    with _client(args) as c:
        if args.action == "registry":
            print(json.dumps(_get(c, "/v1/plugins/registry"), ensure_ascii=False, indent=2))
            return 0
        if args.action in ("install", "uninstall"):
            if not args.name:
                return _fail("需要插件目录/来源（install）或插件名（uninstall）")
            payload = ({"source": args.name, "force": args.force}
                       if args.action == "install" else {"name": args.name})
            path = "/v1/plugins/install" if args.action == "install" else "/v1/plugins/uninstall"
            r = c.post(path, json=payload)
            if r.status_code >= 400:
                return _fail(r.text[:300])
            print(json.dumps(r.json(), ensure_ascii=False))
            return 0
        if args.action == "list":
            d = _get(c, "/v1/plugins")
            for p in d.get("plugins") or []:
                flag = "on " if p.get("enabled") else "off"
                print(f"[{flag}] {p.get('name'):<14} {p.get('status'):<10} "
                      f"{(p.get('description') or '')[:50]}")
            return 0
        name = args.name
        if not name:
            return _fail("需要插件名，例如 cbp plugins enable web-search")
        r = c.post(f"/v1/plugins/{name}/{args.action}")
        if r.status_code >= 400:
            return _fail(r.text[:300])
        print(json.dumps(r.json(), ensure_ascii=False))
    return 0


def cmd_mcp(args) -> int:
    with _client(args) as c:
        if args.action == "list":
            print(json.dumps(_get(c, "/v1/plugins/mcp/api/list"), ensure_ascii=False, indent=2))
            return 0
        if args.action == "add":
            if not args.name:
                return _fail("需要 --name")
            spec = {}
            if args.command:
                spec["command"] = args.command
                spec["args"] = args.arg or []
            if args.http_url:
                spec["url"] = args.http_url
            if not spec:
                return _fail("需要 --command（stdio）或 --http-url（HTTP）")
            r = c.post("/v1/plugins/mcp/api/add", json={"name": args.name, "spec": spec})
            if r.status_code >= 400:
                return _fail(r.text[:300])
            print(json.dumps(r.json(), ensure_ascii=False))
            return 0
        if args.action == "remove":
            if not args.name:
                return _fail("需要 --name")
            r = c.post("/v1/plugins/mcp/api/remove", json={"name": args.name})
            print(r.text[:300])
            return 0
    return _fail(f"未知动作: {args.action}")


# ────────────────────────── 解析入口 ──────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cbp", description="CopilotBridge Agent 命令行（headless）")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"服务地址（默认 {DEFAULT_URL}）")
    ap.add_argument("--token", default="", help="api_token（也可用环境变量 CBP_TOKEN）")
    ap.add_argument("--timeout", type=float, default=900.0, help="HTTP 超时秒数")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="跑一个 Agent 任务")
    p.add_argument("task", help="任务内容")
    p.add_argument("--model", default="copilot-auto")
    p.add_argument("--workspace", default="")
    p.add_argument("--session", default="")
    p.add_argument("--sandbox", default="", choices=["", "read-only", "workspace-write",
                                                     "danger-full-access"])
    p.add_argument("--plan", action="store_true", help="计划模式（只读，产出方案后停）")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true", help="不打印步骤摘要")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("serve", help="启动本地服务")
    p.add_argument("--port", type=int, default=0)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("status", help="健康检查")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("fanout", help="确定性扇出：多个任务各派一个子代理")
    p.add_argument("--task", action="append", required=True,
                   help="子任务（可重复；至少两个）")
    p.add_argument("--model", default="")
    p.add_argument("--workspace", default="")
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fanout)

    p = sub.add_parser("sessions", help="列出会话")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("tools", help="列出工具")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("sandbox", help="查看/切换沙箱档位")
    p.add_argument("mode", nargs="?", default="")
    p.add_argument("--persist", action="store_true")
    p.set_defaults(func=cmd_sandbox)

    p = sub.add_parser("plugins", help="插件管理")
    p.add_argument("action", choices=["list", "enable", "disable", "reload",
                                     "install", "uninstall", "registry"])
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--force", action="store_true", help="允许降级/覆盖")
    p.set_defaults(func=cmd_plugins)

    p = sub.add_parser("mcp", help="MCP server 管理")
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("--name", default="")
    p.add_argument("--command", default="")
    p.add_argument("--arg", action="append", default=[])
    p.add_argument("--http-url", default="")
    p.set_defaults(func=cmd_mcp)
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except httpx.ConnectError:
        return _fail(f"连不上服务 {getattr(args, 'url', DEFAULT_URL)}（先运行 cbp serve）")
    except httpx.HTTPError as exc:
        return _fail(f"HTTP 错误: {exc}")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
