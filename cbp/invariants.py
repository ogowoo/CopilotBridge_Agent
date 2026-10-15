"""运行时不变量注册表（借 DSH 的 dsh-invariants 思路，最小可用版）。

**为什么需要**：本项目大量契约是"隐式"的——工具名与分组表必须一致、步骤表必须
append-only、turn_results 的 status 只能取固定值、可选回调必须向后兼容……
这些约束平时靠人和测试记住，一旦远端重构或本地改动就会静默失效。

**做法**：每个模块把自己的契约注册成一个检查项（`@invariant("名字")`），
`check_all()` 一次跑完并返回失败清单；`scripts/invariants.py` 可在提交前跑，
`tests/test_invariants.py` 让它在测试套件里始终生效。

**刻意不做**的事（与 DSH 相同的取舍）：不检查"方法是否存在"这类反射细节，
只检查**真正会导致行为错误**的关系；检查失败要能指出**归属模块**。
"""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

_REGISTRY: dict[str, Callable[[], None]] = {}


class InvariantError(AssertionError):
    """某条不变量被违反；message 里必须带归属。"""


def invariant(name: str):
    """注册一个不变量检查。被装饰函数无参数、无返回；违反时抛异常即可。"""

    def deco(fn: Callable[[], None]) -> Callable[[], None]:
        if name in _REGISTRY:
            raise RuntimeError(f"不变量重名: {name}")
        _REGISTRY[name] = fn
        return fn

    return deco


def registered() -> list[str]:
    return sorted(_REGISTRY)


def check_all() -> dict[str, str]:
    """跑全部检查，返回 {不变量名: 失败原因}（空字典=全通过）。"""
    failures: dict[str, str] = {}
    for name in registered():
        try:
            _REGISTRY[name]()
        except Exception as exc:  # noqa: BLE001 —— 检查失败不该中断其它检查
            failures[name] = f"{type(exc).__name__}: {exc}"
    return failures


# ── 检查项 ────────────────────────────────────────────────────────────────


@invariant("工具分组表与注册表一致")
def _tool_groups_match_registry() -> None:
    """TOOL_GROUPS 里出现的工具名必须真的存在，且每组至少一个工具。

    远端加/改工具后最容易忘的就是这张表；名字写错会让 P0-1 裁剪把工具误裁掉。
    """
    from .tools import TOOL_GROUPS

    ws = Path(tempfile.mkdtemp(prefix="cbp_inv_"))
    try:
        from .tools import ToolRegistry

        names = {item["name"] for item in ToolRegistry(ws, {})._descriptions_all()}
        for group, members in TOOL_GROUPS.items():
            if not members:
                raise InvariantError(f"分组 {group} 为空")
            missing = []
            for m in members:
                if m in names:
                    continue
                # 允许两类"当前环境不存在但确实合法"的工具：
                #   ① 桥接层 register() 注入的（copilot_*）
                #   ② 条件注册的（memory_* 需注入 MemoryStore；skill_* 需扫描到技能目录）
                if m.startswith(("copilot_", "memory_", "skill_")):
                    continue
                # 兜底：类上真有同名方法（说明工具确实存在，只是本次没进目录）
                if hasattr(ToolRegistry, m):
                    continue
                missing.append(m)
            if missing:
                raise InvariantError(f"分组 {group} 里的工具不存在: {missing}")
    finally:
        import shutil

        shutil.rmtree(ws, ignore_errors=True)


@invariant("core 组始终包含最小工具面")
def _core_group_is_minimal_surface() -> None:
    from .tools import TOOL_GROUPS

    core = set(TOOL_GROUPS.get("core", ()))
    required = {"read_text", "write_text_b64", "replace_text", "validate_python",
                "run_unittest", "list_directory", "tool_list"}
    missing = sorted(required - core)
    if missing:
        raise InvariantError(f"core 组缺少必备工具: {missing}")


@invariant("steps 表保持 append-only")
def _steps_table_is_append_only() -> None:
    """steps 靠自增 id 排序（step_no 会复用），任何 UPDATE 都会破坏时间线。"""
    src = (HERE / "session_store.py").read_text(encoding="utf-8")
    bad = re.findall(r"UPDATE\s+steps\b", src, flags=re.I)
    if bad:
        raise InvariantError("session_store 里出现了 UPDATE steps（应为 append-only）")


