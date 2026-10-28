from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .agent_parse import ParseMixin
from .agent_prompt import PromptMixin
from .agent_text import TextMixin
from .spans import set_recorder, span
from .tools import ToolRegistry

ChatFunction = Callable[[str, str, bool], Awaitable[str]]


def _decode_fragment(value: str) -> str:
    """JSON 字符串片段的容错反转义。

    必须先处理 '\\\\'（字面量反斜杠）再处理 \\n/\\t 等转义，否则源码中的
    \\\\n（字面量反斜杠+n）会被错解成 反斜杠+换行。
    """
    return (value
            .replace('\\\\', '\x00')      # 字面量反斜杠先占位
            .replace('\\r\\n', '\n')
            .replace('\\n', '\n')
            .replace('\\t', '\t')
            .replace('\\"', '"')
            .replace('\x00', '\\'))       # 还原


def validate_token_budget(config: dict[str, Any]) -> None:
 context = int(config.get("context_token_budget", 128000))
 compact = int(config.get("compact_at_tokens", 76000))
 rollover = int(config.get("rollover_at_tokens", 104000))
 if not 0 < compact < rollover <= context:
  raise ValueError(
   "Invalid agent token thresholds: require 0 < compact_at_tokens "
   "< rollover_at_tokens <= context_token_budget; got "
   f"{compact}, {rollover}, {context}"
  )



# 动作协议的 JSON Schema：给"能约束解码"的提供商（OpenAI json_schema /
# DeepSeek·Kimi json_object）用，把协议违规从提示层降到协议层。
# 刻意扁平（type 用 enum + 可选字段），因为多数 strict 模式对顶层 oneOf 支持差。
AGENT_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["tool", "final", "actions"]},
        "tool": {"type": "string", "description": "type=tool 时的工具名，必须来自可用工具清单"},
        "arguments": {"type": "object", "description": "工具参数对象"},
        "reason": {"type": "string", "description": "为什么执行这个动作，简短中文"},
        "content": {"type": "string", "description": "type=final 时的最终答复"},
        "actions": {
            "type": "array",
            "description": "type=actions 时的动作数组（批量），元素为 tool 动作",
            "items": {"type": "object"},
        },
    },
    "required": ["type"],
    "additionalProperties": True,
}


_PRODUCTIVE_TOOLS = ("write_text", "write_text_b64", "append_text", "append_text_b64", "replace_text", "apply_patch", "exec", "pwsh", "copilot_file", "create_goal", "write_office", "edit_office", "send_outlook", "post_web", "subagent_many", "create_skill", "self_write_text_b64", "self_replace_text", "self_append_text_b64")


class AgentPaused(Exception):
    """回合等待期间用户请求暂停，由桥接层抛出，用于立即中断并停放。"""


class CopilotServiceUnavailable(RuntimeError):
    """Copilot 服务暂时不可用（限流/繁忙/无响应），由桥接层识别抛出。

    与回答格式错误（invalid_action）严格区分：不记步、不记解析失败、
    不触发新建会话，由 run() 指数退避后在原会话重试。
    """

class CopilotRateLimited(CopilotServiceUnavailable):
    """真正的服务侧限流/不可用：只有它才启动固定窗口退避与前端倒计时浮窗。

    window_seconds 区分"预计恢复"的算法：
      None → 用 service_retry_window_seconds（Copilot 网页实测的固定窗口，约 1 小时）
      0    → 按本轮退避时间显示"下次重试"（API 提供商的 429/5xx/网络抖动通常
             几秒~几分钟就恢复，套 1 小时窗口会误导：用户实测看到"预计恢复 59:16"
             其实只是网络抖动）
      其它数值 → 用该窗口
    """

    def __init__(self, message: str = "", window_seconds: float | None = None):
        super().__init__(message)
        self.window_seconds = window_seconds


class AgentInterrupted(Exception):
    """用户"强行插入指令"（DSH Ctrl+J 式）：中止当前回合，改为下一回合按新指令执行。

    由桥接层抛出：提供商路径取消底层 HTTP 调用（无状态，取消即"这轮没发生"）；
    网页路径只在"本回合还没产出内容"时中止等待（避免丢掉 Copilot 已生成的回答）。
    run() 收到后本回合作废：不记步、不记失败、不换会话，回到循环顶部读取 pending。
    """


class CopilotSendFailed(RuntimeError):
    """本回合压根没发出去（输入框/发送按钮失效、页面未就绪）。

    与限流无关：不启动 1 小时窗口、不弹倒计时，只做短退避与明确提示，
    避免"明明没被限流却显示限流"的误报（实测 UI 误报来源）。
    """


class CopilotConversationFull(RuntimeError):
    """Copilot 提示当前对话已达到长度上限，由桥接层识别抛出。

    与限流（CopilotServiceUnavailable）不同：限流应原地退避重试，
    对话满了则留在原会话毫无意义，必须总结进度后切换新会话重发本回合。
    """


@dataclass
class AgentResult:
    content: str
    steps: list[dict[str, Any]]

# 连续重复执行会出错的工具：append 会重复追加，move/copy 会重复搬运。
# 换会话重发或模型重复回答时，同一动作可能连续到达，需要幂等保护。
NON_IDEMPOTENT_TOOLS = {"append_text", "append_text_b64", "move_path", "copy_path"}

# ── 任务可拆性识别（决定是否提示模型用子代理扇出）────────────────────────
_ITEM_SPLIT = re.compile(r"[、，,]\s*")
_PATHISH = re.compile(r"[\w./\\-]{2,}")


def delegation_targets(task: str) -> list[str]:
    """从任务文本里粗略挑出"互不依赖的目标"（目录/文件/主题名）。

    只做**提示**用途：命中就给模型一句"可考虑扇出"，不强制、不改变协议。
    判定保守一些，宁可漏提示也不要对普通任务噪声化。
    """
    text = (task or "").strip()
    if not text:
        return []
    # 形如 "a、b、c" / "x.pdf, y.pdf" 的并列项
    cand: list[str] = []
    for m in re.finditer(r"([\w./\\-]{2,}(?:\s*[、，,]\s*[\w./\\-]{2,}){2,})", text):
        parts = [p.strip() for p in _ITEM_SPLIT.split(m.group(1)) if p.strip()]
        if len(parts) >= 3:
            cand.extend(parts)
    # "三个/三种/多个 + 目录/文件/主题/接口" 这类量词
    quant = re.search(r"(?:三个|三种|三份|四个|四种|多个|若干)\s*([\w\u4e00-\u9fff]{1,6})", text)
    # 明确的路径列举（a/、b/、c/ 或 workspace/x）
    paths = re.findall(r"(?:^|[\s（(])((?:[\w.-]+/){1,3}[\w.-]+)", text)
    if len({p for p in paths if not p.startswith("http")}) >= 2:
        cand.extend(paths)
    cleaned, seen = [], set()
    for item in cand:
        low = item.lower()
        if low in seen or low.startswith("http"):
            continue
        seen.add(low)
        cleaned.append(item)
    if len(cleaned) >= 3:
        return cleaned[:6]
    if quant and len(cleaned) >= 2:
        return cleaned[:6]
    return []


