# -*- coding: utf-8 -*-
"""整体回归：**真跑**每个面向用户的能力，而不是只跑单测。

覆盖：健康检查 / 工具面 / 计划模式 / 普通写入任务 / 子代理(建-续-分叉) /
深研 / 定时任务 / MCP(stdio) / 插件安装卸载 / 沙箱接口 / WebUI 审计 / 单测+不变量。

用法:
    python scripts/regression.py                 # 默认 kimi-coding/k3，跑完清理
    python scripts/regression.py --keep          # 保留会话与临时文件（排查用）
    python scripts/regression.py --model copilot-auto
    python scripts/regression.py --skip-slow     # 跳过深研与需要模型的长任务
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "data" / "regression"


class Ctx:
    def __init__(self, url: str, model: str, keep: bool, skip_slow: bool, timeout: float):
        self.url = url.rstrip("/")
        self.model = model
        self.keep = keep
        self.skip_slow = skip_slow
        self.timeout = timeout
        self.results: list[tuple[str, bool, str]] = []
        self.created_sessions: list[str] = []
        self.tmp = Path(tempfile.mkdtemp(prefix="gantry-reg-"))

    # ── HTTP ──
    def req(self, method: str, path: str, body: dict | None = None, timeout: float = 0):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.url + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=timeout or self.timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def get(self, path: str, **kw):
        return self.req("GET", path, **kw)

    def post(self, path: str, body: dict | None = None, **kw):
        return self.req("POST", path, body or {}, **kw)

    def delete(self, path: str):
        return self.req("DELETE", path)

    # ── 断言与记账 ──
    def check(self, name: str, ok: bool, detail: str = ""):
        self.results.append((name, bool(ok), detail))
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}" + (f" — {detail}" if detail else ""), flush=True)
        return ok

    def run_task(self, task: str, model: str = "", extra: dict | None = None,
                 timeout: float = 0) -> tuple[str, dict]:
        body = {"model": model or self.model, "stream": False, "agent_mode": True,
                "messages": [{"role": "user", "content": task}]}
        body.update(extra or {})
        d = self.post("/v1/agent/completions", body, timeout=timeout)
        sid = d.get("session_id") or ""
        if sid:
            self.created_sessions.append(sid)
        return sid, d

    def steps(self, sid: str) -> list[dict]:
        return (self.get(f"/v1/agent/sessions/{sid}") or {}).get("steps") or []

    def cleanup(self):
        if self.keep:
            print(f"\n（保留临时文件：{self.tmp}）")
            return
        for sid in self.created_sessions:
            try:
                self.delete(f"/v1/agent/sessions/{sid}")
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)


# ────────────────────────── 各项检查 ──────────────────────────

def step_health(c: Ctx) -> None:
    h = c.get("/health")
    c.check("健康检查", h.get("status") == "ok",
            f"busy={h.get('busy')} browser={h.get('browser_state')}")


def step_tools(c: Ctx) -> None:
    d = c.get("/v1/agent/tools")
    names = {t["name"] for t in (d.get("tools") or [])}
    need = {"read_text", "write_text", "pwsh", "subagent", "subagent_send",
            "subagent_fork", "create_goal", "todo_write", "exit_plan_mode",
            "web_fetch", "mcp_list", "mcp_call", "mcp_read_resource", "mcp_get_prompt"}
    missing = sorted(need - names)
    c.check("工具面完整", not missing, f"{len(names)} 个工具" + (f"，缺 {missing}" if missing else ""))


def step_sandbox_api(c: Ctx) -> None:
    d = c.get("/v1/agent/sandbox")
    modes = d.get("modes") or []
    ok = d.get("mode") in modes and len(modes) == 3
    # 非法档位必须 400
    bad_ok = False
    try:
        c.post("/v1/agent/sandbox", {"mode": "yolo"})
    except urllib.error.HTTPError as exc:
        bad_ok = exc.code == 400
    c.check("沙箱档位", ok and bad_ok, f"mode={d.get('mode')} modes={modes}")


def step_plan_mode(c: Ctx) -> None:
    target = BASE / "workspace" / "reg_plan_should_not_exist.txt"
    target.unlink(missing_ok=True)
    sid, d = c.run_task("在 workspace 里创建 reg_plan_should_not_exist.txt 写入 ok",
                        extra={"plan_mode": True}, timeout=420)
    steps = c.steps(sid)
    submitted = [s for s in steps if s.get("status") == "plan_submitted"]
    wrote = [s for s in steps if str(s.get("tool") or "") in
             ("write_text", "write_text_b64", "append_text", "append_text_b64")]
    plan_saved = bool((c.get(f"/v1/agent/sessions/{sid}").get("session") or {}).get("plan"))
    c.check("计划模式：提交方案且不写文件",
            bool(submitted) and not wrote and not target.exists() and plan_saved,
            f"steps={len(steps)} submitted={len(submitted)} wrote={len(wrote)} plan={plan_saved}")


def step_write_task(c: Ctx) -> str:
    name = f"reg_write_{int(time.time()) % 100000}.txt"
    sid, d = c.run_task(f"创建文件 {name}，内容写 ok，然后读回来确认。", timeout=420)
    path = BASE / "workspace" / name
    ok = path.exists() and "ok" in path.read_text(encoding="utf-8", errors="replace").lower()
    c.check("普通任务：真的写文件", ok, f"{name} exists={path.exists()}")
    return name


def step_subagent(c: Ctx) -> str:
    sid, d = c.run_task("用 subagent 工具让子代理『回答数字 7，别做别的』，把 agent_id 原样告诉我。",
                        timeout=420)
    subs = (c.get(f"/v1/agent/subagents?parent={sid}") or {}).get("subagents") or []
    child = subs[0]["id"] if subs else ""
    c.check("子代理：创建", bool(child), f"child={child}")
    return child


def step_subagent_send(c: Ctx, child: str) -> None:
    if not child:
        c.check("子代理：继续", False, "没有子代理可继续")
        return
    before = len((c.get(f"/v1/agent/sessions/{child}") or {}).get("messages") or [])
    d = c.post(f"/v1/agent/subagents/{child}/send",
               {"message": "再回答一次：你刚才说的是哪个数字？", "max_steps": 6}, timeout=420)
    after = len((c.get(f"/v1/agent/sessions/{child}") or {}).get("messages") or [])
    c.check("子代理：继续（同上下文）", after > before and not d.get("error"),
            f"messages {before}→{after} error={d.get('error')}")


def step_subagent_fork(c: Ctx, child: str) -> None:
    if not child:
        c.check("子代理：分叉", False, "没有源会话")
        return
    d = c.post(f"/v1/agent/subagents/{child}/fork", {"description": "回归分叉"})
    new_sid = d.get("agent_id") or ""
    if new_sid:
        c.created_sessions.append(new_sid)
    row = (c.get(f"/v1/agent/sessions/{new_sid}") or {}).get("session") or {}
    c.check("子代理：分叉", bool(new_sid) and d.get("messages", 0) > 0,
            f"{new_sid} messages={d.get('messages')} parent={row.get('parent_id')}")


def step_research(c: Ctx) -> None:
    if c.skip_slow:
        c.check("深研", True, "（跳过）")
        return
    t0 = time.time()
    d = c.post("/v1/agent/deep-research",
               {"query": "Python 3.13 有哪些主要新特性", "model": c.model, "angles": 2},
               timeout=600)
    sid = d.get("session_id") or ""
    if sid:
        c.created_sessions.append(sid)
    ok = bool((d.get("report") or "").strip()) and len(d.get("sources") or []) >= 3
    c.check("深研：证据 + 报告", ok,
            f"{time.time() - t0:.0f}s 来源 {len(d.get('sources') or [])} 报告 {len(d.get('report') or '')} 字")


def step_schedule(c: Ctx) -> None:
    name = "reg-" + str(int(time.time()) % 100000)
    c.post("/v1/plugins/schedule/api/add",
           {"name": name, "spec": {"every": "1d"}, "task": "只回答两个字：收到",
            "model": c.model})
    try:
        d = c.post("/v1/plugins/schedule/api/run", {"name": name}, timeout=420)
        res = (d.get("results") or [{}])[0]
        sid = res.get("session_id") or ""
        if sid:
            c.created_sessions.append(sid)
        c.check("定时任务：立即执行", res.get("error") is None and bool(res.get("content")),
                f"session={sid} content={str(res.get('content'))[:30]!r}")
    finally:
        c.post("/v1/plugins/schedule/api/remove", {"name": name})


def step_mcp(c: Ctx) -> None:
    mock = BASE / "tests" / "fixtures" / "mock_mcp_server.py"
    c.post("/v1/plugins/mcp/api/add",
           {"name": "regmock", "spec": {"command": sys.executable, "args": [str(mock)]}})
    try:
        lst = c.get("/v1/plugins/mcp/api/list")
        tools = [t["registered_name"] for t in lst.get("tools") or []]
        d = c.post("/v1/plugins/mcp/api/call",
                   {"server": "regmock", "tool": "echo", "arguments": {"text": "回归 OK"}})
        c.check("MCP：stdio 工具可用",
                "mcp__regmock__echo" in tools and d.get("text") == "回归 OK",
                f"tools={len(tools)} echo={d.get('text')!r}")
    finally:
        c.post("/v1/plugins/mcp/api/remove", {"name": "regmock"})
    # 移除后工具必须摘掉
    with_tools = [t["name"] for t in (c.get("/v1/agent/tools").get("tools") or [])
                  if t["name"].startswith("mcp__regmock__")]
    c.check("MCP：移除后工具摘除", not with_tools, f"残留 {with_tools}")


def step_plugin_install(c: Ctx) -> None:
    src = c.tmp / "regplugin"
    src.mkdir(parents=True, exist_ok=True)
    (src / "plugin.json").write_text(json.dumps({
        "name": "regplugin", "version": "0.1.0", "description": "回归测试插件",
        "gantry": {"entry": "plugin.py", "permissions": ["tools"],
                "contributes": {"tools": ["regplugin_ping"]}},
    }, ensure_ascii=False), encoding="utf-8")
    (src / "plugin.py").write_text(
        "def setup(ctx):\n"
        "    ctx.register_tool('regplugin_ping', lambda: {'pong': True}, '回归演示', {})\n",
        encoding="utf-8")
    c.post("/v1/plugins/install", {"source": str(src)})
    try:
        names = {t["name"] for t in (c.get("/v1/agent/tools").get("tools") or [])}
        reg = {p["name"]: p for p in (c.get("/v1/plugins/registry").get("plugins") or [])}
        ok = "regplugin_ping" in names and reg.get("regplugin", {}).get("version") == "0.1.0"
        c.check("插件：安装即加载", ok, f"version={reg.get('regplugin', {}).get('version')}")
    finally:
        c.post("/v1/plugins/uninstall", {"name": "regplugin"})
    names = {t["name"] for t in (c.get("/v1/agent/tools").get("tools") or [])}
    c.check("插件：卸载即摘除", "regplugin_ping" not in names)


def step_panels(c: Ctx) -> None:
    d = c.get("/v1/plugins/panels")
    panels = [p["id"] for p in d.get("panels") or []]
    c.check("插件面板已声明", bool(panels), f"{panels}")


def step_cli(c: Ctx) -> None:
    py = sys.executable
    cmds = [
        (["status"], "服务"),
        (["tools"], "工具"),
        (["sessions", "--limit", "3"], ""),
        (["plugins", "registry"], "plugins"),
    ]
    ok = True
    detail = []
    for args, expect in cmds:
        r = subprocess.run([py, "-m", "gantry.cli", "--url", c.url, *args],
                           cwd=str(BASE), capture_output=True, text=True, timeout=180)
        good = r.returncode == 0 and (not expect or expect in (r.stdout or ""))
        ok = ok and good
        detail.append(f"{args[0]}={r.returncode}")
        if not good:
            detail.append((r.stderr or "")[:80])
    c.check("CLI 只读命令", ok, " ".join(detail))


def step_audits(c: Ctx) -> None:
    py = sys.executable
    for name, cmd in (("单测", [py, "-m", "unittest", "discover", "-s", "tests"]),
                      ("不变量", [py, "scripts/invariants.py"]),
                      ("UI 冒烟", [py, "scripts/ui_smoke.py"]),
                      ("i18n 审计", [py, "scripts/ui_i18n_audit.py"]),
                      ("布局审计", [py, "scripts/ui_layout_audit.py", "--stress"]),
                      ("对比度审计", [py, "scripts/ui_contrast_audit.py"])):
        if c.skip_slow and name in ("单测", "布局审计"):
            c.check(name, True, "（跳过）")
            continue
        r = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True, timeout=1800)
        lines = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        ran = [x for x in lines if x.startswith("Ran ")]
        tail = (ran[0] if ran else (lines[-1] if lines else ""))[:60]
        c.check(name, r.returncode == 0, tail)


# ────────────────────────── 主流程 ──────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("CBP_URL", "http://127.0.0.1:8765"))
    ap.add_argument("--model", default=os.environ.get("CBP_REGRESSION_MODEL", "kimi-coding/k3"))
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--skip-slow", action="store_true")
    ap.add_argument("--timeout", type=float, default=600)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    # 先回收上一轮可能泄漏的 Playwright 浏览器：实测连续跑几天 UI 审计会攒出 20+ 个
    # msedge（脚本在异常路径上没走到 browser.close()）。只回收"活得够久"的，不影响正在跑的。
    try:
        subprocess.run([sys.executable, str(BASE / "scripts" / "reap_browsers.py"), "--kill"],
                       cwd=BASE, check=False, capture_output=True, timeout=120)
    except Exception:
        pass
    c = Ctx(args.url, args.model, args.keep, args.skip_slow, args.timeout)
    t0 = time.time()
    print(f"回归目标: {c.url} · 模型 {c.model}\n")
    child = ""
    written = ""
    try:
        step_health(c)
        step_tools(c)
        step_sandbox_api(c)
        step_plan_mode(c)
        written = step_write_task(c)
        child = step_subagent(c)
        step_subagent_send(c, child)
        step_subagent_fork(c, child)
        step_research(c)
        step_schedule(c)
        step_mcp(c)
        step_plugin_install(c)
        step_panels(c)
        step_cli(c)
        step_audits(c)
    except urllib.error.HTTPError as exc:
        c.check("回归中断", False, f"HTTP {exc.code}: {exc.read()[:200]!r}")
    except Exception as exc:                       # noqa: BLE001 - 回归脚本要给出可读结论
        c.check("回归中断", False, f"{type(exc).__name__}: {exc}")
    finally:
        if written:
            (BASE / "workspace" / written).unlink(missing_ok=True)
        (BASE / "workspace" / "reg_plan_should_not_exist.txt").unlink(missing_ok=True)
        c.cleanup()

    passed = sum(1 for _, ok, _ in c.results if ok)
    total = len(c.results)
    print(f"\n{'=' * 60}")
    print(f"回归结果: {passed}/{total} 通过 · 用时 {time.time() - t0:.0f}s")
    fails = [(n, d) for n, ok, d in c.results if not ok]
    for n, d in fails:
        print(f"  ✗ {n} — {d}")
    report = {"passed": passed, "total": total, "seconds": round(time.time() - t0, 1),
              "model": c.model, "results": [{"name": n, "ok": ok, "detail": d}
                                            for n, ok, d in c.results]}
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(f"报告: {OUT / 'report.json'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