@invariant("状态列取值受控")
def _status_columns_are_controlled() -> None:
    """只有 turn_results / step_dispatch 的 status 字面量受本约束。

    注意：sessions.state 允许 running/waiting/paused/cancelled/failed/completed，
    那是另一套状态机，不要混进来（第一版检查就把它误判了）。
    """
    src = (HERE / "session_store.py").read_text(encoding="utf-8")
    allowed = {"inflight", "done", "error", "unknown"}
    checked = 0
    for line in src.splitlines():
        if "turn_results" not in line and "step_dispatch" not in line:
            continue
        for literal in re.findall(r"status\s*=\s*'([a-z_]+)'", line):
            checked += 1
            if literal not in allowed:
                raise InvariantError(f"检查点表出现未约定的 status: {literal}")
    if not checked:
        raise InvariantError("未找到检查点表的 status 写入（契约可能已被改掉）")
    for want in ("begin_turn", "save_turn", "begin_step", "finish_step", "pending_steps"):
        if f"def {want}" not in src:
            raise InvariantError(f"缺少方法 {want}（回合/步骤检查点契约）")


@invariant("回合与步骤检查点表可独立存在")
def _checkpoint_tables_work() -> None:
    """新库必须能建出 turn_results / step_dispatch，并支持登记→收尾。"""
    from .session_store import SessionStore

    tmp = Path(tempfile.mkdtemp(prefix="cbp_inv_db_"))
    try:
        st = SessionStore(tmp / "a.db")
        st.create("s1", "任务", str(tmp), "copilot-auto")
        st.begin_turn("s1", 0, "h1", 0)
        if (st.get_turn("s1", 0, "h1") or {}).get("status") != "inflight":
            raise InvariantError("begin_turn 未写入 inflight")
        st.save_turn("s1", 0, "h1", "答案", 1)
        if (st.get_turn("s1", 0, "h1") or {}).get("status") != "done":
            raise InvariantError("save_turn 未转为 done")
        st.begin_step("s1", 0, 1, "outlook_send", "ah")
        if not st.pending_steps("s1"):
            raise InvariantError("begin_step 未留下未收尾步骤")
        st.finish_step("s1", 0, 1)
        if st.pending_steps("s1"):
            raise InvariantError("finish_step 未清掉未收尾步骤")
        con = sqlite3.connect(tmp / "a.db")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        if not st.events("s1"):
            raise InvariantError("检查点动作没有写出任何事件（P2-10 第一期的接线断了）")
        kinds = {e["kind"] for e in st.events("s1")}
        for want in ("turn_begin", "turn_done", "step_begin", "step_done"):
            if want not in kinds:
                raise InvariantError(f"缺少事件 {want}（事件=状态重建的依据）")
        for t in ("turn_results", "step_dispatch", "steps", "sessions", "events", "approvals"):
            if t not in tables:
                raise InvariantError(f"缺少表 {t}")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


@invariant("AgentRunner 可选回调保持向后兼容")
def _runner_callbacks_optional() -> None:
    """step_begin/step_finish 必须是带默认值的可选参数（旧调用方不传也要能跑）。"""
    from .agent import AgentRunner

    sig = inspect.signature(AgentRunner.__init__)
    for name in ("step_begin", "step_finish"):
        if name not in sig.parameters:
            raise InvariantError(f"AgentRunner 缺少可选回调 {name}")
        if sig.parameters[name].default is not inspect._empty:
            continue
        raise InvariantError(f"{name} 必须有默认值（否则旧调用方全部报错）")


@invariant("评测断言名与实现一致")
def _eval_assertions_exist() -> None:
    """tasks.json 里用到的断言键必须在 eval.py 里实现，避免评测静默不生效。"""
    tasks = ROOT / "tests" / "evals" / "tasks.json"
    evals = ROOT / "scripts" / "eval.py"
    if not tasks.is_file() or not evals.is_file():
        raise InvariantError("缺少 tests/evals/tasks.json 或 scripts/eval.py")
    spec = json.loads(tasks.read_text(encoding="utf-8"))
    src = evals.read_text(encoding="utf-8")
    keys: set[str] = set()
    for task in spec.get("tasks", []):
        keys.update((task.get("assert") or {}).keys())
    missing = sorted(k for k in keys if k not in src)
    if missing:
        raise InvariantError(f"tasks.json 用到但 eval.py 未实现: {missing}")


