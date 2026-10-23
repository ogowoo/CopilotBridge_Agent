"""子代理：新建 / 继续 / 分叉 / 列出（每个子代理是独立的会话与上下文）。

为什么单独成模块：这是"派生/驱动子会话"的编排逻辑，与 run_agent 主循环、
页面自动化是不同关注点。

约定（与从 bridge 拆出的其它模块一致）：
- 运行时共享状态一律走 `b.`（AGENT_SESSIONS / task_tools / current_session /
  ACTIVE_SESSION_ID / run_subagent 依赖的页面与提供商通道等），保证测试 rebind
  生效、无 import 期循环；
- 原来函数里的 `global ACTIVE_SESSION_ID` 赋值，搬过来后写成 `b.ACTIVE_SESSION_ID =`
  （否则只改到本模块自己的全局，界面与恢复逻辑会拿到旧值）。
"""

from __future__ import annotations

import asyncio
import time
import uuid

from . import bridge as b

_SUBAGENT_MAX_DEPTH = 1          # 防递归：子代理不能再开子代理
# 在跑的子代理：{child_sid: 开始时间}。**不用裸计数器**——实测一轮子代理被中断
# （浏览器上下文被关掉/回合被取消）时计数没回落，进程内后续所有子代理就永久报
# "不能再创建子代理"，只有重启才恢复。这里按"子会话是否仍在跑 + TTL"自愈。
_SUBAGENT_ACTIVE: dict[str, float] = {}
_SUBAGENT_STALE_SECONDS = 1800


def subagent_depth() -> int:
    """当前活跃子代理数（自愈：已完成/超时的记录自动清掉）。"""
    now = time.time()
    for sid, started in list(_SUBAGENT_ACTIVE.items()):
        state = (b.AGENT_SESSIONS.get(sid) or {}).get("state")
        if state not in ("running", "waiting") or now - started > _SUBAGENT_STALE_SECONDS:
            _SUBAGENT_ACTIVE.pop(sid, None)
    return len(_SUBAGENT_ACTIVE)


async def run_subagent(task: str, description: str = "", model: str = "",
                       max_steps: int = 0) -> dict:
    """在独立子会话里跑一个 Agent 任务，返回结构化结果。"""
    task = str(task or "").strip()
    if not task:
        raise b.ToolError("task 不能为空")
    if b.subagent_chain_depth() >= _SUBAGENT_MAX_DEPTH:
        raise b.ToolError("子代理不能再创建子代理（防止无限递归）")
    # **必须用任务上下文**：模块全局 ACTIVE_SESSION_ID 会被并发任务覆盖，
    # 用它当父会话会把子代理挂到别人的会话下（实测：测试扇出的子代理挂到了
    # 用户正在跑的真实任务里，界面上莫名出现 19 个 Subagents）。
    parent_sid = b.current_session()
    # 必须用**任务级**注册表的 root：并行时全局 AGENT_TOOLS.root 可能是别的会话的
    parent_root = str(b.task_tools().root)
    child_sid = "sub-" + uuid.uuid4().hex[:10]
    parent_state = b.AGENT_SESSIONS.get(parent_sid) or {}
    child_model = b.normalize_model_soft((model or parent_state.get("model") or b.DEFAULT_MODEL).strip())
    pm = b.providers_mod.split_provider_model(child_model)
    is_provider = bool(pm and pm[0] in b.PROVIDERS)
    b.AGENT_SESSIONS.create(child_sid, task, parent_root, child_model)
    # 父子关系与短标签：子代理面板据此列出"谁派生的、在跑什么"
    b.AGENT_SESSIONS.patch(child_sid, parent_id=parent_sid,
                           description=(description or task)[:120])
    try:
        # 继承父会话的会话级授权（子代理是同一任务的一部分，不该重复授权）
        b.AGENT_SESSIONS.copy_session_approvals(parent_sid, child_sid)
    except Exception as exc:
        b.log(f"子会话继承父授权失败（不影响运行）：{exc}")
    return await _drive_child(child_sid, task, child_model, max_steps,
                              description=description, parent_sid=parent_sid,
                              parent_root=parent_root, is_provider=is_provider)


