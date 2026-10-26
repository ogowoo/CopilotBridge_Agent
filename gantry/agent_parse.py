from __future__ import annotations

import base64
import html
import json
import re
from typing import Any

def _decode_fragment(value: str) -> str:
    """延迟从 core 导入，避免 agent.py 启动时的循环依赖。"""
    from .agent import _decode_fragment as _impl
    return _impl(value)


class ParseMixin:
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
        """兼容入口：SKILL.md 元数据解析已统一到 gantry.tools.parse_skill_md。"""
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


    @staticmethod
    def _repair_json_escapes(text: str) -> str:
        """修复模型常见的**非法 JSON 转义**，避免整条动作作废。

        真实时间线（`for debug/…-timeline-2026-10-19-10-35-24.json`）里
        58 次 invalid_action **全部**是这个原因：模型写 PowerShell 命令时沿用了
        shell/Python 习惯，把 `'x'` 写成 `\'x\'`——JSON 没有 `\'` 这个转义，
        整条 JSON 直接解析失败，于是白烧一个 Copilot 回合（那次任务因此耗了
        377 步、7 个会话）。

        只改"确定非法"的转义：反斜杠+单引号 → 单引号、反斜杠+反引号 → 反引号；
        `\\uXXXX` 等合法转义一律不动。注意 `\\'`（转义反斜杠后跟引号）是合法的，
        不能误改，所以按反斜杠奇偶判断。
        """
        if "\\'" not in text and "\\`" not in text:
            return text
        out, i, n = [], 0, len(text)
        while i < n:
            ch = text[i]
            if ch != "\\":
                out.append(ch); i += 1; continue
            # 数连续反斜杠：奇数个才是转义起点
            j = i
            while j < n and text[j] == "\\":
                j += 1
            run = j - i
            nxt = text[j] if j < n else ""
            if run % 2 == 1 and nxt in ("'", "`"):
                # 丢掉一个反斜杠：\' -> '
                out.append("\\" * (run - 1))
                out.append(nxt)
                i = j + 1
                continue
            out.append("\\" * run); i = j
        return "".join(out)

    @classmethod
    def _parse(cls, text: str) -> dict[str, Any]:
        # 先修非法转义再解析：模型常带 `\'` 这类 JSON 不认的转义进来
        text = cls._repair_json_escapes(text or "")
        candidates = list(cls._objects(text))
        # Copilot may print the same action twice. Deduplicate identical actions,
        # but never silently choose between distinct actions in one response.
        unique = []
        seen = set()
        for obj in candidates:
            _t = str(obj.get('type', '')).lower()
            if not _t and str(obj.get('tool') or '').strip():
                # 紧凑格式：模型有时只给 {"tool": "...", "arguments": {...}} 而不带 type。
                # 以前这里直接丢，导致整轮被判成"无法解析的动作"（实测连挂 13 步）。
                obj['type'] = 'tool'
                _t = 'tool'
            if _t not in {'tool', 'final', 'actions'}:
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

        raise ValueError('未返回完整、可解析的 Agent JSON 动作')

