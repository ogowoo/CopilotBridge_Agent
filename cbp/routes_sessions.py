"""routes_sessions.py — 会话端点：列表/详情/快照/改动/回滚/消息/继续/插话/队列/事件/运行时/投影/审批/删除

从 api.py 拆出（第五批解耦）：只做请求/响应适配。共享状态与基础设施仍在
api.py，这里经 ``a.xxx`` 访问（保持测试对 api 模块的打桩语义）；
本模块用自带 APIRouter 注册路由，由 api.py 在末尾 include。
"""
from __future__ import annotations

import asyncio, hmac, json, logging, re, threading, time, uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request as HttpRequest
from fastapi.responses import (StreamingResponse, FileResponse, RedirectResponse,
                               JSONResponse)
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from contextlib import AsyncExitStack

from . import api as a
from . import bridge as b
from . import i18n as backend_i18n
from . import prompt_lang

router = APIRouter()


@router.get("/v1/agent/sessions")
async def sessions(include_archived: bool = False, include_subagents: bool = True):
    """会话列表。默认**不返回子代理会话**——它们已经挂在父会话下（子代理面板可看），
    再占一行会把主列表淹掉。需要时传 include_subagents=true。"""
    rows = b.AGENT_SESSIONS.list(include_archived)
    # 默认返回**全部**（含子代理，带 parent_id）：WebUI 据此把子会话折叠到父会话下；
    # 需要扁平列表的调用方显式传 include_subagents=false。
    if not include_subagents:
        rows = [x for x in rows if not str(x.get("parent_id") or "").strip()]
    # 列表行也要带上"含子代理"的合计：侧栏与详情页/KPI 必须同一口径，否则同一个
    # 任务在两处显示两个步数/Token（用户实测）。用递归版本（含孙代理）。
    try:
        agg = b.AGENT_SESSIONS.subagent_totals_all()
        for x in rows:
            a = agg.get(str(x.get("id") or ""))
            if a and (a.get("count") or a.get("running")):
                x["subagent_count"] = a["count"]
                x["subagent_running"] = a["running"]
                x["subagent_steps"] = a["steps"]
                x["subagent_tokens"] = a["tokens"]
    except Exception:
        pass
    return {"sessions": rows, "subagent_count": sum(
        1 for x in rows if str(x.get("parent_id") or "").strip())}


@router.get("/v1/agent/sessions/{sid}/file")
async def session_file(sid: str, path: str, download: int = 0):
    """按会话工作区读任意文件（"给我看这个文件"的卡片用它取原图/下载）。

    只允许会话工作区内的路径，防目录穿越。
    """
    from fastapi.responses import FileResponse
    st = b.AGENT_SESSIONS.get(sid)
    if not st:
        raise HTTPException(404, "Session 不存在")
    root = Path(str(st.get("workspace") or b.BASE / "workspace")).resolve()
    target = (root / str(path).replace("\\", "/")).resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise HTTPException(404, "文件不存在或不在工作区内")
    return FileResponse(str(target), filename=target.name if download else None,
                        headers=None if download else {"Content-Disposition": "inline"})


@router.get("/v1/agent/sessions/{sid}/turns")
async def session_turns(sid: str):
    """整会话的回合流：每回合的提示词、回答与它之后的步骤数（界面合成"发送→回答→工具"）。"""
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    rows = b.AGENT_SESSIONS.turn_results(sid)
    return {"session_id": sid, "count": len(rows), "turns": rows}


@router.get("/v1/agent/sessions/{sid}")
async def session(sid: str):
    x = b.AGENT_SESSIONS.get(sid)
    if not x:
        raise HTTPException(404, "Session 不存在")
    try:
        _sub = b.AGENT_SESSIONS.subagent_stats(sid)
    except Exception:
        _sub = {"count": 0, "steps": 0, "tokens": 0}
    x = dict(x)
    x["subagent_count"] = _sub.get("count", 0)
    x["subagent_running"] = _sub.get("running", 0)
    x["subagent_steps"] = _sub.get("steps", 0)
    x["subagent_tokens"] = _sub.get("tokens", 0)
    return {"session": x, "steps": b.AGENT_SESSIONS.steps(sid),
            "files": b.AGENT_SESSIONS.file_events(sid),
            "messages": b.AGENT_SESSIONS.messages(sid),
            "token_budget": b.TOKEN_BUDGET, "conv_stats": b.AGENT_SESSIONS.conv_stats(sid),
            "snapshots": b.SNAPSHOTS.summary(sid)}


@router.get("/v1/agent/sessions/{sid}/snapshots")
async def session_snapshots(sid: str):
    """任务改动快照摘要：可回滚的文件清单 + 版本点列表。"""
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    summary = b.SNAPSHOTS.summary(sid)
    summary["versions"] = b.SNAPSHOTS.versions(sid)
    return summary


