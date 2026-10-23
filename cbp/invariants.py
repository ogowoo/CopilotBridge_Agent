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
        # 扩展工具模块自己声明审批（register(..., approval=True)），核心代码不特判
        from .tools_extra import register_extra_tools as _reg_extra
        _reg_extra(t, {"allow_shell": True})
        if "pwsh" in t._tools and not t.requires_approval("pwsh"):
            raise InvariantError("pwsh 未按 register(approval=True) 要求审批")
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
    for fname in ("agent.py", "bridge.py", "api.py",
                     "routes_core.py", "routes_config.py",
                     "routes_agents.py", "routes_sessions.py"):
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


@invariant("事件流足以重建检查点状态")
def _events_reconstruct_checkpoints() -> None:
    """投影（P2-10 第二期）必须能从事件重建列里的事实。

    这条守住的是"日志即真相"的**前提**：只要事件覆盖退化（少发/漏发），投影与列就会
    不一致，这里立刻失败——而不是等到第三期切读取路径时才发现。
    """
    from .projection import compare_with_columns, project
    from .session_store import SessionStore

    tmp = Path(tempfile.mkdtemp(prefix="cbp_inv_proj_"))
    try:
        st = SessionStore(tmp / "a.db")
        st.create("s1", "任务", str(tmp), "m")
        st.patch("s1", state="running")
        st.begin_turn("s1", 0, "ph1", 0)
        st.save_turn("s1", 0, "ph1", "答案", 1)
        st.begin_step("s1", 0, 1, "read_text", "ah")
        st.finish_step("s1", 0, 1, "done")
        st.add_step("s1", 1, 0, {"step": 1, "tool": "read_text", "status": "ok"})
        st.patch("s1", state="completed")

        events = st.events("s1")
        con = st._connect()
        turns = [dict(r) for r in con.execute(
            "SELECT prompt_hash,status,steps_after FROM turn_results WHERE session_id='s1'")]
        steps = [dict(r) for r in con.execute(
            "SELECT step_no,tool,status FROM step_dispatch WHERE session_id='s1'")]
        problems = compare_with_columns(project(events), turns, steps)
        if problems:
            raise InvariantError("投影与列不一致: " + "; ".join(problems[:3]))
        if project(events).get("current_state") != "completed":
            raise InvariantError("状态事件未能重放出最终状态")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


@invariant("未分组工具不得被裁剪滤掉")
def _ungrouped_tools_stay_visible() -> None:
    """新增工具若忘了登记分组，也必须留在提示词里（否则模型看不到、永远不会调用）。

    本轮真机验证抓到的缺陷：`run_code` 没进 TOOL_GROUPS，于是被 P0-1 裁剪静默滤掉，
    模型照旧用 4 次 read_text 而不是 1 次 run_code。工具"存在但不可见"比没有更糟——
    文档里还写着"未登记工具归入 core"，口径与实际不一致。
    """
    from .agent import AgentRunner
    from .tools import ToolRegistry

    tmp = Path(tempfile.mkdtemp(prefix="cbp_inv_vis_"))
    try:
        all_caps = {"allow_delete": True, "allow_python": True, "allow_outlook": True,
                    "allow_outlook_send": True, "allow_office": True, "allow_run_code": True}
        tools = ToolRegistry(tmp, all_caps)
        runner = AgentRunner(tools, lambda *a, **k: None, dict(all_caps, max_total_steps=3),
                             state_getter=lambda: {"state": "ready"},
                             state_saver=lambda **k: None, step_saver=lambda s: None,
                             file_event_saver=lambda e: None)
        runner._task_text = "随便做点事"
        visible = {x["name"] for x in tools.descriptions(only=runner._active_tool_names())}
        from .tools import TOOL_GROUPS

        grouped = set().union(*TOOL_GROUPS.values()) if TOOL_GROUPS else set()
        registered = {x["name"] for x in tools.descriptions()}
        ungrouped = registered - grouped
        missing = sorted(ungrouped - visible)
        if missing:
            raise InvariantError(f"未分组工具被裁剪滤掉: {missing}（应归入 core 保持可见）")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


