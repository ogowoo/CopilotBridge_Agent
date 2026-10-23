"""agentrun.py — Agent 回合编排：等待回答、单次 chat、run_agent 主流程、回合标记与缓存、待办同步、纯对话回合

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


async def wait_agent_answer(previous: str = "", pause_check=None, interrupt_check=None,
                            sent: str = "") -> str:
    """Wait using inactivity timeout, not a fixed per-turn wall-clock timeout.

    TIMEOUT now means: fail only when no answer progress is observed for this
    many seconds. Every text change and every visible generation cycle refreshes
    the idle deadline. A separate hard deadline prevents an infinite wait.
    Malformed but finished output is returned to AgentRunner for repair.

    interrupt_check：用户"强行插入指令"时返回 True。**半中断**——仅当本回合还
    什么都没产出（文本没变、也没在生成）时才中止等待；一旦 Copilot 已开始生成就
    让它答完，避免丢掉已生成的回答、以及"被放弃的回答"与下一轮应答错位。
    """
    started_at = time.monotonic()
    hard_timeout = float(b.CFG.get("reply_hard_timeout_seconds", max(b.TIMEOUT * 5, 900)))
    hard_deadline = started_at + hard_timeout
    idle_deadline = started_at + b.TIMEOUT
    current = ""
    last_change = started_at
    generation_seen = False
    candidate_seen = False
    last_full_check = started_at

    while time.monotonic() < hard_deadline:
        await asyncio.sleep(b.POLL)
        if pause_check and pause_check():
            raise b.AgentPaused("用户请求暂停，回合等待已中断")
        now = time.monotonic()
        still_generating = await b.generating()
        value = b._clean_answer(await b.current_answer())

        # 插入指令的半中断点：还没产出任何内容 -> 放弃等待不丢东西
        if (interrupt_check and interrupt_check() and not still_generating
                and not generation_seen and value == previous):
            await b.diagnostics("agent_answer_interrupted_before_output")
            raise b.AgentInterrupted("已插入新指令，本回合尚未产出，已中止等待")

        # 对话已满时 Copilot 不会开始生成：快速失败，交给上层换会话重发。
        if not still_generating and not candidate_seen and not generation_seen:
            if now - last_full_check >= 5:
                last_full_check = now
                full = await b.conversation_full_check()
                if full:
                    await b.diagnostics("agent_conversation_full_early")
                    raise b.CopilotConversationFull(full)

        if still_generating:
            generation_seen = True
            idle_deadline = now + b.TIMEOUT

        # 只有"我的问题回声 + UI 提示"不算答案：继续等真正的回答出现
        _echo = bool(sent) and b._echo_only(value, sent)
        is_candidate = bool(value) and (value != previous or generation_seen) and not _echo
        if is_candidate:
            candidate_seen = True
            if value != current:
                current = value
                last_change = now
                idle_deadline = now + b.TIMEOUT

        if current and not still_generating:
            quiet_for = now - last_change
            if b._has_complete_agent_action(current) and quiet_for >= max(1.0, b.POLL * 3):
                await b.diagnostics("agent_answer_ready")
                return current
            # 服务限流/不可用提示不是 Agent 回答：立即识别并交给上层退避重试，
            # 不进入解析失败计数，更不会触发新建会话。
            unavailable = b.copilot_unavailable_reason(current)
            if unavailable and quiet_for >= max(1.0, b.POLL * 3):
                await b.diagnostics("copilot_service_unavailable")
                raise b.CopilotRateLimited(unavailable)
            _license = b.license_reason(current)
            if _license and quiet_for >= max(1.0, b.POLL * 3):
                await b.diagnostics("copilot_license_missing")
                raise RuntimeError(
                    "Microsoft 365 Copilot 许可证不可用：" + _license +
                    "。请确认自动化浏览器当前登录的账号已分配 Copilot 许可证"
                    "（不是 Copilot Chat Basic）；换有许可证的账号后重试。")
            # 无法解析的回答有可能是"暂歇"而非写完：Copilot 生成 JSON 中途停顿
            # （渲染文件链接、网络抖动）时，3 秒静默就取快照会拿到半截文本，上层
            # 只能报 invalid_action——实测就发生过：界面上答案完整（final JSON），
            # 我们却抓到 "...sharepoint_uploader_ui.p"。因此畸形回答要等更久，
            # 且认定结束前再读一次页面确认（仍在增长就继续等）。
            malformed_grace = max(b.STABLE * 2.0, 8.0)
            if generation_seen and quiet_for >= malformed_grace:
                confirm = (await b.current_answer()).strip()
                if confirm != current:
                    current = confirm
                    last_change = time.monotonic()
                    idle_deadline = last_change + b.TIMEOUT
                    continue
                if b._has_complete_agent_action(confirm):
                    await b.diagnostics("agent_answer_ready_late")
                    return confirm
                await b.diagnostics("agent_answer_needs_repair")
                return confirm

        if now >= idle_deadline:
            final = (await b.current_answer()).strip()
            if not candidate_seen and not generation_seen:
                full = await b.conversation_full_check()
                if full:
                    await b.diagnostics("agent_conversation_full_idle")
                    raise b.CopilotConversationFull(full)
            unavailable = b.copilot_unavailable_reason(final)
            if unavailable and not b._has_complete_agent_action(final):
                await b.diagnostics("copilot_service_unavailable_idle")
                raise b.CopilotRateLimited(unavailable)
            if final and (final != previous or generation_seen or candidate_seen):
                await b.diagnostics("agent_answer_idle_return")
                return final
            await b.diagnostics("agent_answer_idle_timeout")
            raise TimeoutError(
                f"Copilot 连续 {int(b.TIMEOUT)} 秒没有产生新的回答进度，请查看 last_page.txt"
            )

    final = (await b.current_answer()).strip()
    if not candidate_seen and not generation_seen:
        full = await b.conversation_full_check()
        if full:
            await b.diagnostics("agent_conversation_full_hard")
            raise b.CopilotConversationFull(full)
    unavailable = b.copilot_unavailable_reason(final)
    if unavailable and not b._has_complete_agent_action(final):
        await b.diagnostics("copilot_service_unavailable_hard")
        raise b.CopilotRateLimited(unavailable)
    if final and (final != previous or generation_seen or candidate_seen):
        await b.diagnostics("agent_answer_hard_return")
        return final
    await b.diagnostics("agent_answer_hard_timeout")
    raise TimeoutError(
        f"Copilot 单步等待超过硬上限 {int(hard_timeout)} 秒，请查看 last_page.txt"
    )


async def agent_chat_once(prompt: str, model_id: str, make_new: bool = False, pause_check=None,
                          interrupt_check=None) -> str:
    """Send one Agent turn and bind the response to a unique visible turn marker.

    Copilot virtualizes long chat DOM nodes, so the number of visible "You said"
    blocks can stay unchanged. The marker is unique for every request and cannot
    be confused with a prior turn or text left in the composer.
    """
    previous = "" if make_new else (await b.current_answer()).strip()
    turn_id = uuid.uuid4().hex[:12]
    marker = f"[CBP-TURN:{turn_id}]"
    tagged_prompt = prompt.rstrip() + "\n\n" + marker
    b.runtime_update(phase="sending", prompt_preview=tagged_prompt[-12000:], answer_preview="",
                   detail=f"正在提交 Agent 回合 {turn_id}", make_new=make_new)
    await b.send(tagged_prompt, make_new, model_id)

    # Require the exact unique marker to appear in the rendered conversation.
    # This replaces the unreliable visible-message-count check.
    if not await b._wait_turn_marker(marker, min(b.TIMEOUT, 45)):
        full = await b.conversation_full_check()
        if full:
            await b.diagnostics("agent_conversation_full_at_send")
            raise b.CopilotConversationFull(full)
        await b.diagnostics("agent_turn_marker_missing")
        raise TimeoutError(f"未确认 Agent 回合 {turn_id} 进入 Copilot 会话，已停止以避免读取旧回答")

    b.runtime_update(phase="waiting", detail=f"Agent 回合 {turn_id} 已确认，等待 Copilot 回答")
    _answer = await wait_agent_answer(previous, pause_check=pause_check, sent=prompt,
                                      interrupt_check=interrupt_check)
    return b.strip_suggestion_tail(_answer)


async def run_agent(req: b.AgentRequest, attach_paths: list | None = None):
    if not b.AGENT_CFG.get("enabled", True): raise HTTPException(503, "Agent 功能已禁用")
    # 软归一：历史会话可能保存了已失效的模型 ID，回退默认模型而非 400
    req.model=b.normalize_model_soft(req.model)
    task="\n".join(f"[{m.role}] {m.content.strip()}" for m in req.messages if m.content.strip())
    if not task: raise HTTPException(400,"至少需要一条消息")
    sid=req.session_id or ("task-"+uuid.uuid4().hex[:12])
    b.runtime_update(sid, phase="queued", detail="任务已进入执行队列", answer_preview="", prompt_preview="")
    # 提供商模型（"前缀/模型"）不需要浏览器页；网页通道才占一个自动化窗口
    _need_page = b.providers_mod.split_provider_model(req.model) is None
    async with b._window(sid, need_page=_need_page) as (win, T):
        # ACTIVE_SESSION_ID 必须在锁内声明：等锁的请求若提前覆盖，
        # 正在运行会话的删除保护/busy 判定/resume 守卫会整体失效。
        prev_active = b.ACTIVE_SESSION_ID
        b.ACTIVE_SESSION_ID = sid
        # 同一会话若有排队中的 resume 任务，被本次新消息取代：否则旧任务文本
        # 与本次消息会各跑一遍（顺序双跑，重复动作如 append/下载）。
        _pend = b._PENDING_RESUME.pop(sid, None)
        if _pend is not None and not _pend.done() and _pend is not asyncio.current_task():
            _pend.cancel()
            b.log(f"会话 {sid} 的排队恢复任务已被新消息取代")
        existing=b.AGENT_SESSIONS.get(sid)
        workspace=(req.workspace.strip() if req.workspace else None) or (existing or {}).get("workspace") or b.DEFAULT_WORKSPACE
        T.set_root(workspace,False)
        # 沙箱档位 / 计划模式按**本次请求**生效（配置是默认值）：
        # 计划模式 = 强制只读，模型只探索并产出方案，调用 exit_plan_mode 后解除。
        _mode = str(getattr(req, "sandbox_mode", None) or b.AGENT_CFG.get("sandbox_mode")
                    or "workspace-write").lower()
        T.sandbox_mode = _mode if _mode in b.SANDBOX_MODES else "workspace-write"
        T.plan_mode = bool(getattr(req, "plan_mode", False))
        if T.plan_mode:
            b.log(f"会话 {sid} 进入计划模式（只读）")
            plan_hint = (
                "\n\n[计划模式] 本轮**强制只读**：任何写文件/执行命令/改外部系统的工具都会被拒绝。"
                "请先只读探索（读代码、搜索、看目录），然后产出可执行方案，"
                "并用 exit_plan_mode(plan=<方案 Markdown>) 提交。"
                "提交后**立即返回 final** 把方案呈现给用户，不要在同一轮开始执行——"
                "用户确认（说\"继续\"）后才按方案动手。")
            task = f"{task}{plan_hint}"
            if existing:
                b.AGENT_SESSIONS.patch(sid, plan_mode=1)
        # 记忆作用域随工作区切换：工作区记忆不跨项目泄露；来源会话用于溯源
        T.memory_scope = b.memory_scope_for(T.root)
        T.current_session_id = sid
        if not existing:
            existing=b.AGENT_SESSIONS.create(sid,task,str(T.root),req.model)
            effective_task = task
        else:
            effective_task = existing.get("original_task") or task
            if task.strip() and task.strip() != effective_task.strip():
                effective_task += "\n\n用户追加任务：\n" + task
                pending = list(existing.get("pending") or [])
                pending.append(task)
                # 追加指令不再强制新建会话：Agent 会在当前会话直接下达，保留完整上下文。
                b.AGENT_SESSIONS.patch(sid, pending=pending, state="ready")
        b.AGENT_SESSIONS.patch(sid,workspace=str(T.root),model=req.model)
        def pause_check():
            st = b.AGENT_SESSIONS.get(sid) or {}
            return st.get("state") == "paused"
        _pm = b.providers_mod.split_provider_model(req.model)
        _is_provider = bool(_pm and _pm[0] in b.PROVIDERS)
        # provider 会话禁用"需要 Copilot 网页会话"的工具（copilot_attach/copilot_file：
        # 没有网页对话可驱动，实测连续失败 10 次白烧回合）。浏览器会话保持启用。
        T.set_web_copilot_tools(not _is_provider)
        if _is_provider and attach_paths:
            # 提供商分支没有可附加图片的输入框：明确记录 + 提示（不再静默丢弃）
            try:
                warn_provider_cannot_attach(sid, req.model, attach_paths)
            except Exception as exc:
                b.log(f"记录图片不可投递失败（忽略）: {exc}")
            attach_paths = None      # 明确：本轮无附件可投递
        if _is_provider:
            # API 提供商：不经过浏览器，无页面导航/会话 URL/标记确认簿记；
            # 换会话只影响本地上下文压缩。
            async def turn(prompt, model, force_new=False):
                b.SNAPSHOTS.next_turn(sid)   # 一次 Copilot 回合 = 一个快照版本
                # 上一次的工具调用已派发但结果未知（进程在结果落库前退出）：绝不原样重放，
                # 先让模型只读核对现状；随后把这些步骤标为 unknown，保证只提示一次。
                _pend = []
                try:
                    _pend = pending_steps_via(sid)
                except Exception:
                    _pend = []
                if _pend:
                    for _p in _pend:
                        try:
                            b.AGENT_SESSIONS.finish_step(sid, int(_p.get('conversation_index') or 0),
                                                       int(_p.get('step_no') or 0), 'unknown')
                        except Exception:
                            pass
                    _names = ', '.join(sorted({str(_p.get('tool') or '?') for _p in _pend}))
                    b.log(f'检测到 {len(_pend)} 个结果未知的工具步骤（{_names}），先只读核对不重放')
                    return json.dumps({
                        'type': 'tool', 'tool': 'list_directory', 'arguments': {'path': '.'},
                        'reason': f'上一次工具调用（{_names}）的结果未知：先只读核对当前状态，'
                                  '不要重复可能已发生的写入/删除/发送等副作用；确认后再继续原计划。'},
                        ensure_ascii=False)
                # 提供商回合同样"发出即不可回滚"：先落 inflight，成功后转 done；
                # 崩在途中（HTTP 被中断）→ 恢复时按"结果未知"处理，只做只读核对，不重发。
                import hashlib as _hl
                _ph = _hl.sha1((prompt or "").encode("utf-8")).hexdigest()[:20]
                _ci = int((b.AGENT_SESSIONS.get(sid) or {}).get("conversation_index", 0))
                _steps = int((b.AGENT_SESSIONS.get(sid) or {}).get("total_steps", 0))
                _cache_on = bool(b.AGENT_CFG.get("turn_cache_enabled", True))
                if _cache_on:
                    _act, _ans = b.turn_cache_decision(b.AGENT_SESSIONS.get_turn(sid, _ci, _ph), _steps)
                    if _act == "reuse":
                        b.log(f"提供商回合命中缓存（{_ph[:8]}），跳过重发")
                        return _ans
                    if _act == "probe":
                        b.log(f"提供商回合 {_ph[:8]} 结果未知（中断在途），改为只读核对")
                        return json.dumps({
                            "type": "tool", "tool": "list_directory", "arguments": {"path": "."},
                            "reason": "上一回合结果未知（连接中断）：先只读核对当前状态，"
                                      "不要重复可能已发生的写入/删除等副作用。"}, ensure_ascii=False)
                    try:
                        b.AGENT_SESSIONS.begin_turn(sid, _ci, _ph, _steps)
                    except Exception:
                        pass
                _ans = await b.provider_turn(sid, model, prompt, force_new=force_new)
                if _cache_on and _ans:
                    try:
                        b.AGENT_SESSIONS.save_turn(sid, _ci, _ph, _ans,
                                                 int((b.AGENT_SESSIONS.get(sid) or {}).get("total_steps", 0)))
                    except Exception:
                        pass
                return _ans
        else:
            attached = {"done": False}   # 附件只在首个回合附加一次
            async def turn(prompt,model,force_new=False):
                # 恢复/续跑可能发生在浏览器 idle 时：先确保它有页面，
                # 否则后面读 page.url / 点输入框都会 NoneType 崩（实测 resume 立刻 failed）。
                await b.ensure_browser_ready()
                b.SNAPSHOTS.next_turn(sid)   # 一次 Copilot 回合 = 一个快照版本
                # 上一次的工具调用已派发但结果未知（进程在结果落库前退出）：绝不原样重放，
                # 先让模型只读核对现状；随后把这些步骤标为 unknown，保证只提示一次。
                _pend = []
                try:
                    _pend = pending_steps_via(sid)
                except Exception:
                    _pend = []
                if _pend:
                    for _p in _pend:
                        try:
                            b.AGENT_SESSIONS.finish_step(sid, int(_p.get('conversation_index') or 0),
                                                       int(_p.get('step_no') or 0), 'unknown')
                        except Exception:
                            pass
                    _names = ', '.join(sorted({str(_p.get('tool') or '?') for _p in _pend}))
                    b.log(f'检测到 {len(_pend)} 个结果未知的工具步骤（{_names}），先只读核对不重放')
                    return json.dumps({
                        'type': 'tool', 'tool': 'list_directory', 'arguments': {'path': '.'},
                        'reason': f'上一次工具调用（{_names}）的结果未知：先只读核对当前状态，'
                                  '不要重复可能已发生的写入/删除/发送等副作用；确认后再继续原计划。'},
                        ensure_ascii=False)

                b.clear_interrupt(sid)       # 新回合：上一轮的插入信号已由 pending 承接
                st=b.AGENT_SESSIONS.get(sid)
                make_new = force_new or not st.get("current_url")
                if not make_new:
                    # 页面可能因服务重启/窗口切换被重置到基础地址：续聊前若当前
                    # 页面不是该会话的 Copilot 对话，先导航回已保存的会话 URL，
                    # 避免回合误入一个全新的空对话。
                    expected = st.get("current_url") or ""
                    m = CONVERSATION_URL_RE.search(expected)
                    if m and m.group(0) not in b.page_url_safe():
                        await b.page.goto(expected, wait_until="domcontentloaded", timeout=120000)
                        await asyncio.sleep(1)
                        b._forget_selected_model()
                if attach_paths and not attached["done"]:
                    _ok_att, _bad_att = [], []
                    for p in attach_paths:
                        try:
                            await b.attach_file_to_copilot(p)
                            _ok_att.append(p.name)
                        except Exception as exc:
                            b.log(f"附件 {p.name} 上传失败: {exc}")
                            _bad_att.append(f"{p.name}: {exc}")
                    attached["done"] = True
                    # 写进时间线：图片没送进 Copilot 时，用户和后续排查都能看到，
                    # 而不是只躺在服务端日志里（这正是"贴图识别失败"最难查的地方）。
                    try:
                        _sn = int((b.AGENT_SESSIONS.get(sid) or {}).get("total_steps", 0)) + 1
                        _cix = int((b.AGENT_SESSIONS.get(sid) or {}).get("conversation_index", 0))
                        b.AGENT_SESSIONS.add_step(sid, _sn, _cix, {
                            "step": _sn, "tool": "attach_image",
                            "status": "ok" if (_ok_att and not _bad_att) else
                                      ("error" if _bad_att and not _ok_att else "warning"),
                            "reason": f"附加图片 {len(_ok_att)}/{len(attach_paths)} 张到 Copilot 输入框",
                            "result": {"attached": _ok_att, "failed": _bad_att}})
                    except Exception:
                        pass
                marker = _agent_chat_marker(sid, int(st.get("conversation_index", 0)))
                # Copilot derives the sidebar title from the beginning of the first
                # message. This is more stable than automating the Rename editor.
                if make_new:
                    # 新任务：明确要求忽略此前对话话题——实测 Copilot 新对话会
                    # 沿用上一条聊天的话题，把新任务带到别的项目上（窜任务）。
                    send_prompt = (marker + "\n【新任务】忽略此前任何对话的话题与进度，"
                                   "只按下述内容执行。\n" + prompt)
                else:
                    send_prompt = prompt
                remember_initial_prompt(sid, send_prompt)   # 与实际发送内容一致
                # ── 回合结果缓存（P1-1）：同一会话/对话/提示词的回合若已真正发出并落过步数，
                # 恢复或重试时直接复用答案，不再重发（省一次远端回合 = 省时间与额度）。
                # 键里包含附件指纹（见 turn_cache_key）：换一张新截图不会被旧缓存顶掉
                _ph = turn_cache_key(send_prompt, attach_paths)
                _ci = int((b.AGENT_SESSIONS.get(sid) or {}).get("conversation_index", 0))
                _cache_on = bool(b.AGENT_CFG.get("turn_cache_enabled", True))
                if _cache_on:
                    _hit = b.AGENT_SESSIONS.get_turn(sid, _ci, _ph)
                    _done = int((b.AGENT_SESSIONS.get(sid) or {}).get("total_steps", 0))
                    _act, _ans = b.turn_cache_decision(_hit, _done)
                    if _act == "reuse":
                        b.log(f"回合结果命中缓存（{_ph[:8]}），跳过重发")
                        return _ans
                    if _act == "probe":
                        # 已派发但结果未知：绝不重发（可能重复副作用），先只读核对
                        b.log(f"回合 {_ph[:8]} 结果未知（中断在途），改为只读核对后再继续")
                        return json.dumps({
                            "type": "tool", "tool": "list_directory", "arguments": {"path": "."},
                            "reason": "上一回合在执行途中被中断，其结果未知：先只读核对当前文件与状态，"
                                      "不要重复可能已发生的写入/删除等副作用；确认后再继续原计划。"},
                            ensure_ascii=False)

                if _cache_on:
                    try:
                        b.AGENT_SESSIONS.begin_turn(sid, _ci, _ph,
                                                  int((b.AGENT_SESSIONS.get(sid) or {}).get("total_steps", 0)))
                    except Exception:
                        pass
                try:
                    ans=await agent_chat_once(send_prompt,model,make_new,pause_check=pause_check,
                                              interrupt_check=lambda: b.interrupt_pending(sid))
                    if _cache_on and ans:
                        try:
                            b.AGENT_SESSIONS.save_turn(sid, _ci, _ph, ans,
                                                    int((b.AGENT_SESSIONS.get(sid) or {}).get("total_steps", 0)))
                        except Exception:
                            pass

                except b.AgentPaused:
                    # 回合已发送到 Copilot：保存会话 URL，恢复后继续同一聊天，
                    # 避免暂停-恢复后重复创建新会话。
                    if make_new and b.page_url_safe():
                        paused_url = await stable_conversation_url(timeout=6)
                        if not paused_url:
                            _cand = b.page_url_safe()
                            paused_url = _cand if CONVERSATION_URL_RE.search(_cand) else ""
                        stp=b.AGENT_SESSIONS.get(sid); urls=list(stp.get("conversation_urls") or [])
                        if paused_url and paused_url not in urls: urls.append(paused_url)
                        if paused_url:
                            b.AGENT_SESSIONS.patch(sid,current_url=paused_url,conversation_urls=urls)
                    raise
                # 等 Copilot 把会话 ID 路由进 URL 再保存；无会话 ID 的基础地址
                # 不能当会话身份保存（下回合会误入当前打开的任意会话）。
                final_url = await stable_conversation_url(timeout=15)
                if not final_url:
                    _cand = b.page_url_safe()
                    final_url = _cand if CONVERSATION_URL_RE.search(_cand) else ""
                st=b.AGENT_SESSIONS.get(sid); urls=list(st.get("conversation_urls") or [])
                is_new_url = bool(final_url and final_url not in urls)
                if is_new_url: urls.append(final_url)
                if final_url:
                    b.AGENT_SESSIONS.patch(sid,current_url=final_url,conversation_urls=urls)
                # The marker is already embedded in the first prompt. Do not navigate
                # away after receiving an answer merely to rename the sidebar row;
                # that navigation can invalidate the next-turn DOM baseline.
                if is_new_url:
                    b.runtime_update(sid, detail="新 Copilot 会话已绑定唯一标识")
                return ans
        def get(): return b.AGENT_SESSIONS.get(sid)
        def save(**kw):
            # 状态一旦离开 waiting，就清掉审批残留。否则会话跑完（completed/failed）后
            # approval_pending 还留着，UI 会一直显示「需要授权」横幅——实测：点「暂不授权」
            # 只是本地隐藏，下一秒 runtime 刷新又把它弹回来；残留也会让巡检/投影看到假状态。
            if "state" in kw and kw["state"] != "waiting" and "approval_pending" not in kw:
                kw["approval_pending"] = None
            return b.AGENT_SESSIONS.patch(sid,**kw)
        def add_step(x):
            st=get(); b.AGENT_SESSIONS.add_step(sid,int(x.get("step",st["total_steps"]+1)),int(st["conversation_index"]),x)
            try:
                b.PLUGINS.emit("step_done", session_id=sid, tool=x.get("tool"),
                             status=x.get("status"), reason=x.get("reason") or "")
            except Exception:
                pass
        def add_file(x): b.AGENT_SESSIONS.add_file_event(sid,x)
        def add_conv_stat(x): b.AGENT_SESSIONS.add_conv_stat(sid,x)
        # 写操作前捕获原像：工作区改动与自升级改动分作用域，均可整任务回滚
        T.set_snapshot_hook(b.self_snapshot_hook(sid))
        T.self_rollback_fn = lambda: b.SNAPSHOTS.restore(b.self_scope(sid), b.BASE)
        runner=b.AgentRunner(T,turn,b.AGENT_CFG,get,save,add_step,add_file,add_conv_stat,
                        step_begin=lambda no, tool, h: (
                            b.AGENT_SESSIONS.begin_step(sid, _conv_index(sid), no, tool, h),
                            # 让界面显示"正在执行 X"，而不是停在上一轮的 waiting
                            b.runtime_update(sid, phase="executing",
                                           detail=f"正在执行第 {no} 步 · {tool}",
                                           step_started=time.time()),
                            # 工作区 TODO 文件变了就同步进待办面板（mtime 未变是空操作）
                            sync_session_todos(sid),
                            nudge_todo_update(sid, no)),
                        step_finish=lambda no, st='done': (
                            b.AGENT_SESSIONS.finish_step(sid, _conv_index(sid), no, st),
                            b.runtime_update(sid, phase="thinking", detail=f"第 {no} 步已完成（{st}）")),
                        approval_check=lambda tool,h: b.AGENT_SESSIONS.take_approval(sid, tool, h))
        try:
            b.PLUGINS.emit("session_start", session_id=sid, task=effective_task, model=req.model)
            # 按 todo 自动派子代理（默认关：config.agent.auto_team_enabled）。
            # 每个子代理都是一条独立 Copilot 对话 = 成倍额度，所以必须显式打开。
            try:
                from .auto_team import maybe_dispatch

                _n = await maybe_dispatch(b.AGENT_SESSIONS, sid, b.AGENT_CFG)
                if _n:
                    b.log(f"按待办自动派发 { _n } 个子代理")
            except Exception as exc:      # 自动派发失败绝不影响主任务
                b.log(f"自动派发失败（忽略）：{exc}")
            # 用户任务先落库：**必须在跑之前**——消息与步骤在实时流里按时间穿插，
            # 跑完再写会让"用户提问"排到它自己触发的几十个步骤后面（顺序就乱了）。
            try:
                _conv = _conv_index(sid)
                _have = b.AGENT_SESSIONS.messages(sid) or []
                if not any(m.get("role") == "user" for m in (_have or [])):
                    b.AGENT_SESSIONS.add_message(sid, "user", effective_task, _conv)
            except Exception as exc:
                b.log(f"落用户消息失败（忽略）：{exc}")
            result = await runner.run(effective_task,req.model)
            try:
                if (result.content or "").strip():
                    b.AGENT_SESSIONS.add_message(sid, "assistant", result.content,
                                               _conv_index(sid))
            except Exception as exc:
                b.log(f"落助手消息失败（忽略）：{exc}")
            # 父任务收尾：仍在跑的子代理没人收结果，直接取消（否则面板一直显示 running）
            try:
                _n = b.AGENT_SESSIONS.cancel_running_children(sid, "父任务已完成")
                if _n:
                    b.log(f"父任务 {sid} 完成，取消 { _n } 个仍在运行的子代理")
            except Exception:
                pass
            b.PLUGINS.emit("session_end", session_id=sid, content=result.content,
                         steps=len(result.steps), state="completed")
            b.runtime_update(sid, phase="completed", detail=result.content, answer_preview=result.content)
            b.SNAPSHOTS.prune()
            return result, sid
        except b.AgentPaused:
            # 用户暂停不是失败：写 failed 会让 AgentRunner 的停放逻辑（只认
            # paused）失效、重启后的 auto_resume（只捞 paused）也捞不回来，
            # 于是"暂停"在回合等待期间等于把会话卡死。
            b.AGENT_SESSIONS.patch(sid, state="paused", last_error=None)
            b.runtime_update(sid, phase="paused", detail="用户暂停，可从检查点恢复")
            raise
        except Exception as exc:
            b.runtime_update(sid, phase="failed", detail=str(exc))
            b.AGENT_SESSIONS.patch(sid,state="failed",last_error=str(exc))
            b.SNAPSHOTS.prune()  # 失败/取消路径也要按保留数回收，避免只增不减
            raise
        finally:
            T.set_snapshot_hook(None)
            T.self_rollback_fn = None
            T.set_web_copilot_tools(True)   # 复位，避免影响后续会话
            # 恢复进入前的值而非置 None：排队任务接管时不会被误清
            b.ACTIVE_SESSION_ID = prev_active


def _agent_chat_marker(session_id: str, conversation_index: int) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]+", "-", str(session_id)).strip("-") or "session"
    return f"[CBP-AGENT][{safe[:36]}][C{int(conversation_index):03d}]"


CONVERSATION_URL_RE = re.compile(r"/chat/conversation/[0-9A-Za-z-]{8,}")


async def stable_conversation_url(timeout: float = 12.0) -> str:
    """Wait until the Copilot URL carries a conversation id and return it.

    New chats open at the base /chat address; Copilot routes the conversation
    id into the URL asynchronously. Persisting the base address breaks delete
    targeting later, so poll briefly. Returns "" when no id shows up in time.
    """
    deadline = time.monotonic() + max(0.5, timeout)
    url = ""
    while time.monotonic() < deadline:
        try:
            url = b.page.url or ""
        except Exception:
            url = ""
        if CONVERSATION_URL_RE.search(url):
            return url
        await asyncio.sleep(0.5)
    return ""


def remember_initial_prompt(sid: str, prompt: str) -> None:
    """首个回合发给模型的提示词落库，供事后查看看。

    里头是完整系统提示词（工具清单 + 项目约定 + 验证闭环规则 + 任务），
    是排查"Agent 为什么这么做"的第一现场。此前只写在 RUNTIME.prompt_preview，
    每个回合都被覆盖，初始提示词随即丢失。只记第一次，异常静默（不影响回合）。
    """
    if not prompt:
        return
    try:
        st = b.AGENT_SESSIONS.get(sid)
        if not st or (st.get("initial_prompt") or "").strip():
            return
        b.AGENT_SESSIONS.patch(sid, initial_prompt=prompt[:200000])
    except Exception:
        pass


def rebuild_initial_prompt(state: dict) -> str:
    """按会话的工作区与任务重建"初始提示词"（旧会话没存过时用）。

    用独立的 ToolRegistry 重建，避免动到正在运行任务共享的那个实例
    （改 AGENT_TOOLS.root 会让跑着的任务串台）。内容可能与当初略有差异
    （AGENT.md / skills / 工具开关后来改过），因此界面会标注"近似重建"。
    """
    from .tools import ToolRegistry
    ws = Path(state.get("workspace") or b.DEFAULT_WORKSPACE)
    tools = ToolRegistry(ws, b.AGENT_CFG)
    tools.set_global_root(b.BASE)
    tools.current_session_id = str(state.get("id") or "")
    try:
        tools.memory_scope = b.memory_scope_for(ws)
    except Exception:
        pass
    tools.set_web_copilot_tools(True)
    task = str(state.get("original_task") or "")
    runner = b.AgentRunner(tools, None, b.AGENT_CFG, None, None, None, None)
    runner._task_text = task          # 供工具按任务裁剪
    try:
        runner._agent_md, runner._skills = runner._context_values()
    except Exception:
        pass
    return runner._system_prompt() + "\n\n用户任务：\n" + task


def _conv_index(sid: str) -> int:
    """当前对话序号（换会话后 step_no 会复用，检查点靠它区分代次）。"""
    try:
        return int((b.AGENT_SESSIONS.get(sid) or {}).get("conversation_index", 0))
    except Exception:
        return 0


def event_source() -> str:
    """读取路径来源（P2-10 第三期开关）：columns（默认，读数据库列）/ projection（读事件投影）。

    优先级：config.json 的 agent.event_source → 环境变量 CBP_EVENT_SOURCE → columns。
    默认必须是 columns：投影虽已被测试与巡检证明一致，但"切换读取路径"应当是显式动作，
    且任何时候都能一条命令回退（这也是分期推进第三期的前提）。
    """
    try:
        val = str(b.AGENT_CFG.get("event_source") or "").strip().lower()
    except Exception:
        val = ""
    if not val:
        val = (os.environ.get("CBP_EVENT_SOURCE") or "").strip().lower()
    return val if val in ("columns", "projection") else "columns"


def _todo_nudge_every() -> int:
    """每多少步提醒模型更新待办（0=关闭）。可在设置页/ config.json 的
    `agent.todo_nudge_every` 里改。"""
    try:
        return int(b.AGENT_CFG.get("todo_nudge_every", 12))
    except Exception:
        return 12


def nudge_todo_update(sid: str, step_no: int) -> None:
    """每 N 步没更新待办，就往 next-step 队列塞一句提醒。

    模型普遍"列一次清单就再也不管"（实测整改 15 步只调 1 次 todo_write），
    面板因此永远看不出进度。这里用既有的 pending_steps 注入通道软提醒，
    不打断执行、也不改它的规划，只是让它把状态勾一下。
    """
    try:
        st = b.AGENT_SESSIONS.get(sid) or {}
        todos = st.get("todos") or []
        if not todos or _todo_nudge_every() <= 0:
            return
        last = b._TODO_NUDGE.get(sid)
        if last is None:
            b._TODO_NUDGE[sid] = int(step_no)
            return
        if int(step_no) - last < _todo_nudge_every():
            return
        b._TODO_NUDGE[sid] = int(step_no)
        pend = [str(x) for x in (st.get("pending_steps") or []) if str(x).strip()]
        msg = ("【待办】请顺手调用一次 todo_write：把已完成的项置 completed、"
               "当前正在做的项置 in_progress（整表重写）。这是用户看进度的唯一途径。")
        if msg in pend:
            return
        b.AGENT_SESSIONS.patch(sid, pending_steps=pend + [msg])
    except Exception:
        pass


def sync_session_todos(sid: str) -> bool:
    """按需把工作区 TODO 文件同步进会话待办（解析失败/没变化时安静返回）。

    用户诉求："todo 应该自己分析 todo.md 之类的来更新"——文件里写的是计划，
    以前 CBP 完全不看它，面板只能靠模型自觉调 todo_write（实测整改阶段只调了 1 次）。
    另外：同步后若一条 `in_progress` 都没有，就把第一条 pending 置为 in_progress，
    否则面板上永远看不出"现在在做哪一项"（用户："都没见更新做到哪的"）。
    """
    try:
        from .todo_sync import sync_todos_from_files

        changed = bool(sync_todos_from_files(b.AGENT_SESSIONS, sid))
        st = b.AGENT_SESSIONS.get(sid) or {}
        todos = list(st.get("todos") or [])
        if todos and str(st.get("state")) in ("running", "waiting") \
                and not any(str(t.get("status")) == "in_progress" for t in todos):
            for i, t in enumerate(todos):
                if str(t.get("status")) == "pending":
                    todos[i] = dict(t, status="in_progress")
                    b.AGENT_SESSIONS.patch(sid, todos=todos)
                    changed = True
                    break
        return changed
    except Exception:
        return False


def pending_steps_via(sid: str, source: str | None = None) -> list[dict]:
    """"结果未知"的步骤（未收尾）——可按 columns 或 projection 读取。

    第三期的做法是**同一份事实两条读取路径**：默认读列，切到投影后若两者不一致，
    记一条告警并**回退到列**（安全优先）。这样切换可以小步进行、随时可回退。
    """
    src = source or event_source()
    try:
        via_columns = b.AGENT_SESSIONS.pending_steps(sid)
    except Exception:
        via_columns = []
    if src != "projection":
        return via_columns
    try:
        from .projection import project

        proj = project(b.AGENT_SESSIONS.events(sid))
        via_proj = [{"step_no": s.get("step_no"), "tool": s.get("tool"),
                     "conversation_index": s.get("conversation_index")}
                    for s in proj.get("steps") or [] if s.get("status") == "inflight"]
    except Exception as exc:
        b.log(f"投影读取失败，回退到列：{exc}")
        return via_columns
    key = lambda xs: sorted((int(x.get("conversation_index") or 0), int(x.get("step_no") or 0),
                             str(x.get("tool") or "")) for x in xs)
    if key(via_proj) != key(via_columns):
        b.log(f"事件投影与数据库列不一致（会话 {sid}）：投影 {key(via_proj)} vs 列 {key(via_columns)}；"
            f"本次回退到列，请用 scripts/health.py 排查")
        return via_columns
    return via_proj


def turn_cache_key(prompt: str, attach_paths=None) -> str:
    """回合缓存键：**必须包含附件指纹**。

    同一句"看看这张图"配不同截图时，只用 prompt 哈希会命中上一次的缓存答案，
    模型根本没看到新图 —— 用户侧表现就是"贴图后识别失败"。
    """
    import hashlib as _hl
    key = _hl.sha1((prompt or "").encode("utf-8")).hexdigest()[:20]
    if attach_paths:
        ah = _hl.sha1()
        for p in attach_paths:
            try:
                ah.update(Path(p).read_bytes())
            except Exception:
                ah.update(str(p).encode("utf-8", "replace"))
        key += ":att-" + ah.hexdigest()[:12]
    return key


def warn_provider_cannot_attach(sid: str, model: str, attach_paths) -> None:
    """API 提供商分支收到图片：明确记录 + 提示，而不是静默丢弃。

    提供商走 HTTP API，不经过 Copilot 网页输入框，没有地方附加图片。
    以前这里直接忽略附件，于是模型只看到"看看这张图"这句话，答得像没看图。
    """
    names = [Path(getattr(x, "name", str(x))).name for x in (attach_paths or [])]
    if not names:
        return
    b.log(f"提供商模型 {model} 不能接收图片，已跳过 {len(names)} 张（{', '.join(names)}）")
    try:
        cur = b.AGENT_SESSIONS.get(sid) or {}
        sn = int(cur.get("total_steps", 0)) + 1
        b.AGENT_SESSIONS.add_step(sid, sn, int(cur.get("conversation_index", 0)), {
            "step": sn, "tool": "attach_image", "status": "error",
            "reason": f"当前模型 {model} 是 API 提供商，无法接收图片附件",
            "result": {"skipped": names,
                       "hint": "想让它看图，请把模型切换成 Copilot（copilot-*）再发一次"}})
    except Exception:
        pass
    b.runtime_update(sid, phase="running",
                   detail=f"⚠ 当前模型不支持图片：已跳过 {len(names)} 张附件；想看图请切到 Copilot 模型")


async def plain_chat_turn(sid: str, content: str, model_id: str, attach_paths: list | None = None) -> dict:
    """普通对话回合：在 Session 绑定的 Copilot 会话里问答，不跑工具循环。

    由 continue_session_message（跟进）和 plain_completions（新任务）共用。
    调用方负责写入 user 消息；本函数写入 assistant 回复并维护会话 URL。
    attach_paths 为已落盘的图片/截图附件，发送前附加到 Copilot 输入框。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    async with b._window(sid) as (win, T):
        prev_active = b.ACTIVE_SESSION_ID
        b.ACTIVE_SESSION_ID = sid
        b.runtime_update(sid, phase="manual_sending", detail="正在发送普通对话", prompt_preview=content, answer_preview="")
        url = state.get("current_url")
        make_new = not bool(url)
        if url and b.page.url != url:
            try:
                await b.page.goto(url, wait_until="domcontentloaded", timeout=120000)
                await asyncio.sleep(1)
                b._forget_selected_model()  # 会话切换后页面上选中的模型可能不同
            except Exception:
                make_new = True
        previous = "" if make_new else (await b.current_answer()).strip()
        try:
            # 粘贴的图片/截图附件：发送前附加到 Copilot 输入框（失败不阻断文本发送）
            for p in (attach_paths or []):
                try:
                    await b.attach_file_to_copilot(p)
                except Exception as exc:
                    b.log(f"附件 {p.name} 上传失败: {exc}")
            # 新会话把 CBP-AGENT 标记放进首条消息：Copilot 用它做侧栏标题，
            # 也是日后远程删除时的同属校验依据（与 Agent 模式一致）。
            marker = _agent_chat_marker(sid, int(state.get("conversation_index", 0)))
            send_content = (marker + "\n" + content) if make_new else content
            # 追加唯一回合标记：虚拟化列表里"You said"计数不可靠，
            # sent_successfully 用标记确认送达，避免误判重发。
            send_content = send_content.rstrip() + f"\n\n[CBP-TURN:{uuid.uuid4().hex[:12]}]"
            await b.send(send_content, make_new, model_id)
            answer = (await wait_agent_answer(previous, sent=content)).strip()
            # 显示/存储用 markdown（保留代码块/列表/标题/粗体）；纯文本只用于回合判定。
            # 失败时回退纯文本，保证对话总能落库。
            # 先等 DOM 稳定：否则代码块还没渲染出来，取到的 markdown 缺代码块
            await b._wait_dom_stable()
            display = (await b.answer_markdown(answer)) or answer
            current = b.AGENT_SESSIONS.get(sid)
            if not current:
                raise RuntimeError("会话在执行期间被删除")
            chat_url = await stable_conversation_url(timeout=10)
            if not chat_url:
                _cand = b.page_url_safe()
                chat_url = _cand if CONVERSATION_URL_RE.search(_cand) else ""
            urls = list(current.get("conversation_urls") or [])
            if chat_url and chat_url not in urls: urls.append(chat_url)
            b.AGENT_SESSIONS.add_message(sid, "assistant", display, int(current.get("conversation_index", 0)))
            if chat_url:
                b.AGENT_SESSIONS.patch(sid, current_url=chat_url, conversation_urls=urls, model=model_id)
            else:
                b.AGENT_SESSIONS.patch(sid, conversation_urls=urls, model=model_id)
            b.runtime_update(sid, phase="manual_completed", answer_preview=display[-12000:], detail="普通对话完成")
            return {"session_id":sid,"role":"assistant","content":answer,"url":b.page.url,"mode":"chat"}
        finally:
            b.ACTIVE_SESSION_ID = prev_active


