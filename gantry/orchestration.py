"""编排层：ralph 目标迭代 / workflow 阶段编排 / 对抗式审计。

为什么单独成模块：它们都建立在子代理之上（run_subagent），是"怎么组织子代理"的
高层策略，与会话编排、页面自动化是不同关注点。统一放在这里，bridge.py 只保留
装配与 facade 重新导出。

运行时通过 `b.` 访问 bridge 的共享资源（run_subagent / AGENT_SESSIONS /
task_tools / runtime_update / PROVIDERS 等），保证测试 rebind 生效、无 import 期循环。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid

from . import bridge as b


async def run_ralph(objective: str, max_rounds: int = 5, model: str = "",
                    max_steps_per_round: int = 0) -> dict:
    """目标迭代：每轮用**全新子代理**推进目标，直到子代理声明完成或用尽轮次。"""
    objective = str(objective or "").strip()
    if not objective:
        raise b.ToolError("objective 不能为空")
    rounds = max(1, min(int(max_rounds or 5), 20))
    handoff = ""
    history = []
    for i in range(rounds):
        task = (f"目标：{objective}\n\n这是第 {i + 1}/{rounds} 轮迭代。"
                + (f"上一轮交接：\n{handoff}\n\n" if handoff else "")
                + "推进目标。**如果目标已完全达成**，答复第一行写 GOAL_COMPLETE，然后给结论；"
                  "否则第一行写 GOAL_CONTINUE，并简要说明下一步交给下一轮。")
        res = await b.run_subagent(task, description=f"ralph 第 {i + 1} 轮",
                                   model=model, max_steps=max_steps_per_round)
        content = str(res.get("content") or "")
        history.append({"round": i + 1, "agent_id": res.get("agent_id"),
                        "status": "complete" if "GOAL_COMPLETE" in content[:200] else "continue",
                        "excerpt": content[:500], "error": res.get("error")})
        if "GOAL_COMPLETE" in content[:200]:
            return {"objective": objective, "rounds": history, "complete": True}
        handoff = content[:2000]
        if res.get("error"):
            break
    return {"objective": objective, "rounds": history, "complete": False}


async def run_workflow(spec: dict, model: str = "") -> dict:
    """按阶段编排子代理（顺序执行：浏览器通道只有一个页面，不能真并发）。"""
    if not isinstance(spec, dict):
        raise b.ToolError("workflow 需要对象：{name, phases:[{title, agents:[{prompt,label}]}]}")
    phases = spec.get("phases") or []
    if not isinstance(phases, list) or not phases:
        raise b.ToolError("workflow.phases 不能为空")
    results = []
    for phase in phases[:10]:
        if not isinstance(phase, dict):
            continue
        title = str(phase.get("title") or "")
        agents = phase.get("agents") or []
        for a in (agents or [])[:8]:
            if isinstance(a, str):
                prompt, label = a, title
            elif isinstance(a, dict):
                prompt = str(a.get("prompt") or "")
                label = str(a.get("label") or title)
            else:
                continue
            if not prompt.strip():
                continue
            res = await b.run_subagent(prompt, description=f"{title}｜{label}"[:120], model=model)
            results.append({"phase": title, "label": label,
                            "agent_id": res.get("agent_id"),
                            "content": res.get("content", ""), "error": res.get("error")})
    return {"workflow": str(spec.get("name") or ""), "results": results}


# ── 对抗式审计（adversarial audit）────────────────────────────────────
# 编排：每一轮 N 名审计员（不同角度）各自出 JSON 问题清单 → 裁判核验证据
# （自己 read_text 打开文件与行号）、合并去重、重新分级 → 只剩 p2 或没有
# 问题（verdict=clean/minor）才收敛；否则可选自动修复后再审一轮。
#
# 收敛与资源保护：rounds ≤ 8、auditors ≤ 6；网页通道顺序跑（窗口数 1 时
# 排队等窗口的代价大于并行收益），提供商通道才并行（上限 agent.subagent_parallel）。

_AUDIT_PERSPECTIVES = [
    ("correctness", "正确性：逻辑错误、边界条件、空值/缺省、并发与时序、回滚完整性"),
    ("robustness", "健壮性：异常路径、超时与重试、资源释放、失败降级"),
    ("security", "安全：越权与注入、路径逃逸、凭据泄露、未校验输入"),
    ("consistency", "一致性：与项目约定/文档/测试的一致、命名、i18n、代码风格"),
]

_AUDIT_JSON_RULE = """最终输出**只输出**一个 JSON 数组（不要 markdown 围栏、不要解释）：
[{"title":"一句话问题","severity":"p0|p1|p2","evidence":{"file":"相对路径","lines":"如 120-135"},"suggestion":"怎么修"}]
没有问题就输出 []。不要在 JSON 外面写任何文字。"""


def _audit_parse(text: str):
    """容忍解析模型输出里的 JSON：剥 markdown 围栏，从第一个 { 或 [ 起 raw_decode。"""
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    dec = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch in "{[":
            try:
                return dec.raw_decode(t, i)[0]
            except Exception:
                continue
    return None


def _audit_verdict_of(issues: list) -> str:
    if not issues:
        return "clean"
    return ("minor" if all(str(i.get("severity") or "") == "p2" for i in issues) else "major")


async def adversarial_audit(target: str = "", objective: str = "", auditors: int = 3,
                            max_rounds: int = 3, auto_fix: bool = True, model: str = "",
                            session_id: str = "") -> dict:
    """对抗式审计主循环：审计员(并行) → 裁判核验 → 修复 → 再审，直至仅剩小问题。

    裁判会**亲自核验证据**（read_text 打开文件与行号），合并去重并重新分级，
    从而抑制审计员"幻觉问题"。返回结构化结果，并把每轮报告写进父会话消息。
    """
    objective = str(objective or "").strip() or "审查当前工作区的本次改动"
    target = str(target or "").strip()
    if not target:
        target = "当前工作区（" + str(b.task_tools().root) + "）"
    auditors = max(2, min(int(auditors or 3), 6))
    max_rounds = max(1, min(int(max_rounds or 3), 8))

    if not session_id:
        session_id = "audit-" + uuid.uuid4().hex[:10]
    sid = session_id
    if not b.AGENT_SESSIONS.get(sid):
        b.AGENT_SESSIONS.create(sid, "对抗式审计: " + objective[:80],
                                str(b.task_tools().root), model or b.DEFAULT_MODEL)
    b.AGENT_SESSIONS.add_message(
        sid, "user",
        f"对抗式审计：{objective}\n范围：{target}\n"
        f"配置：{auditors} 名审计员 · 最多 {max_rounds} 轮 · 自动修复={'开' if auto_fix else '关'}",
        int((b.AGENT_SESSIONS.get(sid) or {}).get("conversation_index", 0)))

    perspectives = list(_AUDIT_PERSPECTIVES[:auditors])
    if len(perspectives) < auditors:            # 超过预设角度数就循环使用
        perspectives = (perspectives * ((auditors // len(perspectives)) + 1))[:auditors]

    pm = b.providers_mod.split_provider_model(model or "")
    parallel = bool(pm and pm[0] in b.PROVIDERS) and auditors > 1

    history: list[dict] = []
    fix_count = 0
    final_issues: list[dict] = []

    for rnd in range(1, max_rounds + 1):
        b.runtime_update(sid, phase="running",
                         detail=f"对抗式审计第 {rnd}/{max_rounds} 轮：{auditors} 名审计员各自审查")
        tasks = [
            (f"你是一名对抗式代码审计员（角度：{label}（{desc}））。\n"
             f"审计目标：{objective}\n审查范围：{target}\n"
             "要求：\n"
             "1. 只读探索，绝不修改文件（read_text / find_files / list_directory / grep）。\n"
             "2. 先列出你检查了哪些文件，再逐个报告问题。\n"
             "3. 只报**有证据**的问题：必须给出文件路径与行号；不要猜、不要泛泛。\n"
             "4. severity：p0=数据损坏/安全/服务不可用；p1=明显缺陷/错误；p2=小问题/风格。\n"
             + _AUDIT_JSON_RULE)
            for label, desc in perspectives
        ]

        results: list[dict] = []
        if parallel:
            sem = asyncio.Semaphore(max(1, b.subagent_parallel()))

            async def _one(i: int, task: str):
                async with sem:
                    return await b.run_subagent(task, description=f"审计员{i}·第{rnd}轮",
                                                model=model)

            results = list(await asyncio.gather(*(_one(i + 1, t) for i, t in enumerate(tasks))))
        else:
            for i, t in enumerate(tasks):
                results.append(await b.run_subagent(t, description=f"审计员{i+1}·第{rnd}轮",
                                                    model=model))

        raw_issues: list[dict] = []
        for i, res in enumerate(results):
            content = str(res.get("content") or "")
            obj = _audit_parse(content)
            items = obj if isinstance(obj, list) else []
            for it in items:
                if isinstance(it, dict) and it.get("title"):
                    raw_issues.append({**it, "auditor": perspectives[i][0]})
            if res.get("error"):
                b.AGENT_SESSIONS.add_step(sid, int((b.AGENT_SESSIONS.get(sid) or {})
                                                   .get("total_steps", 0)) + 1,
                                          int((b.AGENT_SESSIONS.get(sid) or {})
                                              .get("conversation_index", 0)),
                                          {"step": 0, "tool": "audit_auditor", "status": "error",
                                           "reason": f"审计员 {perspectives[i][0]} 失败",
                                           "result": {"error": str(res.get("error"))}})

        b.runtime_update(sid, detail=f"第 {rnd} 轮：{len(raw_issues)} 个原始问题，裁判合并核验…")

        judge_prompt = (
            "你是对抗式审计的裁判。下面 JSON 是审计员的原始清单（含 auditor 字段）。\n"
            "要求：\n"
            "1. 合并去重（同一位置/同一问题合并，保留 sources）。\n"
            "2. 对每个证据核验：用 read_text 打开对应文件与行号确认；证据不实的剔除。\n"
            "3. 重新分级 severity（p0/p1/p2）。\n"
            "4. 输出**只输出**一个 JSON 对象：\n"
            '{"issues":[{"title","severity","evidence":{"file","lines"},"suggestion","sources":[...]}],'
            '"false_positives":[{"title","why"}],"verdict":"clean|minor|major"}\n'
            "verdict 判定：没有任何 p0/p1 → minor；有 p0/p1 → major；一条问题都没有 → clean。\n\n"
            f"原始清单：\n{json.dumps(raw_issues, ensure_ascii=False)[:20000]}"
        )
        judge = await b.run_subagent(judge_prompt, description=f"裁判·第{rnd}轮", model=model)
        verdict_obj = _audit_parse(str(judge.get("content") or ""))
        if not isinstance(verdict_obj, dict):
            verdict_obj = {"issues": raw_issues, "false_positives": [],
                           "verdict": "major",
                           "judge_error": "裁判输出无法解析，按 major 处理（保守）"}
        issues = [x for x in (verdict_obj.get("issues") or []) if isinstance(x, dict)]
        verdict = str(verdict_obj.get("verdict") or "").lower()
        if verdict not in ("clean", "minor", "major"):
            verdict = _audit_verdict_of(issues)
        history.append({"round": rnd, "auditors": auditors,
                        "raw_issues": len(raw_issues), "issues": len(issues),
                        "verdict": verdict,
                        "false_positives": len(verdict_obj.get("false_positives") or [])})
        final_issues = issues

        report = [f"### 第 {rnd} 轮审计（裁判判定：**{verdict}**）"]
        if issues:
            for it in issues:
                ev = it.get("evidence") or {}
                report.append(
                    f"- **[{it.get('severity','?')}] {it.get('title','')}**"
                    f"（{ev.get('file','?')}:{ev.get('lines','?')}）"
                    f" {it.get('suggestion','')}")
        else:
            report.append("- 裁判确认没有问题。")
        fps = verdict_obj.get("false_positives") or []
        if fps:
            report.append(f"- 剔除误报 {len(fps)} 条："
                          + "；".join(str(x.get('title', ''))[:40] for x in fps[:5]))
        b.AGENT_SESSIONS.add_message(sid, "assistant", "\n".join(report), b._conv_index(sid))

        if verdict != "major":
            break
        if rnd >= max_rounds or not auto_fix:
            break

        # 修复轮：修完下一轮再审
        b.runtime_update(sid, detail=f"第 {rnd} 轮有 p0/p1，进入修复…")
        fixer_prompt = (
            "你是对抗式审计的修复工程师。以下是裁判确认的问题（JSON）。\n"
            "要求：\n"
            "1. 逐个修复，**最小改动**，不顺手重构无关代码；\n"
            "2. 改完必须跑 validate_python + run_unittest 双通过；\n"
            "3. 最终输出一个 JSON 对象 {\"fixed\":[\"标题\"],\"failed\":[\"标题\"]}\n\n"
            f"问题清单：\n{json.dumps(issues, ensure_ascii=False)[:20000]}"
        )
        fix_res = await b.run_subagent(fixer_prompt, description=f"修复·第{rnd}轮", model=model)
        fix_count += 1
        b.AGENT_SESSIONS.add_message(sid, "assistant",
                                     f"### 修复（第 {rnd} 轮）\n"
                                     + str(fix_res.get("content") or "")[:4000],
                                     b._conv_index(sid))

    complete = bool(history) and history[-1]["verdict"] in ("clean", "minor")
    b.AGENT_SESSIONS.add_message(
        sid, "assistant",
        ("### 审计完成\n"
         f"- 轮次：{len(history)}，修复 {fix_count} 次\n"
         f"- 收敛：{'仅剩小问题，通过' if complete else '未收敛（仍有 p0/p1）'}\n"
         f"- 最终问题数：{len(final_issues)}"),
        b._conv_index(sid))
    b.AGENT_SESSIONS.patch(sid, state="completed")
    b.runtime_update(sid, phase="idle", detail="对抗式审计完成")
    return {"session_id": sid, "objective": objective, "target": target,
            "rounds": history, "fixes": fix_count, "complete": complete,
            "verdict": (history[-1]["verdict"] if history else ""),
            "issues": final_issues}