def _session_workspace_root(sid: str) -> Path:
    state = b.AGENT_SESSIONS.get(sid) or {}
    return Path(str(state.get("workspace") or b.AGENT_TOOLS.root))


@router.get("/v1/agent/sessions/{sid}/changes")
async def session_changes(sid: str):
    """任务完成后的「改了哪些文件」：每个文件的行级增删 + present 登记的交付物。

    数据来自**快照原像**（写操作前的状态）对比当前文件，不依赖 git；任务还在跑时
    也可以调用（前端只在完成/空闲时展示）。二进制文件只列路径，不给行数。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    try:
        data = b.SNAPSHOTS.changes(sid, default_root=_session_workspace_root(sid))
    except Exception as exc:
        raise HTTPException(500, f"统计改动失败: {exc}") from exc
    data["deliverables"] = b.declared_deliverables(sid)
    data["session_state"] = state.get("state")
    return data


@router.get("/v1/agent/sessions/{sid}/changes/diff")
async def session_change_diff(sid: str, path: str):
    """单个文件的 unified diff（只允许该会话快照里记录过的路径，杜绝任意读文件）。"""
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    out = b.SNAPSHOTS.diff(sid, path, default_root=_session_workspace_root(sid))
    if out is None:
        raise HTTPException(404, "该路径不在本任务的改动记录里")
    return out


class RollbackRequest(BaseModel):
    version: int | None = None


@router.post("/v1/agent/sessions/{sid}/rollback")
async def session_rollback(sid: str, req: RollbackRequest | None = None):
    """回滚该 Session 的工作区改动。

    不带 version：整任务回滚到开始前；带 version：撤销该版本之后的写操作。
    """
    version = req.version if req else None
    if b.session_running(sid):
        raise HTTPException(409, "Session 正在运行，不能回滚（先暂停或终止再回滚）")
    result = await b.rollback_session_changes(sid, version=version)
    return result


class SessionTagRequest(BaseModel):
    tag: str = Field(default="", max_length=40)


@router.post("/v1/agent/sessions/{sid}/tag")
async def set_session_tag(sid: str, req: SessionTagRequest):
    """给会话打标签（列表按标签分组；空串=取消标签）。

    标签是纯本地的组织手段：不参与 Agent 逻辑，也不影响恢复/续跑。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    tag = (req.tag or "").strip()
    b.AGENT_SESSIONS.patch(sid, tag=tag)
    return {"session_id": sid, "tag": tag}


@router.post("/v1/agent/sessions/{sid}/model")
async def set_session_model(sid: str, req: b.SessionModelRequest):
    """切换该会话使用的模型：立即持久化，下一回合生效，服务重启后仍沿用。

    会话级模型是续跑的依据——resume/auto_resume 都用它，所以这里落库后
    就不会再出现"重启后悄悄换回 Auto"。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    requested = (req.model or "").strip()
    model_id = b.normalize_model_soft(requested)
    b.AGENT_SESSIONS.patch(sid, model=model_id)
    updated = b.AGENT_SESSIONS.get(sid)
    note = "" if model_id == requested else f"{requested} 已失效，回退为 {model_id}"
    return {**updated, "applies": "下一回合生效（服务重启后仍沿用）",
            "applies_code": "next_turn", "note": note}


@router.post("/v1/agent/sessions/{sid}/actions/{action}")
async def control(sid: str, action: str):
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    maps = {"pause": {"state": "paused"}, "resume": {"state": "ready", "cancel_requested": False, "rollover_requested": False},
            "cancel": {"state": "cancelled", "cancel_requested": True}, "compact": {"compact_requested": True},
            "rollover": {"rollover_requested": True}, "archive": {"state": "archived", "archived": True}}
    if action not in maps:
        raise HTTPException(400, "未知操作")
    prev_state = (b.AGENT_SESSIONS.get(sid) or {}).get("state")
    if action == "resume":
        if prev_state in {"paused", "waiting", "suspended", "ready", "failed"}:
            # 恢复即续跑：直接从检查点拉起 runner，不再需要手动"继续执行"
            # was_running 决定提示语：runner 还活着（占着窗口/逻辑槽）时只唤醒不新建
            was_running = b.session_running(sid)
            started = await b.resume_session_task(sid)
        else:
            # 终态会话（completed/cancelled/archived）不改成 ready 也不拉起
            started = False
            return {**b.AGENT_SESSIONS.get(sid), "runner_started": False,
                    "note": f"会话状态为 {prev_state}，不支持恢复", "note_kind": "err",
                    "note_code": "resume_terminal", "note_vars": {"state": str(prev_state)}}
        result = dict(b.AGENT_SESSIONS.get(sid) or {})
        result["runner_started"] = started
        if started:
            result["note"] = "已恢复，正在续跑任务"
            result["note_kind"] = "ok"
        elif was_running or result.get("state") == "ready":
            # runner 还活着（正停在暂停循环里），已把状态改回 ready 唤醒它
            result["note"] = "已解除暂停：该会话的 runner 仍在运行，会继续往下跑"
            result["note_kind"] = "ok"
        else:
            result["note"] = "未能拉起 runner（会话可能正被其它任务占用），请稍后重试"
            result["note_kind"] = "err"
        return result
    return b.AGENT_SESSIONS.patch(sid, **maps[action])


@router.get("/v1/agent/sessions/{sid}/deletion-plan")
async def session_deletion_plan(sid: str):
    """删除前先问一句：这个任务用过 Copilot 网页会话吗？

    WebUI 用它做成"一次点击、自动判断"的删除：用过 Copilot 就一并删远端记录，
    只跑过 API 提供商模型的就只删本地——用户不必自己分辨该点哪个菜单项。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    urls = list(dict.fromkeys(state.get("conversation_urls") or []))
    if state.get("current_url") and state["current_url"] not in urls:
        urls.append(state["current_url"])
    model = str(state.get("model") or "")
    used_copilot = bool(urls) or model.startswith("copilot")
    return {
        "session_id": sid,
        "used_copilot": used_copilot,
        "model": model,
        "conversation_urls": urls,
        "remote_count": len(urls),
        "running": b.session_running(sid),
        # 本地记录永远可删；远端只在真用过 Copilot 时才有意义
        "suggest_delete_remote": used_copilot and bool(urls),
    }