class AgentRunner(PromptMixin, ParseMixin, TextMixin):
    # 一次回答里最多执行多少个动作（配置 max_actions_per_turn 可调，硬顶 12）。
    # 越长的批量输出越容易被 Copilot 输出上限截断，默认保守取 4。
    MAX_BATCH_ACTIONS = 4
 # removed compatibility method: def __new__(cls, tools, chat, config, *args, **kwargs):
  # removed compatibility allocation: super().__new__(cls)
  # compatibility assignment removed
 
    # 历史版本遗留的旧签名 __init__（引用不存在的 config）已移除，
    # 该类唯一的构造函数是下方接收 config/state_getter 的版本。
    def __init__(self, tools: ToolRegistry, chat: ChatFunction, config: dict[str, Any],
                 state_getter, state_saver, step_saver, file_event_saver, conv_stat_saver=None,
                 step_begin=None, step_finish=None, approval_check=None):
        self.tools, self.chat, self.config = tools, chat, config
        self.state_getter, self.state_saver = state_getter, state_saver
        self.step_saver, self.file_event_saver = step_saver, file_event_saver
        self.conv_stat_saver = conv_stat_saver
        # 工具派发检查点（可选）：派发前登记 inflight，结果落库后收尾。
        # 崩在「工具已执行、步骤未落库」窗口时靠它识别结果未知，避免重复副作用。
        self.step_begin, self.step_finish = step_begin, step_finish
        # 破坏性工具的一次性审批（fail-closed）：没有回调=不放行。
        self.approval_check = approval_check
        self.max_total_steps = min(int(config.get("max_total_steps", 1000)), 10000)
        # 一次回答最多执行几个动作（批量协议）：上限越小越不容易被输出上限截断
        self.max_actions_per_turn = max(1, min(int(config.get("max_actions_per_turn", 4)), 12))
        self.per_conversation = max(5, int(config.get("max_steps_per_conversation", 40)))
        self.max_rollovers = max(1, int(config.get("max_conversation_rollovers", 25)))
        self.compact_at = int(config.get("compact_at_tokens", 18000))
        self.rollover_at = int(config.get("rollover_at_tokens", 22000))
        self.token_budget = int(config.get("context_token_budget", 24000))
        self.keep_recent = max(2, int(config.get("keep_recent_steps", 8)))
        self.max_summary_tokens = int(config.get("max_summary_tokens", 5000))
        self.max_context_chars = int(config.get("max_context_chars", 90000))
        self.max_parse_failures = max(3, int(config.get("max_parse_failures", 5)))
        self.min_steps_before_rollover = max(10, int(config.get("min_steps_before_rollover", 30)))
        # 主动换会话（按本地 Token/字符/步数阈值）。M365 Copilot 的对话上限
        # 不公开且动态变化，本地压缩只是缩短后续提示、不会缩短远端对话本身，
        # 因此建议在 config.json 保持 auto_rollover=true 并配合保守阈值；
        # 每个会话结束时的实测数据会写入 conv_stats，用于持续校准阈值。
        self.auto_rollover = bool(config.get("auto_rollover", False))
        # auto_compact=false 可关闭按阈值的自动压缩（手动"压缩上下文"仍然可用）。
        self.auto_compact = bool(config.get("auto_compact", True))
        self.max_service_unavailable = max(3, int(config.get("max_service_unavailable", 30)))
        self.service_retry_base_seconds = max(5.0, float(config.get("service_retry_base_seconds", 30)))
        self.service_retry_max_seconds = max(60.0, float(config.get("service_retry_max_seconds", 600)))
        # Copilot 限流窗口：首次超限后到可再次使用大致要等这么久（实测约 1 小时，
        # 多加 1 分钟余量避免卡在临界点又撞一次限流）。倒计时浮窗锚定
        # "首次超限时间 + 该窗口"，而不是每次退避时长，否则用户会看到每 10 分钟
        # 跳一次的假倒数。Copilot 不会在文案里写明时长，所以这里用配置项，可按
        # 实际观测调整。
        self.service_retry_window_seconds = max(60.0, float(config.get("service_retry_window_seconds", 3660)))

    def _goal_continue_step(self, total: int) -> dict | None:
        """目标未完成 → 返回一条 goal_round 步骤并递增轮次；完成/无目标返回 None。

        对应 DSH 的 goal：目标处于 active 时，模型返回 final 不算结束，自动续轮；
        轮次用尽则标记 blocked（不再无限续）。状态存在会话 state["goal"] 里，
        由扩展工具 create_goal/update_goal 维护。
        """
        try:
            goal = (self.state_getter() or {}).get("goal")
        except Exception:
            return None
        if not isinstance(goal, dict) or str(goal.get("status")) != "active":
            return None
        rounds = int(goal.get("rounds") or 0)
        max_rounds = int(goal.get("max_rounds") or 10)
        if rounds >= max_rounds:
            goal["status"] = "blocked"
            goal["blocked_reason"] = f"达到轮次上限 {max_rounds}"
            try:
                self.state_saver(goal=goal)
            except Exception:
                pass
            return None
        goal["rounds"] = rounds + 1
        try:
            self.state_saver(goal=goal)
        except Exception:
            pass
        return {"step": total + 1, "status": "goal_round",
                "reason": f"目标「{str(goal.get('objective') or '')[:80]}」未完成，"
                          f"开始第 {goal['rounds']}/{max_rounds} 轮；请继续推进目标，"
                          "只有确认目标已完成时才返回 final（否则会自动续轮）",
                "error": ""}

    def _gate_message(self, strikes: int, auto: dict | None) -> str:
        """验证门拒绝消息：必须报出**逐字可照抄**的工具名与参数。

        实测教训：只说"尚未通过语法检查和单元测试"时，忘记工具名的模型会去编
        run_tests / check_syntax / execute_command，永远过不了门。
        """
        base = (f"代码已修改但尚未通过语法检查和单元测试，拒绝提前完成（第 {strikes} 次）。"
                "请立即依次调用这两个工具（**工具名逐字照抄**，不要用 run_tests、check_syntax、"
                "execute_command 等不存在的名字）：\n"
                '1) {"type":"tool","tool":"validate_python","arguments":{"path":"."}}\n'
                '2) {"type":"tool","tool":"run_unittest","arguments":{"path":"tests"}}\n'
                "两者都返回 passed=true 之后才能返回 final。")
        if auto and not (auto.get("syntax_ok") and auto.get("tests_ok")):
            detail = str(auto.get("detail") or "")[:800]
            base += f"\n（系统已代你补跑过一次，真实结果：{detail}）"
        return base

    async def _auto_validate(self, history: list, all_steps: list, total: int) -> dict[str, Any]:
        """系统代跑验证：validate_python(root) + run_unittest(tests|root)。

        补跑的是真实验证（py_compile / unittest），每步照常记进时间线，
        因此闭环没有放松；只是不再依赖模型记得工具名（长任务换会话后极易忘）。
        返回 {total, syntax_ok, tests_ok, detail}。
        """
        results: dict[str, Any] = {}
        detail_bits: list[str] = []
        calls = [("validate_python", {"path": "."})]
        tests_dir = self.tools.root / "tests"
        calls.append(("run_unittest", {"path": "tests" if tests_dir.is_dir() else "."}))
        for tool, args in calls:
            status, res = "ok", None
            try:
                res = await self.tools.execute(tool, args)
            except Exception as exc:
                status, res = "error", {"error": str(exc)}
            step = {"step": total + 1, "tool": tool, "arguments": args, "status": status,
                    "reason": "验证门补跑（模型未自行验证）"}
            if status == "ok":
                step["result"] = res
            else:
                step["error"] = str((res or {}).get("error") or "")
            total += 1
            history.append(step); all_steps.append(step); self.step_saver(step)
            passed = status == "ok" and isinstance(res, dict) and bool(res.get("passed"))
            results[tool] = passed
            if not passed:
                if status == "ok" and isinstance(res, dict):
                    fails = res.get("failures") or res.get("output") or ""
                    detail_bits.append(f"{tool} 未通过：{str(fails)[:300]}")
                else:
                    detail_bits.append(f"{tool} 执行失败：{step.get('error','')[:200]}")
        return {"total": total,
                "syntax_ok": bool(results.get("validate_python")),
                "tests_ok": bool(results.get("run_unittest")),
                "detail": "；".join(detail_bits) or "两项均未通过"}

    def _deterministic_summary(self, task: str, history: list[dict[str, Any]], state: dict) -> dict[str, Any]:
        completed, errors, modified, findings = [], [], dict(state.get("modified_files") or {}), []
        for item in history:
            tool, status = item.get("tool"), item.get("status")
            args = item.get("arguments") or {}
            if status == "ok" and tool:
                completed.append(f"{tool}: {args.get('path') or args.get('source') or ''}".strip())
            if status == "error": errors.append(str(item.get("error", ""))[:500])
            if tool in {"write_text", "append_text", "write_text_b64", "append_text_b64",
                        "replace_text", "copy_path", "move_path", "delete_path"}:
                path = args.get("path") or args.get("destination") or args.get("source")
                if path: modified[path] = {"operation": tool, "verified": tool == "read_text", "status": status}
            result = item.get("result")
            if isinstance(result, dict) and result.get("truncated"): findings.append(f"读取被截断: {result.get('path','')}")
        summary = {
            "goal": task,
            "completed": completed[-80:],
            "files_modified": modified,
            "pending": list(state.get("pending") or []),
            "important_findings": findings[-30:],
            "errors": errors[-20:],
            "last_successful_step": max([x.get("step", 0) for x in history if x.get("status") == "ok"] or [0]),
            "workspace": str(self.tools.root),
        }
        while self.estimate_tokens(json.dumps(summary, ensure_ascii=False)) > self.max_summary_tokens and (
                len(summary["completed"]) > 10 or summary["errors"] or summary["important_findings"]):
            # completed 砍 2/3，不够再砍 errors/findings，防止单侧截不下来的死循环
            if len(summary["completed"]) > 10:
                summary["completed"] = summary["completed"][len(summary["completed"]) // 3:]
            elif summary["errors"]:
                summary["errors"] = summary["errors"][len(summary["errors"]) // 2:]
            else:
                summary["important_findings"] = summary["important_findings"][len(summary["important_findings"]) // 2:]
        return summary

    def _work_ledger(self, state: dict, recent: list[dict]) -> dict:
        """把"已经看过什么、改过什么"压成清单，换会话时一起带进新会话。

        为什么需要：pyshot 任务换了 4 次 Copilot 会话，每次模型都要重新定位，
        `list_directory` 调用 105 次而只有 13 次真的执行——新会话只拿到 8 条
        `recent_steps`，目录/文件清单早被压缩截断，模型只能重新列一遍。
        """
        read, listed, grepped = [], [], []
        for st in recent or []:
            if st.get("status") != "ok":
                continue
            args = st.get("arguments") or {}
            path = str(args.get("path") or "").strip()
            tool = str(st.get("tool") or "")
            if not path:
                continue
            bucket = listed if tool == "list_directory" else (
                grepped if tool in ("grep", "find_files") else (
                    read if tool in ("read_text", "read_excel", "read_csv", "read_pdf",
                                     "read_docx", "copilot_file") else None))
            if bucket is None or path in bucket:
                continue
            bucket.append(path)

        try:
            modified = [str(x) for x in (state.get("modified_files") or [])][-12:]
        except Exception:
            modified = []
        todos = state.get("todos") or []
        done = sum(1 for t in todos if str((t or {}).get("status")) == "completed")
        left = [str((t or {}).get("content") or "")[:60] for t in todos
                if str((t or {}).get("status")) != "completed"][:5]
        ledger = {
            "已读过的文件": read[-12:],
            "已列过的目录": listed[-8:],
            "已搜索过的范围": grepped[-6:],
            "已改过的文件": modified,
            "待办进度": (f"{done}/{len(todos)} 已完成" if todos else "（无待办清单）"),
            "还没做完的事": left,
            "上次的错误": str(state.get("last_error") or "")[:200] or "（无）",
        }
        return {k: v for k, v in ledger.items() if v not in ([], "", None)}

    def _continuation_prompt(self, task: str, summary: dict, recent: list[dict], state: dict) -> str:
        payload = {
            "original_goal": task,
            "long_term_summary": summary,
            "already_looked_at": self._work_ledger(state, recent),
            "recent_steps": [self.compact_value(x, 2500) for x in recent[-self.keep_recent:]],
            "current_workspace": str(self.tools.root),
            "total_steps": state.get("total_steps", 0),
            "conversation_index": state.get("conversation_index", 0),
        }
        return (self._system_prompt() + "\n\n这是长期任务的继续执行，不要重新开始。\n"
                + json.dumps(payload, ensure_ascii=False)
                + "\n`already_looked_at` 是已经看过的目录/文件与待办进度：**这些不要重新列/重新读**，"
                  "需要复核就 read_text 带 line_start/line_count 只看那一段。"
                  "从待完成事项继续，返回下一个 JSON 动作（互不依赖的小动作可批量）。")

    def _record_conv_stat(self, state: dict, conv_steps: int, tokens: int, chars: int,
                          ended_by: str, model: str) -> None:
        """每个 Copilot 会话结束时记录实测数据，用于校准本地阈值。

        M365 Copilot 的对话长度上限不公开且动态变化；本地 compact/rollover
        阈值只是估算起点，需要用这些真实样本（多少回合/Token/字符时触顶、
        是主动切换还是被页面上限截断）持续修正。
        """
        if not self.conv_stat_saver:
            return
        try:
            self.conv_stat_saver({
                "conversation_index": int(state.get("conversation_index", 0)),
                "model": model,
                "steps": conv_steps,
                "tokens": tokens,
                "chars": chars,
                "ended_by": ended_by,
                "url": state.get("current_url") or "",
            })
        except Exception:
            pass

    async def run(self, task: str, model: str) -> AgentResult:
        # 解析失败的措辞要**按来源**：provider 模型 id 形如 `kimi-coding/k3`（带斜杠），
        # Copilot 网页模型是 `copilot-*`（不带）。此前硬编码 "Copilot 未返回…"，
        # 用 Kimi 跑的时候时间线会误导成"Copilot 的问题"（用户实测抓到的文案 bug）。
        self._model_source = "提供商模型" if "/" in str(model or "") else "Copilot"
        state = self.state_getter()
        history = list(state.get("recent_steps") or [])
        all_steps: list[dict[str, Any]] = []
        parse_failures = 0
        # 限流重试次数必须从持久状态恢复：挂起/暂停/重启会新建 runner，
        # 若从 0 起步，界面上每次都显示"第 1 次"，用户无法判断到底重试了多少次。
        service_failures = int(state.get("retry_attempt") or 0)
        send_failures = 0
        # 验证闭环三面旗必须从持久状态恢复：暂停→继续/服务重启会新建 runner，
        # 若从 False 起步，"代码已改但没验证"的记忆就丢了，恢复后的 Agent 直接
        # 返回 final 即可绕过验证门（实测过）。
        gate = {}
        try:
            raw_gate = state.get("gate_state")
            if isinstance(raw_gate, str):
                gate = json.loads(raw_gate) if raw_gate.strip() else {}
            elif isinstance(raw_gate, dict):
                gate = raw_gate
        except Exception:
            gate = {}
        validation_required = bool(gate.get("validation_required"))
        syntax_validated = bool(gate.get("syntax_validated"))
        tests_passed = bool(gate.get("tests_passed"))

        gate_strikes = 0   # 连续被验证门拒绝的次数：超限即失败收尾，防无限重试烧 token
        # no-op 完成守卫：任务明显要求动手（创建/修改/运行…）却没真动手就 final。
        # 实测两个缺口：① 模型幻觉式宣称"已记录/已完成"（零工具调用）；
        # ② 先读一堆文件、再直接 final 不写任何产出——原先只拦 total==0，
        #    这种"读完了就宣布完成"完全不被拦（用户实测：能读，没有写步骤）。
        noop_strikes = 0
        _memory_nudged = False   # 记忆提醒只拦一次（fail-open），不无限拦
        # 实例级标记：换会话（rollover）发生在同一次 run() 内，而 history 会被
        # 截断；用实例标记记住"本次运行有没有真的产出/尝试过产出"。
        self._action_done = False
        self._action_tried = False
        # 回合预算：连续多少个回合"没有新改动、也没有新信息"（读过的路径不算新信息）
        self._dry_turns = 0
        self._turn_dry = True
        self._seen_paths: set[str] = set()
        _action_words = ("创建", "新建", "写", "修改", "改", "删除", "运行", "执行", "生成",
                         "保存", "记录", "记到", "整理", "转换",
                         "create", "write", "modify", "delete", "run", "save")
        # 只看**最新一条指令**：original_task 里的"生成文档"不该让后续的
        # "帮我治理发布流水线问题"（纯分析）也被逼着产出文件（实测模型会 copy 一份
        # 文档当"产出"来满足守卫）。
        _latest_ins = (task or "")
        try:
            _pend = [str(x) for x in (state.get("pending") or []) if str(x).strip()]
            if _pend:
                _latest_ins = _pend[-1]
            elif "用户追加任务：" in _latest_ins:
                _latest_ins = _latest_ins.rsplit("用户追加任务：", 1)[1]
        except Exception:
            pass
        _needs_action = any(w in _latest_ins for w in _action_words)
        # "动手"类工具：成功执行即视为真的产生了产出/改动（读取、验证类不算）。
        _action_tools = {"write_text", "append_text", "write_text_b64", "append_text_b64",
                         "replace_text", "make_directory", "copy_path", "move_path",
                         "delete_path", "run_python", "write_excel", "write_docx",
                         "outlook_send", "copilot_file", "skill_create", "memory_write",
                         "self_write_text_b64", "self_append_text_b64", "self_replace_text"}

        def _did_action() -> bool:
            # 先看实例标记：换会话（rollover）会截断 history，只看 history 会在
            # 换会话后误判"没有任何产出"（实测：一个 493 步、6 次换会话的真实
            # 任务里，模型已成功 replace_text 99 次，仍被误判并弹了 noop_guard）。
            if getattr(self, "_action_done", False):
                return True
            return any((s.get("tool") or "") in _action_tools and s.get("status") == "ok"
                       for s in history)

        def _tried_action() -> bool:
            return getattr(self, "_action_tried", False) or any(
                (s.get("tool") or "") in _action_tools for s in history)

        def _save_gate() -> None:
            self.state_saver(gate_state=json.dumps({
                "validation_required": validation_required,
                "syntax_validated": syntax_validated,
                "tests_passed": tests_passed,
            }, ensure_ascii=False))
        # 工作区项目上下文：AGENT.md 全量注入 + skills 清单。每次 run 重新读取，
        # 恢复运行/换工作区后总能拿到最新版本。
        self._session_id = str(state.get("id") or state.get("session_id") or "")
        # 验证门的"比例原则"：默认要求 validate+unittest 双通过；但任务本身只要求
        # 语法检查（或明确不需要单测）时，强制单测会逼模型给一次性脚本造 tests/
        # 目录与测试文件——实测 gate-limit 因此涨到 24 步。故允许按任务放宽。
        _relax_words = ("只做语法检查", "只需语法检查", "不用单测", "无需单测", "不需要单测",
                        "不用写测试", "syntax only", "no tests")
        # 工作区原本没有 tests/ 时，不强制单测：否则模型会给一次性脚本现造
        # tests/ 与测试文件，测试写坏了反而把简单修改卡成 gate_blocked。
        # 需要“即使没有测试也必须跑 unittest”的场景可显式开
        # require_unittest_without_tests=true。
        _tests_dir = Path(getattr(self.tools, "root", ".")) / "tests"
        _has_project_tests = _tests_dir.is_dir() and any(_tests_dir.glob("test_*.py"))
        _force_unittest = bool(self.config.get("require_unittest_without_tests", False))
        self._require_unittest = (
            bool(self.config.get("require_unittest", True))
            and not any(w in (task or "") for w in _relax_words)
            and (_has_project_tests or _force_unittest)
        )
        self._task_text = task
        self._agent_md = self._load_agent_md()
        self._skills = self._load_skill_catalog()
        # 恢复运行不自动新建 Copilot 会话；只有显式 rollover_requested 才换会话。
        force_rollover = bool(state.get("rollover_requested"))
        rollover_reason = "Token/步数/手动触发自动创建新 Copilot 会话"
        # 已存在 Copilot 会话时，即使 total==0 也不再新建聊天（暂停/重启恢复场景）。
        chat_active = bool(state.get("current_url"))
        # 启动时尊重已存在的暂停请求，不把 paused 覆盖成 running。
        if state.get("state") == "paused":
            self.state_saver(last_error=None, cancel_requested=False)
        else:
            self.state_saver(state="running", last_error=None, cancel_requested=False)

        while int(state.get("total_steps", 0)) < self.max_total_steps:
            state = self.state_getter()
            if state.get("cancel_requested"):
                self.state_saver(state="cancelled")
                return AgentResult("任务已取消。", all_steps)
            while state.get("state") == "paused":
                await asyncio.sleep(1)
                state = self.state_getter()
                if state.get("cancel_requested"):
                    self.state_saver(state="cancelled")
                    return AgentResult("任务已取消。", all_steps)

            total = int(state.get("total_steps", 0))
            conv_steps = int(state.get("conversation_steps", 0))
            tokens = int(state.get("estimated_tokens", 0))
            chars = int(state.get("estimated_chars", 0))
            # 会话级计数：只在换会话时清零，压缩不重置——本地压缩缩短的是后续
            # 提示，M365 远端对话长度不变，rollover 压力必须按会话全程累计。
            conv_t = int(state.get("conv_tokens", 0))
            conv_c = int(state.get("conv_chars", 0))
            rollovers = int(state.get("conversation_index", 0))
            manual_compact = bool(state.get("compact_requested"))
            manual_rollover = bool(state.get("rollover_requested"))
            need_compact = manual_compact or (self.auto_compact and
                                              (tokens >= self.compact_at or chars >= self.max_context_chars))
            summary = dict(state.get("summary") or {})
            if need_compact:
                summary = self._deterministic_summary(task, history, state)
                history = history[-self.keep_recent:]
                self.state_saver(summary=summary, recent_steps=history, compact_requested=False,
                                 estimated_tokens=self.estimate_tokens(json.dumps(summary, ensure_ascii=False)),
                                 estimated_chars=len(json.dumps(summary, ensure_ascii=False)))
                state = self.state_getter()
                tokens = int(state.get("estimated_tokens", 0))
                chars = int(state.get("estimated_chars", 0))

            # 压缩后使用最新计数重新判断；rollover 压力看会话级 conv_* 计数。
            automatic_pressure = (conv_t >= self.rollover_at or
                                  conv_c >= self.max_context_chars or
                                  conv_steps >= self.per_conversation)
            automatic_rollover = (self.auto_rollover and automatic_pressure and
                                  conv_steps >= self.min_steps_before_rollover)
            need_rollover = bool(force_rollover or manual_rollover or automatic_rollover)
            carrying_pending = False
            carrying_step = False

            # 上一回合结算：整轮都没有进展就累加，有进展就清零
            if getattr(self, "_turn_dry", False):
                self._dry_turns = getattr(self, "_dry_turns", 0) + 1
            else:
                self._dry_turns = 0
            self._turn_dry = True
            if need_rollover:
                # 记录上一个 Copilot 会话的实测数据（主动切换或触顶被动切换）
                self._record_conv_stat(state, conv_steps, conv_t, conv_c, rollover_reason, model)
                if rollovers >= self.max_rollovers:
                    self.state_saver(state="failed", last_error="超过最大 Copilot 会话切换次数")
                    return AgentResult("超过最大 Copilot 会话切换次数，任务已停止。", all_steps)
                pending_carry = [x for x in (state.get("pending") or []) if str(x).strip()]
                if not summary or pending_carry:
                    # pending 非空时必须重算摘要：用户追加的指令要随检查点
                    # 带进新会话，不能被无条件 pending=[] 静默丢弃。
                    summary = self._deterministic_summary(task, history, state)
                prompt = self._continuation_prompt(task, summary, history, state)
                force_new = True
                # 若用户在回合间隙请求了暂停，保留 paused，循环顶部会立即停放。
                park_state = "paused" if (self.state_getter() or {}).get("state") == "paused" else "running"
                self.state_saver(conversation_index=rollovers + 1, conversation_steps=0,
                                 estimated_tokens=0, estimated_chars=0,
                                 conv_tokens=0, conv_chars=0,
                                 rollover_requested=False, compact_requested=False,
                                 summary=summary, state=park_state, last_error=None,
                                 pending=[])  # pending 已随 continuation 摘要带入新会话
                tokens = 0
                chars = 0
                conv_t = 0
                conv_c = 0
                conv_steps = 0
                force_rollover = False
                event = {"step": total + 1, "status": "session_rollover", "reason": rollover_reason}
                rollover_reason = "Token/步数/手动触发自动创建新 Copilot 会话"
                all_steps.append(event); self.step_saver(event)
            elif total == 0:
                prompt = self._system_prompt() + "\n\n用户任务：\n" + task
                # 任务看起来能拆成几个互不依赖的目标时，只在**用户消息**里给一句
                # 可选提示（系统提示词保持逐字稳定）：弱模型不会自己想到扇出。
                if "subagent_many" in self._active_tool_names():
                    targets = delegation_targets(task)
                    if len(targets) >= 2:
                        prompt += ("\n\n（提示：这个任务里有 %d 个互不依赖的目标：%s。"
                                   "若它们各自都需要多轮读取/分析，可以先用 subagent_many "
                                   "一次把它们派出去，各自跑完再汇总；若几步就能直接做完，"
                                   "就按普通流程做。）" % (len(targets), "、".join(targets)))
                # 首回合也要带上"运行中插入的指令"：被强行插入中断时本回合不计步，
                # total 仍是 0，若这里不读 pending，插入的指令会被推迟一个回合。
                # （run_agent 会把"同一会话的新任务文本"也塞进 pending，那段已在
                # task 里了，用 `not in task` 过滤掉，避免重复下达。）
                pending_now = [str(x).strip() for x in (state.get("pending") or [])
                               if str(x).strip() and str(x).strip() not in task]
                if pending_now:
                    prompt += ("\n\n【当前目标】用户最新提出的要求（以这条为准，"
                               "不要再重复上一轮的结论）：\n"
                               + "\n".join(pending_now)
                               + "\n\n如果它与原任务无关，就按这条新要求直接执行；"
                                 "不要再用上一轮的说法回答。")
                    carrying_pending = True
                # 新话题首轮：Copilot 网页通道可以"直接生成文件"——copilot_file 让
                # Copilot 生成并提供下载入口，内容不经过聊天消息，因此不受单条输出
                # 上限约束（大文件一个回合拿到，不用 write_text_b64 分块写、也不会
                # 截断）。这里只在首轮、且确实有这个工具时给一次建议，不写进系统
                # 提示词（系统提示词要保持逐字稳定，prompt cache 依赖它）。
                if self._first_turn_copilot_hint():
                    prompt += ("\n\n首轮提示（新话题·一次成型）：本轮是新会话第一轮，目标是"
                               "**尽快拿到可运行的结果**，不是逐步探索。若任务要产出新的东西"
                               "（脚本/页面/小工具/demo/文档）：\n"
                               "1) 不要先逐个 list/read 探索，也不要 write_text_b64 分块拼；"
                               "直接用 copilot_file(path, instruction) 让 Copilot **一次生成完整文件**"
                               "（instruction 写全功能、入口、依赖、示例；多文件就多次调用，连续调用，"
                               "不要写一段就停下来汇报）。\n"
                               "2) 文件拿到后**本地**立刻验证：run_python / validate_python / run_unittest。\n"
                               "3) 出错**先本地修**（read_text 定位 + replace_text 改）；本地修两轮仍不行，"
                               "才带着具体报错定向问 Copilot（只改出错那一处，不要整篇重生成）。\n"
                               "目标：一次任务 1~3 个 Copilot 回合交付。只有需要精确修改**已有**文件、"
                               "或内容很短需要逐块校验时，才用 write_text_b64/append_text_b64/replace_text。")
                force_new = not chat_active
            else:
                pending_now = [str(x) for x in (state.get("pending") or []) if str(x).strip()]
                if pending_now:
                    # 用户追加的指令直接在当前会话下达（不新建会话），保留完整上下文；
                    # 送达成功后才清除 pending，发送失败/对话已满时保留并随换会话摘要携带。
                    prompt = ("【当前目标】用户最新提出的要求（以这条为准，"
                              "不要再重复上一轮的结论）：\n"
                              + "\n".join(pending_now)
                              + "\n\n如果它与原任务无关，就按这条新要求直接执行；"
                                "不要再用上一轮的说法回答。")
                    force_new = not chat_active
                    carrying_pending = True
                else:
                    last = self.compact_value(history[-1], 6000) if history else summary
                    last_json = json.dumps(last, ensure_ascii=False)
                    if isinstance(last, dict) and last.get("status") == "invalid_action":
                        prompt = self._repair_prompt(last, last_json)
                    else:
                        prompt = "继续同一任务。上一动作实际结果：\n" + last_json
                    force_new = False

            # ── next-step 队列（P1-8，对应 DSH 的 ReactLoopInbox 两级队列）──
            # pending（next-turn）：整轮替换为"优先执行追加指令"；
            # pending_steps（next-step）：细粒度、**立即生效**，追加在当前提示词后面，
            # 用于"执行途中改主意"（例如"别用 run_python"），不必等下一轮重排计划。
            _urgent_now = []
            try:
                _urgent_now = [str(x) for x in (self.state_getter() or {}).get("pending_steps") or []
                               if str(x).strip()]
            except Exception:
                _urgent_now = []
            if _urgent_now:
                prompt += ("\n\n用户在执行途中插入的紧急指令（立即遵守，优先于原计划）：\n"
                           + "\n".join(_urgent_now))
                carrying_step = True

            # 回合预算提示：连续若干回合没有新改动/新信息，就催它做一次收尾判断，
            # 别在"再确认一下"里空转（实测一个 388 步任务里有大段这种长尾）。
            _nudge_at = int(self.config.get("turn_budget_nudge", 6) or 0)
            if _nudge_at and self._dry_turns >= _nudge_at:
                prompt += (f"\n\n【收尾判断】你已经连续 {self._dry_turns} 个回合没有产生新的改动或新信息。"
                           "现在二选一：① 直接返回 final 汇报现状（完成了什么 / 验证结果 / 还缺什么）；"
                           "② 明确说出下一步要做的**具体动作**并立刻执行它——不要再来一轮同样的核对。")
            prompt += f"\n\n当前总步骤 {total + 1}/{self.max_total_steps}。返回下一个 JSON 动作（互不依赖的小动作可批量）。"
            # 发送前最后确认暂停/取消，避免暂停生效后仍向 Copilot 发起新回合。
            state = self.state_getter()
            if state.get("cancel_requested"):
                self.state_saver(state="cancelled")
                return AgentResult("任务已取消。", all_steps)
            while state.get("state") == "paused":
                await asyncio.sleep(1)
                state = self.state_getter()
                if state.get("cancel_requested"):
                    self.state_saver(state="cancelled")
                    return AgentResult("任务已取消。", all_steps)
            _turn_span = span("agent.turn", "invoke_agent",
                              **{"gen_ai.request.model": model,
                                 "gen_ai.provider.name": ("copilot-web" if "/" not in str(model)
                                                          else str(model).split("/", 1)[0]),
                                 "gantry.session_id": str(getattr(self, "_session_id", "") or ""),
                                 "gantry.step": total,
                                 "gantry.prompt_chars": len(prompt or "")})
            try:
                with _turn_span:
                    raw = await self.chat(prompt, model, force_new)
                    _turn_span.set(**{"gantry.answer_chars": len(raw or "")})
                chat_active = True
                chat_active = True
            except CopilotConversationFull as exc:
                # Copilot 提示对话达到长度上限：本回合未生效，不算失败。
                # 置 force_rollover 回到循环顶部，走换会话分支总结进度后在新会话重发。
                rollover_reason = f"Copilot 提示对话达到长度上限，自动切换新会话（{str(exc)[:100]}）"
                force_rollover = True
                continue
            except AgentPaused:
                # 回合等待中被暂停：本轮不计步，停放至恢复或取消。
                # 回合已发送到 Copilot，标记会话有效，恢复后不再新建聊天。
                chat_active = True
                state = self.state_getter()
                while not state.get("cancel_requested") and state.get("state") == "paused":
                    await asyncio.sleep(1)
                    state = self.state_getter()
                if state.get("cancel_requested"):
                    self.state_saver(state="cancelled")
                    return AgentResult("任务已取消。", all_steps)
                continue
            except AgentInterrupted as exc:
                # 用户"强行插入指令"（DSH Ctrl+J 式）：本回合作废——不记步、不记失败、
                # 不换会话、不清 pending；回到循环顶部后，pending 里的新指令会被
                # "请优先执行最新指令"分支读取并执行。
                # 提供商路径是取消 HTTP 调用（无状态，取消即"这轮没发生"）；
                # 网页路径只在"还没产出内容"时才中止等待（见 wait_agent_answer）。
                chat_active = True     # 网页路径回合可能已发出，重试留在原会话
                parse_failures = 0
                service_failures = 0
                ev = {"step": total + 1, "status": "user_interrupt",
                      "reason": f"已插入新指令，当前回合被中断：{str(exc)[:160]}"}
                history.append(ev); all_steps.append(ev); self.step_saver(ev)
                self.state_saver(recent_steps=history[-self.keep_recent:], last_error=None)
                continue
            except CopilotSendFailed as exc:
                # 没发出去：短退避重试，不进入"限流窗口"，不设置 retry_resume_at
                send_failures += 1
                reason = str(exc)[:300]
                step = {"step": total + 1, "status": "send_failed",
                        "error": f"消息未能发送（第 {send_failures} 次）：{reason}"}
                all_steps.append(step); self.step_saver(step)
                self.state_saver(recent_steps=history[-self.keep_recent:], last_error=step["error"])
                if send_failures >= 3:
                    return AgentResult(
                        f"连续 {send_failures} 次未能把消息送进 Copilot 输入框：{reason}。"
                        "这通常不是限流，而是页面/输入框状态异常——可在界面点『切换到页面』查看，"
                        "或点『重试浏览器』重建页面后继续。", all_steps)
                _base = float(self.config.get("send_retry_base_seconds", 5) or 5)
                _d = min(_base * send_failures, max(_base * 4, 20))
                _dl = time.monotonic() + _d
                while time.monotonic() < _dl:
                    _s = self.state_getter() or {}
                    if _s.get("cancel_requested"):
                        self.state_saver(state="cancelled", last_error=None)
                        return AgentResult("任务已取消。", all_steps)
                    if _s.get("state") == "paused":
                        # 暂停即停放：退避停止，恢复后回循环顶部重发
                        break
                    await asyncio.sleep(1)
                continue
            except (CopilotRateLimited, TimeoutError) as exc:
                # 服务限流/不可用/长时间无进度：不记步、不记解析失败、不换会话，
                # 指数退避后在原 Copilot 会话重试；退避期间可暂停、可取消。
                chat_active = True  # 回合大概率已发出，重试留在原会话
                # 已在"已知限流窗口"内的重复失败属于同一次故障：不累加计数、不重复记事件，
                # 否则一次窗口就能把 max_service_unavailable（30）吃满，任务被误判为"服务持续不可用"。
                _prev = self.state_getter() or {}
                _prev_resume = float(_prev.get("retry_resume_at") or 0)
                # 只有"网页版固定窗口"（retry_note='window'）在窗口过去后才把计数归零；
                # 提供商模型的 next_retry 语义里，每次重试都是一次新的失败，
                # 若也归零就会永远到不了 max_service_unavailable（实测死循环）。
                _window_passed = (str(_prev.get("retry_note") or "") == "window"
                                  and bool(_prev_resume) and _prev_resume <= time.time())
                service_failures = 1 if _window_passed else service_failures + 1
                parse_failures = 0
                # 异常消息可能为空（实测 httpx 的 ReadTimeout/ConnectError 常是
                # 空字符串），那样步骤里就只剩"（第 N 次）："什么都看不到——退化成
                # 类名，保证用户至少知道是哪类错误。
                reason = str(exc).strip()[:300] or f"{type(exc).__name__}（无详细消息）"
                # transient=True：这类记录不是模型的失败，也不消耗步数预算；
                # 前端把连续的若干条合成一行「服务限流/超时（重试 N 次）」。
                step = {"step": total + 1, "status": "service_unavailable", "transient": True,
                        "error": f"服务暂时不可用（第 {service_failures} 次）：{reason}"}
                # 降噪：只记第 1/5/10/… 次，时间线里由前端合并成一行
                if service_failures == 1 or service_failures % 5 == 0:
                    all_steps.append(step); self.step_saver(step)
                # 退避期间状态置 waiting（前端显示"等待服务"）；用户已暂停时不抢状态。
                _updates = {"recent_steps": history[-self.keep_recent:], "last_error": step["error"]}
                if (self.state_getter() or {}).get("state") == "running":
                    _updates["state"] = "waiting"
                # 本轮退避时长（先算，倒计时要用）
                delay = min(self.service_retry_base_seconds * (2 ** min(service_failures - 1, 4)),
                            self.service_retry_max_seconds)
                # 倒计时终点：
                # - Copilot 网页（window_seconds=None）：固定窗口（实测约 1 小时），
                #   锚定"首次超限"，退避重试期间终点不跳变。
                # - API 提供商（window_seconds=0）：按本轮退避时间显示"下次重试"——
                #   提供商的 429/5xx/网络抖动通常几秒~几分钟就恢复，套 1 小时窗口
                #   会误导（用户实测看到"预计恢复 59:16"其实只是网络抖动）。
                # - 显式给数值：用该窗口。
                _window = getattr(exc, "window_seconds", None)
                if _window == 0:
                    _updates.update({"rate_limited_since": None,
                                     "retry_resume_at": time.time() + delay,
                                     "retry_attempt": service_failures,
                                     "retry_note": "next_retry"})
                else:
                    limited_since = (self.state_getter() or {}).get("rate_limited_since") or time.time()
                    _updates.update({"rate_limited_since": limited_since,
                                     "retry_resume_at": limited_since + (
                                         self.service_retry_window_seconds if _window is None
                                         else float(_window)),
                                     "retry_attempt": service_failures,
                                     "retry_note": "window"})
                self.state_saver(**_updates)
                # ── 挂起（suspend）────────────────────────────────
                # 只有 Copilot 网页模型才挂起：限流是网页通道的问题，提供商模型有自己的
                # 429 语义（window_seconds=0，走上面的 next_retry）。
                # 挂起 = 会话停在"suspended"，不再反复撞墙、不再刷事件；窗口到点由后台
                # 周期任务自动继续。若用户配了别的提供商模型，界面会在挂起卡片里给
                # 「换模型继续」，不必等到窗口结束。
                _is_copilot_model = "/" not in str(model or "")
                _is_rate_limit = isinstance(exc, CopilotRateLimited)
                _suspend_on = bool(self.config.get("rate_limit_suspend", True))
                if _suspend_on and _is_copilot_model and _is_rate_limit and _window is None:
                    _end = float((self.state_getter() or {}).get("retry_resume_at")
                                 or (time.time() + self.service_retry_window_seconds))
                    self.state_saver(
                        state="suspended", suspend_reason="rate_limit",
                        retry_resume_at=_end, retry_attempt=service_failures,
                        last_error=(
                            f"Copilot 网页限流，已挂起：预计 {time.strftime('%H:%M', time.localtime(_end))} "
                            "后可自动继续；如果你配了其它提供商模型，也可以现在换模型继续（挂起卡片里选）。"))
                    b_log = getattr(__import__("gantry.bridge", fromlist=["x"]), "log", None)
                    if callable(b_log):
                        try:
                            b_log(f"会话因 Copilot 限流挂起，预计 {time.strftime('%H:%M', time.localtime(_end))} 恢复")
                        except Exception:
                            pass
                    return AgentResult("Copilot 网页限流，任务已挂起；窗口结束会自动继续，也可换用其它提供商模型立即继续。",
                                       all_steps)
                if service_failures >= self.max_service_unavailable:
                    # 用户已请求暂停时不覆盖 paused，只记录原因。
                    if (self.state_getter() or {}).get("state") == "paused":
                        self.state_saver(last_error="Copilot 服务持续不可用，已停止自动重试；恢复后可点击继续",
                                         retry_resume_at=None, retry_attempt=0, rate_limited_since=None)
                    else:
                        self.state_saver(state="waiting",
                                         last_error="Copilot 服务持续不可用，已停止自动重试；服务恢复后请点击继续",
                                         retry_resume_at=None, retry_attempt=0, rate_limited_since=None)
                    return AgentResult("Copilot 服务持续不可用，任务已安全暂停，可稍后继续。", all_steps)
                # 保持指数退避：长窗口内长睡会让暂停/取消变迟钝，而且 Copilot 可能提前恢复；
                # 真正的问题是「一次长限流把 30 次失败预算吃满」——上面用 _window_passed 在窗口过后归零解决。
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    state = self.state_getter()
                    if state.get("cancel_requested"):
                        self.state_saver(state="cancelled", retry_resume_at=None, retry_attempt=0,
                                         rate_limited_since=None)
                        return AgentResult("任务已取消。", all_steps)
                    if state.get("state") == "paused":
                        # 暂停即停止倒数：恢复后重新走退避，不必保留过期 resume_at
                        self.state_saver(retry_resume_at=None, retry_attempt=0, rate_limited_since=None)
                        break  # 回到循环顶部停放
                    await asyncio.sleep(1)
                continue
            if carrying_step:
                # 紧急指令已随本回合送达，清空 next-step 队列（避免每条提示词都重复注入）
                self.state_saver(pending_steps=[])
            if carrying_pending:
                # 追加指令已成功送达 Copilot 会话，清除待送达列表避免重复下达。
                self.state_saver(pending=[])
            used_tokens = self.estimate_tokens(prompt) + self.estimate_tokens(raw)
            used_chars = len(prompt) + len(raw or "")
            try:
                action = self._parse(raw)
                parse_failures = 0
                service_failures = 0
                send_failures = 0
                # 服务恢复：waiting 状态回到 running（paused 状态不受影响），
                # 并清掉退避倒计时（浮窗消失）。
                if (self.state_getter() or {}).get("state") == "waiting":
                    self.state_saver(state="running", retry_resume_at=None, retry_attempt=0,
                                     rate_limited_since=None)
                elif (self.state_getter() or {}).get("retry_resume_at"):
                    self.state_saver(retry_resume_at=None, retry_attempt=0, rate_limited_since=None)
            except Exception as exc:
                parse_failures += 1
                _src = getattr(self, "_model_source", "模型")
                _msg = str(exc)
                if not _msg.startswith(_src):
                    _msg = f"{_src}{_msg}"
                # 解析失败的提示要**可操作**：实测 37 次里多数是"回了一段解释/计划
                # 或多个 JSON 混排"。这里补上最小示例与相关工具名，让下一回合能直接纠正。
                _msg = (_msg.rstrip() + "\n\n【如何纠正】下一次回复**只**输出一个 JSON 对象，"
                        "不要任何解释、不要代码围栏、不要多个对象并列。例："
                        '{"type":"tool","tool":"read_text","arguments":{"path":"REL/PATH","line_start":1,'
                        '"line_count":80},"reason":"看实现"}。'
                        "常用工具名：list_directory / read_text / grep / find_files / replace_text / "
                        "write_text_b64 / validate_python / run_unittest / todo_write / pwsh。"
                        '整个任务做完才用 {"type":"final","markdown":"结论"}。')
                step = {"step": total + 1, "status": "invalid_action", "error": _msg,
                        "raw_tail": (raw or "")[-2000:]}
                history.append(step); all_steps.append(step); self.step_saver(step)
                self.state_saver(total_steps=total + 1, conversation_steps=conv_steps + 1,
                                 estimated_tokens=tokens + used_tokens, estimated_chars=chars + used_chars,
                                 conv_tokens=conv_t + used_tokens, conv_chars=conv_c + used_chars,
                                 recent_steps=history[-self.keep_recent:], last_error=str(exc))
                if parse_failures >= self.max_parse_failures:
                    # 连续解析失败是回答格式问题，不是上下文耗尽：压缩后继续用当前会话。
                    # 不新建 Copilot 会话，也不覆盖用户可能已请求的 paused 状态。
                    self.state_saver(compact_requested=True, rollover_requested=False,
                                     last_error=_msg)
                    force_rollover = False
                    parse_failures = 0
                continue

            # 单个动作与批量动作统一处理：批量协议一次回答执行多个工具动作，
            # 省掉"每个分块一个 Copilot 回合"的开销；每个动作仍单独记一步
            # （时间线、文件审计、幂等保护、验证闭环都不变）。
            todo = (action.get("actions") if str(action.get("type")) == "actions" else [action]) or []
            todo = todo[:self.max_actions_per_turn]
            batch_size = len(todo)
            batch_ok = batch_errors = 0
            usage_counted = False
            for idx, sub in enumerate(todo):

                # next-step 队列：批量执行途中若出现紧急指令，立刻放弃剩余动作，
                # 让下一回合按新指令走（避免已经过时的动作继续产生副作用）。
                _urgent = []
                try:
                    _urgent = [str(x) for x in (self.state_getter() or {}).get('pending_steps') or []
                               if str(x).strip()]
                except Exception:
                    _urgent = []
                if _urgent:
                    _pre = {'step': total + 1, 'status': 'step_preempted',
                            'reason': '用户插入紧急指令，放弃剩余 ' + str(len(todo) - idx) + ' 个动作',
                            'error': '紧急指令：' + '；'.join(_urgent)[:300]}
                    history.append(_pre); all_steps.append(_pre); self.step_saver(_pre)
                    total += 1
                    self.state_saver(total_steps=total, recent_steps=history[-self.keep_recent:],
                                     last_error=_pre['error'])
                    break
                if str(sub.get("type")) == "final":
                    if _needs_action and not _did_action() and noop_strikes < 2:
                        noop_strikes += 1
                        if _tried_action():
                            _where = ("尝试过修改但每次都失败（见时间线里的 error 步骤），"
                                      "没有任何成功产出")
                            _how = ("先用 read_text 取到文件的精确片段（注意缩进与行尾）再 replace_text，"
                                    "写新文件用 write_text_b64")
                        elif total == 0:
                            _where = "还没有执行任何工具动作"
                            _how = "（写文件用 write_text_b64/append_text_b64，改现有内容用 replace_text）"
                        else:
                            _where = "至今只做了读取/检查，没有任何产出或改动"
                            _how = "（写文件用 write_text_b64/append_text_b64，改现有内容用 replace_text）"
                        warn = {"step": total + 1, "status": "noop_guard",
                                "error": f"任务要求实际动手（创建/修改/写入/运行），但你{_where}"
                                         f"就宣布完成。请先用工具落地结果{_how}，"
                                         "再返回 final；若确实无需任何改动，请明确说明原因后再次 final。"}
                        history.append(warn); all_steps.append(warn); self.step_saver(warn)
                        total += 1
                        self.state_saver(total_steps=total, recent_steps=history[-self.keep_recent:],
                                         last_error=warn["error"])
                        continue
                    # 记忆提醒（软钩子，与 noop_guard 同模式）：做了实质工作却一条
                    # memory_write 都没记过时，拦一次 final 提醒它——实测历史里 memory_write
                    # 全库只被调 1 次，软提示对模型等于不存在。拦一次就放行（不无限拦）。
                    _mem_enabled = str(self.config.get("memory_nudge", "1")) not in ("0", "false", "False")
                    _did_memory_write = any(
                        (s2.get("tool") == "memory_write") and (s2.get("status") == "ok")
                        for s2 in all_steps)
                    _productive_n = sum(
                        1 for s2 in all_steps
                        if (s2.get("status") == "ok") and (s2.get("tool") in _PRODUCTIVE_TOOLS))
                    # 阈值 ≥3：写 1~2 个文件的小任务不打扰（批量测试/日常小改就是这么挂的），
                    # 真正做了一段活才提醒收尾记一条。
                    if (_mem_enabled and _productive_n >= 3 and not _did_memory_write
                            and not _memory_nudged):
                        _memory_nudged = True
                        warn = {"step": total + 1, "status": "memory_nudge",
                                "error": "本次任务若有**跨任务仍有价值**的事实（用户偏好、环境特征、"
                                         "某个接口的坑、已验证的结论），先用 memory_write 记一条再 final；"
                                         "确实没有可记的，就明说「没有」然后再次 final（只提醒这一次）。"}
                        history.append(warn); all_steps.append(warn); self.step_saver(warn)
                        total += 1
                        self.state_saver(total_steps=total, recent_steps=history[-self.keep_recent:],
                                         last_error=warn["error"])
                        continue
                    _tests_ok = tests_passed or not getattr(self, '_require_unittest', True)
                    if validation_required and not (syntax_validated and _tests_ok):
                        gate_strikes += 1
                        # 第一次被拒时由系统补跑验证。实测（493 步真实任务）：模型
                        # 换过多次会话后**忘记了工具名**，瞎编 run_tests / check_syntax
                        # / execute_command / execute_python，怎么都过不了门，几小时
                        # 的成果被判 failed。补跑的是**真验证**（py_compile + unittest），
                        # 结果照常记进时间线，闭环不放松——只是不再赌模型记得工具名。
                        _auto = None
                        if gate_strikes == 1:
                            _auto = await self._auto_validate(history, all_steps, total)
                            total = _auto["total"]
                            syntax_validated, tests_passed = _auto["syntax_ok"], _auto["tests_ok"]
                            _save_gate()
                            if syntax_validated and tests_passed:
                                self._record_conv_stat(state, conv_steps + 1, conv_t + used_tokens,
                                                       conv_c + used_chars, "completed", model)
                                self.state_saver(state="completed", total_steps=total + 1,
                                                 conversation_steps=conv_steps + 1,
                                                 estimated_tokens=tokens + used_tokens,
                                                 estimated_chars=chars + used_chars)
                                validation_required = syntax_validated = tests_passed = False
                                _save_gate()
                                _g = self._goal_continue_step(total)
                                if _g is not None:
                                    history.append(_g); all_steps.append(_g); self.step_saver(_g)
                                    total += 1
                                    self.state_saver(total_steps=total, recent_steps=history[-self.keep_recent:])
                                    break
                                return AgentResult(sub.get("content") or "任务已完成。", all_steps)
                        step = {"step": total + 1, "status": "validation_gate",
                                "error": self._gate_message(gate_strikes, _auto)}
                        if gate_strikes >= 3:
                            step["status"] = "gate_blocked"
                            step["error"] = (self._gate_message(gate_strikes, _auto)
                                             + " 已停止自动重试以避免无限循环。")
                            history.append(step); all_steps.append(step); self.step_saver(step)
                            self.state_saver(state="failed", total_steps=total + 1,
                                             recent_steps=history[-self.keep_recent:],
                                             last_error=step["error"])
                            return AgentResult(step["error"], all_steps)
                        history.append(step); all_steps.append(step); self.step_saver(step)
                        self.state_saver(total_steps=total + 1, conversation_steps=conv_steps + 1,
                                         recent_steps=history[-self.keep_recent:], last_error=step["error"])
                        total += 1; conv_steps += 1
                        break
                    _g = self._goal_continue_step(total)
                    if _g is not None:
                        history.append(_g); all_steps.append(_g); self.step_saver(_g)
                        total += 1
                        self.state_saver(total_steps=total, recent_steps=history[-self.keep_recent:])
                        break
                    self._record_conv_stat(state, conv_steps + 1, conv_t + used_tokens,
                                           conv_c + used_chars, "completed", model)
                    self.state_saver(state="completed", total_steps=total + 1,
                                     conversation_steps=conv_steps + 1,
                                     estimated_tokens=tokens + used_tokens,
                                     estimated_chars=chars + used_chars)
                    # 正常完成：清掉验证旗，避免会话被追加新任务时被旧旗误拦
                    validation_required = syntax_validated = tests_passed = False
                    _save_gate()
                    return AgentResult(sub.get("content") or "任务已完成。", all_steps)

                tool, arguments = sub.get("tool", ""), sub.get("arguments") or {}
                step = {"step": total + 1, "tool": tool, "arguments": arguments,
                        "reason": sub.get("reason", "")}
                if batch_size > 1:
                    step["batch_of"] = batch_size
                event = None
                # 幂等保护：换会话接力时摘要若没让模型明确感知"上一动作已完成"，
                # 或回合重发，同一动作可能连续到达。对非幂等工具（append 会重复
                # 追加、move/copy 会重复搬运），与上一成功动作完全相同时跳过执行。
                # ── 无进展循环检测（合法 JSON 也可能在策略上打转）──
                # 实测：提供商模型会在"写码→验证→再验证"间无限循环，601 步仍未 final。
                # 判据：最近 12 步内出现过同一 (tool, arguments) 签名 ≥2 次。
                _sig = tool + "|" + json.dumps(arguments, sort_keys=True, ensure_ascii=False)
                # 只读工具的"重复"不是无进展：换会话后模型需要重新定位（pyshot 实测
                # list_directory 被拦 92 次、每次白花一个 81 秒的回合）。这里直接回放
                # 缓存结果，让模型拿到数据继续干活，同时**不进**无进展判定。
                _ro_name = str(tool) in tuple(
                    getattr(__import__("gantry.tools", fromlist=["x"]), "READ_ONLY_TOOLS", ()))
                # 与后面执行分支同一个 hash 算法（此处还不能用 _dispatch_hash，它后面才算）
                _sig_hash = hashlib.sha1(json.dumps(arguments, ensure_ascii=False,
                                                   sort_keys=True).encode("utf-8")).hexdigest()[:16]
                if _ro_name:
                    _ro_hit = (getattr(self, "_ro_results", None) or {}).get((str(tool), _sig_hash))
                    if _ro_hit is not None:
                        _cst = {"step": total + 1, "status": "ok", "tool": str(tool),
                                "arguments": arguments, "cached": True,
                                "reason": "同一只读动作重复调用：直接回放上次结果（未再执行）",
                                "result": _ro_hit}
                        history.append(_cst); all_steps.append(_cst); self.step_saver(_cst)
                        total += 1
                        self.state_saver(total_steps=total,
                                         recent_steps=history[-self.keep_recent:])
                        continue
                _recent = [_s for _s in history[-12:]
                           if _s.get("status") == "ok" and not _s.get("cached")]
                _seen = sum(1 for _s in _recent
                            if (_s.get("tool") or "") + "|" + json.dumps(
                                _s.get("arguments") or {}, sort_keys=True, ensure_ascii=False) == _sig)
                if _seen >= 3 and not _ro_name:
                    step = {"step": total + 1, "status": "no_progress",
                            "error": f"检测到无进展循环：动作 {tool} 已重复 {_seen + 1} 次仍无进展，任务停止",
                            "tool": tool, "arguments": arguments}
                    history.append(step); all_steps.append(step); self.step_saver(step)
                    self.state_saver(state="failed", total_steps=total + 1,
                                     recent_steps=history[-self.keep_recent:],
                                     last_error=step["error"])
                    return AgentResult(
                        f"任务因无进展循环被停止：{tool} 重复 {_seen + 1} 次。"
                        f"建议改用更强的模型或把任务拆小。", all_steps)
                if _seen == 2 and not _ro_name:
                    # 第一次只警告（给模型换策略的机会），并把提示塞进上下文
                    warn = {"step": total + 1, "status": "no_progress_warning",
                            "error": f"动作 {tool} 已重复 {_seen} 次且无进展：请立刻改变策略，"
                                     f"不要重复同一动作；若已无法推进就返回 final 说明卡点"}
                    history.append(warn); all_steps.append(warn); self.step_saver(warn)
                    total += 1
                    self.state_saver(total_steps=total, recent_steps=history[-self.keep_recent:],
                                     last_error=warn["error"])
                    continue
                duplicate = False
                if tool in NON_IDEMPOTENT_TOOLS and history:
                    # 只看 history[-1] 会被"中间夹一个其它成功动作"击穿：模型重放
                    # 更早的 append/move 时会真实执行第二遍。改为回看最近若干步。
                    key = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
                    for prev in reversed(history[-10:]):
                        if (prev.get("status") == "ok" and prev.get("tool") == tool and
                                json.dumps(prev.get("arguments") or {}, sort_keys=True,
                                           ensure_ascii=False) == key):
                            duplicate = True
                            break
                if duplicate:
                    step.update({"status": "ok", "result": {"skipped_duplicate": True,
                                 "detail": "与上一成功动作完全相同，幂等保护已跳过重复执行"}})
                else:
                    try:
                        _dispatch_hash = hashlib.sha1(json.dumps(arguments, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:16]

                        _approval_hash = hashlib.sha1((str(tool) + '|' + json.dumps(arguments, ensure_ascii=False, sort_keys=True)).encode('utf-8')).hexdigest()[:16]
                        # ── 重复动作护栏：同一个工具 + 同样参数**连续/累计**做了多次，
                        # 说明这条路走不通（典型：改"制表符→4空格"用 replace_text 默认
                        # count=1，改一处、读一次、还有、再改……真实任务里这样空转了几
                        # 十步、换会话 6 次）。到阈值就不再执行，直接告诉它换工具。
                        _rep_key = (str(tool), _dispatch_hash)
                        _rep_n = (self._repeat_counts.get(_rep_key, 0) + 1
                                  if hasattr(self, "_repeat_counts") else 1)
                        if not hasattr(self, "_repeat_counts"):
                            self._repeat_counts = {}
                        self._repeat_counts[_rep_key] = _rep_n
                        _REPEAT_LIMIT = int(self.config.get("repeat_guard_limit", 3) or 3)
                        # 只读工具重复**不该被拦**：它幂等、模型通常是在会话切换后重新
                        # 定位（pyshot 实测：list_directory 被拦 92 次，每次都白花一个
                        # Copilot 回合，平均 81 秒/回合）。缓存还在就直接回放上次结果。
                        _ro_cache = getattr(self, "_ro_results", None) or {}
                        _ro_tools = tuple(getattr(__import__("gantry.tools", fromlist=["x"]),
                                                  "READ_ONLY_TOOLS", ()))
                        if _rep_n > _REPEAT_LIMIT and str(tool) in _ro_tools:
                            _cached = _ro_cache.get(_rep_key)
                            if _cached is not None:
                                _cst = {"step": total + 1, "status": "ok", "tool": str(tool),
                                        "reason": "同一只读动作重复调用：直接回放第 %d 次的缓存结果"
                                                  "（未再执行，内容一模一样）" % (_rep_n - 1),
                                        "result": _cached, "cached": True}
                                history.append(_cst); all_steps.append(_cst); self.step_saver(_cst)
                                total += 1
                                self.state_saver(total_steps=total,
                                                 recent_steps=history[-self.keep_recent:])
                                continue
                        if _rep_n > _REPEAT_LIMIT and str(tool) not in ("validate_python", "run_unittest"):
                            _hint = ("同一动作（工具与参数完全相同）已做过 %d 次仍无进展，就不要再重复它。"
                                     % (_rep_n - 1))
                            if str(tool) in ("replace_text", "replace_lines", "convert_indentation"):
                                _hint += ("改缩进/空白：缩进一次性转换用 convert_indentation"
                                          "（tabs2spaces, width=4）；按行改某几行用 replace_lines；"
                                          "要全部替换把 replace_text 的 count 传 0。"
                                          "先 read_text(line_start=…, reveal_whitespace=true) 看清真实空白。")
                            else:
                                _hint += "换一个工具或换一种思路（先 read_text 看清现状再动手）。"
                            _st = {"step": total + 1, "status": "repeat_guard", "tool": str(tool),
                                   "reason": "重复动作被拦下（没有推进）", "error": _hint}
                            history.append(_st); all_steps.append(_st); self.step_saver(_st)
                            self.state_saver(total_steps=total + 1,
                                             recent_steps=history[-self.keep_recent:],
                                             last_error=_hint)
                            continue
                        # ── 一次性审批门（fail-closed）：破坏性工具必须有本步专属的授权，授权用后即焚；
                        # 没有回调就视为不放行（绝不自动批准）——对应 DSH 的 allowed-once 语义。
                        if self.tools.requires_approval(str(tool)):
                            _granted = False
                            if self.approval_check:
                                try:
                                    _granted = bool(self.approval_check(str(tool), _approval_hash))
                                except Exception:
                                    _granted = False
                            if not _granted:
                                _msg = ('工具 ' + str(tool) + ' 属于破坏性操作，需要用户一次性授权后才会执行；'
                                        '请让用户在本会话授权该操作，然后继续任务。')
                                _st = {'step': total + 1, 'status': 'approval_required', 'tool': str(tool),
                                       'reason': '破坏性工具未获一次性授权', 'error': _msg}
                                history.append(_st); all_steps.append(_st); self.step_saver(_st)
                                self.state_saver(recent_steps=history[-self.keep_recent:], last_error=_msg,
                                                 state='waiting',
                                                 approval_pending={'tool': str(tool), 'args_hash': _approval_hash})
                                return AgentResult(_msg, all_steps)
                        if self.step_begin:
                            try:
                                self.step_begin(total + 1, str(tool), _dispatch_hash)
                            except Exception:
                                pass

                        _tspan = span("tool." + str(tool), "execute_tool",
                                      **{"gen_ai.tool.name": str(tool),
                                         "gen_ai.tool.call.id": f"{getattr(self, '_session_id', '') or 's'}-{total + 1}",
                                         "gantry.session_id": str(getattr(self, "_session_id", "") or ""),
                                         "gantry.step": total + 1,
                                         "gantry.args_chars": len(json.dumps(arguments, ensure_ascii=False))})
                        try:
                            with _tspan:
                                result = await self.tools.execute(tool, arguments)
                                try:
                                    _tspan.set(**{"gantry.result_chars": len(json.dumps(result, ensure_ascii=False))})
                                except Exception:
                                    pass
                        except Exception as _exc:
                            _tspan.fail(_exc)
                            if self.step_finish:
                                try:
                                    self.step_finish(total + 1, "error")
                                except Exception:
                                    pass
                            raise
                        else:
                            if self.step_finish:
                                try:
                                    self.step_finish(total + 1, "done")
                                except Exception:
                                    pass
                        _spill = self._spill_result(str(tool), total + 1, result)
                        if _spill:
                            if isinstance(result, dict):
                                result = dict(result)
                                result["spill"] = _spill
                            step["spill"] = _spill
                        step.update({"status": "ok", "result": result})
                        # 只读结果留一份缓存，供"重复调用"时回放（见上面的 repeat 分流）
                        try:
                            if str(tool) in tuple(getattr(__import__("gantry.tools", fromlist=["x"]),
                                                         "READ_ONLY_TOOLS", ())):
                                if not hasattr(self, "_ro_results"):
                                    self._ro_results = {}
                                self._ro_results[_rep_key] = result
                        except Exception:
                            pass
                        # 计划模式：提交方案就结束本轮（模型不一定听话地返回 final，
                        # 实测它提交完会继续动手执行——那等于计划模式没起作用）。
                        if tool == "exit_plan_mode" and isinstance(result, dict) and result.get("ok"):
                            # 先把这一步记进时间线：早退会跳过循环末尾的常规落库，
                            # 否则"探索→提交方案"的过程在界面上完全看不到。
                            history.append(step); all_steps.append(step); self.step_saver(step)
                            plan_text = str(result.get("plan") or "").strip()
                            final_step = {"step": total + 2, "status": "plan_submitted",
                                          "reason": "计划已提交，等待用户确认后执行"}
                            history.append(final_step); all_steps.append(final_step)
                            self.step_saver(final_step)
                            return AgentResult(plan_text or "计划已提交", all_steps)
                        if tool in _action_tools:
                            self._action_done = True
                        event = result.get("file_event") if isinstance(result, dict) else None
                    except Exception as exc:
                        # 报错要说"下一步怎么办"：审计发现 116 条工具报错里 102 条
                        # 只描述了"不合法/不允许"，模型只能靠猜重试（常见于换会话后）。
                        _err = str(exc)
                        if not any(k in _err for k in ("下一步", "请", "改用", "试试", "建议")):
                            _err += (f"\n下一步：核对 {tool} 的参数（见工具描述）与路径是否存在；"
                                     "需要看现状就先用 read_text/list_directory，"
                                     "换个思路也可以（同类工具可互换）。")
                        step.update({"status": "error", "error": _err})
                        if tool in _action_tools:
                            self._action_tried = True
                if step.get("status") == "ok":
                    # "有进展"= 真的动了文件，或读到了之前没读过的路径
                    _p = str(arguments.get("path") or arguments.get("destination") or "")
                    _mutating = str(tool) in _action_tools
                    if _mutating or (event is not None):
                        self._turn_dry = False
                    elif _p and _p not in self._seen_paths:
                        self._seen_paths.add(_p)
                        self._turn_dry = False
                if step.get("status") == "ok" and not duplicate:
                    changed_path = str(arguments.get("path") or arguments.get("destination") or "")
                    # 注意必须含 b64 写入族：提示词要求代码/JSON/HTML 一律用
                    # write_text_b64 / append_text_b64 写，漏掉它们等于给验证
                    # 闭环开了个后门（写完代码直接 final 也不会被拦）。
                    if tool in {"write_text", "append_text", "write_text_b64", "append_text_b64",
                                "replace_text", "copy_path",
                                "move_path", "copilot_file"} and changed_path.lower().endswith((".py", ".json", ".html")):
                        validation_required = True
                        syntax_validated = False
                        tests_passed = False
                    # 自升级（改动服务自身代码）：同样进验证闭环，且必须用
                    # self_validate / self_unittest 证明，不能只跑工作区的验证。
                    if tool in {"self_write_text_b64", "self_append_text_b64", "self_replace_text"}:
                        validation_required = True
                        syntax_validated = False
                        tests_passed = False
                    # 自我指令更新（AGENT.md / skills/*.md）：允许，但必须在时间线里
                    # 显式标记 —— 这类改动会改变后续所有任务的行为，且原像已进快照，
                    # 可用"回滚任务改动"撤销。
                    if changed_path and self._is_instruction_path(changed_path):
                        step["instruction_update"] = True
                        step["reason"] = (str(step.get("reason") or "")
                                          + "（自我指令更新：AGENT.md/skills，可用回滚撤销）").strip()
                    if tool == "validate_python" and isinstance(step.get("result"), dict):
                        syntax_validated = bool(step["result"].get("passed"))
                    if tool == "run_unittest" and isinstance(step.get("result"), dict):
                        tests_passed = bool(step["result"].get("passed"))
                    if tool == "self_validate" and isinstance(step.get("result"), dict):
                        syntax_validated = bool(step["result"].get("passed"))
                    if tool == "self_unittest" and isinstance(step.get("result"), dict):
                        tests_passed = bool(step["result"].get("passed"))
                    if syntax_validated and tests_passed:
                        gate_strikes = 0   # 真正通过验证后重置，允许后续文件再改动
                    _save_gate()   # 旗一变就落库：暂停/重启后仍记得"改了还没验证"
                if step.get("status") == "ok":
                    batch_ok += 1
                else:
                    batch_errors += 1
                # 批量汇总写在最后一个动作上（在保存之前）：让模型下一回合能
                # 看到"这批整体执行结果"。注意不要再调一次 step_saver —— 那会
                # 在时间线里多存一条重复记录。
                if batch_size > 1 and idx == len(todo) - 1:
                    step["batch_result"] = {"size": batch_size, "ok": batch_ok,
                                           "error": batch_errors}
                history.append(step); all_steps.append(step); self.step_saver(step)
                if event: self.file_event_saver(event)
                # 回合成本（prompt+回答）只记一次：同一回合内的多个动作不重复计入，
                # 否则批量执行会把 token 计数放大 N 倍、提前触发压缩/换会话。
                add_t = 0 if usage_counted else used_tokens
                add_c = 0 if usage_counted else used_chars
                usage_counted = True
                total += 1; conv_steps += 1
                tokens += add_t; chars += add_c; conv_t += add_t; conv_c += add_c
                self.state_saver(total_steps=total, conversation_steps=conv_steps,
                                 estimated_tokens=tokens, estimated_chars=chars,
                                 conv_tokens=conv_t, conv_chars=conv_c,
                                 recent_steps=history[-self.keep_recent:])
                if total >= self.max_total_steps:
                    break
            continue

        self._record_conv_stat(state, int(state.get("conversation_steps", 0)),
                               int(state.get("conv_tokens", 0)), int(state.get("conv_chars", 0)),
                               "failed: 达到最大总步骤数", model)
        self.state_saver(state="failed", last_error="达到最大总步骤数")
        # 提示里要带"下一步怎么办"：这条消息会被父会话/用户看到，只说"已停止"没有出路。
        return AgentResult(
            "达到最大总步骤数（%d 步），任务已停止。\n"
            "接着做的方法（择一）：\n"
            "1) 继续本会话：在「继续对话」里说一句「接着做」，已完成的改动都在，不用重来；\n"
            "2) 如果是子代理：用 subagent_send 传本会话 id 继续（上下文与文件都保留），"
            "或把 max_steps 调大重跑；\n"
            "3) 想放宽默认值：config.json 的 agent.subagent_max_steps（当前上限 %d，界面主会话"
            "由 agent.max_total_steps 控制）。" % (self.max_total_steps, 200),
            all_steps)
