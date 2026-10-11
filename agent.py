

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from tools import ToolRegistry

ChatFunction = Callable[[str, str, bool], Awaitable[str]]


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


class AgentPaused(Exception):
    """回合等待期间用户请求暂停，由桥接层抛出，用于立即中断并停放。"""


class CopilotServiceUnavailable(RuntimeError):
    """Copilot 服务暂时不可用（限流/繁忙/无响应），由桥接层识别抛出。

    与回答格式错误（invalid_action）严格区分：不记步、不记解析失败、
    不触发新建会话，由 run() 指数退避后在原会话重试。
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

class AgentRunner:
 # removed compatibility method: def __new__(cls, tools, chat, config, *args, **kwargs):
  # removed compatibility allocation: super().__new__(cls)
  # compatibility assignment removed
 
    # 历史版本遗留的旧签名 __init__（引用不存在的 config）已移除，
    # 该类唯一的构造函数是下方接收 config/state_getter 的版本。

    def _system_prompt(self) -> str:
        return """你是本地任务 Agent，可以通过工具处理当前工作目录中的文件。
每次只返回一个 JSON 对象。不得使用 Markdown 代码块，不得在 JSON 前后添加解释。
格式只有两种：
{"type":"tool","tool":"工具名","arguments":{},"reason":"原因"}
{"type":"final","content":"最终答复"}
规则：先检查再修改；路径相对于当前工作目录；工具失败后调整方案；完成后必须立即返回 final。
修改现有文本优先使用 replace_text，不得使用 run_python 修改项目文件。
创建或追加代码、JSON、HTML、Markdown 时必须使用 write_text_b64 或 append_text_b64，禁止把源码直接放入普通 JSON content。每次解码后内容不超过 8000 字符。文件写完后用 read_text 验证。
单个 JSON 动作必须尽量短，禁止一次返回两个 JSON 对象。
治理或代码修改任务必须形成验证闭环：写入后执行 validate_python，再执行 run_unittest；验证失败必须继续修复，不得返回 final。
只有 validate_python 和 run_unittest 均返回 passed=true 后，才可宣称治理完成或把 TODO 标记为完成。
可用工具：
""" + json.dumps(self.tools.descriptions(), ensure_ascii=False)

    @staticmethod
    def _objects(text: str):
        text = html.unescape((text or '').strip()).replace('\ufeff', '')
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I | re.S).strip()
        variants = [text]
        # Copilot may return a JSON string that contains an escaped JSON object.
        try:
            decoded = json.loads(text)
            if isinstance(decoded, str):
                variants.append(decoded)
            elif isinstance(decoded, dict):
                yield decoded
        except Exception:
            pass
        decoder = json.JSONDecoder()
        for value in variants:
            for start, ch in enumerate(value):
                if ch != '{':
                    continue
                try:
                    obj, _ = decoder.raw_decode(value[start:])
                    if isinstance(obj, dict):
                        yield obj
                except Exception:
                    continue

    @classmethod
    def _parse(cls, text: str) -> dict[str, Any]:
        candidates = list(cls._objects(text))
        # Copilot may print the same action twice. Deduplicate identical actions,
        # but never silently choose between distinct actions in one response.
        unique = []
        seen = set()
        for obj in candidates:
            if str(obj.get('type', '')).lower() not in {'tool', 'final'}:
                continue
            key = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if key not in seen:
                seen.add(key); unique.append(obj)
        if len(unique) > 1:
            raise ValueError('Copilot 一次返回了多个不同动作，已拒绝执行')
        candidates = unique
        # 外层动作损坏时，_objects 可能从字符串碎片里解析出嵌套的
        # {"type":"final",...}（典型场景：写入的测试代码里嵌了一段 JSON）。
        # 文本带有工具动作标记但外层 JSON 无法整体解析时，该候选是嵌套碎片，
        # 不得胜出——清空候选并跳过 final 兜底，交给下方的写/替换容错恢复。
        outer_broken_tool = False
        _probe = html.unescape(text or '').strip()
        _first = _probe.find('{')
        _outer_ok = False
        if _first >= 0:
            try:
                _outer, _ = json.JSONDecoder().raw_decode(_probe[_first:])
                _outer_ok = isinstance(_outer, dict)
            except Exception:
                _outer_ok = False
        if not _outer_ok and re.search(r'["\']tool["\']\s*:\s*["\'][A-Za-z_]', _probe, re.I):
            outer_broken_tool = True
            candidates = []
        for wanted in ('final', 'tool'):
            for obj in reversed(candidates):
                kind = str(obj.get('type', '')).strip().lower()
                if kind == wanted:
                    obj['type'] = kind
                    if kind == 'final':
                        content = obj.get('content', obj.get('message', obj.get('answer', '')))
                        if isinstance(content, (dict, list)):
                            content = json.dumps(content, ensure_ascii=False)
                        obj['content'] = str(content or '').strip()
                    else:
                        obj['tool'] = str(obj.get('tool', '')).strip()
                        if not isinstance(obj.get('arguments'), dict):
                            obj['arguments'] = {}
                    return obj
        # Last-resort final tolerance for a visibly complete final payload.
        # 外层工具动作损坏时不做此兜底：命中的多半是嵌套在字符串里的碎片。
        if not outer_broken_tool:
            m = re.search(r'["\']type["\']\s*:\s*["\']final["\']', html.unescape(text or ''), re.I)
            if m:
                m2 = re.search(r'["\']content["\']\s*:\s*["\'](.*)["\']\s*\}?\s*$', html.unescape(text or ''), re.I | re.S)
                if m2:
                    content = m2.group(1).replace('\\n', '\n').replace('\\"', '"')
                    return {'type': 'final', 'content': content}
        # Copilot frequently emits write_text content containing raw, unescaped
        # double quotes. The outer action is then invalid JSON even though the
        # tool, path and content boundaries are still unambiguous. Recover this
        # specific write action instead of entering a useless repair loop.
        raw = html.unescape(text or '').strip()
        tool_match = re.search(
            r'["\'](?:tool|type)["\']\s*:\s*["\'](write_text|append_text)["\']',
            raw, re.I,
        )
        path_match = re.search(
            r'["\']path["\']\s*:\s*["\']([^"\']+)["\']',
            raw, re.I,
        )
        if tool_match and path_match:
            content_key = re.search(r'["\']content["\']\s*:\s*["\']', raw, re.I)
            if content_key:
                content_start = content_key.end()
                # reason is outside arguments and is the safest right boundary.
                # 允许 content 后还有 count 等标量字段；取最后一个匹配，
                # 防止内容里恰好出现类似结构时被提前截断。
                _bounds = list(re.finditer(
                    r'["\'](?:\s*,\s*["\'][A-Za-z_][A-Za-z0-9_]*["\']\s*:\s*(?:["\'][^"\'\\]*["\']|-?\d+(?:\.\d+)?|true|false|null))*\s*}\s*,\s*["\']reason["\']\s*:',
                    raw[content_start:], re.I | re.S,
                ))
                boundary = _bounds[-1] if _bounds else None
                if boundary:
                    content = raw[content_start:content_start + boundary.start()]
                    content = (content
                        .replace('\\r\\n', '\n')
                        .replace('\\n', '\n')
                        .replace('\\t', '\t')
                        .replace('\\"', '"')
                        .replace('\\\\', '\\'))
                    return {
                        'type': 'tool',
                        'tool': tool_match.group(1).lower(),
                        'arguments': {
                            'path': path_match.group(1),
                            'content': content,
                        },
                        'reason': '已容错恢复 Copilot 返回的未转义写文件动作',
                        '_recovered_malformed_json': True,
                    }
        # Recover malformed replace_text actions. Copilot Chat sometimes
        # removes JSON escaping from quotes inside old/new code fragments.
        # The action structure is still deterministic, so parse by field markers.
        if re.search(r'["\']tool["\']\s*:\s*["\']replace_text["\']', raw, re.I):
            def field_start(name: str):
                return re.search(r'["\']' + re.escape(name) + r'["\']\s*:\s*["\']', raw, re.I)

            path_key = field_start('path')
            old_key = field_start('old')
            new_key = field_start('new')
            reason_key = field_start('reason')
            if path_key and old_key and new_key:
                path_end = raw.find('",', path_key.end())
                if path_end < 0:
                    path_end = raw.find("',", path_key.end())
                old_boundary = re.search(r'["\']\s*,\s*["\']new["\']\s*:\s*["\']', raw[old_key.end():], re.I)
                _nb = list(re.finditer(r'["\'](?:\s*,\s*["\'][A-Za-z_][A-Za-z0-9_]*["\']\s*:\s*(?:["\'][^"\'\\]*["\']|-?\d+(?:\.\d+)?|true|false|null))*\s*}\s*,\s*["\']reason["\']\s*:', raw[new_key.end():], re.I | re.S))
                new_boundary = _nb[-1] if _nb else None
                if path_end >= 0 and old_boundary and new_boundary:
                    def decode_fragment(value: str) -> str:
                        return (value
                            .replace('\\r\\n', '\n')
                            .replace('\\n', '\n')
                            .replace('\\t', '\t')
                            .replace('\\"', '"')
                            .replace('\\\\', '\\'))

                    old_value = raw[old_key.end():old_key.end() + old_boundary.start()]
                    new_value = raw[new_key.end():new_key.end() + new_boundary.start()]
                    reason = '已容错恢复 Copilot 返回的未转义 replace_text 动作'
                    if reason_key:
                        reason_end = raw.rfind('"')
                        if reason_end > reason_key.end():
                            reason = decode_fragment(raw[reason_key.end():reason_end])
                    return {
                        'type': 'tool',
                        'tool': 'replace_text',
                        'arguments': {
                            'path': decode_fragment(raw[path_key.end():path_end]),
                            'old': decode_fragment(old_value),
                            'new': decode_fragment(new_value),
                        },
                        'reason': reason,
                        '_recovered_malformed_json': True,
                    }

        raise ValueError('Copilot 未返回完整、可解析的 Agent JSON 动作')


    @staticmethod
    def estimate_tokens(text: str) -> int:
        text = text or ""
        chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        ascii_text = ''.join(c if ord(c) < 128 else ' ' for c in text)
        words = len(re.findall(r"[A-Za-z0-9_]+", ascii_text))
        punctuation = len(re.findall(r"[^\w\s]", ascii_text))
        code_weight = len(re.findall(r"[{}\[\]():;=<>/\\]", text)) // 2
        base64_weight = sum(len(x) // 3 for x in re.findall(r"[A-Za-z0-9+/]{80,}={0,2}", text))
        return max(1, chinese + words + punctuation // 2 + code_weight + base64_weight)

    @staticmethod
    def compact_value(value: Any, limit: int = 3000) -> Any:
        if isinstance(value, str):
            return value if len(value) <= limit else value[:limit] + "...[truncated]"
        if isinstance(value, list):
            return [AgentRunner.compact_value(x, limit) for x in value]
        if isinstance(value, dict):
            return {k: AgentRunner.compact_value(v, limit) for k, v in value.items()
                    if k not in {"raw_tail", "content_b64"}}
        return value

    def __init__(self, tools: ToolRegistry, chat: ChatFunction, config: dict[str, Any],
                 state_getter, state_saver, step_saver, file_event_saver, conv_stat_saver=None):
        self.tools, self.chat, self.config = tools, chat, config
        self.state_getter, self.state_saver = state_getter, state_saver
        self.step_saver, self.file_event_saver = step_saver, file_event_saver
        self.conv_stat_saver = conv_stat_saver
        self.max_total_steps = min(int(config.get("max_total_steps", 1000)), 10000)
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

    def _deterministic_summary(self, task: str, history: list[dict[str, Any]], state: dict) -> dict[str, Any]:
        completed, errors, modified, findings = [], [], dict(state.get("modified_files") or {}), []
        for item in history:
            tool, status = item.get("tool"), item.get("status")
            args = item.get("arguments") or {}
            if status == "ok" and tool:
                completed.append(f"{tool}: {args.get('path') or args.get('source') or ''}".strip())
            if status == "error": errors.append(str(item.get("error", ""))[:500])
            if tool in {"write_text", "append_text", "replace_text", "copy_path", "move_path", "delete_path"}:
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
        while self.estimate_tokens(json.dumps(summary, ensure_ascii=False)) > self.max_summary_tokens and len(summary["completed"]) > 10:
            summary["completed"] = summary["completed"][len(summary["completed"]) // 3:]
        return summary

    def _continuation_prompt(self, task: str, summary: dict, recent: list[dict], state: dict) -> str:
        payload = {
            "original_goal": task,
            "long_term_summary": summary,
            "recent_steps": [self.compact_value(x, 2500) for x in recent[-self.keep_recent:]],
            "current_workspace": str(self.tools.root),
            "total_steps": state.get("total_steps", 0),
            "conversation_index": state.get("conversation_index", 0),
        }
        return (self._system_prompt() + "\n\n这是长期任务的继续执行，不要重新开始。\n"
                + json.dumps(payload, ensure_ascii=False)
                + "\n从待完成事项继续，不要重复已完成的读取。只返回一个 Agent JSON 动作。")

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
        state = self.state_getter()
        history = list(state.get("recent_steps") or [])
        all_steps: list[dict[str, Any]] = []
        parse_failures = 0
        service_failures = 0
        validation_required = False
        syntax_validated = False
        tests_passed = False
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

            # 压缩后使用最新计数重新判断，防止旧阈值连续创建新会话。
            automatic_pressure = (tokens >= self.rollover_at or
                                  chars >= self.max_context_chars or
                                  conv_steps >= self.per_conversation)
            automatic_rollover = (self.auto_rollover and automatic_pressure and
                                  conv_steps >= self.min_steps_before_rollover)
            need_rollover = bool(force_rollover or manual_rollover or automatic_rollover)
            carrying_pending = False

            if need_rollover:
                # 记录上一个 Copilot 会话的实测数据（主动切换或触顶被动切换）
                self._record_conv_stat(state, conv_steps, tokens, chars, rollover_reason, model)
                if rollovers >= self.max_rollovers:
                    self.state_saver(state="failed", last_error="超过最大 Copilot 会话切换次数")
                    return AgentResult("超过最大 Copilot 会话切换次数，任务已停止。", all_steps)
                if not summary:
                    summary = self._deterministic_summary(task, history, state)
                prompt = self._continuation_prompt(task, summary, history, state)
                force_new = True
                # 若用户在回合间隙请求了暂停，保留 paused，循环顶部会立即停放。
                park_state = "paused" if (self.state_getter() or {}).get("state") == "paused" else "running"
                self.state_saver(conversation_index=rollovers + 1, conversation_steps=0,
                                 estimated_tokens=0, estimated_chars=0,
                                 rollover_requested=False, compact_requested=False,
                                 summary=summary, state=park_state, last_error=None,
                                 pending=[])  # pending 已随 continuation 摘要带入新会话
                tokens = 0
                chars = 0
                conv_steps = 0
                force_rollover = False
                event = {"step": total + 1, "status": "session_rollover", "reason": rollover_reason}
                rollover_reason = "Token/步数/手动触发自动创建新 Copilot 会话"
                all_steps.append(event); self.step_saver(event)
            elif total == 0:
                prompt = self._system_prompt() + "\n\n用户任务：\n" + task
                force_new = not chat_active
            else:
                pending_now = [str(x) for x in (state.get("pending") or []) if str(x).strip()]
                if pending_now:
                    # 用户追加的指令直接在当前会话下达（不新建会话），保留完整上下文；
                    # 送达成功后才清除 pending，发送失败/对话已满时保留并随换会话摘要携带。
                    prompt = ("用户在原任务基础上追加新指令，请优先执行最新指令：\n"
                              + "\n".join(pending_now))
                    force_new = not chat_active
                    carrying_pending = True
                else:
                    last = self.compact_value(history[-1], 6000) if history else summary
                    prompt = "继续同一任务。上一动作实际结果：\n" + json.dumps(last, ensure_ascii=False)
                    force_new = False

            prompt += f"\n\n当前总步骤 {total + 1}/{self.max_total_steps}。只返回下一个 JSON 动作。"
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
            try:
                raw = await self.chat(prompt, model, force_new)
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
            except (CopilotServiceUnavailable, TimeoutError) as exc:
                # 服务限流/不可用/长时间无进度：不记步、不记解析失败、不换会话，
                # 指数退避后在原 Copilot 会话重试；退避期间可暂停、可取消。
                chat_active = True  # 回合大概率已发出，重试留在原会话
                service_failures += 1
                parse_failures = 0
                reason = str(exc)[:300]
                step = {"step": total + 1, "status": "service_unavailable",
                        "error": f"Copilot 服务暂时不可用（第 {service_failures} 次）：{reason}"}
                all_steps.append(step); self.step_saver(step)
                self.state_saver(recent_steps=history[-self.keep_recent:], last_error=step["error"])
                if service_failures >= self.max_service_unavailable:
                    self.state_saver(state="waiting",
                                     last_error="Copilot 服务持续不可用，已停止自动重试；服务恢复后请点击继续")
                    return AgentResult("Copilot 服务持续不可用，任务已安全暂停，可稍后继续。", all_steps)
                delay = min(self.service_retry_base_seconds * (2 ** min(service_failures - 1, 4)),
                            self.service_retry_max_seconds)
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    state = self.state_getter()
                    if state.get("cancel_requested"):
                        self.state_saver(state="cancelled")
                        return AgentResult("任务已取消。", all_steps)
                    if state.get("state") == "paused":
                        break  # 回到循环顶部停放
                    await asyncio.sleep(1)
                continue
            if carrying_pending:
                # 追加指令已成功送达 Copilot 会话，清除待送达列表避免重复下达。
                self.state_saver(pending=[])
            used_tokens = self.estimate_tokens(prompt) + self.estimate_tokens(raw)
            used_chars = len(prompt) + len(raw or "")
            try:
                action = self._parse(raw)
                parse_failures = 0
                service_failures = 0
            except Exception as exc:
                parse_failures += 1
                step = {"step": total + 1, "status": "invalid_action", "error": str(exc), "raw_tail": (raw or "")[-2000:]}
                history.append(step); all_steps.append(step); self.step_saver(step)
                self.state_saver(total_steps=total + 1, conversation_steps=conv_steps + 1,
                                 estimated_tokens=tokens + used_tokens, estimated_chars=chars + used_chars,
                                 recent_steps=history[-self.keep_recent:], last_error=str(exc))
                if parse_failures >= self.max_parse_failures:
                    # 连续解析失败是回答格式问题，不是上下文耗尽：压缩后继续用当前会话。
                    # 不新建 Copilot 会话，也不覆盖用户可能已请求的 paused 状态。
                    self.state_saver(compact_requested=True, rollover_requested=False,
                                     last_error=str(exc))
                    force_rollover = False
                    parse_failures = 0
                continue

            if action["type"] == "final":
                if validation_required and not (syntax_validated and tests_passed):
                    step = {"step": total + 1, "status": "validation_gate",
                            "error": "代码已修改但尚未通过语法检查和单元测试，拒绝提前完成"}
                    history.append(step); all_steps.append(step); self.step_saver(step)
                    self.state_saver(total_steps=total + 1, conversation_steps=conv_steps + 1,
                                     recent_steps=history[-self.keep_recent:], last_error=step["error"])
                    continue
                self._record_conv_stat(state, conv_steps + 1, tokens + used_tokens,
                                       chars + used_chars, "completed", model)
                self.state_saver(state="completed", total_steps=total + 1, conversation_steps=conv_steps + 1,
                                 estimated_tokens=tokens + used_tokens, estimated_chars=chars + used_chars)
                return AgentResult(action.get("content") or "任务已完成。", all_steps)

            tool, arguments = action.get("tool", ""), action.get("arguments") or {}
            step = {"step": total + 1, "tool": tool, "arguments": arguments, "reason": action.get("reason", "")}
            event = None
            # 幂等保护：换会话接力时摘要若没让模型明确感知"上一动作已完成"，
            # 或回合重发，同一动作可能连续到达。对非幂等工具（append 会重复
            # 追加、move/copy 会重复搬运），与上一成功动作完全相同时跳过执行。
            duplicate = False
            if tool in NON_IDEMPOTENT_TOOLS and history:
                last = history[-1]
                duplicate = (last.get("status") == "ok" and last.get("tool") == tool and
                             json.dumps(last.get("arguments") or {}, sort_keys=True, ensure_ascii=False)
                             == json.dumps(arguments, sort_keys=True, ensure_ascii=False))
            if duplicate:
                step.update({"status": "ok", "result": {"skipped_duplicate": True,
                             "detail": "与上一成功动作完全相同，幂等保护已跳过重复执行"}})
            else:
                try:
                    result = await self.tools.execute(tool, arguments)
                    step.update({"status": "ok", "result": result})
                    event = result.get("file_event") if isinstance(result, dict) else None
                except Exception as exc:
                    step.update({"status": "error", "error": str(exc)})
            if step.get("status") == "ok" and not duplicate:
                changed_path = str(arguments.get("path") or arguments.get("destination") or "")
                if tool in {"write_text", "append_text", "replace_text", "copy_path", "move_path"} and changed_path.lower().endswith((".py", ".json", ".html")):
                    validation_required = True
                    syntax_validated = False
                    tests_passed = False
                if tool == "validate_python" and isinstance(step.get("result"), dict):
                    syntax_validated = bool(step["result"].get("passed"))
                if tool == "run_unittest" and isinstance(step.get("result"), dict):
                    tests_passed = bool(step["result"].get("passed"))
            history.append(step); all_steps.append(step); self.step_saver(step)
            if event: self.file_event_saver(event)
            self.state_saver(total_steps=total + 1, conversation_steps=conv_steps + 1,
                             estimated_tokens=tokens + used_tokens, estimated_chars=chars + used_chars,
                             recent_steps=history[-self.keep_recent:])

        self._record_conv_stat(state, int(state.get("conversation_steps", 0)),
                               int(state.get("estimated_tokens", 0)), int(state.get("estimated_chars", 0)),
                               "failed: 达到最大总步骤数", model)
        self.state_saver(state="failed", last_error="达到最大总步骤数")
        return AgentResult("达到最大总步骤数，任务已停止。", all_steps)