@router.delete("/v1/agent/sessions/{sid}")
async def delete_session(sid: str, delete_remote: bool = False, force_local: bool = False,
                         request: HttpRequest = None):
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    # delete_remote + force_local 是 WebUI「重试远端并强制清理本地记录」的合法组合：
    # 先尽力删远端，失败的条目如实返回，再清本地。不视为参数冲突。

    remote_results = []
    if delete_remote:
        urls = list(dict.fromkeys(state.get("conversation_urls") or []))
        if state.get("current_url") and state["current_url"] not in urls:
            urls.append(state["current_url"])
        if b.session_running(sid):
            raise HTTPException(409, "Session 正在运行，不能删除")
        try:
            # 删除要导航 + 点侧栏，必须独占一个窗口；以前只拿全局锁，多窗口下会
            # 和任务抢同一个页面（删到一半被别的回合导航走 → 删错会话）。
            # 浏览器还没起来时（只有本地记录 / 测试）不为了删除去拉起浏览器：
            # 没有页面时远端删除本来就会逐条报失败，force_local 仍能清本地。
            stack = AsyncExitStack()
            try:
                if b.current_page() is not None:
                    await stack.enter_async_context(b._window("", wait_timeout=15))
                # 取到窗口后再复检一次：等待窗口期间该会话可能刚被拉起 runner
                if b.session_running(sid):
                    raise HTTPException(409, "Session 正在运行，不能删除")
                for index, url in enumerate(urls):
                    marker = b._agent_chat_marker(sid, index)
                    remote_results.append(await a.cd.delete_copilot_conversation(url, marker))
                # 对账式终验：按 [CBP-AGENT][sid] 标记清点侧栏——补删漏网会话，
                # 并把弹窗识别误判的失败条目改判为已删除（以侧栏实际残留为准）。
                remote_results = await a.cd.finalize_session_remote_deletion(sid, remote_results)
                b._forget_selected_model()  # 删除过程中页面在多个会话间跳转
            finally:
                await stack.aclose()
        except b.CopilotBusy as exc:
            raise HTTPException(409, f"{exc}（删除需要独占一个浏览器窗口）") from exc
        failed = [x for x in remote_results if not x["deleted"]]
        # force_local=true（WebUI 的"一并删，本地必须清掉"）不在这里中断：
        # 远端删不掉时仍然要删本地，并把失败的远端明细如实带回（否则用户看到的就是
        # "任务删不掉"——实测报错"左侧 Chats 中找不到…"时任务会留在列表里）。
        if failed and not force_local:
            lang = a._lang_of(request)
            for item in remote_results:
                if item.get("error"):
                    item["error"] = backend_i18n.translate(str(item["error"]), lang)
            return {
                "deleted": False,
                "local_deleted": False,
                "remote_results": remote_results,
                "message": backend_i18n.translate(
                    "部分 Copilot 会话删除失败，本地 Session 已保留。可检查后使用 "
                    "force_local=true 仅清理本地记录。", lang),
            }

    local_deleted = b.AGENT_SESSIONS.delete(sid)
    b.RUNTIME.pop(sid, None)
    b.cancel_message_queue(sid)   # 清理该会话的排队消息与 worker
    b.discard_provider_agent_history(sid)   # 清理提供商 Agent 对话历史
    b.discard_interrupts(sid)               # 清理强行插入的中断信号
    if local_deleted:
        # 会话记录已删除，其快照不再可回滚（回滚端点要求会话存在）：回收磁盘。
        # 自升级作用域 _self_<sid> 同样要清——否则孤儿作用域会被"回滚最近一次
        # 自升级"选中，把服务代码恢复到某个已不存在任务的旧原像。
        b.SNAPSHOTS.clear(sid)
        b.SNAPSHOTS.clear(b.self_scope(sid))
    lang = a._lang_of(request)
    for item in remote_results:
        if item.get("error"):
            item["error"] = backend_i18n.translate(str(item["error"]), lang)
    return {
        "deleted": local_deleted,
        "local_deleted": local_deleted,
        "remote_results": remote_results,
        "remote_skipped": backend_i18n.translate("未记录任何远端会话 URL，跳过远端删除", lang)
        if delete_remote and not remote_results else None,
    }