@invariant("API 只调用存在且有白名单的方法")
def _api_calls_exist_on_store() -> None:
    """api.py 里对会话存储的调用必须真实存在。

    真事：授权路由写了 `AGENT_SESSIONS.update(...)`，而 SessionStore 只有 `patch()` —— 
    运行时 500，静态检查与单测都没覆盖到（端到端验证才发现）。这里做一次静态核对。
    """
    from .session_store import SessionStore

    src = "".join((HERE / f).read_text(encoding="utf-8") for f in
                ("api.py", "routes_core.py", "routes_config.py",
                 "routes_agents.py", "routes_sessions.py"))
    names = set(re.findall(r"AGENT_SESSIONS\.([a-z_]+)\(", src))
    if not names:
        raise InvariantError("未在 api.py 里解析到 AGENT_SESSIONS 调用（正则失效？）")
    missing = sorted(n for n in names if not hasattr(SessionStore, n))
    if missing:
        raise InvariantError(f"api.py 调用了 SessionStore 上不存在的方法: {missing}")


@invariant("前端 data-act 与 ACTIONS 表一一对应")
def _ui_actions_wired() -> None:
    """事件委托迁移后的护栏。

    背景：为了收紧 CSP（去掉 script-src 'unsafe-inline'），页面里 49 处内联处理器
    全部改成了 `data-act`，由 app.js 的 `ACTIONS` 表统一分发。风险是**名字写错就静默失效**
    （按钮点了没反应，控制台也不报错）。这里做静态双向核对：
    - 页面/JS 里出现的每个 data-act 都要在 ACTIONS 里有实现；
    - ACTIONS 里每一项都要被引用（否则是死代码或拼错的名字）。
    """
    web = ROOT / "webui"          # 注意是 ROOT：HERE 指向 cbp/ 包目录
    if not web.is_dir():
        raise InvariantError(f"找不到 webui 目录: {web}")
    html = (web / "index.html").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")
    used = set(re.findall(r'data-act(?:-input|-key)?="([A-Za-z0-9_]+)"', html + js))
    # 动态生成的元素用 el.dataset.act='x'（不是 HTML 属性），同样算引用
    used |= set(re.findall(r"dataset\.act\s*=\s*['\"]([A-Za-z0-9_]+)['\"]", js))
    if not used:
        raise InvariantError("没有解析到任何 data-act（事件委托被回退了？）")
    parts = js.split("const ACTIONS={", 1)
    if len(parts) < 2:
        raise InvariantError("app.js 里找不到 ACTIONS 表")
    table = parts[1].split("\n};", 1)[0]
    defined = set(re.findall(r'^\s*([A-Za-z0-9_]+):', table, re.M))
    missing = sorted(used - defined)
    if missing:
        raise InvariantError(f"这些 data-act 没有实现（点击无反应）: {missing}")
    extra = sorted(defined - used)
    if extra:
        raise InvariantError(f"ACTIONS 里有未被引用的条目（死代码或拼错）: {extra}")


@invariant("页面引用的静态资源都在白名单内")
def _ui_assets_whitelisted() -> None:
    """防止"新增前端文件但忘了加白名单"。

    真事：并发工作里新增了 `webui/i18n.js` 并在 index.html 里引用，但 `/ui/{asset}` 的白名单
    仍是 `style.css/app.js/boot.js` → 该文件 404，三语言界面直接不可用，而**控制台只是一条
    资源加载失败**，很容易被忽略。这里把"页面引用"与"白名单"做静态比对。
    """
    web = ROOT / "webui"
    html = (web / "index.html").read_text(encoding="utf-8")
    api = "".join((HERE / f).read_text(encoding="utf-8") for f in
                ("api.py", "routes_core.py"))  # /ui/{asset} 端点已拆到 routes_core.py
    m = re.search(r'if asset not in \(([^)]*)\):', api)
    if not m:
        raise InvariantError("api.py 里找不到 /ui/{asset} 白名单")
    allowed = {x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()}
    refs = set(re.findall(r'<(?:script|link)[^>]*?(?:src|href)="([^"]+)"', html))
    local = {r.split("?")[0] for r in refs
             if not r.startswith(("http://", "https://", "data:", "//"))}
    missing = sorted(x for x in local if x not in allowed)
    if missing:
        raise InvariantError(f"index.html 引用了未加白名单的资源（会 404）: {missing}")
    absent = sorted(x for x in local if not (web / x).is_file())
    if absent:
        raise InvariantError(f"index.html 引用了不存在的文件: {absent}")
    stale = sorted(a2 for a2 in allowed if a2 not in local)
    if stale:
        raise InvariantError(f"白名单里有页面未引用的条目（可能已废弃）: {stale}")


