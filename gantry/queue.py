"""会话消息队列：跟进消息入队即返回，后台 worker 按序消费（DSH 式串行队列）。

为什么单独成模块：这是"消息编排"逻辑（排队/插队/编辑/删除/处理），与浏览器
自动化、Agent 主循环是不同关注点。它不持有自己的全局状态，全部通过 `b.`
访问 bridge 的共享状态（AGENT_SESSIONS / run_agent / provider_session_chat /
plain_chat_turn / runtime_update / publish_event）。

关键约定：`_message_worker` 里调用 `_process_queued_message` 必须走
`b._process_queued_message`——测试会用 `b._process_queued_message = fake` 替换它，
直接引用本模块的名字会导致 patch 不生效（队列按错的路径消费）。
"""

from __future__ import annotations

import asyncio
import collections
import uuid

from . import bridge as b

_MSG_QUEUES: dict[str, "collections.deque"] = {}
_MSG_WAKE: dict[str, asyncio.Event] = {}
_MSG_WORKERS: dict[str, asyncio.Task] = {}
_MSG_INFLIGHT: dict[str, dict] = {}


def queue_depth(sid: str) -> int:
    dq = _MSG_QUEUES.get(sid)
    return len(dq) if dq else 0


def queue_items(sid: str) -> list[dict]:
    """队列条目（含正在处理的那条）：处理中的标 processing 且位置 0，其余按 FIFO。

    只有"等待中"的条目（deque 内）可编辑/删除；处理中的只展示。
    """
    out = []
    inflight = _MSG_INFLIGHT.get(sid)
    if inflight:
        out.append({"id": inflight["id"], "position": 0, "processing": True,
                    "content": inflight["content"],
                    "mode": "agent" if inflight.get("agent_mode") else "chat",
                    "model_id": inflight.get("model_id")})
    dq = _MSG_QUEUES.get(sid)
    if dq:
        for i, item in enumerate(dq):
            out.append({"id": item["id"], "position": i + 1, "content": item["content"],
                        "mode": "agent" if item.get("agent_mode") else "chat",
                        "model_id": item.get("model_id")})
    return out


async def _message_worker(sid: str):
    dq = _MSG_QUEUES[sid]
    wake = _MSG_WAKE[sid]
    while True:
        while dq:
            item = dq.popleft()
            _MSG_INFLIGHT[sid] = item
            try:
                # 必须走 b.：测试会替换 b._process_queued_message 成可控桩
                await b._process_queued_message(sid, item)
            except Exception as exc:  # 单条失败不影响队列继续
                b.log(f"会话 {sid} 队列消息处理失败: {exc}")
            finally:
                _MSG_INFLIGHT.pop(sid, None)
        wake.clear()
        try:
            await wake.wait()        # 无待处理任务时挂起，等 enqueue 唤醒
        except asyncio.CancelledError:
            raise


def cancel_message_queue(sid: str) -> None:
    """删除会话时清理其队列与 worker（避免等待中的 worker 永久挂起）。"""
    task = _MSG_WORKERS.pop(sid, None)
    if task and not task.done():
        task.cancel()
    _MSG_QUEUES.pop(sid, None)
    _MSG_WAKE.pop(sid, None)
    _MSG_INFLIGHT.pop(sid, None)


