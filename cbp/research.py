"""research.py — 深研任务、插件任务与确定性扇出

从 bridge.py 拆出（第四批解耦）。约定：模块级可变状态与配置仍在 bridge.py，
这里经 ``b.xxx`` 读写；本模块内部互相调用保持直接调用。本模块不定义 FastAPI 端点。
"""
from __future__ import annotations

import asyncio, base64, collections, contextlib, json, os, re, shutil, time, uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import HTTPException

from . import bridge as b


# ── 深研一键任务（WebUI「深研」按钮）──────────────────────────────────
async def deep_research_task(query: str, model: str = "", angles: int = 3,
                             fetch_top: int = 3, lang: str = "zh") -> dict:
    """一键深研：插件收集证据包 → **一次直接调用模型**综合成带来源的报告。

    为什么不走 Agent 循环：实测走循环时模型会把"写报告"变成**分块写文件**
    （16 步、11 分钟）。深研的综合是一次性文本生成，直连模型即可（提供商走
    complete，Copilot 网页走单个聊天回合），并把结果落成会话 + 工作区 md 文件。
    """
    query = str(query or "").strip()
    if not query:
        raise b.ToolError("研究问题不能为空")
    fn = b.PLUGINS.plugin_function("research", "deep_research")
    if fn is None:
        raise b.ToolError("research 插件未启用：WebUI → 更多 → 插件管理 里启用它")
    try:
        pack = await fn(query, angles=max(1, min(int(angles or 3), 5)),
                        fetch_top=max(0, min(int(fetch_top or 3), 8)))
    except Exception as exc:
        raise b.ToolError(f"证据收集失败: {exc}") from exc
    sources = pack.get("sources") or []
    if not sources:
        raise b.ToolError("没有搜到可用来源；请换个说法或稍后重试")
    evidence = json.dumps(sources[:12], ensure_ascii=False)
    if len(evidence) > 40000:
        evidence = evidence[:40000] + "…（截断）"
    prompt = (
        "你是一位严谨的调研员。下面是 deep_research 收集到的证据包"
        "（JSON：title/url/snippet/excerpt）。\n"
        f"调研问题：{query}\n\n"
        f"证据包：\n{evidence}\n\n"
        "请直接输出一份 Markdown 调研报告：\n"
        "1) 开头 3-5 句结论摘要；\n"
        "2) 分点展开，每个要点标注来源链接；\n"
        "3) 结尾「来源」清单（标题 + URL）；\n"
        "4) 证据不足处明确说明，不要编造。\n"
        "只输出报告正文，不要写文件、不要调用工具。")

    use_model = b.normalize_model_soft(model or b.DEFAULT_MODEL)
    pm = b.providers_mod.split_provider_model(use_model)
    sid = "research-" + uuid.uuid4().hex[:10]
    # 系统生成的标题也要跟界面语言走（否则英文界面里冒出一行中文 "[深研] …"）
    tag = "[Research]" if str(lang).lower().startswith("en") else "[深研]"
    b.AGENT_SESSIONS.create(sid, f"{tag} {query}", str(b.AGENT_TOOLS.root), use_model)
    b.AGENT_SESSIONS.add_message(sid, "user", f"{tag} {query}", 0)
    b.AGENT_SESSIONS.patch(sid, state="running")
    t0 = time.time()
    try:
        if pm and pm[0] in b.PROVIDERS:
            text = await asyncio.wait_for(
                b.PROVIDERS[pm[0]].complete(pm[1], [{"role": "user", "content": prompt}]),
                timeout=float(b.AGENT_CFG.get("research_timeout_seconds", 300)))
        else:
            text = await asyncio.wait_for(
                b.agent_chat_once(prompt, use_model, True),
                timeout=float(b.AGENT_CFG.get("research_timeout_seconds", 600)))
            try:
                await b._wait_dom_stable()
                md = await b.answer_markdown(text)
                if md:
                    text = md
            except Exception:
                pass
    except Exception as exc:
        b.AGENT_SESSIONS.patch(sid, state="failed", last_error=str(exc)[:500])
        raise b.ToolError(f"综合失败: {exc}") from exc
    report = (text or "").strip()
    if not report:
        b.AGENT_SESSIONS.patch(sid, state="failed", last_error="模型返回为空")
        raise b.ToolError("综合失败：模型返回为空")
    elapsed = round(time.time() - t0, 1)
    b.AGENT_SESSIONS.add_message(sid, "assistant", report, 0)
    b.AGENT_SESSIONS.patch(sid, state="completed",
                         summary={"research": True, "sources": len(sources),
                                  "elapsed": elapsed})
    # 同时落一份 md 到工作区，方便直接取用
    try:
        fname = f"research-{time.strftime('%Y%m%d-%H%M%S')}.md"
        b.save_workspace_file(fname, report.encode("utf-8"))
    except Exception:
        fname = ""
    return {"query": query, "session_id": sid, "report": report,
            "sources": sources, "queries": pack.get("queries") or [],
            "file": fname, "elapsed": elapsed, "error": None}


# ── 插件跑任务（定时任务/生图等插件用）────────────────────────────────
async def run_plugin_task(task: str, model: str = "", workspace: str = "",
                          prefix: str = "cron") -> dict:
    """跑一个独立 Agent 任务并返回结果（供插件调用；新会话，不打断当前任务）。"""
    task = str(task or "").strip()
    if not task:
        raise b.ToolError("task 不能为空")
    sid = f"{prefix}-{uuid.uuid4().hex[:10]}"
    use_model = b.normalize_model_soft(model or b.DEFAULT_MODEL)
    req = b.AgentRequest(model=use_model,
                       messages=[b.Message(role="user", content=task)],
                       stream=False, session_id=sid,
                       workspace=workspace or None)
    try:
        result, real_sid = await b.run_agent(req)
        return {"session_id": real_sid, "content": (result.content or "")[:4000],
                "steps": len(result.steps), "error": None}
    except Exception as exc:
        return {"session_id": sid, "content": "", "steps": 0, "error": str(exc)}


async def fanout(tasks, model: str = "", workspace: str = "",
                 max_steps: int = 0, parent_title: str = "") -> dict:
    """确定性扇出（实现在 cbp/fanout.py，延迟导入避免循环依赖）。"""
    from .fanout import fanout as _fanout
    return await _fanout(tasks, model, workspace, max_steps, parent_title)
