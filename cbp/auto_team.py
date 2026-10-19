# -*- coding: utf-8 -*-
"""按 todo 自动派子代理（默认关闭）。

用户诉求："subagent 也应该由 cbp 来自动动态分配"。

现状：`subagent_*` 完全靠模型自觉调用（实测整改阶段 15 步一次没调），面板里那几个
还是上一阶段的遗留，看着像摆设。

这个模块提供一个**保守**的自动派发：只在
  · 配置 `auto_team_enabled=true`（默认关，面板/配置里手动打开）
  · 存在 ≥2 个 `pending` 且**看起来互不依赖**的待办
  · 当前没有子代理在跑
时，把这几项派给子代理跑一轮，并把 agent_id 记回对应待办。

风险说明：每个子代理都是一条独立的 Copilot 对话 = 成倍消耗额度，所以默认关闭。
"""
from __future__ import annotations

import re
from typing import Any

# 视为"同一范畴"的关键词：两个待办共享这些词就当作有依赖，不并行派发
_PATH_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.(?:py|js|ts|md|json|yml|yaml|toml|cfg|ini)")
_MOD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def is_enabled(config: dict[str, Any] | None) -> bool:
    cfg = config or {}
    return bool(cfg.get("auto_team_enabled", False))


def _scope_of(todo: dict[str, Any]) -> set[str]:
    """一项待办的"作用域"：路径与长标识符，用来判断两项是否互不依赖。"""
    text = str(todo.get("content") or "")
    scope = {m.group(0).lower() for m in _PATH_RE.finditer(text)}
    scope |= {m.group(0).lower() for m in _MOD_RE.finditer(text) if len(m.group(0)) >= 4}
    return scope


def plan_dispatch(todos: list[dict[str, Any]] | None, running_children: int = 0,
                  max_agents: int = 4) -> list[dict[str, Any]]:
    """挑出可以并行派发的待办（纯函数，便于测试）。

    规则：
    - 只看 `pending`；
    - 跳过 `blocked`/已完成；
    - 作用域有交集的两项视为有依赖，只取前者（保持清单顺序）；
    - 至少 2 项才值得派（1 项自己做完更快，子代理有固定开销）；
    - 已有子代理在跑时不派（避免叠加）。
    """
    if running_children > 0:
        return []
    pending = [t for t in (todos or []) if str(t.get("status")) == "pending"]
    picked: list[dict[str, Any]] = []
    used: set[str] = set()
    for t in pending:
        scope = _scope_of(t)
        if scope & used:                     # 与已选项有交集 → 有依赖
            continue
        used |= scope
        picked.append(t)
        if len(picked) >= max(2, max_agents):
            break
    return picked if len(picked) >= 2 else []


def build_tasks(items: list[dict[str, Any]], goal: str = "") -> list[str]:
    """把待办转成自足的子代理任务文本（子代理看不到父会话）。"""
    tasks: list[str] = []
    for i, t in enumerate(items, 1):
        content = str(t.get("content") or "").strip()
        if not content:
            continue
        head = f"任务目标（第 {i} 项，独立完成）：{content}"
        ctx = f"\n所属大目标：{goal.strip()}" if goal else ""
        tasks.append(
            head + ctx +
            "\n要求：只做这一项；涉及文件先读后改；改完在本机验证（validate_python / "
            "run_unittest）；最后用 3 行以内汇报：改了什么、验证结果、还有什么风险。"
        )
    return tasks


async def maybe_dispatch(session_store: Any, sid: str, config: dict[str, Any] | None) -> int:
    """满足条件时派一轮子代理；返回派出去的个数（0 表示没派）。

    调用方负责 try/except —— 自动派发失败绝不能影响主任务。
    """
    if not is_enabled(config):
        return 0
    state = session_store.get(sid) or {}
    todos = state.get("todos") or []
    if not todos:
        return 0
    try:
        children = [c for c in session_store.list(include_archived=False)
                    if c.get("parent_id") == sid and c.get("state") in ("running", "waiting")]
    except Exception:
        children = []
    picked = plan_dispatch(todos, running_children=len(children),
                           max_agents=int((config or {}).get("auto_team_max_agents", 4) or 4))
    if not picked:
        return 0

    from . import bridge as b
    from . import fanout as fo

    tasks = build_tasks(picked, goal=str(state.get("original_task") or "")[:200])
    if len(tasks) < 2:
        return 0
    res = await fo.fanout(tasks, model=str(state.get("model") or ""),
                          workspace=str(state.get("workspace") or ""),
                          max_steps=int((config or {}).get("auto_team_max_steps", 30) or 30),
                          parent_title=f"自动派发：{str(state.get('title') or sid)[:20]}")
    agents = [x.get("agent_id") for x in (res.get("results") or []) if x.get("agent_id")]
    # 把 agent_id 记回对应待办，面板/清单能看出"这项由谁在跑"
    by_content = {str(t.get("content") or "").strip(): t for t in picked}
    updated = []
    for t in todos:
        key = str(t.get("content") or "").strip()
        if key in by_content and agents:
            t = dict(t)
            t["status"] = "in_progress"
            t["agent_id"] = agents[min(len(updated), len(agents) - 1)]
            updated.append(t)
        else:
            updated.append(t)
    if updated:
        session_store.patch(sid, todos=updated)
    return len(agents)