@invariant("提示词工具清单来自单一数据源")
def _prompt_tools_single_source() -> None:
    """系统提示词里的工具清单必须由 tools.descriptions() 生成，不得手写第三方清单。"""
    src = (HERE / "agent.py").read_text(encoding="utf-8")
    if "self.tools.descriptions(" not in src:
        raise InvariantError("未从 tools.descriptions() 取工具清单")
    if re.search(r"可用工具：\n\[\s*\{", src):
        raise InvariantError("提示词里出现了手写的工具清单")


@invariant("破坏性工具的一次性审批契约")
def _approval_contract() -> None:
    """三类破坏性工具必须默认要求审批，且授权是"消费式"的（allowed-once）。"""
    from .tools import APPROVAL_REQUIRED_TOOLS, ToolRegistry

    expected = {"delete_path", "outlook_send", "run_python"}
    if set(APPROVAL_REQUIRED_TOOLS) != expected:
        raise InvariantError(f"审批工具集合变化了: {sorted(APPROVAL_REQUIRED_TOOLS)}（需同步文档与测试）")
    tmp = Path(tempfile.mkdtemp(prefix="cbp_inv_appr_"))
    try:
        t = ToolRegistry(tmp, {"allow_delete": True, "allow_outlook_send": True, "allow_python": True})
        for name in expected:
            if not t.requires_approval(name):
                raise InvariantError(f"{name} 在能力开启时未要求审批（fail-closed 被破坏）")
        t2 = ToolRegistry(tmp, {"allow_delete": True, "approval_policy": "never"})
        if t2.requires_approval("delete_path"):
            raise InvariantError("approval_policy=never 应按约定关闭审批要求")

        from .session_store import SessionStore

        st = SessionStore(tmp / "a.db")
        st.create("s1", "t", str(tmp), "m")
        if st.take_approval("s1", "delete_path", "h"):
            raise InvariantError("未授权就放行了（fail-open）")
        st.grant_approval("s1", "delete_path", "h")
        if not st.take_approval("s1", "delete_path", "h"):
            raise InvariantError("授权后仍不放行")
        if st.take_approval("s1", "delete_path", "h"):
            raise InvariantError("授权被复用（应为 allowed-once，用后即焚）")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


@invariant("状态键都在存储白名单内")
def _state_keys_are_persistable() -> None:
    """runner/bridge/api 写入的状态键必须被 SessionStore.patch 接受。

    这是真被抓到的缺陷类别：patch 有键名白名单，写不在名单里的键会**静默丢弃**
    （pending_steps、approval_pending 都曾因此失效，而单测完全看不出来）。
    用 AST 取调用的关键字实参，避免把 json.dumps(ensure_ascii=...) 这类嵌套调用误判。
    """
    import ast

    store_src = (HERE / "session_store.py").read_text(encoding="utf-8")
    m = re.search(r"allowed\s*=\s*\{(.*?)\}", store_src, re.S)
    if not m:
        raise InvariantError("未能在 session_store 里解析出 allowed 白名单")
    allowed = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    if len(allowed) < 10:
        raise InvariantError("allowed 白名单解析异常（过少）")

    checked = 0
    for fname in ("agent.py", "bridge.py", "api.py"):
        tree = ast.parse((HERE / fname).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in ("state_saver", "patch", "save"):
                continue
            for kw in node.keywords:
                if not kw.arg:
                    continue
                checked += 1
                if kw.arg not in allowed:
                    raise InvariantError(
                        f"{fname}:{node.lineno} 写入状态键 {kw.arg}，不在存储白名单里（会被静默丢弃）")
    if checked < 15:
        raise InvariantError(f"状态键检查覆盖太少（{checked} 处），AST 扫描可能失效")
