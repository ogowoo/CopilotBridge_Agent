

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

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
        return """你是本地任务 Agent，可以通过工具处理当前工作目录中的文件。
每次返回一个 JSON 动作；若下一步是几个**互不依赖**的小动作（多处替换、写多个短文件、读几个小文件），用下方"批量"格式一次返回。不得使用 Markdown 代码块，不得在 JSON 前后添加解释。
格式只有两种：
{"type":"tool","tool":"工具名","arguments":{},"reason":"原因"}
{"type":"final","content":"最终答复"}
规则：先检查再修改；路径相对于当前工作目录；工具失败后调整方案；完成后必须立即返回 final。
文件分流：只需分析（总结/找问题/提取信息）的大文件（约 20000 字符以上）优先用 copilot_attach 丢给 Copilot 分析，一个回合搞定，不要逐段翻页；
需要精确修改的文件必须本地工具操作；大文件只需看局部时用 read_text 的 offset/limit 读目标段。
修改现有文本优先使用 replace_text，不得使用 run_python 修改项目文件。
需要分段读本地文件时：read_text 的 limit 参数每段可到 20000 字符（别分太碎浪费回合），按返回的 total_chars 和 next_offset 翻页，直到读完目标范围再下结论。
创建或追加代码、JSON、HTML、Markdown 时必须使用 write_text_b64 或 append_text_b64，禁止把源码直接放入普通 JSON content。每次解码后内容不超过 3000 字符；大文件必须分多次 append_text_b64 追加，宁可多回合也不要让单个 JSON 动作过长（过长会被截断导致整个动作作废）。如果上一回合因过长被截断，下一回合只输出未写入的剩余分块。文件写完后用 read_text 验证。
写大文件的标准流程：write_text_b64 写第一块 → append_text_b64 逐块追加直到全部内容写完（每一块都不要重复之前已写的内容）→ read_text 抽查 → 最后才执行 validate_python 和 run_unittest。
文件没有写完之前禁止执行 validate_python 或 run_unittest：只写了一半的文件必然语法检查失败，这不是需要修复的缺陷。若验证失败且原因是文件不完整，正确做法是继续 append_text_b64 补全剩余内容，而不是重写整个文件或对残缺文件反复打补丁。
单个 JSON 动作必须尽量短；互不依赖的小动作按下方批量规则合并，不要一次只做一个、也不要为了凑批量把连续的大分块塞进同一个动作。
少量互相独立的小动作可以合并成一次批量，省掉来回回合（每个分块一个回合很浪费）：格式为 {"type":"actions","actions":[动作1,动作2,...]}，数组里只能是工具动作（不能含 final），最多 4 个，每个动作仍满足上面的分块上限。适合批量的是：多处小替换、多个短文件写入、读几个小文件后统一决策。**大内容的连续分块不要塞进一个批量**（输出越长越容易被截断，一批失败反而更贵），这类仍按"每回合一块"的节奏追加。
治理或代码修改任务必须形成验证闭环：文件全部写完后执行 validate_python，再执行 run_unittest；真实验证失败必须继续修复，不得返回 final（文件未写完时的验证不通过不算失败，按上面的分块流程继续追加即可）。
只有 validate_python 和 run_unittest 均返回 passed=true 后，才可宣称治理完成或把 TODO 标记为完成。
你可以按需更新工作区的 AGENT.md 与 skills/*/SKILL.md：把本次任务中验证有效的项目约定、命令、踩坑结论沉淀进去（保持精炼，AGENT.md 控制在 8000 字符内），让后续任务直接受益。禁止删除既有的安全约束与验证闭环规则；这类改动会被标记为"自我指令更新"并可通过回滚撤销，改动前先 read_text 看清原文再替换，不要整篇重写。
记忆沉淀（任务收尾同样要做一次判断）：本次任务若产生了**跨任务仍有价值的事实**（用户偏好、环境特征、某接口的坑、已验证的结论），用 memory_write 记一条——写"自足"的陈述句（"X 环境的 Y 需要 Z"），不要写"本次任务做了…"这类只对当下有意义的流水账；琐碎过程不要记。pinned 只留给必须每回合遵守的极重要约定（其余交给检索）。
技能沉淀（任务收尾必做的一次判断）：完成任务前先 skill_list 看有没有相关技能；如果这次任务暴露了「可复用的方法/命令/坑」且没有对应技能，用 skill_create 新建一个（scope 默认 project 写入当前工作区 skills/，只有确实对所有工作区都通用、且已开启自升级时才用 scope=global）。技能内容写清楚：什么时候用、关键步骤、踩过的坑与验证方式，控制在 8000 字符内；不要为了记录而记录，琐碎或一次性的过程不要沉淀。
可用工具：
""" + json.dumps(self.tools.descriptions(), ensure_ascii=False) + self._self_upgrade_rules() + self._workspace_context()

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

    def _load_agent_md(self) -> str:
        """合并注入：全局 AGENT.md + 项目 AGENT.md（同文件去重，各自截断）。"""
        cap = int(self.config.get("agent_md_max_chars", 8000))
        parts: list[str] = []
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
                        if len(text) > cap:
                            text = text[:cap] + f"\n…[共 {len(text)} 字符，已截断到 {cap}]"
                        parts.append(f"【{label} AGENT.md】\n{text}")
                    break
                except Exception:
                    continue
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

    @staticmethod
    def compact_value(value: Any, limit: int = 3000) -> Any:
        if isinstance(value, str):
            return value if len(value) <= limit else value[:limit] + "...[truncated]"
        if isinstance(value, list):
            return [AgentRunner.compact_value(x, limit) for x in value]
        if isinstance(value, dict):
            return {k: AgentRunner.compact_value(v, limit) for k, v in value.items()
                    if k != "content_b64"}  # raw_tail 保留：模型需要看到自己被截断的输出
        return value

    def __init__(self, tools: ToolRegistry, chat: ChatFunction, config: dict[str, Any],
                 state_getter, state_saver, step_saver, file_event_saver, conv_stat_saver=None):
        self.tools, self.chat, self.config = tools, chat, config
        self.state_getter, self.state_saver = state_getter, state_saver
        self.step_saver, self.file_event_saver = step_saver, file_event_saver
        self.conv_stat_saver = conv_stat_saver
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

        def _save_gate() -> None:
            self.state_saver(gate_state=json.dumps({
                "validation_required": validation_required,
                "syntax_validated": syntax_validated,
                "tests_passed": tests_passed,
            }, ensure_ascii=False))
        # 工作区项目上下文：AGENT.md 全量注入 + skills 清单。每次 run 重新读取，
        # 恢复运行/换工作区后总能拿到最新版本。
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
                # 退避期间状态置 waiting（前端显示"等待服务"）；用户已暂停时不抢状态。
                _updates = {"recent_steps": history[-self.keep_recent:], "last_error": step["error"]}
                if (self.state_getter() or {}).get("state") == "running":
                    _updates["state"] = "waiting"
                # 限流窗口：首次超限记起点，之后重试都锚定同一终点（=起点 + 窗口），
                # 前端浮窗按它倒数到"Copilot 预计恢复可用"。
                limited_since = (self.state_getter() or {}).get("rate_limited_since") or time.time()
                resume_at = limited_since + self.service_retry_window_seconds
                _updates.update({"rate_limited_since": limited_since,
                                 "retry_resume_at": resume_at,
                                 "retry_attempt": service_failures})
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
                delay = min(self.service_retry_base_seconds * (2 ** min(service_failures - 1, 4)),
                            self.service_retry_max_seconds)
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
            if carrying_pending:
                # 追加指令已成功送达 Copilot 会话，清除待送达列表避免重复下达。
                self.state_saver(pending=[])
            used_tokens = self.estimate_tokens(prompt) + self.estimate_tokens(raw)
            used_chars = len(prompt) + len(raw or "")
            try:
                action = self._parse(raw)
                parse_failures = 0
                service_failures = 0
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
                if str(sub.get("type")) == "final":
                    if validation_required and not (syntax_validated and tests_passed):
                        gate_strikes += 1
                        step = {"step": total + 1, "status": "validation_gate",
                                "error": "代码已修改但尚未通过语法检查和单元测试，拒绝提前完成"
                                         f"（第 {gate_strikes} 次）"}
                        if gate_strikes >= 3:
                            # 实测：任务只要求 validate_python 时，模型会陷入
                            # 「final→被拒→再 final」的无限重试（每轮一次模型调用，
                            # 300 秒上百轮、零工具步骤、纯烧 token）。超限即停。
                            step["status"] = "gate_blocked"
                            step["error"] = ("验证门连续 3 次未通过，已停止任务以避免无限重试。"
                                             "缺少的通常是 run_unittest（或 validate_python）；"
                                             "请明确要求两者通过，或放宽该任务的验证要求。")
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
                        result = await self.tools.execute(tool, arguments)
                        step.update({"status": "ok", "result": result})
                        event = result.get("file_event") if isinstance(result, dict) else None
                    except Exception as exc:
                        step.update({"status": "error", "error": str(exc)})
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
