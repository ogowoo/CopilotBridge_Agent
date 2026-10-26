"""API 提供商（OpenAI 兼容 HTTP）通道：协议约束、按会话历史、普通对话与 Agent 回合。

为什么单独成模块：网页 Copilot 自己保留对话上下文，而提供商是**无状态 HTTP**，
必须把历史一起发过去（否则模型看不到工具清单与任务目标）。这套"增量累积 + 校验
自愈"的历史管理是独立的逻辑单元，不依赖浏览器页面。

运行时状态（`_PROVIDER_*`）只通过 `b.` 访问 bridge 的共享资源（PROVIDERS /
AGENT_SESSIONS / runtime_update / remember_initial_prompt），保证测试 rebind 生效。
"""

from __future__ import annotations

import asyncio
import time

from . import bridge as b
from .interrupts import _await_interruptible, clear_interrupt

# API 提供商的协议强化：网页 Copilot 对 JSON 协议遵循度高，但 DeepSeek/Kimi
# 等聊天模型爱在动作外面加解释或输出两段 JSON（撞严格防线烧回合）。
# 用独立 system 消息下达硬性输出约束，实测可显著降低解析失败率。
_PROVIDER_PROTOCOL = """你在与一个自动化执行器对话。你的整个回复必须严格遵守输出协议：
1. 只输出一个 JSON 对象：第一个字符必须是 {，最后一个字符必须是 }。
2. 禁止任何解释、思考过程、Markdown、代码块围栏（```），禁止输出第二个 JSON 对象。
3. JSON 只能是两种之一：
   {"type":"tool","tool":"工具名","arguments":{},"reason":"原因"}
   {"type":"final","content":"最终答复"}
4. 创建/追加代码、JSON、HTML、Markdown 内容必须用 write_text_b64 / append_text_b64，
   内容放 content_b64 字段（UTF-8 的 Base64），单次解码后不超过 8000 字符。
5. 工具名只能从用户消息末尾的可用工具清单里逐字照抄（如 list_directory、read_text），
   禁止猜测或创造工具名（如 list_dir、list_files、bash 都不存在）。
违反任何一条都会被系统拒绝执行并浪费回合。"""


# provider 普通对话的按会话锁：同 sid 并发请求基于相同历史交错写回的防护
_PROVIDER_LOCKS: dict[str, asyncio.Lock] = {}

# API 提供商 Agent 模式的按会话对话历史。
#
# 为什么必须留历史：AgentRunner 只在首回合（total==0）把系统提示词（含可用
# 工具清单）放进 prompt，后续回合只发"继续同一任务。上一动作实际结果：…"。
# 浏览器版 Copilot 自己保留对话上下文所以没问题；提供商是无状态 HTTP 调用，
# 若不把历史一起发过去，第二回合起模型既看不到工具清单也看不到任务目标
# （实测报错："本轮消息仍未附带可用工具清单，也没有给出任务目标…"直接卡死）。
_PROVIDER_AGENT_HISTORY: dict[str, list[dict]] = {}
_PROVIDER_HISTORY_MAX_CHARS = 160000


def _trim_provider_history(hist: list[dict], max_chars: int | None = None) -> None:
    """按字符上限回收历史：保留 system、**首条任务/工具清单消息**与最近一轮。

    首条 user 消息里带着完整的系统提示词（含可用工具清单），必须保住：
    实测 493 步的真实任务里，历史被裁剪后模型忘记了工具名，开始瞎编
    run_tests / check_syntax / execute_command，最后卡在验证门上判 failed。
    因此裁剪从 index 2 开始（跳过 system 与首条消息）。

    max_chars 取调用时的模块常量（作为函数默认参数会在定义时固定，
    运行期改常量就失效了）。至少留 system + 首条 + 最近一轮。
    """
    cap = b._PROVIDER_HISTORY_MAX_CHARS if max_chars is None else max_chars

    def total() -> int:
        return sum(len(m.get("content") or "") for m in hist)
    while total() > cap and len(hist) > 4:
        hist.pop(2)                                   # 保留 system 与首条（工具清单）
        if len(hist) > 2 and hist[2].get("role") == "assistant":
            hist.pop(2)                               # 它的回复一起丢，保持配对