async def _drive_child(child_sid: str, task: str, model_id: str, max_steps: int,
                       description: str = "", parent_sid: str = "",
                       parent_root: str = "", is_provider: bool = False) -> dict:
    """在**已存在**的子会话上跑一轮 Agent（run_subagent 与 continue_subagent 共用）。

    注意不能走 run_agent：父回合**正持着自己的窗口**，再进 run_agent 会去抢同一把
    窗口锁而死锁（实测：subagent_send 卡了 600 秒直到客户端超时）。
    """
    # 没有父任务的页面上下文（用户入口 /v1/agent/fanout、CLI）：子代理必须**自己**
    # 占一个窗口。否则 `page` 会退回共享页且不持锁，和正在跑的任务抢同一个标签页
    # （两边的输入框/回答互相覆盖）。有父上下文时不另开窗口：父任务正持着它等我们，
    # 再要一个窗口会把自己等死（窗口数=1 时必然 30s 超时）。
    _own_window = None
    if not is_provider and b._PAGE_CTX.get() is None:
        try:
            _own_window = b._window(child_sid)
            await _own_window.__aenter__()
        except Exception as exc:
            _own_window = None
            b.log(f"子代理 {child_sid} 未拿到自动化窗口: {exc}")
            b.AGENT_SESSIONS.patch(child_sid, state="failed", last_error=str(exc))
            return {"agent_id": child_sid, "description": (description or task)[:120],
                    "content": "", "steps": 0, "model": model_id, "error": str(exc)}
    _SUBAGENT_ACTIVE[child_sid] = time.time()
    # 任务级上下文：并发跑多个子代理时，会话 id / 网页工具开关 / 递归深度必须
    # 各自独立（全局单值会互相覆盖）。token 在 finally 里按任务复位。
    _tok_sid = b._SESSION_CTX.set(child_sid)
    _tok_web = b._WEB_TOOLS_CTX.set(not is_provider)
    _tok_depth = b._SUBAGENT_DEPTH_CTX.set(b.subagent_chain_depth() + 1)
    _tok_tools = None
    try:
        b.AGENT_SESSIONS.patch(child_sid, state="running")
        # 子会话也要有对话记录：否则面板点进去只有步骤时间线、看不到"问了什么答了什么"
        try:
            b.AGENT_SESSIONS.add_message(child_sid, "user", task,
                                         b._conv_index(child_sid))
        except Exception:
            pass
        b.ACTIVE_SESSION_ID = child_sid
        # 子代理也要有自己的注册表：工作区/沙箱/网页工具开关按它来。直接用全局
        # AGENT_TOOLS 会让子代理读写"默认工作区"，父任务选的 workspace 形同虚设。
        child_reg = b.task_tools().clone()
        try:
            child_reg.set_root(parent_root or str(b.task_tools().root), False)
        except Exception as exc:
            b.log(f"子代理 {child_sid} 切换工作区失败（沿用父工作区）: {exc}")
        child_reg.current_session_id = child_sid
        child_reg.set_web_copilot_tools(not is_provider)
        # 让扩展工具也看到子代理自己的注册表（否则 pwsh/search 仍用父/全局 root）
        _tok_tools = b._TOOLS_CTX.set(child_reg)

        if is_provider:
            async def turn(prompt, model_id, force_new=False):
                return await b.provider_turn(child_sid, model_id, prompt, force_new=force_new)
        else:
            async def turn(prompt, model_id, force_new=False):
                st = b.AGENT_SESSIONS.get(child_sid) or {}
                make_new = force_new or not st.get("current_url")
                if not make_new:
                    expected = st.get("current_url") or ""
                    m = b.CONVERSATION_URL_RE.search(expected)
                    if m and m.group(0) not in b.page_url_safe():
                        await b.page.goto(expected, wait_until="domcontentloaded", timeout=120000)
                        await asyncio.sleep(1)
                        b._forget_selected_model()
                marker = b._agent_chat_marker(child_sid, int(st.get("conversation_index", 0)))
                send_prompt = (marker + "\n" + prompt) if make_new else prompt
                ans = await b.agent_chat_once(send_prompt, model_id, make_new)
                final_url = await b.stable_conversation_url(timeout=15)
                if final_url:
                    st2 = b.AGENT_SESSIONS.get(child_sid) or {}
                    urls = list(st2.get("conversation_urls") or [])
                    if final_url not in urls:
                        urls.append(final_url)
                    b.AGENT_SESSIONS.patch(child_sid, current_url=final_url, conversation_urls=urls)
                return ans

        def get():
            return b.AGENT_SESSIONS.get(child_sid)

        def save(**kw):
            if "state" in kw and kw["state"] != "waiting" and "approval_pending" not in kw:
                kw["approval_pending"] = None
            return b.AGENT_SESSIONS.patch(child_sid, **kw)

        def add_step(x):
            st = get()
            b.AGENT_SESSIONS.add_step(child_sid, int(x.get("step", st["total_steps"] + 1)),
                                      int(st["conversation_index"]), x)

        def add_file(x):
            b.AGENT_SESSIONS.add_file_event(child_sid, x)

        def add_conv_stat(x):
            b.AGENT_SESSIONS.add_conv_stat(child_sid, x)

        cfg_child = dict(b.AGENT_CFG)
        cfg_child["max_total_steps"] = max(3, min(int(max_steps or b.subagent_max_steps()), 200))
        runner = b.AgentRunner(child_reg, turn, cfg_child, get, save, add_step, add_file,
                               add_conv_stat,
                               step_begin=lambda no, tool, h: (
                                   b.AGENT_SESSIONS.begin_step(
                                       child_sid, b._conv_index(child_sid), no, tool, h),
                                   b.sync_session_todos(child_sid)),
                               step_finish=lambda no, st='done': b.AGENT_SESSIONS.finish_step(
                                   child_sid, b._conv_index(child_sid), no, st),
                               approval_check=lambda tool, h: b.AGENT_SESSIONS.take_approval(
                                   child_sid, tool, h))
        try:
            result = await runner.run(task, model_id)
            try:
                b.AGENT_SESSIONS.add_message(child_sid, "assistant",
                                             str(result.content or ""), b._conv_index(child_sid))
            except Exception:
                pass
            b.AGENT_SESSIONS.patch(child_sid, state="completed")
            return {"agent_id": child_sid, "description": (description or task)[:120],
                    "content": result.content, "steps": len(result.steps),
                    "model": model_id, "error": None}
        except Exception as exc:
            b.AGENT_SESSIONS.patch(child_sid, state="failed", last_error=str(exc))
            return {"agent_id": child_sid, "description": (description or task)[:120],
                    "content": "", "steps": 0, "model": model_id, "error": str(exc)}
    finally:
        _SUBAGENT_ACTIVE.pop(child_sid, None)
        if _own_window is not None:
            try:
                await _own_window.__aexit__(None, None, None)
            except Exception:
                pass
        # 先复位任务级上下文（并行的正确性靠它），再复位全局展示值
        try:
            if _tok_tools is not None:
                b._TOOLS_CTX.reset(_tok_tools)
            b._SESSION_CTX.reset(_tok_sid)
            b._WEB_TOOLS_CTX.reset(_tok_web)
            b._SUBAGENT_DEPTH_CTX.reset(_tok_depth)
        except Exception:
            pass
        b.ACTIVE_SESSION_ID = parent_sid
        b.AGENT_TOOLS.current_session_id = parent_sid or ""
        # 不再改全局 root：子代理用的是自己的 child_reg；改全局会把并行中的
        # 其它会话/接口看到的"当前工作区"改成父任务的工作区。
        b.AGENT_TOOLS.set_web_copilot_tools(True)