@invariant("子代理面板按父会话过滤")
def _subagent_panel_filters_by_parent() -> None:
    """前端拉子代理列表时必须带 `parent=`。

    真事：面板调用 `/v1/agent/subagents` 不带 parent，后端返回**全部**子代理，
    于是用户真实任务的"Subagents"里冒出 19 个与它无关的子代理（一次测试扇出留下的）。
    """
    src = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
    for m in re.finditer(r"api\('([^']*?/v1/agent/subagents[^']*)'", src):
        if "parent=" not in m.group(1):
            line = src[:m.start()].count("\n") + 1
            raise InvariantError(
                f"webui/app.js:{line} 拉子代理没带 parent=（会串台显示别人的子代理）")


@invariant("页面里所有静态文案都有英文键")
def _static_text_translated() -> None:
    """`index.html` 里的中文文本与属性都必须能在 i18n.js 找到英文词条。

    与运行态覆盖审计的分工：运行态只能看到**当时打开**的界面，隐藏弹窗（删除结果、
    放大查看等）里的文案它永远扫不到——实测就是这样漏掉了「删除结果」「复制结果 JSON」
    「没有远程删除明细。」。这里做静态全量扫描，`data-i18n-skip` 标记的子树按数据跳过。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "i18n_static_scan", ROOT / "scripts" / "i18n_static_scan.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    missing = mod.check()
    if missing:
        raise InvariantError(f"这些静态文案没有英文词条: {missing[:6]}")


@invariant("静态属性文案都有英文键")
def _static_attrs_translated() -> None:
    """`index.html` 里含中文的 title/placeholder/aria-label/alt 必须有英文词条。

    `translateAttrs()` 在运行时按**整串精确匹配**翻这些属性；键缺失就会在英文界面
    原样露出中文（真事：工作区弹窗的「上一级 / 回到默认工作区 / 在此新建目录 /
    选择此目录」只有中文，英文模式下全露出来）。运行时扫描能抓到，但只有静态核对
    能在**改代码时**就拦住。
    """
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    i18n = (ROOT / "webui" / "i18n.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'"([^"\n]{1,80})"\s*:', i18n))
    missing = []
    for m in re.finditer(r'(title|placeholder|aria-label|alt)="([^"]*)"', html):
        attr, value = m.group(1), m.group(2)
        if not re.search(r"[\u4e00-\u9fff]", value):
            continue
        if value in keys:
            continue
        # 动态模板（含 {{ }} 或 data-i18n 标记）跳过
        if "{" in value:
            continue
        missing.append(f'{attr}="{value[:30]}"')
    if missing:
        raise InvariantError(f"这些静态属性没有英文词条: {sorted(set(missing))[:6]}")


@invariant("后端错误文案都有英文翻译")
def _backend_errors_translated() -> None:
    """`HTTPException(4xx, "中文…")` 的 detail 必须能被 `cbp/i18n.py` 翻成英文。

    真事：界面文案三语了，但错误 detail 一直中文——英文界面下 toast 里弹出
    "Session 不存在"。48 处 raise 靠人记不住，改成全局处理器 + 翻译表之后，
    这条静态核对防止新加的错误文案漏翻。
    """
    from . import i18n as backend_i18n

    src = "".join((ROOT / "cbp" / f).read_text(encoding="utf-8") for f in
                ("api.py", "routes_core.py", "routes_config.py",
                 "routes_agents.py", "routes_sessions.py"))
    msgs = re.findall(r'HTTPException\(\s*\d{3}\s*,\s*"([^"]+)"', src)
    # 纯英文/无中文的（如 "Not Found"）不需要翻译
    cjk = [m for m in msgs if re.search(r"[\u4e00-\u9fff]", m)]
    missing = backend_i18n.untranslated(cjk, "en")
    # 允许"已经是英文"的输出（模式替换后不含中文即算翻到）
    really_missing = [m for m in missing
                      if not re.search(r"[\u4e00-\u9fff]", backend_i18n.translate(m, "en"))]
    if really_missing:
        raise InvariantError(
            f"这些错误文案在英文下仍是中文（补到 cbp/i18n.py）: {really_missing[:6]}")


@invariant("title 提示不要直接写中文")
def _title_strings_translated() -> None:
    """`x.title='中文…'` 必须包 `tr()`。

    真事：模型下拉 `<option>` 的 `title` 取自后端中文说明，英文界面弹出
    "GPT-5.6 Sol 深入思考（GPT 子菜单）"；`browserBtn.title` / `memBtn.title`
    也是裸中文字面量。这类原生提示是浏览器画的，不走 DOM 翻译，也不在 i18n
    审计范围内。
    """
    cjk = re.compile(r"[\u4e00-\u9fff]")
    bad = []
    for f in sorted((ROOT / "webui").glob("*.js")):
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            m = re.search(r"\.title\s*=\s*(['\"`])(.*?)\1", line)
            if m and cjk.search(m.group(2)):
                bad.append(f"{f.name}:{i}")
            m2 = re.search(r"o\.title\s*=\s*m\.description", line)
            if m2:
                bad.append(f"{f.name}:{i}（应走 modelDesc(m)）")
    if bad:
        raise InvariantError(f"这些 title 直接写了中文（必须包 tr()）: {bad[:8]}")


@invariant("手册中英两版章节一致")
def _manual_bilingual_parity() -> None:
    """中文手册与英文手册的章节数必须相同。

    真事：手册正文原来只有中文（只翻了 h3），英文界面下"夹生"；补英文时若只改一边，
    切到另一语言就会缺章节/多了空节。两套模板都是 `const MANUAL_HTML*=` 里的模板串，
    直接数 `<h3>` 即可静态核对。
    """
    src = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
    counts = {}
    for name in ("MANUAL_HTML", "MANUAL_HTML_EN"):
        m = re.search(rf"const {name}=`(.*?)`;", src, re.S)
        if not m:
            raise InvariantError(f"webui/app.js 找不到 {name} 模板")
        counts[name] = m.group(1).count("<h3>")
    if counts["MANUAL_HTML"] != counts["MANUAL_HTML_EN"]:
        raise InvariantError(f"手册中英章节数不一致: {counts}")


# 所有"文案型"前端调用：参数里的中文必须走 tr()（否则英文界面原样露中文）
_UI_TEXT_CALLS = ("toast", "dlgConfirm", "dlgPrompt", "dlgAlert", "dlgText",
                  "showConnBanner", "showStopped", "showDeleteResult",
                  "showBoot", "gmShow", "noteText", "setStatus")


@invariant("弹窗/提示文案必须走 tr()")
def _ui_text_calls_translated() -> None:
    """toast、dlgConfirm/dlgPrompt（确认框正文）、横幅等调用里的中文必须包在 tr() 里。

    真事：`toast('已折叠 ' + n + ' ' + tr('个子代理归属会话'))` —— 只包了后半句，
    英文界面下弹出 "已折叠 2 subagent sessions"（用户实测抓到）。
    DOM 审计扫不到 toast（它是动态插入的），所以只能静态核对调用参数。
    """
    cjk = re.compile(r"[\u4e00-\u9fff]")
    bad = []
    names = "|".join(_UI_TEXT_CALLS)
    for f in sorted((ROOT / "webui").glob("*.js")):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(rf"(?<![\w.$])({names})\(", text):
            depth, j = 1, m.end()
            while j < len(text) and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            expr = text[m.end():j - 1]
            if cjk.search(expr) and "tr(" not in expr:
                line = text[:m.start()].count("\n") + 1
                bad.append(f"{f.name}:{line}({m.group(1)})")
    if bad:
        raise InvariantError(
            f"这些调用直接传了中文（必须包 tr()）: {bad[:10]}")


@invariant("i18n 调用没有被写成字符串字面量")
def _i18n_calls_not_literals() -> None:
    """防"批量替换做坏"这一真实事故。

    并发工作里有人把中文串批量包成了 `'tr(重试浏览器)'` **字符串字面量**而不是调用
    `tr('重试浏览器')`——共 61 处，用户看到的直接是按钮上的 "tr(重试浏览器)"、
    toast 里的 "tr(会话模型已切换为 )"。这类错误语法合法、单测也不覆盖，只有静态核对能拦住。
    """
    bad = []
    for f in sorted((ROOT / "webui").glob("*.js")):
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"""['"`]tr\(""", line):
                bad.append(f"{f.name}:{i}")
    if bad:
        raise InvariantError(f"这些位置把 tr(...) 写成了字符串字面量: {bad[:8]}")
    # 反向：文件里必须真的有 tr( 调用，否则说明 i18n 接线被整体回退了
    total = sum(len(re.findall(r"\btr\(", (ROOT / "webui" / n).read_text(encoding="utf-8")))
                for n in ("app.js", "i18n.js"))
    if total < 30:
        raise InvariantError(f"tr() 调用总数异常偏少（{total}），i18n 接线可能被回退")
