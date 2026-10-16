

from __future__ import annotations

import asyncio
import base64
import html
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .prompt import AGENT_PROTOCOL, PromptRegistry
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

class AgentRunner:
    # 一次回答里最多执行多少个动作（配置 max_actions_per_turn 可调，硬顶 12）。
    # 越长的批量输出越容易被 Copilot 输出上限截断，默认保守取 4。
    MAX_BATCH_ACTIONS = 4
 # removed compatibility method: def __new__(cls, tools, chat, config, *args, **kwargs):
  # removed compatibility allocation: super().__new__(cls)
  # compatibility assignment removed
 
    # 历史版本遗留的旧签名 __init__（引用不存在的 config）已移除，
    # 该类唯一的构造函数是下方接收 config/state_getter 的版本。

    def _system_prompt(self) -> str:
        # copilot_attach / copilot_file 只在浏览器（Copilot 网页）会话可用。
        # provider 会话里不能提这两个工具名，否则模型会照着提示词去调用
        # （实测：提供商会话里 copilot_file 连续失败 10 次纯烧回合）。
        if getattr(self.tools, "allow_web_copilot_tools", True):
            _routing = ("只需分析（总结/找问题/提取信息）的大文件（约 20000 字符以上）优先用 "
                        "copilot_attach 丢给 Copilot 分析，一个回合搞定，不要逐段翻页；\n"
                        "需要精确修改的文件必须本地工具操作；大文件只需看局部时用 read_text 的 "
                        "offset/limit 读目标段。")
        else:
            _routing = ("需要修改的文件直接本地工具操作；大文件只需看局部时用 read_text 的 "
                        "offset/limit 读目标段，分段读完再下结论。（本会话走 API 提供商，没有 "
                        "Copilot 网页可用，copilot_attach/copilot_file 不可用）")
        # 分段装配（P2-9）：协议 / 工具清单 / 自升级 / 工作区上下文。
        # 分隔符为空串且顺序与原拼接一致，因此输出**逐字不变**（有 golden 比对守着）。
        reg = PromptRegistry()
        reg.add("protocol", AGENT_PROTOCOL, order=-1000)
        reg.add("tools", json.dumps(self.tools.descriptions(only=self._active_tool_names()),
                                    ensure_ascii=False), order=100)
        reg.add("self_upgrade", self._self_upgrade_rules(), order=200)
        reg.add("workspace", self._workspace_context(), order=300)
        return reg.render(variables={"CBP_FILE_ROUTING": _routing})

    def _first_turn_copilot_hint(self) -> bool:
        """首轮是否提示"可直接让 Copilot 生成文件"。

        条件全部满足才提示：
        1. 浏览器（Copilot 网页）通道——provider 会话没有 copilot_file，
           提示了只会让模型去调用不存在的工具（实测连续失败 10 次）；
        2. copilot_file 确实在本次工具面里（allow_copilot_file / 工具裁剪都过了）。
        """
        if not getattr(self.tools, "allow_web_copilot_tools", True):
            return False
        try:
            names = self._active_tool_names()
        except Exception:
            names = None
        if names is not None and "copilot_file" not in names:
            return False
        return "copilot_file" in getattr(self.tools, "_tools", {})

    def _active_tool_names(self) -> set[str] | None:
        """按任务文本裁剪工具面（P0-1）；None 表示不裁剪。

        core 组始终包含；其它组按关键词命中展开；allow_self_upgrade 时带上 self 组。
        config 的 tool_visibility="all" 可整体关闭裁剪（排障用）。
        """
        if str(self.config.get("tool_visibility", "auto")).lower() == "all":
            return None
        from .tools import GROUP_KEYWORDS, TOOL_GROUPS
        task = (getattr(self, "_task_text", "") or "").lower()
        names: set[str] = set(TOOL_GROUPS.get("core", ()))
        # **未登记任何分组的工具一律视为 core**：否则新加的工具（例如 run_code）会被
        # 裁剪从提示词里静默滤掉——模型看不到就不可能调用（本轮真机验证抓到的缺陷）。
        # 这也与 tool_group() 的口径一致（未登记→core）。
        _grouped: set[str] = set()
        for _members in TOOL_GROUPS.values():
            _grouped.update(_members)
        try:
            for _item in self.tools.descriptions():
                _n = _item.get("name")
                if _n and _n not in _grouped:
                    names.add(_n)
        except Exception:
            pass
        for group, words in GROUP_KEYWORDS.items():
            if any(w in task for w in words):
                names.update(TOOL_GROUPS.get(group, ()))
        if getattr(self.tools, "allow_self_upgrade", False):
            names.update(TOOL_GROUPS.get("self", ()))
        return names

    def _self_upgrade_rules(self) -> str:
        """自升级（修改服务自身代码）的专用规则，仅在 allow_self_upgrade 时注入。"""
        if not getattr(self.tools, "allow_self_upgrade", False):
            return ""
        return """

【自升级模式已开启】你可以修改 CopilotBridge 服务自身的代码（cbp/、webui/、scripts/、tests/、app.py 等白名单路径），用 self_* 系列工具操作；config.json、data/、EdgeProfile/ 禁止触碰。硬性流程：
1. 先 self_read_text 看清现状，修改优先用 self_replace_text 精确替换；写文件用 self_write_text_b64/self_append_text_b64（Base64、每块≤3000字符、分块续写），一次只做一件相关的改动。
2. 全部写完后执行 self_validate（全安装目录语法检查）与 self_unittest（服务自身单测），两者 passed=true 前禁止返回 final。
3. 改坏了（验证不过且修不动）就用 self_rollback 回滚本次自升级，严禁在坏状态上继续叠加修改。
4. 自升级改动需要人工重启服务才生效，返回 final 时必须明确告知：改了什么、验证结果、需要重启、以及可用什么命令回滚。"""

    def _workspace_context(self) -> str:
        """项目上下文：全局 + 项目两级 AGENT.md 全量注入，skills 清单渐进式披露。

        - 全局：服务安装目录的 AGENT.md / skills（对所有工作区生效）
        - 项目：当前工作区的 AGENT.md / skills（同路径去重，同名技能覆盖全局）
        """
        parts: list[str] = []
        agent_md = getattr(self, "_agent_md", None)
        skills = getattr(self, "_skills", None)
        if agent_md is None or skills is None:   # 未经 run() 时按需加载
            agent_md, skills = self._context_values()
        agent_md = agent_md or ""
        if agent_md:
            parts.append("项目约定（全局 + 项目两级 AGENT.md，必须遵守；项目级优先）：\n" + agent_md)
        skills = skills or []
        if skills:
            lines = ["可用技能（判断相关时用 skill_read 读取全文再执行；[项目] 优先于 [全局]）："]
            for s in skills:
                scope = "项目" if s.get("scope") == "project" else "全局"
                lines.append(f"- [{scope}] {s['name']}：{s['description']}（skill_read \"{s['name']}\"）")
            parts.append("\n".join(lines))
        # 长期记忆：按当前任务文本检索最相关的若干条（关键词匹配，零依赖）。
        # 只注入少量：记忆是可检索知识，不是每回合都要读的硬约定。
        try:
            mem = getattr(self.tools, "memory", None)
            task_text = getattr(self, "_task_text", "") or ""
            if mem is not None and task_text:
                scopes = ["global", getattr(self.tools, "memory_scope", "global")]
                hits = mem.search(task_text, scopes, limit=5)
                if hits:
                    lines = ["相关长期记忆（来自以往任务，可能过时；与当前事实冲突时以当前为准，"
                             "过时或错误的用 memory_forget 删除）："]
                    for m in hits:
                        pin = "★" if m.get("pinned") else ""
                        lines.append(f"- {pin}[{m.get('kind')}#{m['id']}] {m['text']}")
                    lines.append("需要更多时用 memory_search 检索。")
                    parts.append("\n".join(lines))
        except Exception:
            pass
        return ("\n\n" + "\n\n".join(parts)) if parts else ""

    def _context_values(self) -> tuple[str, list[dict[str, Any]]]:
        """懒加载并缓存工作区上下文（run() 每轮会显式刷新）。"""
        if getattr(self, "_agent_md", None) is None:
            self._agent_md = self._load_agent_md()
        if getattr(self, "_skills", None) is None:
            self._skills = self._load_skill_catalog()
        return self._agent_md, self._skills

    def _md_roots(self) -> list[tuple[str, Path]]:
        """两级 AGENT.md 根目录：全局（服务安装目录）在前，项目（工作区）在后。"""
        roots: list[tuple[str, Path]] = []
        g = getattr(self.tools, "global_root", None)
        if g:
            roots.append(("全局", Path(g)))
        ws = getattr(self.tools, "root", None)
        if ws:
            roots.append(("项目", Path(ws)))
        return roots

    # 会被解析器当作"块边界"的闭合标签：正文里出现就可能越出注入区，统一转义。
    _ESCAPE_TAGS = ("</system-reminder>", "</instructions>", "</workspace_instructions>")

    @staticmethod
    def escape_instruction_text(text: str) -> str:
        """转义指令文本里的块闭合标签（DSH：注入正文里的 </system-reminder> 要转义）。"""
        for tag in AgentRunner._ESCAPE_TAGS:
            if tag in text:
                text = text.replace(tag, "<\\/" + tag[2:])
        return text

    def _load_agent_md(self) -> str:
        """合并注入：全局 AGENT.md + 项目 AGENT.md。

        预算策略参照 DSH 的 workspace instructions：**超预算先整体丢"宽泛"文件
        （全局），最后才截断"最具体"文件（项目）**，并渲染一条**可见的预算提示**，
        而不是对所有文件一视同仁地硬截断（那会把最具体的项目约定也砍掉）。
        """
        cap = int(self.config.get("agent_md_max_chars", 8000))
        total_cap = int(self.config.get("agent_md_total_max_chars", cap * 2))
        loaded: list[tuple[str, str]] = []          # (label, text) —— 顺序=宽泛→具体
        seen: set[str] = set()
        for label, root in self._md_roots():
            for name in ("AGENT.md", "agent.md"):
                p = root / name
                try:
                    if not p.is_file():
                        continue
                    rp = str(p.resolve()).lower()
                    if rp in seen:
                        break
                    seen.add(rp)
                    text = p.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        loaded.append((label, text))
                    break
                except Exception:
                    continue

        notes: list[str] = []
        # 超总预算：从头（最宽泛）开始整体丢弃，直到放进预算
        while len(loaded) > 1 and sum(len(t) for _l, t in loaded) > total_cap:
            label, text = loaded.pop(0)
            notes.append(f"[{label} AGENT.md 共 {len(text)} 字符，超出指令预算已整体省略]")
        parts: list[str] = []
        for label, text in loaded:
            if len(text) > cap:
                notes.append(f"[{label} AGENT.md 共 {len(text)} 字符，已截断到 {cap}]")
                text = text[:cap]
            parts.append(f"【{label} AGENT.md】\n{self.escape_instruction_text(text)}")
        if notes:
            parts.append("指令预算提示：" + "；".join(notes))
        return "\n\n".join(parts)

    def _load_skill_catalog(self) -> list[dict[str, Any]]:
        """技能清单：优先由工具层统一扫描（含全局+项目与去重），失败则本地兜底。"""
        try:
            listed = self.tools.skill_list()
            if listed:
                return listed
        except Exception:
            pass
        return []

    @staticmethod
    def _repair_prompt(last: dict, last_json: str) -> str:
        """按现场诊断生成修复提示词——不要一律让模型"分块写文件"。

        实测教训：上一回合其实是 final 答复被截断（我们抓到半截文本），
        却提示"请用 write_text_b64 拆分内容"，模型会去重写本来已经正确的
        文件，白干活还可能改坏。因此按 raw_tail 里露出的动作类型分别指引。
        """
        tail = str(last.get("raw_tail") or "")
        head = "继续同一任务。上一回合的回答被截断了，未能解析成可执行动作。"
        if re.search(r'["\']type["\']\s*:\s*["\']final["\']', tail, re.I):
            return (head + "从截断的片段看，你上一回合要返回的是 final（最终答复）"
                    "——如果任务其实已经完成，请**直接重新返回完整的 final JSON**，"
                    "不要重写文件、不要重复验证、不要拆分内容；"
                    "若确实还有没做完的事，再返回下一个工具动作。"
                    "注意整个 JSON 要一次写完、不要中途停顿。"
                    "上一回合记录（含被截断的输出尾部）：\n" + last_json)
        if re.search(r'["\']tool["\']\s*:\s*["\'](write_text|append_text|self_write|self_append)',
                     tail, re.I):
            return (head + "从截断的片段看，你上一回合在写文件："
                    "请把内容拆成更小的分块（write_text_b64/append_text_b64，"
                    "单次解码后不超过 3000 字符），只返回下一个 JSON 动作，"
                    "已完成的分块不要重复写。"
                    "上一回合记录（含被截断的输出尾部）：\n" + last_json)
        return (head + "请返回一个短小完整的 JSON 动作（内容长就分块；互不依赖的"
                "几个小动作可用批量格式一次返回），不要在 JSON 前后添加解释。"
                "上一回合记录（含被截断的输出尾部）：\n" + last_json)

    @staticmethod
    def _is_instruction_path(path: str) -> bool:
        """是否为"自我指令"文件：工作区根的 AGENT.md 或 skills/ 下的 Markdown。"""
        p = str(path or "").replace("\\", "/").lstrip("./").lower()
        if p in {"agent.md"}:
            return True
        return p.startswith("skills/") and p.endswith(".md")

    @staticmethod
    def _parse_skill_md(dirname: str, text: str) -> tuple[str, str]:
        """兼容入口：SKILL.md 元数据解析已统一到 cbp.tools.parse_skill_md。"""
        from .tools import parse_skill_md
        return parse_skill_md(dirname, text)

    @staticmethod
    def _objects(text: str):
        raw = (text or '').strip().replace('\ufeff', '')
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.I | re.S).strip()
        # 先按原文解析：合法 JSON 字符串值里的 &amp;/&quot; 不应被篡改。
        # 原文一个候选都解析不出时，才尝试 HTML 反转义变体（兼容 Copilot
        # 偶尔输出的转义文本）。整体解析成功时不再扫描字符串碎片，避免把
        # content 里的 JSON 文本误认为嵌套动作。
        decoder = json.JSONDecoder()

        def scan(value: str):
            found = False
            try:
                decoded = json.loads(value)
                if isinstance(decoded, dict):
                    yield decoded, True
                    return
                if isinstance(decoded, str) and decoded != value:
                    # Copilot may return a JSON string that contains an escaped JSON object.
                    for obj, _ in scan(decoded):
                        yield obj, True
                    return
            except Exception:
                pass
            for start, ch in enumerate(value):
                if ch != '{':
                    continue
                try:
                    obj, _ = decoder.raw_decode(value[start:])
                    if isinstance(obj, dict):
                        found = True
                        yield obj, False
                except Exception:
                    continue
            if found:
                return

        for obj, _whole in scan(raw):
            yield obj
        else:
            # for-else 不便判断生成器是否产出，改用显式收集重来
            pass

    @classmethod
    def _build_batch(cls, items: list[dict[str, Any]]) -> dict[str, Any]:
        """把多个动作规范成批量动作：只允许 tool，去重，按上限截断。

        - 批量里不允许 final：完成语义必须单独一个动作，避免"边写边宣布完成"
          绕过验证闭环；
        - 动作数上限 MAX_BATCH_ACTIONS（配置 max_actions_per_turn，硬顶 12），
          超出部分丢弃并标记，避免一次输出过长被 Copilot 截断；
        - 同一动作重复出现时去重（模型偶尔会重复粘贴）。
        """
        cleaned: list[dict[str, Any]] = []
        deferred_final: dict[str, Any] | None = None
        seen: set = set()
        for obj in items:
            kind = str(obj.get('type', '')).strip().lower()
            if kind == 'final':
                # 批内 final 不能静默丢弃：丢弃会让这一轮的验证门根本没被评估，
                # 批内若写了代码就等于绕过闭环。保留下来交给 run() 按"批内 final"
                # 规则处理（未通过验证时记 validation_gate 事件并拒绝完成）。
                if deferred_final is None:
                    deferred_final = {"type": "final", "content": obj.get("content") or "任务已完成。"}
                continue
            if kind != 'tool':
                continue
            obj['type'] = 'tool'
            obj['tool'] = str(obj.get('tool', '')).strip()
            if not isinstance(obj.get('arguments'), dict):
                obj['arguments'] = {}
            if not obj['tool']:
                continue
            key = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(obj)
        if not cleaned:
            raise ValueError('批量动作里没有可执行的工具动作'
                             + ('（其中包含 final，已拒绝——完成必须单独返回）' if deferred_final else ''))
        over = len(cleaned) > cls.MAX_BATCH_ACTIONS
        cleaned = cleaned[:cls.MAX_BATCH_ACTIONS]
        if deferred_final is not None:
            # final 放在工具动作之后：工具先执行，再由 run() 决定能否完成
            cleaned.append(deferred_final)
        batch = {"type": "actions", "actions": cleaned, "batch_size": len(cleaned)}
        if over:
            batch["_batch_truncated"] = True
        if deferred_final is not None:
            batch["_batch_has_final"] = True
        return batch

    @classmethod
    def _recover_partial_batch(cls, text: str) -> dict[str, Any] | None:
        """批量动作被 Copilot 输出上限截断时的部分恢复。

        截断发生在数组中间时，外层 JSON 不完整，但前面的动作对象是完整的：
        _objects 的碎片扫描已经能逐个解析出来。这里把它们当批量执行——
        比整批作废、下回合重发全部内容划算得多。
        """
        probe = html.unescape(text or '').strip()
        if not re.search(r'["\']type["\']\s*:\s*["\']actions["\']', probe, re.I):
            return None
        try:
            _first = probe.find('{')
            outer_ok = False
            if _first >= 0:
                try:
                    outer, _ = json.JSONDecoder().raw_decode(probe[_first:])
                    outer_ok = isinstance(outer, dict)
                except Exception:
                    outer_ok = False
            if outer_ok:
                return None      # 完整批量由正常路径处理
            found = []
            for obj in cls._objects_fallback(probe):
                if str(obj.get('type', '')).strip().lower() == 'tool' and obj.get('tool'):
                    found.append(obj)
            if len(found) < 2:      # 单个动作的截断由 _recover_truncated_write 处理
                return None
            batch = cls._build_batch(found)
            batch["_recovered_malformed_json"] = True
            batch["_recovered_truncated_batch"] = True
            return batch
        except Exception:
            return None

    @staticmethod
    def _objects_fallback(text: str):
        """原文解析失败后的 HTML 反转义变体扫描。"""
        unescaped = html.unescape((text or '').strip().replace('\ufeff', ''))
        unescaped = re.sub(r'^```(?:json)?\s*|\s*```$', '', unescaped, flags=re.I | re.S).strip()
        decoder = json.JSONDecoder()
        try:
            decoded = json.loads(unescaped)
            if isinstance(decoded, dict):
                yield decoded
                return
            if isinstance(decoded, str):
                unescaped = decoded
        except Exception:
            pass
        for start, ch in enumerate(unescaped):
            if ch != '{':
                continue
            try:
                obj, _ = decoder.raw_decode(unescaped[start:])
                if isinstance(obj, dict):
                    yield obj
            except Exception:
                continue

    @staticmethod
    def _recover_truncated_write(raw: str) -> dict[str, Any] | None:
        """Copilot 单条消息有输出上限：写入动作可能在 content 中间被截断。

        此时 JSON 永远不完整，常规解析与未转义容错都会失败，整个分块作废。
        但 tool/path 前缀是完整的，b64/文本内容也保留了截断前的全部有效
        部分——宽容解码后按部分写入（write→覆盖首个分块，append→保住已
        追加部分），让被截断的回合也能落盘，配合分块追加最终写全文件。
        """
        tool_match = re.search(
            r'["\'](?:tool|type)["\']\s*:\s*["\'](write_text|append_text|write_text_b64|append_text_b64)["\']',
            raw, re.I,
        )
        if not tool_match:
            return None
        path_match = re.search(r'["\']path["\']\s*:\s*["\']([^"\']+)["\']', raw, re.I)
        key_match = re.search(r'["\'](content_b64|content)["\']\s*:\s*["\']', raw, re.I)
        if not path_match or not key_match:
            return None
        value = raw[key_match.end():].strip()
        if len(value) < 8:
            return None
        tool = tool_match.group(1).lower()
        base_tool = tool.replace('_b64', '')
        arguments: dict[str, Any] = {'path': path_match.group(1)}
        if key_match.group(1).lower() == 'content_b64':
            # 只保留 b64 字母表字符，丢弃截断处的引号/空白残留；尾部 0~3 字节
            # 可能切断多字节 UTF-8 字符，逐字符回退直到能完整解码。
            candidate = re.sub(r'[^A-Za-z0-9+/=]', '', value)
            content = None
            for cut in range(0, 5):
                piece = candidate[:len(candidate) - cut] if cut else candidate
                if not piece:
                    break
                piece = piece.rstrip('=')
                pad = (-len(piece)) % 4
                try:
                    content = base64.b64decode(piece + '=' * pad, validate=False).decode('utf-8')
                    break
                except Exception:
                    continue
            if content is None or not content:
                return None
            arguments['content'] = content
        else:
            arguments['content'] = _decode_fragment(value)
        return {
            'type': 'tool',
            'tool': base_tool,
            'arguments': arguments,
            'reason': '已恢复被截断的写入动作（仅部分内容）。请用 append_text_b64 从断开处继续写剩余分块，每次不超过 3000 字符',
            '_recovered_malformed_json': True,
            '_truncated_partial': True,
        }

    @classmethod
    def _parse(cls, text: str) -> dict[str, Any]:
        candidates = list(cls._objects(text))
        # Copilot may print the same action twice. Deduplicate identical actions,
        # but never silently choose between distinct actions in one response.
        unique = []
        seen = set()
        for obj in candidates:
            if str(obj.get('type', '')).lower() not in {'tool', 'final', 'actions'}:
                continue
            key = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if key not in seen:
                seen.add(key); unique.append(obj)
        # 批量协议：显式 {"type":"actions","actions":[...]} 或模型直接给出的动作数组。
        # 多个动作不再拒绝——每个分块一个回合太浪费 Copilot 会话；改为按顺序执行，
        # 每步仍单独记账（时间线/审计/幂等保护不变）。
        batch: list[dict[str, Any]] = []
        if len(unique) == 1 and str(unique[0].get('type', '')).lower() == 'actions':
            raw_items = unique[0].get('actions')
            batch = [x for x in raw_items if isinstance(x, dict)] if isinstance(raw_items, list) else []
        elif len(unique) > 1:
            batch = list(unique)
        if batch:
            return cls._build_batch(batch)
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
                    content = _decode_fragment(m2.group(1))
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
                    content = _decode_fragment(content)
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
                _ob = list(re.finditer(r'["\']\s*,\s*["\']new["\']\s*:\s*["\']', raw[old_key.end():], re.I))
                old_boundary = _ob[-1] if _ob else None
                _nb = list(re.finditer(r'["\'](?:\s*,\s*["\'][A-Za-z_][A-Za-z0-9_]*["\']\s*:\s*(?:["\'][^"\'\\]*["\']|-?\d+(?:\.\d+)?|true|false|null))*\s*}\s*,\s*["\']reason["\']\s*:', raw[new_key.end():], re.I | re.S))
                new_boundary = _nb[-1] if _nb else None
                if path_end >= 0 and old_boundary and new_boundary:
                    def decode_fragment(value: str) -> str:
                        return _decode_fragment(value)

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

        # 截断的 final 也要救：Copilot 生成 JSON 中途停顿/被上限截断时，界面上
        # 答案可能是完整的，我们却抓到最后少了一个引号的半截文本。final 没有
        # 副作用（只是答复），把已写出的内容取出来比白白浪费一个修复回合好。
        m = re.search(r'["\']type["\']\s*:\s*["\']final["\']\s*,\s*["\']content["\']\s*:\s*["\'](.*)$',
                      raw, re.I | re.S)
        if m:
            content = _decode_fragment(m.group(1))
            content = re.sub(r'["\'}\s]+$', '', content).strip()
            if content:
                return {'type': 'final', 'content': content,
                        '_recovered_truncated_json': True}

        # 最后兜底：动作在 content 中间被 Copilot 输出上限截断。tool/path 前缀
        # 完整、内容保留截断前有效部分时，宽容解码后按部分写入，不让整个
        # 分块作废（否则文件永远停在最后一个完整分块）。
        recovered = cls._recover_partial_batch(raw)
        if recovered:
            return recovered

        recovered = cls._recover_truncated_write(raw)
        if recovered:
            return recovered

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

    def _spill_result(self, tool: str, step_no: int, result: Any) -> dict[str, Any] | None:
        """结果过大时落盘（对应 DSH 的 spill）：完整内容写到工作区 `.spill/` 下，
        上下文里只放"保头保尾"的摘要 + 路径提示，需要全文时用 read_text 读。

        放在**工作区内**是刻意的：沙箱不允许读工作区外的绝对路径，落盘在外部等于
        模型永远读不到；放 `.spill/` 也让用户能自己看、并可用普通的文件工具清理。
        """
        try:
            threshold = int(self.config.get("spill_threshold_chars", 20000))
            if threshold <= 0:
                return None
            text = json.dumps(result, ensure_ascii=False)
            if len(text) < threshold:
                return None
            max_bytes = int(self.config.get("spill_max_bytes", 2_000_000))
            root = Path(getattr(self.tools, "root", "."))
            sid = str(getattr(self, "_session_id", "") or "session").replace("/", "_")[:40]
            out_dir = root / ".spill" / sid
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"{step_no}-{tool}.json"
            data = text.encode("utf-8")
            if len(data) > max_bytes:
                data = data[:max_bytes]
            out.write_bytes(data)
            return {"spill_path": out.relative_to(root).as_posix(),
                    "spill_chars": len(text),
                    "hint": f"完整结果已落盘（{len(text)} 字符）：用 read_text 读 "
                            f"{out.relative_to(root).as_posix()}（可配合 offset/limit 分段）"}
        except Exception:
            return None          # 落盘失败绝不能影响任务

    @staticmethod
    def retain_text(text: str, limit: int) -> str:
        """超长文本**保头保尾**（DSH 的 head/tail 保留思路）。

        原来只留头部（`value[:limit] + "[truncated]"`），把**尾部**丢掉了——而工具
        结果的尾部往往正是错误信息与结论（例如 unittest 的失败列表、traceback 末尾）。
        这里保留头 60% / 尾 40%，中间替换为统一省略标记，并给出省略字符数。
        Python 的字符串切片按码点，不会切坏 UTF-8。
        """
        if limit <= 0 or len(text) <= limit:
            return text
        head = max(1, int(limit * 0.6))
        tail = max(1, limit - head)
        removed = len(text) - head - tail
        if removed <= 0:
            return text[:limit]
        return (text[:head]
                + f"\n\n[... 中间 {removed} 字符已省略（保留头尾；错误与结论通常在尾部）...]\n\n"
                + text[-tail:])

    @staticmethod
    def compact_value(value: Any, limit: int = 3000) -> Any:
        if isinstance(value, str):
            return AgentRunner.retain_text(value, limit)
        if isinstance(value, list):
            return [AgentRunner.compact_value(x, limit) for x in value]
        if isinstance(value, dict):
            return {k: AgentRunner.compact_value(v, limit) for k, v in value.items()
                    if k != "content_b64"}  # raw_tail 保留：模型需要看到自己被截断的输出
        return value

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
                + "\n从待完成事项继续，不要重复已完成的读取。返回下一个 JSON 动作（互不依赖的小动作可批量）。")

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
        noop_strikes = 0   # 允许连续拒绝的次数，超限放行，避免小模型无限重试烧 token
        # 实例级标记：换会话（rollover）发生在同一次 run() 内，而 history 会被
        # 截断；用实例标记记住"本次运行有没有真的产出/尝试过产出"。
        self._action_done = False
        self._action_tried = False
        _action_words = ("创建", "新建", "写", "修改", "改", "删除", "运行", "执行", "生成",
                         "保存", "记录", "记到", "整理", "转换",
                         "create", "write", "modify", "delete", "run", "save")
        _needs_action = any(w in (task or "") for w in _action_words)
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
        self._require_unittest = bool(self.config.get("require_unittest", True)) and not any(
            w in (task or "") for w in _relax_words)
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
                # 首回合也要带上"运行中插入的指令"：被强行插入中断时本回合不计步，
                # total 仍是 0，若这里不读 pending，插入的指令会被推迟一个回合。
                # （run_agent 会把"同一会话的新任务文本"也塞进 pending，那段已在
                # task 里了，用 `not in task` 过滤掉，避免重复下达。）
                pending_now = [str(x).strip() for x in (state.get("pending") or [])
                               if str(x).strip() and str(x).strip() not in task]
                if pending_now:
                    prompt += ("\n\n用户在原任务基础上追加新指令，请优先执行最新指令：\n"
                               + "\n".join(pending_now))
                    carrying_pending = True
                # 新话题首轮：Copilot 网页通道可以"直接生成文件"——copilot_file 让
                # Copilot 生成并提供下载入口，内容不经过聊天消息，因此不受单条输出
                # 上限约束（大文件一个回合拿到，不用 write_text_b64 分块写、也不会
                # 截断）。这里只在首轮、且确实有这个工具时给一次建议，不写进系统
                # 提示词（系统提示词要保持逐字稳定，prompt cache 依赖它）。
                if self._first_turn_copilot_hint():
                    prompt += ("\n\n首轮提示（新话题）：若任务要**产出新文件**（文档/代码/"
                               "数据/页面），优先一次调用 copilot_file(path, instruction) "
                               "让 Copilot 直接生成文件并下载到工作区——一个大文件一个"
                               "回合即可拿到，比 write_text_b64 分块写更快也不易截断；"
                               "只有需要精确修改**已有**文件、或内容很短需要逐块校验时，"
                               "才用 write_text_b64/append_text_b64/replace_text。")
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
                                 "cbp.session_id": str(getattr(self, "_session_id", "") or ""),
                                 "cbp.step": total,
                                 "cbp.prompt_chars": len(prompt or "")})
            try:
                with _turn_span:
                    raw = await self.chat(prompt, model, force_new)
                    _turn_span.set(**{"cbp.answer_chars": len(raw or "")})
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
                    if (self.state_getter() or {}).get("cancel_requested"):
                        self.state_saver(state="cancelled", last_error=None)
                        return AgentResult("任务已取消。", all_steps)
                    await asyncio.sleep(1)
                continue
            except (CopilotRateLimited, TimeoutError) as exc:
                # 服务限流/不可用/长时间无进度：不记步、不记解析失败、不换会话，
                # 指数退避后在原 Copilot 会话重试；退避期间可暂停、可取消。
                chat_active = True  # 回合大概率已发出，重试留在原会话
                service_failures += 1
                parse_failures = 0
                # 异常消息可能为空（实测 httpx 的 ReadTimeout/ConnectError 常是
                # 空字符串），那样步骤里就只剩"（第 N 次）："什么都看不到——退化成
                # 类名，保证用户至少知道是哪类错误。
                reason = str(exc).strip()[:300] or f"{type(exc).__name__}（无详细消息）"
                step = {"step": total + 1, "status": "service_unavailable",
                        "error": f"服务暂时不可用（第 {service_failures} 次）：{reason}"}
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
                step = {"step": total + 1, "status": "invalid_action", "error": str(exc), "raw_tail": (raw or "")[-2000:]}
                history.append(step); all_steps.append(step); self.step_saver(step)
                self.state_saver(total_steps=total + 1, conversation_steps=conv_steps + 1,
                                 estimated_tokens=tokens + used_tokens, estimated_chars=chars + used_chars,
                                 conv_tokens=conv_t + used_tokens, conv_chars=conv_c + used_chars,
                                 recent_steps=history[-self.keep_recent:], last_error=str(exc))
                if parse_failures >= self.max_parse_failures:
                    # 连续解析失败是回答格式问题，不是上下文耗尽：压缩后继续用当前会话。
                    # 不新建 Copilot 会话，也不覆盖用户可能已请求的 paused 状态。
                    self.state_saver(compact_requested=True, rollover_requested=False,
                                     last_error=str(exc))
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
                _recent = [_s for _s in history[-12:] if _s.get("status") == "ok"]
                _seen = sum(1 for _s in _recent
                            if (_s.get("tool") or "") + "|" + json.dumps(
                                _s.get("arguments") or {}, sort_keys=True, ensure_ascii=False) == _sig)
                if _seen >= 3:
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
                if _seen == 2:
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
                                         "cbp.session_id": str(getattr(self, "_session_id", "") or ""),
                                         "cbp.step": total + 1,
                                         "cbp.args_chars": len(json.dumps(arguments, ensure_ascii=False))})
                        try:
                            with _tspan:
                                result = await self.tools.execute(tool, arguments)
                                try:
                                    _tspan.set(**{"cbp.result_chars": len(json.dumps(result, ensure_ascii=False))})
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
                        if tool in _action_tools:
                            self._action_done = True
                        event = result.get("file_event") if isinstance(result, dict) else None
                    except Exception as exc:
                        step.update({"status": "error", "error": str(exc)})
                        if tool in _action_tools:
                            self._action_tried = True
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
        return AgentResult("达到最大总步骤数，任务已停止。", all_steps)