async def continue_subagent(session_id: str, message: str, model: str = "",
                            max_steps: int = 0) -> dict:
    """继续一个已存在的子代理会话（同一上下文，追加一轮）。

    与 subagent 的区别：subagent 每次新建会话；这里**复用**子会话，模型能接着说，
    适合"先让它查，再根据结果追问"的两段式协作。
    """
    sid = str(session_id or "").strip()
    child = b.AGENT_SESSIONS.get(sid)
    if not child:
        raise b.ToolError(f"没有会话 {sid}")
    if not str(child.get("parent_id") or "").strip():
        raise b.ToolError(f"{sid} 不是子代理会话（parent_id 为空），请用普通任务继续它")
    msg = str(message or "").strip()
    if not msg:
        raise b.ToolError("message 不能为空")
    if b.subagent_chain_depth() >= _SUBAGENT_MAX_DEPTH:
        raise b.ToolError("子代理不能在子代理里继续（防止递归）")
    model_id = b.normalize_model_soft((model or child.get("model") or b.DEFAULT_MODEL).strip())
    pm = b.providers_mod.split_provider_model(model_id)
    is_provider = bool(pm and pm[0] in b.PROVIDERS)
    # 关键：**不能**调 run_agent——父回合持着全局锁，再抢锁会死锁
    # （实测 subagent_send 卡 600 秒）。走与 run_subagent 相同的 _drive_child。
    return await _drive_child(sid, msg, model_id, max_steps,
                              description=str(child.get("description") or ""),
                              parent_sid=str(child.get("parent_id") or ""),
                              parent_root=str(b.task_tools().root),
                              is_provider=is_provider)