class MessageEditRequest(BaseModel):
    content: str = Field(min_length=1, max_length=200000)


@router.patch("/v1/agent/sessions/{sid}/messages/{mid}")
async def edit_session_message(sid: str, mid: int, req: MessageEditRequest):
    """编辑一条已入库的消息（WebUI 对话里的铅笔按钮）。

    ⚠ 语义要说清：只改本地历史。**已发给网页 Copilot 的内容无法撤回**；提供商
    会话则会把内存里的对话历史一并同步（否则模型下一回合仍看到旧文本）。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    msgs = {m["id"]: m for m in b.AGENT_SESSIONS.messages(sid)}
    old = msgs.get(int(mid))
    if old is None:
        raise HTTPException(404, "消息不存在")
    content = req.content.strip()
    if not content:
        raise HTTPException(400, "内容不能为空")
    if not b.AGENT_SESSIONS.update_message(sid, int(mid), content):
        raise HTTPException(500, "写入失败")
    patched = b.sync_provider_history_message(sid, str(old.get("content") or ""), content)
    note = ("已更新本地历史"
            + ("；提供商会话的上下文也已同步" if patched else "")
            + ("；已发给网页 Copilot 的内容无法撤回" if old.get("role") == "user" else ""))
    return {"updated": True, "message_id": int(mid), "history_synced": patched, "note": note}


@router.delete("/v1/agent/sessions/{sid}/messages/{mid}")
async def delete_session_message(sid: str, mid: int):
    """删除一条已入库的消息（只影响本地历史，语义同上）。"""
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    msgs = {m["id"]: m for m in b.AGENT_SESSIONS.messages(sid)}
    old = msgs.get(int(mid))
    if old is None:
        raise HTTPException(404, "消息不存在")
    if not b.AGENT_SESSIONS.delete_message(sid, int(mid)):
        raise HTTPException(500, "删除失败")
    patched = b.sync_provider_history_message(sid, str(old.get("content") or ""), None)
    return {"deleted": True, "message_id": int(mid), "history_synced": patched}


@router.post("/v1/agent/sessions/{sid}/messages")
async def continue_session_message(sid: str, req: a.SessionMessageRequest,
                                  request: HttpRequest = None):
    prompt_lang.set_request_lang(request.headers.get(a._LANG_HEADER) if request is not None else None)
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    if state.get("state") == "archived":
        raise HTTPException(409, "Session 已归档，不能继续；请新建任务")
    if state.get("state") == "cancelled":
        # 用户在被取消的会话里又发了消息 = 明确想接着做：等价于点「恢复」，
        # 保留历史与工作区，把状态拉回 ready 再走正常入队（以前这里直接 409，
        # 界面上只得到一句原始报错，用户实测以为是坏了）。
        b.AGENT_SESSIONS.patch(sid, state="ready", cancel_requested=False)
        state = b.AGENT_SESSIONS.get(sid) or state
        b.log(f"会话 {sid} 状态为 cancelled，收到新消息后自动恢复")
    content = req.content.strip()
    attach_paths = b.save_image_attachments(req.attachments)
    if not content and not attach_paths:
        raise HTTPException(400, "消息不能为空")
    if not content:
        content = "请分析我附上的图片。"
    model_id = b.normalize_model_soft(req.model or state.get("model") or b.DEFAULT_MODEL)
    if model_id != (req.model or state.get("model")):
        b.AGENT_SESSIONS.patch(sid, model=model_id)  # 清理会话里遗留的失效模型
    # 先把用户消息落库（历史里立即可见），再入队异步处理，避免阻塞 HTTP 响应。
    # 附件只记文件名（data/uploads 下），绝对路径会因服务目录搬家而失效。
    message_id = b.AGENT_SESSIONS.add_message(sid, "user", content, int(state.get("conversation_index", 0)),
                                              attachments=[p.name for p in attach_paths])
    # 「给我看 xxx 文件」：本地能读就直接读出来回给界面（DSH 那样的文件卡片），
    # 不调用 Copilot —— 否则它会去开右侧面板，我们的聊天里什么都看不到。
    try:
        from .fileview import maybe_file_view, render_message
        _root = Path(str(state.get("workspace") or (b.BASE / "workspace"))).resolve()
        _view = maybe_file_view(content, _root)
        if _view:
            _msg = render_message(_view, sid)
            b.AGENT_SESSIONS.add_message(sid, "assistant", _msg,
                                         int(state.get("conversation_index", 0)))
            b.runtime_update(sid, phase="completed",
                             detail=f"已读取文件 {_view['path']}")
            return {"queued": False, "local_file": _view["path"],
                    "kind": _view.get("kind"), "message_id": message_id}
    except Exception as exc:      # 读文件失败不阻断正常流程
        b.log(f"本地文件直读失败（回退到模型）：{exc}")

    b.runtime_update(sid, phase="followup_queued", detail=f"跟进消息已排队：{content[:80]}")
    return await b.enqueue_session_message(sid, content, model_id,
                                           bool(req.agent_mode), attach_paths, message_id)


@router.post("/v1/agent/sessions/{sid}/continue")
async def continue_agent_session(sid: str, req: a.SessionMessageRequest):
    req.agent_mode = True
    return await continue_session_message(sid, req)


class InjectRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    queue: str = "turn"   # turn=整轮重排（默认）；step=立即生效并抢占批量剩余动作


@router.post("/v1/agent/sessions/{sid}/inject")
async def inject_session_message(sid: str, req: InjectRequest):
    """强行插入指令（类似 DSH 的 Ctrl+J）：不抢锁、不等这轮跑完，立即生效。

    区别于 /messages 的"排队跟进"（那要等当前 run 结束再起一轮）：这里把指令写进
    会话的 pending，运行中的 Agent **在每个回合开头都会重读 state**
    （agent.py: `state = self.state_getter()`），因此下一回合就优先执行——用于
    "打断当前计划、插入新要求"。

    会话空闲时 pending 没人消费，改为按普通跟进入队，避免指令悬空。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    if state.get("state") in {"cancelled", "archived"}:
        raise HTTPException(409, f"Session 状态为 {state.get('state')}，不能插入指令")
    content = req.content.strip()
    if not content:
        raise HTTPException(400, "指令不能为空")
    conv_idx = int(state.get("conversation_index", 0))
    running = b.session_running(sid)
    b.AGENT_SESSIONS.add_message(sid, "user", content, conv_idx)
    step = {"step": int(state.get("total_steps", 0)) + 1, "status": "user_inject",
            "reason": f"运行中插入指令：{content[:120]}",
            "result": {"chars": len(content), "running": running}}
    b.AGENT_SESSIONS.add_step(sid, step["step"], conv_idx, step)
    if running:
        # 两级队列（P1-8）：默认 turn（整轮按新指令重排）；queue=step 时进 next-step
        # 队列——立即注入下一条提示词，并在批量执行途中抢占剩余动作。
        _queue = str(getattr(req, "queue", None) or "turn").lower()
        if _queue == "step":
            pending_steps = [str(x) for x in (state.get("pending_steps") or []) if str(x).strip()]
            pending_steps.append(content)
            b.AGENT_SESSIONS.patch(sid, pending_steps=pending_steps)
            b.request_interrupt(sid)
            b.runtime_update(sid, detail=f"已插入紧急指令（next-step）：{content[:80]}")
            return {"session_id": sid, "injected": True, "running": True, "queued": False,
                    "interrupted": True, "queue": "step", "pending_steps": len(pending_steps),
                    "note": "已插入 next-step 队列：立即生效，且批量中的剩余动作会被放弃",
                    "note_code": "inject_step"}
        pending = [str(x) for x in (state.get("pending") or []) if str(x).strip()]
        pending.append(content)
        b.AGENT_SESSIONS.patch(sid, pending=pending)
        # 立即中断当前回合（提供商：取消 HTTP 调用；网页：仅在还没产出内容时中止
        # 等待），这样新指令不必等这一轮跑完。
        b.request_interrupt(sid)
        b.runtime_update(sid, detail=f"已插入指令并中断当前回合：{content[:80]}")
        return {"session_id": sid, "injected": True, "running": True, "queued": False,
                "interrupted": True, "pending": len(pending),
                "note": "已插入并中断当前回合：Agent 会立即按新指令继续",
                "note_code": "inject_turn"}
    # 空闲会话：pending 不会有人消费 -> 走队列起一轮（等同普通跟进）
    queued = await b.enqueue_session_message(
        sid, content, b.normalize_model_soft(state.get("model") or b.DEFAULT_MODEL),
        True, None, None)
    return {"session_id": sid, "injected": True, "running": False, "queued": True,
            "position": queued.get("position"),
            "note": "会话当前空闲，已按跟进排队执行（运行中插入才会即时生效）",
            "note_code": "inject_idle"}