async def plain_completions(req: b.AgentRequest):
    """普通对话模式发起新任务：创建 Session 但不启动 Agent 工具循环。"""
    if not b.AGENT_CFG.get("enabled", True): raise HTTPException(503, "Agent 功能已禁用")
    model_id = b.normalize_model(req.model)
    contents = [m.content.strip() for m in req.messages if m.role == "user" and m.content.strip()]
    if not contents: raise HTTPException(400, "至少需要一条 user 消息")
    content = contents[-1]
    attach_paths = b.save_image_attachments(req.attachments)
    sid = req.session_id or ("task-" + uuid.uuid4().hex[:12])
    if not b.AGENT_SESSIONS.get(sid):
        workspace = (req.workspace.strip() if req.workspace else None) or str(b.AGENT_WORKSPACE)
        b.AGENT_SESSIONS.create(sid, content, workspace, model_id)
    b.AGENT_SESSIONS.add_message(sid, "user", content, 0)
    pm = b.providers_mod.split_provider_model(model_id)
    if pm and pm[0] in b.PROVIDERS:
        result = await b.provider_session_chat(sid, content, model_id)
    else:
        result = await plain_chat_turn(sid, content, model_id, attach_paths=attach_paths)
    return {"id": "chat-" + uuid.uuid4().hex, "object": "chat.completion",
            "created": int(time.time()), "model": model_id,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": result["content"]},
                         "finish_reason": "stop"}],
            "session_id": sid, "mode": "chat"}