def _repair_provider_history(hist: list[dict]) -> int:
    """按**真实 API 契约**校验并自愈提供商对话序列，返回修掉的条数。

    契约（实测 OpenAI 兼容行为，不是我当初想当然的"严格交替"）：
      · system 必须在首位（缺失则补回）；
      · 其后**不能以 assistant 打头**（没有对应用户输入的回答）；
      · 连续的 user 消息是**合法的**（服务端会顺次理解），不能为了"交替"去删；
      · 连续 assistant 才会被合并（保留较新的一条）。

    这里踩过两次坑，都由既有测试当场抓到：
      1. 先写"严格交替"→ 把锚点（首条含工具清单的 prompt）与最近一轮 user 拆开，
         既有测试断言"最近一轮仍在"立刻失败；
      2. 再写"与锚点冲突就丢新来的"→ 又把最近一轮 user 丢了，仍然失败。
    教训：契约要按**真实 API 行为**定，不要按"看起来更整齐"定。
    """
    fixed = 0
    if not hist:
        return fixed

    head = hist[0] if hist[0].get("role") == "system" else {"role": "system", "content": _PROVIDER_PROTOCOL}
    if head is not hist[0]:
        fixed += 1
    rest = list(hist[1:]) if hist[0].get("role") == "system" else list(hist)

    out: list[dict] = [head]
    for msg in rest:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            fixed += 1
            continue
        if role == "assistant" and len(out) == 1:
            fixed += 1                     # 不能以 assistant 打头（其 user 已丢失）
            continue
        if role == "assistant" and out[-1].get("role") == "assistant":
            out[-1] = msg                  # 连续 assistant：保留较新
            fixed += 1
            continue
        out.append(msg)                    # 连续 user 合法，原样保留

    if fixed:
        hist[:] = out
        b.log(f"提供商对话历史结构已自愈：修正 {fixed} 条（system 首位、不以 assistant 打头）")
    return fixed


def discard_provider_agent_history(sid: str) -> None:
    """会话删除时清理提供商对话历史（避免长期运行内存里堆积）。"""
    _PROVIDER_AGENT_HISTORY.pop(sid, None)


def sync_provider_history_message(sid: str, old: str, new: str | None) -> bool:
    """把"编辑/删除了一条消息"同步到提供商的内存对话历史。

    为什么必须做：提供商路径的上下文来自 `_PROVIDER_AGENT_HISTORY`（增量累积），
    不是每回合从 messages 表重建——只改库不同步内存，模型**下一回合仍会看到旧文本**。

    匹配策略：从后往前找第一条 content 与 old 相同的条目（最近的更可能是它）。
    找不到就返回 False（例如该回合已被上下文裁剪掉），由调用方如实告知用户。
    """
    hist = _PROVIDER_AGENT_HISTORY.get(sid)
    if not hist:
        return False
    for i in range(len(hist) - 1, -1, -1):
        item = hist[i]
        if not isinstance(item, dict) or item.get("role") == "system":
            continue
        if str(item.get("content") or "") == str(old or ""):
            if new is None:
                hist.pop(i)
            else:
                item["content"] = new
            return True
    return False