async def _process_queued_message(sid: str, item: dict):
    content, model_id = item["content"], item["model_id"]
    attach_paths = item.get("attach_paths") or []
    state = b.AGENT_SESSIONS.get(sid)
    conv_idx = int(state.get("conversation_index", 0)) if state else 0
    b.runtime_update(sid, phase="followup_processing", detail=f"正在处理跟进消息：{content[:80]}")
    if item.get("agent_mode"):
        req = b.AgentRequest(
            model=model_id,
            messages=[b.Message(role="user", content=content)],
            stream=False, session_id=sid, reuse_session=True,
            workspace=state.get("workspace") if state else None)
        try:
            result, _ = await b.run_agent(req, attach_paths=attach_paths)
            answer = result.content
            # run_agent 收尾时**已经**把最终回答落库了（幂等）。这里以前也存一次，
            # 于是同一条回答出现两条（实测 message id 181/182，同一秒同内容）。
            # 与 L2510 那次修复同类问题，只是当年 agent 路径不走 run_agent 的落库。
        except Exception as exc:
            answer = f"[错误] {exc}"
            b.AGENT_SESSIONS.add_message(sid, "assistant", answer, conv_idx)
        b.runtime_update(sid, phase="followup_completed", answer_preview=answer[-12000:],
                         detail="跟进消息处理完成")
    else:
        pm = b.providers_mod.split_provider_model(model_id)
        try:
            if pm and pm[0] in b.PROVIDERS:
                answer = (await b.provider_session_chat(sid, content, model_id))["content"]
            else:
                answer = (await b.plain_chat_turn(sid, content, model_id,
                                                  attach_paths=attach_paths))["content"]
            # 内层（plain_chat_turn / provider_session_chat）已存消息并设了 answer_preview
            # ——且对话模式存的是 markdown 版——这里**不再重复存**、也不覆盖 answer_preview。
            # 此前外层 L2017 又存了一次：同一回复出现两条（一条 markdown、一条纯文本）。
            b.runtime_update(sid, phase="followup_completed", detail="跟进消息处理完成")
        except Exception as exc:
            answer = f"[错误] {exc}"
            b.AGENT_SESSIONS.add_message(sid, "assistant", answer, conv_idx)
            b.runtime_update(sid, phase="followup_completed", answer_preview=answer[-12000:],
                             detail="跟进消息处理完成")
    b.publish_event(sid, "messages")     # 新回复：通知前端立即刷新对话


async def enqueue_session_message(sid: str, content: str, model_id: str,
                                  agent_mode: bool, attach_paths: list | None = None,
                                  message_id: int | None = None) -> dict:
    """把跟进消息入队并确保 worker 在跑。返回排队位置（1 表示马上处理）。"""
    dq = _MSG_QUEUES.get(sid)
    if dq is None:
        dq = collections.deque()
        _MSG_QUEUES[sid] = dq
        _MSG_WAKE[sid] = asyncio.Event()
        # 必须走 b.：测试会替换 b._message_worker 成可控桩（不消费的 worker）
        _MSG_WORKERS[sid] = asyncio.create_task(b._message_worker(sid))
    ahead = len(dq)
    item_id = uuid.uuid4().hex[:12]
    dq.append({"id": item_id, "content": content, "model_id": model_id,
               "agent_mode": agent_mode, "attach_paths": attach_paths or [],
               "message_id": message_id})
    _MSG_WAKE[sid].set()
    b.publish_event(sid, "queue")
    b.publish_event(sid, "messages")
    return {"session_id": sid, "queued": True, "id": item_id, "position": ahead + 1,
            "mode": "agent" if agent_mode else "chat"}


def find_queue_item(sid: str, item_id: str) -> dict | None:
    dq = _MSG_QUEUES.get(sid)
    if not dq:
        return None
    for item in dq:
        if item.get("id") == item_id:
            return item
    return None


def remove_queue_item(sid: str, item_id: str) -> bool:
    """从等待队列移除一条；同步删除其已落库的 user 消息。返回是否移除。"""
    dq = _MSG_QUEUES.get(sid)
    if not dq:
        return False
    for i, item in enumerate(list(dq)):
        if item.get("id") == item_id:
            del dq[i]
            if item.get("message_id"):
                b.AGENT_SESSIONS.delete_message(sid, item["message_id"])
            b.publish_event(sid, "queue")
            b.publish_event(sid, "messages")
            return True
    return False


def update_queue_item(sid: str, item_id: str, content: str | None = None,
                      model_id: str | None = None) -> bool:
    """修改等待队列里的一条；同步更新其已落库的 user 消息。返回是否更新。"""
    item = find_queue_item(sid, item_id)
    if not item:
        return False
    if content is not None and content.strip():
        item["content"] = content.strip()
        if item.get("message_id"):
            b.AGENT_SESSIONS.update_message(sid, item["message_id"], content.strip())
    if model_id:
        item["model_id"] = b.normalize_model_soft(model_id)
    return True
