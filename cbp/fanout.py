# -*- coding: utf-8 -*-
"""确定性扇出：**不经模型判断**，把 N 个任务直接派给 N 个子代理。

为什么需要：提示词 + 运行时提示能让模型"想起"子代理，但实测弱模型（Kimi）在有捷径时
仍然选择自己串行做完（30 步 vs 扇出）。所以给一个**显式入口**：用户直接给几段任务，
服务就把它们各自丢给一个子代理，跑完汇总到同一个父会话里——不依赖模型是否愿意拆。

    POST /v1/agent/fanout {"tasks": ["...", "..."], "model": "kimi-coding/k3"}
    cbp fanout --task "..." --task "..."

与 subagent_many 的区别：那个是**模型**调用的工具；这里是**用户/CLI**调用的入口，
同样顺序执行（浏览器通道只有一个页面），每个子任务仍是独立会话，可单独查看。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from . import bridge as b


def _normalize(tasks: list | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for raw in tasks or []:
        if isinstance(raw, str):
            task, label = raw.strip(), ""
        elif isinstance(raw, dict):
            task = str(raw.get("task") or raw.get("prompt") or "").strip()
            label = str(raw.get("description") or raw.get("label") or "").strip()
        else:
            raise b.ToolError("tasks 里每项应为字符串或 {task, description}")
        if task:
            out.append({"task": task, "description": label})
    return out


async def fanout(tasks: list | None, model: str = "", workspace: str = "",
                 max_steps: int = 40, parent_title: str = "") -> dict[str, Any]:
    """建一个父会话，把每个任务交给一个子代理，结果写回父会话消息。"""
    items = _normalize(tasks)
    if not items:
        raise b.ToolError("tasks 不能为空")
    if len(items) > 8:
        raise b.ToolError(f"一次最多 8 个子任务（收到 {len(items)} 个）")

    root = str(b.AGENT_TOOLS.root)
    use_ws = str(workspace or "").strip()
    if use_ws:
        b.AGENT_TOOLS.set_root(use_ws, bool(getattr(b, "ALLOW_OUTSIDE_WORKSPACE", False)))
        root = str(b.AGENT_TOOLS.root)
    parent_sid = "fan-" + uuid.uuid4().hex[:10]
    title = (parent_title or items[0]["task"] or "扇出任务")[:120]
    b.AGENT_SESSIONS.create(parent_sid, title, root, b.normalize_model_soft(model or b.DEFAULT_MODEL))
    b.AGENT_SESSIONS.patch(parent_sid, description=f"扇出 {len(items)} 个子任务")
    b.AGENT_SESSIONS.add_message(parent_sid, "user",
                                 "扇出任务：\n" + "\n".join(
                                     f"{i}. {it['task']}" for i, it in enumerate(items, 1)),
                                 0)

    results: list[dict[str, Any]] = []
    t0 = time.time()
    # 用**任务上下文**绑定父会话（并发安全）；全局变量只作展示用途，
    # 若同时有真实任务在跑，写全局会把别人的子代理挂到我们这儿来（实测过）。
    tok_sid = b._SESSION_CTX.set(parent_sid)
    prev_active = b.ACTIVE_SESSION_ID
    b.ACTIVE_SESSION_ID = parent_sid
    # 是否并行：只有 **API 提供商** 模型能并行（HTTP 请求彼此独立）；网页 Copilot
    # 通道只有一个 page，并发会互相踩页（真并发得为每个子代理开独立浏览器上下文，
    # 代价是 N 个 Copilot 会话 + 限流风险）。并发上限由 agent.subagent_parallel 控制。
    parallel = b.model_is_provider(model or b.DEFAULT_MODEL) and len(items) > 1
    cap = b.subagent_parallel() if parallel else 1
    try:
        if parallel:
            sem = asyncio.Semaphore(cap)

            async def run_one(idx: int, item: dict) -> dict:
                async with sem:
                    try:
                        out = await b.run_subagent(item["task"], item["description"],
                                                   model, max_steps)
                        # _drive_child **不抛异常**也会带着 error 返回（子会话失败时），
                        # 只看有没有异常会把失败当成功（实测：子代理 500 了却报 ok=true）
                        err = out.get("error")
                        return {"index": idx, "task": item["task"][:120], "ok": not err,
                                "agent_id": out.get("agent_id"), "steps": out.get("steps"),
                                "content": out.get("content") or "",
                                **({"error": err} if err else {})}
                    except Exception as exc:      # noqa: BLE001 单项失败不影响其余
                        return {"index": idx, "task": item["task"][:120], "ok": False,
                                "error": f"{type(exc).__name__}: {exc}"}

            # gather 保序：结果顺序 == 任务顺序（不要按完成先后排，否则汇总会乱）
            results = list(await asyncio.gather(
                *(run_one(i, it) for i, it in enumerate(items, 1))))
            # 父会话消息按顺序写入（并发写同一条时间线会乱序，这里统一落）
            for r in results:
                if r["ok"]:
                    b.AGENT_SESSIONS.add_message(
                        parent_sid, "assistant",
                        f"### 子任务 {r['index']}\n{r.get('content') or ''}", 0)
                else:
                    b.AGENT_SESSIONS.add_message(
                        parent_sid, "assistant",
                        f"### 子任务 {r['index']} 失败\n{r.get('error')}", 0)
        else:
            for idx, item in enumerate(items, 1):
                try:
                    out = await b.run_subagent(item["task"], item["description"],
                                               model, max_steps)
                    err = out.get("error")
                    results.append({"index": idx, "task": item["task"][:120],
                                    "ok": not err, "agent_id": out.get("agent_id"),
                                    "steps": out.get("steps"),
                                    "content": out.get("content") or "",
                                    **({"error": err} if err else {})})
                    b.AGENT_SESSIONS.add_message(
                        parent_sid, "assistant",
                        (f"### 子任务 {idx}\n{out.get('content') or ''}" if not err
                         else f"### 子任务 {idx} 失败\n{err}"), 0)
                except Exception as exc:              # noqa: BLE001 单项失败不影响其余
                    results.append({"index": idx, "task": item["task"][:120], "ok": False,
                                    "error": f"{type(exc).__name__}: {exc}"})
                    b.AGENT_SESSIONS.add_message(
                        parent_sid, "assistant", f"### 子任务 {idx} 失败\n{exc}", 0)
    finally:
        try:
            b._SESSION_CTX.reset(tok_sid)
        except Exception:
            pass
        b.ACTIVE_SESSION_ID = prev_active
    ok = [r for r in results if r["ok"]]
    b.AGENT_SESSIONS.patch(parent_sid, state="completed")
    return {"session_id": parent_sid, "requested": len(items), "succeeded": len(ok),
            "failed": len(results) - len(ok), "seconds": round(time.time() - t0, 1),
            "parallel": bool(parallel), "concurrency": cap,
            "model": model or b.DEFAULT_MODEL, "workspace": root, "results": results}