async def provider_turn(sid: str, model_id: str, prompt: str, force_new: bool = False) -> str:
    """API 提供商的 Agent 回合：带会话历史的 system 协议约束 + 累积对话。

    与浏览器版的关键差异：提供商每回合都是独立 HTTP 调用，必须把历史一起
    发过去（否则模型看不到工具清单与任务目标）。force_new（换会话/滚动摘要）
    时重置历史，从新的摘要提示重新开始。

    限流/5xx/超时映射为 CopilotRateLimited，复用 AgentRunner 的指数退避重试；
    其他错误原样抛出计入解析失败/失败处理。失败回合会把刚追加的 user 消息
    回滚掉，避免重试时同一提示在历史里堆叠。
    """
    pm = b.providers_mod.split_provider_model(model_id)
    provider = b.PROVIDERS[pm[0]]
    if force_new or sid not in _PROVIDER_AGENT_HISTORY:
        _PROVIDER_AGENT_HISTORY[sid] = [{"role": "system", "content": _PROVIDER_PROTOCOL}]
    hist = _PROVIDER_AGENT_HISTORY[sid]
    hist.append({"role": "user", "content": prompt})
    _trim_provider_history(hist)
    _repair_provider_history(hist)
    b.remember_initial_prompt(sid, prompt)
    # 提供商 Agent 回合原先完全不写 runtime，导致 WebUI 的"最近发送内容/
    # Copilot 当前回答/当前执行状态"三个面板全程空白——这里补齐。
    clear_interrupt(sid)   # 新回合开始：上一轮的插入信号已由 pending 承接，别误伤本回合
    b.runtime_update(sid, phase="api_sending", detail=f"正在调用 {pm[0]} API（{pm[1]}）",
                     prompt_preview=prompt[-12000:], answer_preview="")

    def _rollback_user() -> None:
        """回滚本次追加的 user 消息（含取消/中断），避免重试时重复追加。"""
        if hist and hist[-1].get("role") == "user":
            hist.pop()

    async def _call() -> str:
        """调用提供商：优先流式（边收边写 answer_preview，界面就能"逐字"看到），
        流式不可用（服务不支持 response_format+stream 等）则退回非流式 complete()。

        只在**一个增量都没收到**时才退回：已经收到内容再重发会重复计费、
        也可能拿到两份不同的回答。
        """
        if not bool(b.CFG.get("stream_provider_agent", True)) or not hasattr(provider, "stream"):
            return await provider.complete(pm[1], hist, response_schema=b.AGENT_ACTION_SCHEMA)
        chunks: list[str] = []
        last_push = 0.0
        try:
            async for piece in provider.stream(pm[1], hist,
                                               response_schema=b.AGENT_ACTION_SCHEMA):
                if not piece:
                    continue
                chunks.append(piece)
                now = time.monotonic()
                # 节流：最多每 150ms 写一次 runtime，避免高频 DB 写入
                if now - last_push >= 0.15:
                    last_push = now
                    b.runtime_update(sid, phase="api_streaming",
                                     detail=f"{pm[0]} API 正在流式返回…",
                                     answer_preview="".join(chunks)[-12000:])
        except (b.providers_mod.ProviderError, b.providers_mod.ProviderUnavailable) as exc:
            if chunks:
                raise
            b.log(f"提供商流式不可用，退回非流式：{exc}")
            return await provider.complete(pm[1], hist, response_schema=b.AGENT_ACTION_SCHEMA)
        except AttributeError as exc:
            # 自定义 provider 对象没实现 stream（保留兼容）
            b.log(f"提供商不支持流式（{exc}），改用非流式")
            return await provider.complete(pm[1], hist, response_schema=b.AGENT_ACTION_SCHEMA)
        text = "".join(chunks)
        if not text.strip():
            b.log("提供商流式返回为空，退回非流式")
            return await provider.complete(pm[1], hist, response_schema=b.AGENT_ACTION_SCHEMA)
        # 收尾必推完整文本：节流可能让最后一次增量没发出去，预览会停在半截
        b.runtime_update(sid, phase="api_streaming",
                         detail=f"{pm[0]} API 流式返回完成，正在解析动作",
                         answer_preview=text[-12000:])
        return text

    try:
        # 强行插入指令时立即取消这次 HTTP 调用：提供商是无状态请求，取消即
        # "这轮没发生"，下一回合直接带上新指令重发，不用等它跑完。
        answer = await _await_interruptible(sid, _call(), "调用提供商 API")
    except b.providers_mod.ProviderUnavailable as exc:
        _rollback_user()
        # window_seconds=0：提供商走"下次重试"倒计时，不用 Copilot 网页实测的
        # 1 小时固定窗口（提供商的 429/5xx/网络抖动通常几秒~几分钟就恢复）。
        raise b.CopilotRateLimited(str(exc), window_seconds=0) from exc
    except BaseException:
        # BaseException：包含 CancelledError——外部取消整个任务时也要回滚，
        # 否则下次运行会看到一条残留的 user 消息（重复追加）。
        _rollback_user()
        raise
    hist.append({"role": "assistant", "content": answer})
    _trim_provider_history(hist)
    _repair_provider_history(hist)
    b.runtime_update(sid, phase="api_received", detail=f"{pm[0]} API 已返回，正在解析动作",
                     answer_preview=answer[-12000:])
    return answer


async def provider_session_chat(sid: str, content: str, model_id: str) -> dict:
    """API 提供商的普通对话：用 Session 的消息记录作上下文，写回双方消息。

    调用方（continue_session_message）已写入本次 user 消息；新任务由
    plain_completions 先建 Session 再调本函数。
    """
    async with _PROVIDER_LOCKS.setdefault(sid, asyncio.Lock()):
        pm = b.providers_mod.split_provider_model(model_id)
        provider = b.PROVIDERS[pm[0]]
        history = [{"role": m["role"], "content": m["content"]}
                   for m in b.AGENT_SESSIONS.messages(sid)]
        b.runtime_update(sid, phase="api_sending", detail=f"正在调用 {pm[0]} API",
                         prompt_preview=content, answer_preview="")
        try:
            answer = await provider.complete(pm[1], history)
        except b.providers_mod.ProviderUnavailable as exc:
            raise b.CopilotRateLimited(str(exc), window_seconds=0) from exc
        current = b.AGENT_SESSIONS.get(sid)
        if not current:
            raise RuntimeError("会话在执行期间被删除")
        b.AGENT_SESSIONS.add_message(sid, "assistant", answer, int(current.get("conversation_index", 0)))
        b.AGENT_SESSIONS.patch(sid, model=model_id)
        b.runtime_update(sid, phase="api_completed", answer_preview=answer[-12000:],
                         detail=f"{pm[0]} API 调用完成")
        return {"session_id": sid, "role": "assistant", "content": answer,
                "url": None, "mode": "chat", "provider": pm[0]}