def _runtime_snapshot(sid: str, state: dict) -> dict:
    """运行时快照的**唯一**构造点：HTTP `/runtime` 与 SSE 都调它。

    教训：SSE 之前只推 `RUNTIME` 原始内容，缺 session_state/total_steps/queue_pending/
    retry_resume_at/token_budget 等字段，前端收到后把状态置成「—」、KPI 清零、限流倒计时
    隐藏，下一秒 1 秒轮询又改回来——任务运行中界面每秒抖动。两条读取路径必须给出同一份事实。
    """
    runtime = dict(b.RUNTIME.get(sid) or {})
    try:
        _sub = b.AGENT_SESSIONS.subagent_stats(sid)
    except Exception:
        _sub = {"count": 0, "steps": 0, "tokens": 0}
    runtime.update({
        "session_id": sid,
        "session_state": (state or {}).get("state"),
        "total_steps": (state or {}).get("total_steps", 0),
        "conversation_steps": (state or {}).get("conversation_steps", 0),
        "estimated_tokens": (state or {}).get("estimated_tokens", 0),
        "conversation_index": (state or {}).get("conversation_index", 0),
        "current_url": (state or {}).get("current_url"),
        "token_budget": b.TOKEN_BUDGET,
        # 并行窗口：busy 只看"这个会话是否正占着一个窗口/逻辑槽"
        "busy": b.session_running(sid),
        "windows": b.WINDOWS.stats(),
        "queue_pending": b.queue_depth(sid),
        # 子代理合计（KPI 要显示"含子代理"的步数/token，否则跑完 4 个子代理界面看不出来）
        "subagent_steps": _sub.get("steps", 0),
        "subagent_tokens": _sub.get("tokens", 0),
        "subagent_count": _sub.get("count", 0),
        # 服务不可用的**持续可见**信号：last_error 在恢复后才清，比 retry_resume_at 持久。
        # 前端据此在顶栏显示"⚠ 服务不可用/自动重试"小标（不弹遮罩，不打断操作）。
        "service_error": (state or {}).get("last_error") or "",
        "service_waiting": (state or {}).get("state") == "waiting",
        # 仍有后代在跑的个数：父会话即使已 completed，界面也不能笼统显示"已完成"
        "subagent_running": _sub.get("running", 0),
        "retry_resume_at": (state or {}).get("retry_resume_at"),
        "retry_attempt": (state or {}).get("retry_attempt", 0),
        "rate_limited_since": (state or {}).get("rate_limited_since"),
        # 破坏性工具待授权：前端据此显示"授权执行 <tool>"入口（否则用户只能手工 curl）。
        # 只有真的处于 waiting 才暴露：历史会话可能残留旧值（bridge.save 已负责清理），
        # 那会让界面把已完成的任务显示成"需要授权"，且每秒重新弹出来。
        "approval_pending": ((state or {}).get("approval_pending")
                             if (state or {}).get("state") == "waiting" else None),
        # 授权范围：once（逐参数）/ session（本会话内该工具都放行），前端据此措辞
        "approval_scope": str(b.AGENT_CFG.get("approval_scope", "once")).lower(),
        # 目标（goal 模式）与计划（todo_write）：让界面能显示"第几轮/还剩几项"，
        # 否则 /goal 跑起来之后用户只能从时间线猜进度
        "sandbox_mode": str(getattr(b.AGENT_TOOLS, "sandbox_mode", "workspace-write")),
        "plan_mode": bool((state or {}).get("plan_mode")),
        "plan": (state or {}).get("plan"),
        "goal": (state or {}).get("goal"),
        "todos": (state or {}).get("todos"),
    })
    return runtime


