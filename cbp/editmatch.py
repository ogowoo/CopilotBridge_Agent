# -*- coding: utf-8 -*-
"""编辑匹配内核：让"改文件"在空白差异下也能一次成功。

**为什么需要**（真实痛点）：`replace_text` 要求片段与文件**逐字相同**。模型凭记忆抄
片段时，缩进（tab/空格、层级深浅）、行尾空格、换行风格稍有出入就匹配失败；失败后模型
往往再读一遍、再猜一遍——真实会话里因此出现了几十上百个回合、token 直接爆掉。

**做法**（参考 Codex 的 apply_patch 与 DSH 的 str_replace_editor）：
1. **多级降级匹配**：exact → 行尾空白 → 行首缩进弹性 → 空白折叠。每级都只在
   唯一命中时采用，并在结果里回传 `strategy`，让模型知道这次是"模糊匹配成功"。
2. **缩进校正**：模糊命中后，把模型给的新片段按**文件实际缩进**重排（保持相对层级），
   避免把 tab 文件写成 4 空格、或把嵌套层级改错。
3. **失败诊断**：给出首个差异位置（行/列 + 两侧上下文），比"未找到"有用得多。
4. **补丁**：一次提交多个 hunk（Codex 风格 `*** Begin Patch` 或简单 edits 列表），
   **原子应用**（任一失败则整体不写），把 N 次调用压成 1 次。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# 匹配策略（由严到宽）
STRATEGY_EXACT = "exact"
STRATEGY_LINE_ENDINGS = "line-endings"      # CRLF/LF 归一
STRATEGY_TRAILING_WS = "trailing-whitespace"  # 行尾空白差异
STRATEGY_INDENT = "indentation"             # 行首缩进差异（含 tab/空格）
STRATEGY_WS = "whitespace"                  # 任意空白折叠等价

_WS_RE = re.compile(r"[ \t]+")


def _indent_of(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _strip_indent(lines: list[str]) -> list[str]:
    return [ln.lstrip(" \t") for ln in lines]


def _collapse(line: str) -> str:
    """把一行归一成"无缩进 + 内部空白折叠"的形式，用于最宽松的比较。"""
    return _WS_RE.sub(" ", line.strip())


def _detect_eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _to_eol(fragment: str, eol: str) -> str:
    return fragment.replace("\r\n", "\n").replace("\n", eol)


@dataclass
class Match:
    start: int          # 在原文中的起始偏移
    end: int            # 结束偏移（不含）
    matched: str        # 原文里实际被命中的片段
    strategy: str
    count: int          # 原文中该片段的出现次数（>1 时调用方决定是否拒绝）

    @property
    def unique(self) -> bool:
        return self.count == 1


def _find_all(text: str, frag: str) -> int:
    if not frag:
        return 0
    n, i = 0, 0
    while True:
        j = text.find(frag, i)
        if j < 0:
            return n
        n += 1
        i = j + 1


def find_span(text: str, old: str, *, allow_fuzzy: bool = True) -> Match | None:
    """在 `text` 中定位 `old`（多级降级）。找不到返回 None。

    只在**唯一命中**时返回模糊匹配；多处命中时返回 count 供调用方提示"片段太短"。
    """
    if not old:
        return None
    # 1) 完全一致
    n = _find_all(text, old)
    if n:
        i = text.find(old)
        return Match(i, i + len(old), old, STRATEGY_EXACT, n)
    if not allow_fuzzy:
        return None

    old_lines = old.replace("\r\n", "\n").split("\n")
    # 末尾空行（模型常多带一个换行）不影响定位
    while old_lines and old_lines[-1] == "":
        old_lines.pop()
    if not old_lines:
        return None
    file_lines = text.splitlines(keepends=True)
    if not file_lines:
        return None

    tiers: list[tuple[str, Any]] = [
        (STRATEGY_LINE_ENDINGS, lambda ln: ln.rstrip("\r\n")),
        (STRATEGY_TRAILING_WS, lambda ln: ln.rstrip(" \t\r\n")),
        (STRATEGY_INDENT, lambda ln: ln.strip(" \t\r\n")),
        (STRATEGY_WS, lambda ln: _collapse(ln)),
    ]
    wanted_raw = [ln.rstrip("\r\n") for ln in old_lines]
    for strategy, key in tiers:
        wanted = [key(ln) for ln in wanted_raw]
        if not any(wanted):
            continue
        hits = _scan(file_lines, wanted, key)
        if len(hits) == 1:
            lo, hi = hits[0], hits[0] + len(wanted)
            start = sum(len(x) for x in file_lines[:lo])
            matched = "".join(file_lines[lo:hi])
            return Match(start, start + len(matched), matched, strategy, 1)
        if len(hits) > 1:
            start = sum(len(x) for x in file_lines[:hits[0]])
            matched = "".join(file_lines[hits[0]:hits[0] + len(wanted)])
            return Match(start, start + len(matched), matched, strategy, len(hits))
    return None


def _scan(file_lines: list[str], wanted: list[str], key) -> list[int]:
    """返回所有匹配起始行号（连续行块，按 key 归一后逐行相等）。"""
    keys = [key(ln) for ln in file_lines]
    hits = []
    span = len(wanted)
    for i in range(len(keys) - span + 1):
        if keys[i:i + span] == wanted:
            hits.append(i)
    return hits


def reindent(fragment: str, matched: str, eol: str) -> str:
    """把 `fragment` 的缩进校正到 `matched`（文件实际缩进）的基准上。

    规则：以两边**第一行的行首缩进**为基准；片段里以旧缩进开头的行换成文件缩进，
    其余行保留相对层级。这样 tab 文件不会被写成空格、嵌套也不会整体错位。
    """
    frag_lines = fragment.replace("\r\n", "\n").split("\n")
    if not frag_lines:
        return fragment
    file_indent = _indent_of(matched.replace("\r\n", "\n").split("\n")[0])
    model_indent = _indent_of(frag_lines[0])
    out: list[str] = []
    for ln in frag_lines:
        if not ln.strip():
            out.append("")
            continue
        if model_indent and ln.startswith(model_indent):
            out.append(file_indent + ln[len(model_indent):])
        elif not model_indent:
            # 片段整体没有缩进（模型按顶格写）：补上文件缩进，保留内部相对层级
            out.append(file_indent + ln)
        else:
            out.append(ln)
    text = eol.join(out)
    # 被替换的块若带结尾换行，新块也必须带——否则 replace 会把下一行"粘"上来
    if matched.endswith(eol) and not text.endswith(eol):
        text += eol
    return text


def first_diff(text: str, old: str, width: int = 60) -> str:
    """给出首个差异位置与两侧上下文，便于模型一轮内改正。"""
    a = text.replace("\r\n", "\n").split("\n")
    b = (old or "").replace("\r\n", "\n").split("\n")
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            col = next((k for k in range(min(len(a[i]), len(b[i])))
                        if a[i][k] != b[i][k]), min(len(a[i]), len(b[i])))
            return (f"首个差异在第 {i + 1} 行第 {col + 1} 列：\n"
                    f"  文件: {a[i][:width]!r}\n  片段: {b[i][:width]!r}")
    if len(a) != len(b):
        longer = "文件" if len(a) > len(b) else "片段"
        return f"{longer}多出内容：文件 {len(a)} 行 / 片段 {len(b)} 行"
    return "两者内容一致（差异仅可能来自行尾或不可见字符）"


# ────────────────────────── 补丁 ──────────────────────────

@dataclass
class Hunk:
    old_lines: list[str]     # 含上下文行的原文块（' ' 与 '-' 行）
    new_lines: list[str]     # 替换后的块（' ' 与 '+' 行）


def parse_patch(patch: str) -> list[Hunk]:
    """解析 Codex 风格补丁：

        *** Begin Patch
        *** Update File: a.py
        @@ 可选锚点
         context
        -old
        +new
        *** End Patch

    也接受不含 Begin/End 的裸 hunk 序列。多个 `*** Update File:` 目前只取最后一个文件
    （单文件工具），跨文件请分次调用。
    """
    hunks: list[Hunk] = []
    old_buf: list[str] = []
    new_buf: list[str] = []

    def flush() -> None:
        if old_buf or new_buf:
            hunks.append(Hunk(list(old_buf), list(new_buf)))
            old_buf.clear()
            new_buf.clear()

    for raw in (patch or "").replace("\r\n", "\n").split("\n"):
        if raw.startswith("*** ") or raw.startswith("@@"):
            flush()
            continue
        if not raw:
            # 补丁里的空行按"文件中的空行"处理（上下文行）
            old_buf.append("")
            new_buf.append("")
            continue
        tag, body = raw[0], raw[1:]
        if tag == " ":
            old_buf.append(body)
            new_buf.append(body)
        elif tag == "-":
            old_buf.append(body)
        elif tag == "+":
            new_buf.append(body)
        else:                      # 没写前缀：宽容地当作上下文
            old_buf.append(raw)
            new_buf.append(raw)
    flush()
    return [h for h in hunks if h.old_lines or h.new_lines]


def parse_edits(edits: Any) -> list[tuple[str, str, int]]:
    """解析简单编辑列表：[{old, new, count?}] → [(old, new, count)]。"""
    out: list[tuple[str, str, int]] = []
    if not isinstance(edits, list):
        return out
    for item in edits:
        if not isinstance(item, dict):
            continue
        old = str(item.get("old") or "")
        new = str(item.get("new") or "")
        if not old:
            continue
        try:
            count = int(item.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        out.append((old, new, count))
    return out
