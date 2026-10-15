"""事件投影（P2-10 第二期）：把事件流重放成状态，验证"日志足以重建状态"。

**本期的边界**：只**新增**投影函数与一致性校验，**不改变任何读取路径**——运行时仍然读
数据库列。这样做的好处是零风险：投影算错也不影响任务；等投影被测试与不变量反复证明
与列一致之后，第三期才考虑把读取路径切到投影上。

投影覆盖的生命周期：回合（turn_begin/turn_done）、工具派发（step_begin/step_done）、
工具结果入库（step_recorded）、审批（approval_granted/taken/denied）、状态迁移（state）。
"""

from __future__ import annotations

from typing import Any

# 投影认识的 kind（未知 kind 会被计数但不参与状态重建——便于将来加事件而不破坏旧投影）
KNOWN_KINDS = (
    "turn_begin", "turn_done", "step_begin", "step_done", "step_recorded",
    "approval_granted", "approval_taken", "approval_denied", "state",
    "message", "files_modified", "file_event", "queue",
)


def project(events: list[dict[str, Any]]) -> dict[str, Any]:
    """把按 id 升序的事件流折叠成一个状态快照（纯函数，便于测试与重放）。"""
    turns: dict[str, dict[str, Any]] = {}
    steps: dict[tuple[int, str], dict[str, Any]] = {}
    approvals = {"granted": 0, "taken": 0, "denied": 0}
    states: list[dict[str, Any]] = []
    recorded_steps: list[int] = []
    messages: list[dict[str, Any]] = []
    changed_files: list[str] = []
    queues: dict[str, dict[str, Any]] = {}
    unknown: dict[str, int] = {}

    for ev in events:
        kind = ev.get("kind")
        payload = ev.get("payload") or {}
        if kind == "turn_begin":
            key = str(payload.get("prompt_hash"))
            turns[key] = {"prompt_hash": key,
                          "steps_before": payload.get("steps_before"),
                          "conversation_index": payload.get("conversation_index"),
                          "steps_after": None, "answer_chars": None, "done": False}
        elif kind == "turn_done":
            key = str(payload.get("prompt_hash"))
            item = turns.setdefault(key, {"prompt_hash": key, "steps_before": None,
                                          "conversation_index": None})
            item.update({"steps_after": payload.get("steps_after"),
                         "answer_chars": payload.get("answer_chars"), "done": True})
        elif kind == "step_begin":
            steps[(int(payload.get("step_no") or 0), str(payload.get("tool") or ""))] = {
                "step_no": payload.get("step_no"), "tool": payload.get("tool"),
                "args_hash": payload.get("args_hash"), "status": "inflight"}
        elif kind == "step_done":
            for (no, tool), item in steps.items():
                if no == int(payload.get("step_no") or 0) and item["status"] == "inflight":
                    item["status"] = payload.get("status") or "done"
                    break
        elif kind == "step_recorded":
            recorded_steps.append(int(payload.get("step_no") or 0))
        elif kind == "approval_granted":
            approvals["granted"] += 1
        elif kind == "approval_taken":
            approvals["taken"] += 1
        elif kind == "approval_denied":
            approvals["denied"] += 1
        elif kind == "state":
            states.append({"from": payload.get("from"), "to": payload.get("to"),
                           "id": ev.get("id")})
        elif kind == "message":
            messages.append({"role": payload.get("role"), "chars": payload.get("chars"),
                             "conversation_index": payload.get("conversation_index"),
                             "message_id": payload.get("message_id")})
        elif kind == "queue":
            queues[str(payload.get("queue") or "pending")] = {
                "before": payload.get("before"), "after": payload.get("after")}
        elif kind == "file_event":
            path = str(payload.get("path") or "")
            if path and path not in changed_files:
                changed_files.append(path)
        elif kind == "files_modified":
            for path in (payload.get("added") or []):
                if path not in changed_files:
                    changed_files.append(path)
        else:
            unknown[str(kind)] = unknown.get(str(kind), 0) + 1

    return {
        "turns": list(turns.values()),
        "steps": list(steps.values()),
        "approvals": approvals,
        "states": states,
        "recorded_steps": sorted(recorded_steps),
        "messages": messages,
        "changed_files": changed_files,
        "queues": queues,
        "unknown_kinds": unknown,
        "event_count": len(events),
        "last_event_id": max((int(e.get("id") or 0) for e in events), default=0),
        "current_state": (states[-1]["to"] if states else None),
    }


def compare_with_columns(projection: dict[str, Any], turns_rows: list[dict[str, Any]],
                         steps_rows: list[dict[str, Any]]) -> list[str]:
    """返回投影与数据库列不一致的说明（空列表=一致）。

    这是第二期的**验收标准**：投影必须能重建列里的事实，否则"日志即真相"就是空话。
    """
    problems: list[str] = []
    proj_turns = {t["prompt_hash"]: t for t in projection.get("turns") or []}
    for row in turns_rows:
        key = str(row.get("prompt_hash"))
        item = proj_turns.get(key)
        if item is None:
            problems.append(f"事件里缺少回合 {key[:8]}")
            continue
        if str(row.get("status")) == "done" and not item.get("done"):
            problems.append(f"回合 {key[:8]} 列里是 done，事件里没有 turn_done")
        if item.get("steps_after") not in (None, row.get("steps_after")):
            problems.append(f"回合 {key[:8]} steps_after 不一致："
                            f"事件 {item.get('steps_after')} vs 列 {row.get('steps_after')}")

    proj_steps = {(int(s.get("step_no") or 0), str(s.get("tool") or "")): s
                  for s in projection.get("steps") or []}
    for row in steps_rows:
        key = (int(row.get("step_no") or 0), str(row.get("tool") or ""))
        item = proj_steps.get(key)
        if item is None:
            problems.append(f"事件里缺少步骤 {key}")
            continue
        if str(row.get("status")) != "inflight" and item.get("status") == "inflight":
            problems.append(f"步骤 {key} 列里已收尾（{row.get('status')}），事件里仍 inflight")
    return problems