@router.get("/v1/agent/sessions/{sid}/events")
async def session_events(sid: str):
    """SSE 实时推送：runtime 快照 / 队列变化 / 新消息 / 会话列表变化。

    前端用 EventSource 连它，就不用轮询了——提供商流式输出因此是真·逐字。
    EventSource 不能带自定义请求头，token 走 `?token=`（鉴权中间件已支持）。
    连接断开（客户端离开/服务重启）时在 finally 里退订，不留内存。
    """
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    queue = b.subscribe_events(sid)

    async def gen():
        try:
            # 先送一份当前快照，客户端连上即有内容，不必等第一次变化
            snap = _runtime_snapshot(sid, b.AGENT_SESSIONS.get(sid) or {})
            yield f"data: {json.dumps({'type': 'runtime', 'data': snap}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"        # 心跳：防代理/浏览器把空闲连接掐掉
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            b.unsubscribe_events(sid, queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


@router.post("/v1/agent/sessions/{sid}/answer")
async def agent_answer(sid: str, payload: dict | None = None):
    """回答 Agent 的中途提问（ask_user_question 阻塞等待的就是这里）。"""
    fut = b.EXTRA_ANSWERS.get(sid)
    if fut is None or fut.done():
        # 已经没有挂起提问：顺手把 runtime 里的 question 清掉，
        # 否则前端那条提问条会因为陈旧快照一直显示（再点还报错）。
        b.runtime_update(sid, question=None)
        raise HTTPException(404, "该会话没有等待回答的问题")
    answer = str((payload or {}).get("answer") or "")
    fut.set_result(answer)
    b.runtime_update(sid, question=None)   # 立刻清，别等轮询
    return {"ok": True, "session_id": sid, "answer": answer}


@router.get("/v1/agent/sessions/{sid}/runtime")
async def session_runtime(sid: str):
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    return _runtime_snapshot(sid, state)


@router.get("/v1/agent/sessions/{sid}/prompt")
async def session_prompt(sid: str):
    """查看该会话的提示词：初始（首回合完整内容）与最近一条。

    初始提示词是排查"Agent 为什么这么做"的第一现场（含系统提示词、工具清单、
    项目约定、验证闭环规则）。旧会话未落库时会**按当前工作区近似重建**并标注。
    """
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    initial = (state.get("initial_prompt") or "").strip()
    runtime = b.RUNTIME.get(sid) or {}
    out = {"session_id": sid, "task": state.get("original_task") or "",
           "initial_prompt": initial, "rebuilt": False, "rebuild_error": None,
           "last_prompt": runtime.get("prompt_preview") or ""}
    if not initial:
        try:
            out["initial_prompt"] = b.rebuild_initial_prompt(state)
            out["rebuilt"] = True
        except Exception as exc:
            out["rebuild_error"] = str(exc)
    return out


# ── 跟进消息队列：查看 / 修改 / 删除排队中的消息 ──────────────────────
@router.get("/v1/agent/sessions/{sid}/queue")
async def list_queue(sid: str):
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    return {"session_id": sid, "pending": b.queue_items(sid), "depth": b.queue_depth(sid)}


@router.delete("/v1/agent/sessions/{sid}/queue/{item_id}")
async def delete_queue_item(sid: str, item_id: str):
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    if not b.remove_queue_item(sid, item_id):
        raise HTTPException(404, "队列条目不存在（可能已被处理）")
    return {"session_id": sid, "removed": True, "depth": b.queue_depth(sid)}


class QueueEditRequest(BaseModel):
    content: str | None = None
    model: str | None = None


@router.post("/v1/agent/sessions/{sid}/queue/{item_id}")
async def edit_queue_item(sid: str, item_id: str, req: QueueEditRequest):
    if not b.AGENT_SESSIONS.get(sid):
        raise HTTPException(404, "Session 不存在")
    if not req.content and not req.model:
        raise HTTPException(400, "content 或 model 至少提供一个")
    if not b.update_queue_item(sid, item_id, req.content, req.model):
        raise HTTPException(404, "队列条目不存在（可能已被处理）")
    item = b.find_queue_item(sid, item_id)
    return {"session_id": sid, "updated": True,
            "item": {"id": item["id"], "content": item["content"], "model_id": item["model_id"]} if item else None}


@router.post("/v1/agent/sessions/{sid}/activate")
async def activate_session(sid: str):
    state = b.AGENT_SESSIONS.get(sid)
    if not state:
        raise HTTPException(404, "Session 不存在")
    url = state.get("current_url")
    if not url:
        raise HTTPException(409, "该 Session 暂无 Copilot 会话 URL")
    if b.page is None:
        raise HTTPException(503, "浏览器未就绪")
    if b.any_window_busy():
        raise HTTPException(409, "有请求/任务正在使用浏览器，请稍后再切换页面")
    try:
        # 独占一个窗口再导航：多窗口下"共享页"可能正被某个任务的窗口用着
        async with b._window("", wait_timeout=10):
            # 回合等待中导航别的窗口会把回答错绑到别的会话，必须独占
            await b.page.bring_to_front()
            if b.page.url != url:
                await b.page.goto(url, wait_until="domcontentloaded", timeout=120000)
                b._forget_selected_model()
            shown_url = b.page.url
    except b.CopilotBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    b.runtime_update(sid, phase="activated", detail="已切换到该 Session 的 Copilot 页面")
    return {"activated": True, "url": shown_url, "headless": b.HEADLESS_CURRENT}


@router.get("/v1/agent/sessions/{sid}/event-log")
async def session_event_log(sid: str, limit: int = 200, kinds: str = ""):
    """会话的 append-only 事件日志（P2-10），只读，一次性返回历史事件。

    与 `/events` 的区别：`/events` 是 **SSE 实时推送**（前端 EventSource 长连接，
    调用方要按流读取，普通 GET 会一直挂着——这是设计如此）；本路由用于排障与审计。
    `kinds` 支持逗号分隔过滤（如 `kinds=turn_begin,turn_done`）。
    """
    kind_filter = tuple(k.strip() for k in kinds.split(",") if k.strip()) or None
    events = b.AGENT_SESSIONS.events(sid, limit=limit, kinds=kind_filter)
    return {"session_id": sid, "count": len(events), "kinds": list(kind_filter or ()),
            "events": events}


@router.get("/v1/agent/sessions/{sid}/projection")
async def session_projection(sid: str):
    """由事件流重建的状态快照（P2-10 第二期），只读；并附与数据库列的一致性核对。

    `consistency` 为空列表表示"事件足以重建列里的事实"——这是第二期的验收标准。
    运行时读取路径仍走数据库列；本路由只是旁路视图，不改变任何行为。
    """
    from .projection import compare_with_columns, project

    proj = project(b.AGENT_SESSIONS.events(sid, limit=10000))
    con = b.AGENT_SESSIONS._connect()
    turns = [dict(r) for r in con.execute(
        "SELECT prompt_hash,status,steps_after FROM turn_results WHERE session_id=?", (sid,))]
    steps = [dict(r) for r in con.execute(
        "SELECT step_no,tool,status FROM step_dispatch WHERE session_id=?", (sid,))]
    return {"session_id": sid, "projection": proj,
            "consistency": compare_with_columns(proj, turns, steps)}


@router.get("/v1/agent/sessions/{sid}/approvals")
async def list_approvals(sid: str):
    """本会话待用的一次性授权（allowed-once，无 allow-always）。"""
    return {"pending": b.AGENT_SESSIONS.pending_approvals(sid),
            "session": b.AGENT_SESSIONS.get(sid) or {},
            "approval_pending": (b.AGENT_SESSIONS.get(sid) or {}).get("approval_pending")}


@router.post("/v1/agent/sessions/{sid}/approvals")
async def grant_approval(sid: str, payload: dict):
    """用户下达一次性授权：{"tool": "delete_path", "args_hash": "<可选，精确到参数>"}

    不传 args_hash 时，按会话状态里记录的 approval_pending 精确授权（推荐：避免
    授权被用在意料之外的同名操作上）。
    """
    session = b.AGENT_SESSIONS.get(sid)
    if not session:
        raise HTTPException(404, "会话不存在")
    tool = str(payload.get("tool") or "").strip()
    args_hash = str(payload.get("args_hash") or "").strip()
    if not args_hash:
        pend = session.get("approval_pending") or {}
        if pend.get("tool") == tool and pend.get("args_hash"):
            args_hash = str(pend["args_hash"])
    if not tool or not args_hash:
        raise HTTPException(400, "需要 tool 与 args_hash（或先用 approval_pending 记录）")
    # 授权范围：once（逐参数，用后即焚，默认）/ session（本会话内该工具都放行）
    scope = str(payload.get("scope") or b.AGENT_CFG.get("approval_scope", "once")).lower()
    if scope == "session":
        b.AGENT_SESSIONS.grant_session_approval(sid, tool)
        note = "已授予本会话级授权；该工具在本会话内不再逐次询问"
        note_code = "approval_session_granted"
    else:
        b.AGENT_SESSIONS.grant_approval(sid, tool, args_hash)
        note = "已授权一次；该授权用后即焚，下次同类操作需重新授权。"
        note_code = "approval_granted"
    # SessionStore 没有 update()（只有 patch()）：原先这里抛 AttributeError → 授权接口 500，
    # 也就是"审批功能在 UI/API 侧根本走不通"。端到端验证才发现。
    try:
        # 同时清掉 approval_pending：否则前端授权横幅会一直挂着（已授权却还在提示）
        b.AGENT_SESSIONS.patch(sid, last_error=None, approval_pending=None)
    except Exception:
        pass
    # 时间线要有一条**正面**记录：否则拦下那条 "approval_required" 会成为唯一
    # 显示，看起来像"一直没授权"（用户实测反馈）。
    try:
        _step_no = int((session or {}).get("total_steps", 0)) + 1
        _step = {"step": _step_no, "status": "approval_granted", "tool": tool,
                 "reason": ("已授予本会话级授权" if scope == "session" else "已授权一次（用后即焚）"),
                 "result": {"tool": tool, "args_hash": args_hash, "scope": scope}}
        b.AGENT_SESSIONS.add_step(sid, _step_no, int((session or {}).get("conversation_index", 0)), _step)
    except Exception:
        pass
    # 恢复时明确告诉模型"已授权"，否则它可能沿用历史里"需要授权"的判断而放弃重试
    try:
        _hint = ("用户已授权对 %s 执行**这一次**操作（参数指纹 %s%s），请继续刚才被拦下的步骤。"
                 % (tool, args_hash,
                    "，本会话内该工具不再逐次询问" if scope == "session" else "，用后即焚"))
        b.set_resume_hint(sid, _hint)
    except Exception:
        pass
    return {"ok": True, "tool": tool, "args_hash": args_hash,
            "scope": scope, "note": note, "note_code": note_code}
