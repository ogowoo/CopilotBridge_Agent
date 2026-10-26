"""程序化工具调用（P2-13）：让模型写一段代码，在**独立进程**里批量调用只读工具。

**为什么做**：实测瓶颈 99% 在远端回合。像"读 20 个文件并汇总"这类分析任务，用工具循环要
20 个回合；写成一段代码只要 1 个回合。这是目前最直接的降本手段。

**边界（v1 刻意保守）**：
  · 只暴露**只读**工具（READ_ONLY_TOOLS 与已注册工具的交集），**不含任何写入/删除/发信**；
  · 子进程隔离：单独进程、独立工作目录继承、无继承式共享内存；
  · 硬限额：超时、工具调用次数上限、回传内容上限（超出截断并标注）；
  · 路径沙箱仍然生效（工具侧 `_path` 照旧拦截越界）。
默认**关闭**（`allow_run_code=false`），需要显式开启；开启后建议同时列入
`approval_required_tools` 以便逐次授权（与 run_python 同规格）。

**协议**：父进程用 `-c` 注入 prelude 后附加用户代码；子进程按行输出 JSON：
  {"call": "read_text", "args": {...}}   → 父进程回一行 {"ok": true, "result": ...}
  {"final": <任意 JSON>}                 → 结束（父进程取它作为结果）
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

PRELUDE = r'''
import json, sys, builtins


class ToolError(RuntimeError):
    """工具调用被拒绝/失败（工具名不在白名单、超出调用预算、工具自身报错）。

    刻意做成**具名异常**：模型代码可以 try/except 后优雅收尾（例如把已收集到的部分结果
    回传），而不是因为一次拒绝就整个崩掉。
    """

def __rpc(tool, **kwargs):
    sys.stdout.write(json.dumps({"call": tool, "args": kwargs}, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        raise RuntimeError("父进程连接已断开")
    msg = json.loads(line)
    if not msg.get("ok"):
        raise ToolError(str(msg.get("error") or "工具调用失败"))
    return msg.get("result")

def read_text(path, offset=0, limit=120000):
    return __rpc("read_text", path=path, offset=offset, limit=limit)

def list_directory(path="."):
    return __rpc("list_directory", path=path)

def find_files(pattern):
    return __rpc("find_files", pattern=pattern)

def skill_list():
    return __rpc("skill_list")

def memory_search(query, limit=8, scope="all"):
    return __rpc("memory_search", query=query, limit=limit, scope=scope)

def tool_list():
    return __rpc("tool_list")

def final(value):
    """结束并回传结果（任意可 JSON 序列化的值）。"""
    sys.stdout.write(json.dumps({"final": value}, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    raise SystemExit(0)
'''


def allowed_tools(tools: Any) -> list[str]:
    """可用工具白名单：**只读**且当前确实已注册。"""
    from .tools import READ_ONLY_TOOLS

    out = []
    for name in READ_ONLY_TOOLS:
        if name in getattr(tools, "_tools", {}):
            out.append(name)
    return out


async def run_code(tools: Any, code: str, *, timeout: float = 30.0, max_calls: int = 50,
                   max_output_chars: int = 20000) -> dict[str, Any]:
    """在子进程里执行 code，按协议代理只读工具调用。返回 {ok, result/error, calls, stdout}。"""
    allowed = set(allowed_tools(tools))
    if not allowed:
        return {"ok": False, "error": "当前环境没有可用的只读工具，run_code 不可用"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-X", "utf8", "-u", "-c", PRELUDE + "\n" + (code or ""),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    calls = 0        # 尝试次数（含被拒的）
    executed = 0     # 真正执行的次数（预算按它计）
    final_value: Any = None
    tail: list[str] = []
    over_budget = False

    async def pump() -> None:
        nonlocal calls, executed, final_value, over_budget
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                tail.append(line[:500])
                continue
            if "call" in msg:
                name = str(msg.get("call"))
                args = msg.get("args") or {}
                calls += 1
                if name not in allowed:
                    reply = {"ok": False, "error": f"工具 {name} 不在只读白名单内"}
                elif executed >= max_calls:
                    over_budget = True
                    reply = {"ok": False, "error": f"工具调用次数超过上限 {max_calls}"
                                                     f"（本次运行已执行 {executed} 次）"}
                elif calls > max_calls * 5:
                    # 绝对上限：模型代码在循环里疯狂重试时，直接终止而不是无限代理
                    reply = {"ok": False, "error": "尝试次数远超预算，已终止本次 run_code"}
                    over_budget = True
                    proc.kill() if proc.returncode is None else None
                else:
                    executed += 1
                    try:
                        result = await tools.execute(name, args)
                        text = json.dumps(result, ensure_ascii=False, default=str)
                        if len(text) > max_output_chars:
                            text = (text[:max_output_chars]
                                    + f"…[结果被截断，原长 {len(text)} 字符]")
                        reply = {"ok": True, "result": json.loads(text)} if text.endswith("}") \
                            else {"ok": True, "result": text}
                    except Exception as exc:
                        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                assert proc.stdin is not None
                proc.stdin.write((json.dumps(reply, ensure_ascii=False, default=str) + "\n").encode())
                await proc.stdin.drain()
            elif "final" in msg:
                final_value = msg.get("final")
            else:
                tail.append(line[:500])

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "error": f"代码执行超过 {timeout:.0f} 秒已终止",
                "calls": calls, "executed": executed}
    err = b""
    if proc.stderr is not None:
        err = await proc.stderr.read()
    if proc.returncode not in (0, None) and final_value is None:
        return {"ok": False, "calls": calls, "executed": executed,
                "error": (err.decode("utf-8", errors="replace")[-1500:] or
                          f"子进程退出码 {proc.returncode}")}
    return {"ok": True, "result": final_value, "calls": calls, "executed": executed,
            "calls_over_budget": over_budget,
            "stdout_tail": "\n".join(tail[-20:])[:2000],
            "stderr_tail": err.decode("utf-8", errors="replace")[-800:]}