async def fork_subagent(session_id: str, description: str = "",
                        keep_messages: int = 60) -> dict:
    """从某个会话派生出新的子代理（复制最近的对话，用于"另一个方案"分支）。

    新会话的 parent_id 指向源会话的父（若源本身就是顶层会话，则指向源本身），
    因此它仍出现在子代理面板里，不会污染父会话上下文。
    """
    src_sid = str(session_id or "").strip()
    src = b.AGENT_SESSIONS.get(src_sid)
    if not src:
        raise b.ToolError(f"没有会话 {src_sid}")
    new_sid = "sub-" + uuid.uuid4().hex[:10]
    keep = max(0, min(int(keep_messages or 60), 400))
    msgs = b.AGENT_SESSIONS.messages(src_sid, limit=keep)
    b.AGENT_SESSIONS.create(new_sid,
                            src.get("original_task") or f"fork of {src_sid}",
                            src.get("workspace") or str(b.AGENT_TOOLS.root),
                            src.get("model") or b.DEFAULT_MODEL)
    b.AGENT_SESSIONS.patch(new_sid,
                           parent_id=str(src.get("parent_id") or src_sid),
                           description=(description or f"分叉自 {src_sid}")[:120])
    copied = 0
    for item in msgs:
        role = str(item.get("role") or "")
        if role not in {"user", "assistant", "system"}:
            continue
        try:
            b.AGENT_SESSIONS.add_message(new_sid, role, str(item.get("content") or ""),
                                         int(item.get("conversation_index") or 0))
            copied += 1
        except Exception:
            continue
    try:
        b.AGENT_SESSIONS.copy_session_approvals(src_sid, new_sid)
    except Exception:
        pass
    return {"agent_id": new_sid, "source": src_sid, "messages": copied,
            "description": description or f"分叉自 {src_sid}"}


async def list_subagents(parent_id: str = "", limit: int = 50) -> dict:
    """列出子代理会话（可按父会话过滤），带状态与步数。"""
    rows = b.AGENT_SESSIONS.list_subagents(parent_id or None,
                                           limit=max(1, min(int(limit or 50), 200)))
    return {"subagents": [
        {"agent_id": x.get("id"), "parent_id": x.get("parent_id"),
         "description": x.get("description"), "state": x.get("state"),
         "model": x.get("model"), "total_steps": x.get("total_steps"),
         "updated": x.get("updated")}
        for x in rows]}
