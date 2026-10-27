"""tools_files.py — 文件读写与编辑工具（ToolRegistry 的 mixin）：读、写、追加、精确替换、行级替换、补丁、缩进整形、目录/复制/移动/删除。

从 tools.py 拆出（第四批解耦）：方法体原样搬入 mixin，运行时行为不变。
"""
from __future__ import annotations

import ast
import asyncio
import base64
import difflib
import hashlib
import inspect
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import editmatch

from .tools import ToolError  # noqa: F401


class FilesMixin:
    async def list_directory(self, path: str = ".", since_mtime: float | None = None) -> dict[str, Any]:
        target = self._path(path)
        if not target.is_dir():
            raise ToolError(f"目录不存在: {path}")
        # "自上次列出以来的变化"：模型反复列目录多半是在确认有没有变。
        # 记住每个目录上次列出的时间（进程内），下一次列同一目录时顺带告诉它哪些文件是新/改的。
        key = str(target.resolve())
        cache = getattr(self, "_dir_seen", None)
        if cache is None:
            cache = self._dir_seen = {}
        prev = cache.get(key)
        items, changed = [], []
        for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                stat = item.stat()  # 悬空符号链接会抛错，跳过而不是拖垮整列
                entry = {"name": item.name, "type": "directory" if item.is_dir() else "file",
                         "size": stat.st_size, "mtime": int(stat.st_mtime)}
                items.append(entry)
                base = since_mtime if since_mtime is not None else prev
                if base is not None and stat.st_mtime > float(base):
                    changed.append(item.name)
            except OSError:
                items.append({"name": item.name, "type": "unknown", "size": 0})
        import time as _t
        cache[key] = _t.time()
        out = {"path": path, "items": items[:1000], "truncated": len(items) > 1000}
        if changed:
            out["changed_since_last_list"] = sorted(changed)[:200]
            out["note"] = ("自上次列出以来有 %d 个条目发生变化（见 changed_since_last_list）——"
                           "只需要看这些，不必再逐层重列。" % len(changed))
        elif prev is not None and since_mtime is None:
            out["note"] = "自上次列出以来没有变化（内容与上次一致，可直接用之前的结果）。"
        return out

    async def find_files(self, pattern: str) -> dict[str, Any]:
        if not pattern:
            raise ToolError("pattern 不能为空")
        matches = []
        for item in self.root.glob(pattern):
            try:
                rel = item.resolve().relative_to(self.root)
            except ValueError:
                continue
            matches.append(str(rel))
            if len(matches) >= 1000:
                break
        return {"pattern": pattern, "matches": matches, "truncated": len(matches) >= 1000}

    async def read_text(self, path: str, offset: int = 0, limit: int = 0,
                        line_start: int = 0, line_count: int = 0,
                        reveal_whitespace: bool = False) -> dict[str, Any]:
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        self._mark_read(path)
        offset = max(0, self._int_arg(offset, 0, "offset"))
        limit = max(0, self._int_arg(limit, 0, "limit"))
        line_start = max(0, self._int_arg(line_start, 0, "line_start"))
        line_count = max(0, self._int_arg(line_count, 0, "line_count"))
        # 按行读（line_start 1 起）：真实任务里模型要"看第 514 行/改缩进"，
        # 而 offset 是**字符**偏移，表达不了"第几行"，于是它去用
        # PowerShell 的 (Get-Content x)[514] —— 那一步就踩到编码坑（见
        # for debug 里那份时间线：Set-Content 把整文件写成乱码，花 10 步修）。
        # 这里给出行号与精确行区间，按行定位不再需要 shell。
        if line_start:
            try:
                with target.open("r", encoding="utf-8", newline="") as f:
                    raw = f.read()
            except UnicodeDecodeError as exc:
                raise ToolError(f"文件不是有效的 UTF-8 文本: {exc}")
            lines = raw.replace("\r\n", "\n").split("\n")
            total_lines = len(lines) - (1 if lines and lines[-1] == "" else 0)
            if line_start > total_lines:
                return {"path": path, "lines": "", "line_start": line_start,
                        "line_count": 0, "total_lines": total_lines, "truncated": False}
            want = line_count or (total_lines - line_start + 1)
            want = max(1, min(want, 2000, self.max_read // 40 or 1))
            lo = line_start - 1
            hi = min(total_lines, lo + want)
            def _vis(t: str) -> str:
                # 空白可视化：空格→·、制表符→→。模型以前只能用
                # PowerShell 的 .Replace(' ','·') 才看得见缩进差异（还会踩编码坑），
                # 直接给它这个开关更省回合。
                return t.replace("\t", "→").replace(" ", "·")
            body = "\n".join(
                ((f"{i + 1}: " + _vis(lines[i])) if reveal_whitespace
                 else f"{i + 1}: {lines[i]}") for i in range(lo, hi))
            _res = {"path": path, "lines": body, "line_start": line_start,
                    "line_count": hi - lo, "total_lines": total_lines,
                    "truncated": hi < total_lines, "numbered": True}
            if reveal_whitespace:
                _res["whitespace"] = "空格=·  制表符=→"
            return _res
        if offset or limit:
            # 分段读取：整文件统计 total_chars 后按字符切片。供 Agent 翻页
            # 读大文件——单次读取的上下文预算约 6000 字符，超过就读不到尾部，
            # 必须靠 offset 定位。limit 封顶 max_read，防止一次灌爆上下文。
            try:
                with target.open("r", encoding="utf-8", newline="") as f:
                    full = f.read()
            except UnicodeDecodeError as exc:
                raise ToolError(f"文件不是有效的 UTF-8 文本: {exc}")
            total = len(full)
            if offset >= total:
                return {"path": path, "content": "", "offset": offset, "returned_chars": 0,
                        "total_chars": total, "truncated": False}
            cap = min(limit, self.max_read) if limit else self.max_read
            end = min(total, offset + cap)
            return {"path": path, "content": full[offset:end], "offset": offset,
                    "returned_chars": end - offset, "total_chars": total,
                    "truncated": end < total,
                    "next_offset": end if end < total else None}
        # 流式读 max_read+1 个字符即停：GB 级文件不会全量进内存。
        # newline="" 不做换行转换，读到什么是什么。
        with target.open("r", encoding="utf-8", errors="replace", newline="") as f:
            text = f.read(self.max_read + 1)
        truncated = len(text) > self.max_read
        if truncated:
            text = text[:self.max_read]
        result = {"path": path, "content": text, "truncated": truncated}
        if truncated:
            result["hint"] = "文件超过单次读取上限，已截断：请用 offset 参数分段读取全文"
        return result

    def _python_symbol_table(self, text: str) -> list[dict[str, Any]]:
        """列出 Python 顶层/嵌套函数与类（含限定名与行区间）。"""
        tree = ast.parse(text)
        out: list[dict[str, Any]] = []

        def walk(node: ast.AST, prefix: str = "") -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = f"{prefix}{child.name}"
                    # 装饰器在 def/class 行之前：整段替换要连同装饰器一起替换，
                    # 否则会留下孤立的 @decorator。
                    starts = [int(getattr(child, "lineno", 0))]
                    starts += [int(getattr(d, "lineno", 0))
                               for d in getattr(child, "decorator_list", []) if getattr(d, "lineno", 0)]
                    out.append({"name": name,
                                "kind": type(child).__name__,
                                "start": min(starts),
                                "end": int(getattr(child, "end_lineno", 0) or 0)})
                    walk(child, name + ".")

        walk(tree)
        return out

    async def python_symbols(self, path: str) -> dict[str, Any]:
        """按 AST 列出 Python 符号与行号，便于按函数/类精确修改。"""
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        if target.suffix.lower() != ".py":
            raise ToolError("python_symbols 只支持 .py 文件")
        try:
            text = target.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ToolError(f"文件不是有效的 UTF-8 文本: {exc}") from exc
        self._mark_read(path)
        try:
            symbols = self._python_symbol_table(text)
        except SyntaxError as exc:
            raise ToolError(f"Python 语法错误，无法解析符号: {exc}") from exc
        return {"path": path, "symbols": symbols, "count": len(symbols)}

    async def replace_python_symbol(self, path: str, name: str,
                                    content: str) -> dict[str, Any]:
        """用 AST 定位函数/类，整段替换（比按文本匹配更稳）。"""
        if not str(name or "").strip():
            raise ToolError("name 不能为空")
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        self._require_read(path)
        try:
            text = target.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ToolError(f"文件不是有效的 UTF-8 文本: {exc}") from exc
        try:
            symbols = self._python_symbol_table(text)
        except SyntaxError as exc:
            raise ToolError(f"Python 语法错误，无法定位符号: {exc}") from exc
        hit = next((s for s in symbols if s["name"] == str(name).strip()), None)
        if not hit:
            names = ", ".join(s["name"] for s in symbols[:20]) or "（无）"
            raise ToolError(f"未找到 Python 符号: {name}。可用符号: {names}")
        start, end = int(hit["start"]), int(hit["end"])
        if start < 1 or end < start:
            raise ToolError(f"符号 {name} 的行号范围无效: {start}-{end}")
        return await self.replace_lines(path, start, end - start + 1, content)

    @staticmethod
    def _hash(path: Path) -> str | None:
        if not path.is_file(): return None
        h=hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda:f.read(1048576),b""): h.update(chunk)
        return h.hexdigest()

    def _read_key(self, path: str) -> str:
        try:
            return str(self._path(path)).casefold()
        except Exception:
            return str(path).casefold()

    def _mark_read(self, path: str) -> None:
        """记录本会话读过的文件：编辑现有文件前必须有这个记录。"""
        sid = str(getattr(self, "current_session_id", "") or "")
        cache = getattr(self, "_read_files", None)
        if cache is None:
            cache = self._read_files = {}
        cache.setdefault(sid, set()).add(self._read_key(path))

    def _require_read(self, path: str) -> None:
        sid = str(getattr(self, "current_session_id", "") or "")
        # 直接调用 ToolRegistry 的脚本/单测没有会话上下文：不做读后编辑约束。
        # 真实 Agent 回合会在 agentrun 里设置 current_session_id，才启用该守卫。
        if not sid:
            return
        seen = (getattr(self, "_read_files", None) or {}).get(sid) or set()
        if self._read_key(path) not in seen:
            raise ToolError(
                f"编辑前必须先读取文件: {path}。请先 read_text（可用 line_start/line_count 只看目标段），"
                "再用 apply_patch / replace_text / replace_lines 做局部修改；不要凭记忆整篇重写。")

    @staticmethod
    def _diff_payload(path: str, before: str, after: str) -> dict[str, Any]:
        """返回结构化 diff：让模型/用户看到改了哪几行，而不是“写了多少字符”。"""
        before_lines = str(before or "").splitlines()
        after_lines = str(after or "").splitlines()
        diff_lines = list(difflib.unified_diff(
            before_lines, after_lines,
            fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))
        changed = sum(1 for line in diff_lines
                      if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
        hunks = sum(1 for line in diff_lines if line.startswith("@@"))
        text = "\n".join(diff_lines)
        if len(text) > 12000:
            text = text[:12000] + "\n...（diff 过长，已截断）"
        return {"changed": before != after, "changed_lines": changed,
                "hunks": hunks, "diff": text}

    def _check_plain_code_size(self, path: str, content: str) -> None:
        """代码类文件不允许"一大段明文塞进单个 JSON 动作"。

        为什么：模型把长代码直接放进 action JSON 时，引号/反斜杠转义和输出截断
        会把内容搞坏（实测）。所以代码类超过 `plain_code_limit`（默认 6000） 时
        要求改用 write_text_b64 / append_text_b64 分块。
        **文档类（md/txt/log 等）不受此限**：写长报告本来就该明文分块，不该逼
        模型去产 Base64——历史上正是这个压力把它推去用 PowerShell heredoc，
        然后踩了编码坑（见 for debug 里的时间线）。
        """
        suffix = Path(path).suffix.lower()
        if suffix in self.code_exts and len(content) > self.plain_code_limit:
            raise ToolError(
                f"{path} 是代码类文件，单次明文写入上限 {self.plain_code_limit} 字符"
                f"（本次 {len(content)}）。请改用 write_text_b64 / append_text_b64："
                "Base64 分块每块不超过 3000 字符，写第一块用 write_text_b64、后续用 "
                "append_text_b64 追加。文档类（.md/.txt/.log）才允许明文分块直接写。")

    async def write_text(self, path: str, content: str, via_b64: bool = False,
                         overwrite: bool = False) -> dict[str, Any]:
        if len(content) > self.max_write:
            raise ToolError("写入内容超过限制")
        # via_b64=True 时不限明文长度：Base64 分块就是代码类大文件的正路
        if not via_b64:
            self._check_plain_code_size(path, content)
        target = self._path(path)
        existed = target.is_file() and target.stat().st_size > 0
        if existed and not overwrite:
            raise ToolError(
                f"文件已存在: {path}。修改现有文件请用 apply_patch / replace_text / replace_lines "
                "做局部修改；确实要整体重写时才传 overwrite=true（不要为了省事整篇覆盖）。")
        if existed:
            self._require_read(path)
        before_text = ""
        if existed:
            try:
                with target.open("r", encoding="utf-8", errors="replace", newline="") as f:
                    before_text = f.read()
            except Exception:
                before_text = ""
        self._snap("write_text", path, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        before=self._hash(target)
        # newline=""：原样写入，Windows 下不把 \n 静默转成 \r\n
        with target.open("w", encoding="utf-8", newline="") as f: f.write(content)
        after=self._hash(target)
        out = {"path":path,"written_chars":len(content),
               "file_event":{"tool":"write_text","path":path,"before_hash":before,"after_hash":after,"result":"ok"}}
        out.update(self._diff_payload(path, before_text, content))
        if existed and out.get("changed_lines", 0):
            out["note"] = "本次为整体覆盖写入；已返回 diff。后续修改现有文件优先用 apply_patch / replace_text。"
        return out

    async def append_text(self, path: str, content: str, via_b64: bool = False) -> dict[str, Any]:
        if len(content) > self.max_write:
            raise ToolError("追加内容超过限制")
        if not via_b64:
            self._check_plain_code_size(path, content)
        target = self._path(path)
        self._snap("append_text", path, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        before=self._hash(target)
        with target.open("a", encoding="utf-8", newline="") as f: f.write(content)
        after=self._hash(target)
        return {"path":path,"appended_chars":len(content),"file_event":{"tool":"append_text","path":path,"before_hash":before,"after_hash":after,"result":"ok"}}

    @staticmethod
    def _decode_b64(content_b64: str) -> str:
        try:
            raw = base64.b64decode(content_b64, validate=True)
            return raw.decode("utf-8")
        except Exception as exc:
            # 提示要可自救：模型（尤其小模型）常见错误是把明文直接塞进
            # content_b64，或漏做 Base64。给出"下一步怎么做"，别让它原地重试。
            s = (content_b64 or "").strip()
            hint = ""
            if s and not re.fullmatch(r"[A-Za-z0-9+/=\s]*", s):
                hint = ("；检测到 content_b64 含非 Base64 字符——请把原文按 UTF-8 做 "
                        "Base64 编码后放进 content_b64（纯文本文件也可改用 write_text/append_text）")
            raise ToolError(f"Base64 UTF-8 内容无效: {exc}{hint}") from exc

    async def write_text_b64(self, path: str, content_b64: str,
                             overwrite: bool = False) -> dict[str, Any]:
        content = self._decode_b64(content_b64)
        result = await self.write_text(path, content, via_b64=True, overwrite=overwrite)
        result["tool"] = "write_text_b64"
        return result

    async def append_text_b64(self, path: str, content_b64: str) -> dict[str, Any]:
        content = self._decode_b64(content_b64)
        result = await self.append_text(path, content, via_b64=True)
        result["tool"] = "append_text_b64"
        return result

    @staticmethod
    def _nearest_hint(text: str, old: str) -> str:
        """片段没匹配上时，把文件里最接近的原文摘给模型，便于一轮内纠正。

        模型常见错误是缩进/空格与文件不一致（或凭记忆写片段）。只说"未找到"
        它会反复重试同一片段；把真实内容摆出来它才能改成精确片段。
        """
        first = next((ln.strip() for ln in (old or "").splitlines() if ln.strip()), "")
        if not first:
            return ""
        lines = text.replace("\r\n", "\n").splitlines()
        for i, ln in enumerate(lines):
            if first in ln.strip():
                lo, hi = max(0, i - 1), min(len(lines), i + 3)
                excerpt = "\n".join(lines[lo:hi])[:600]
                return (f"。文件第 {i + 1} 行附近实际内容是：\n{excerpt}\n"
                        "请照抄其中的精确片段（注意缩进与行尾）后重试")
        return ""

    async def replace_text(self, path: str, old: str, new: str, count: int = 1) -> dict[str, Any]:
        if not old:
            raise ToolError("old 不能为空")
        if len(new) > self.max_write:
            raise ToolError("替换内容超过限制")
        # count=0 表示"全部替换"：模型做"把制表符统一换成 4 空格"这类批量替换时，
        # 默认 count=1 只改第一处，于是它一遍遍改、一遍遍读——真实时间线里正是
        # 这样空转了几十步（用户："一直在重复替换空格/制表的动作"）。保留 1 的默认，
        # 但接受 0=全部，并在有残留时把数量明确回给模型。
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise ToolError("count 必须是整数（0 = 全部替换）")
        count = 0 if count <= 0 else min(count, 100)
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        self._require_read(path)
        # 严格解码 + newline=""：非 UTF-8 文件拒绝替换（避免乱码写回），
        # CRLF 文件保持 CRLF，不做换行符静默转换。
        try:
            with target.open("r", encoding="utf-8", errors="strict", newline="") as f:
                text = f.read()
        except UnicodeDecodeError as exc:
            raise ToolError(f"文件不是有效的 UTF-8 文本，拒绝替换（防止乱码写回）: {exc}")
        found = text.count(old)
        old_used, new_used = old, new
        strategy = "exact"
        if found == 0:
            # 行尾容错：Windows 工作区文件是 CRLF，而模型从 JSON 里给出的多行
            # 片段通常是 LF（JSON 里 \n 更自然）。按文件自身的行尾风格改写片段
            # 再试一次——实测这是"未找到要替换的原文本"失败的主因（真实任务里
            # 一个会话失败 91 次）。只替换文件里真实存在的那个变体，不会改动
            # 文件其余部分的行尾风格。
            if "\r\n" in text and "\r\n" not in old:
                cand = old.replace("\n", "\r\n")
                if text.count(cand):
                    old_used = cand
                    new_used = new if "\r\n" in new else new.replace("\n", "\r\n")
                    found = text.count(cand)
            elif "\r\n" not in text and "\r\n" in old:
                cand = old.replace("\r\n", "\n")
                if text.count(cand):
                    old_used = cand
                    new_used = new.replace("\r\n", "\n")
                    found = text.count(cand)
        if found == 0:
            # 多级降级匹配（Codex/DSH 的编辑工具同样思路）：模型抄片段时最容易失真
            # 的是行首缩进与行尾空白；卡在"逐字相等"上会烧掉大量回合。
            m = editmatch.find_span(text, old)
            if m and m.unique:
                eol = editmatch._detect_eol(text)
                fixed_old = m.matched
                fixed_new = editmatch.reindent(new, m.matched, eol)
                if fixed_old and text.count(fixed_old):
                    old_used, new_used = fixed_old, fixed_new
                    found = text.count(fixed_old)
                    strategy = m.strategy
            if found == 0:
                raise ToolError("未找到要替换的原文本，请先 read_text 后使用精确短片段"
                                + self._nearest_hint(text, old)
                                + "。" + editmatch.first_diff(text, old))
        replaced = found if count == 0 else min(found, count)
        after_text = (text.replace(old_used, new_used) if count == 0
                      else text.replace(old_used, new_used, count))
        before=self._hash(target)
        self._snap("replace_text", path, target)
        with target.open("w", encoding="utf-8", newline="") as f:
            f.write(after_text)
        after=self._hash(target)
        _left = max(0, found - replaced)
        _out = {"path":path,"replaced":replaced,"remaining_matches":_left,"match":strategy,
                "file_event":{"tool":"replace_text","path":path,"before_hash":before,"after_hash":after,"result":"ok"}}
        _out.update(self._diff_payload(path, text, after_text))
        if strategy != "exact":
            _out["note"] = (f"片段与文件并非逐字相同（按 {strategy} 容错匹配，且已按文件缩进校正新内容）；"
                            "下次可直接复用文件里的原样片段")
        if _left:
            _out["hint"] = (f"还有 {_left} 处要替换：要一次全改请传 count=0（0=全部）；"
                            "改缩进/空白这类批量编辑更稳的是 convert_indentation 或 replace_lines")
        if before == after:
            _out["warning"] = ("内容没有变化：old 与 new 可能等价，或该片段被前导空白干扰没匹配上。"
                               "先 read_text(reveal_whitespace=true) 看清真实空白再改")
        return _out

    async def replace_lines(self, path: str, start: int = 0, count: int = 1,
                            content: str = "") -> dict[str, Any]:
        """按行号替换整行区间（1 起，含首尾），保留文件原有换行风格。

        为什么需要：改缩进/整段替换用 `replace_text` 很脆——模型给的片段里
        前导空格容易在 JSON 里失真，实测一个真实任务因此反复失败，最后去用
        PowerShell 逐行改（然后踩了编码坑）。按行号改不依赖片段匹配。
        """
        try:
            start = int(start)
            count = max(1, min(int(count), 5000))
        except (TypeError, ValueError):
            raise ToolError("start / count 必须是整数")
        if start < 1:
            raise ToolError("start 从 1 开始（第 1 行）")
        if len(content) > self.max_write:
            raise ToolError("替换内容超过限制")
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        self._require_read(path)
        try:
            with target.open("r", encoding="utf-8", errors="strict", newline="") as f:
                raw = f.read()
        except UnicodeDecodeError as exc:
            raise ToolError(f"文件不是有效的 UTF-8 文本，拒绝改写（防止乱码写回）: {exc}")
        crlf = "\r\n" in raw
        lines = raw.replace("\r\n", "\n").split("\n")
        trailing_nl = raw.endswith(("\n"))
        body = lines[:-1] if trailing_nl else lines[:]
        total = len(body)
        if start > total + 1:
            raise ToolError(f"start={start} 超出文件行数（共 {total} 行）")
        lo = start - 1
        hi = min(total, lo + count)
        new_lines = content.split("\n") if content != "" else []
        out_lines = body[:lo] + new_lines + body[hi:]
        text = "\n".join(out_lines) + ("\n" if trailing_nl or not body else "")
        # 行尾风格跟随原文件：CRLF 文件不因为这次编辑被改成 LF（避免整文件 diff）
        if crlf:
            text = text.replace("\n", "\r\n")
        before = self._hash(target)
        self._snap("replace_lines", path, target)
        with target.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
        after = self._hash(target)
        out = {"path": path, "replaced_lines": hi - lo, "start": start,
               "inserted_lines": len(new_lines), "total_lines": len(out_lines),
               "changed": before != after,
               "file_event": {"tool": "replace_lines", "path": path,
                              "before_hash": before, "after_hash": after, "result": "ok"}}
        out.update(self._diff_payload(path, raw, text))
        return out

    async def apply_patch(self, path: str, patch: str = "", edits: Any = None,
                          count: int = 1) -> dict[str, Any]:
        """一次调用完成多处修改（**原子**：任一失败则整体不写）。

        两种输入：
        - `patch`：Codex 风格补丁，`*** Begin Patch` / `*** Update File: x` / `@@` 分块，
          每块用上下文行定位（` ` 上下文、`-` 旧行、`+` 新行）
        - `edits`：[{old, new, count?}] 简单替换列表

        为什么需要：逐条 `replace_text` 要在模型与文件之间来回 N 次，任一条因空白
        对不上就整轮重来——真实任务里这是回合与 token 爆掉的主因。补丁语义让
        "多处小改"变成一次调用、一次写入、一次验证。
        """
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}（apply_patch 只改现有文件；新建请用 write_text）")
        self._require_read(path)
        try:
            with target.open("r", encoding="utf-8", errors="strict", newline="") as f:
                text = f.read()
        except UnicodeDecodeError as exc:
            raise ToolError(f"文件不是有效的 UTF-8 文本，拒绝改写（防止乱码写回）: {exc}")
        eol = editmatch._detect_eol(text)
        try:
            batch = min(int(count), 100) if int(count) > 0 else 0
        except (TypeError, ValueError):
            batch = 1

        plan: list[tuple[str, str, int]] = []
        if isinstance(edits, list) and edits:
            plan = editmatch.parse_edits(edits)
            if not plan:
                raise ToolError("edits 为空或格式不对（每项需 {old, new}）")
        elif str(patch or "").strip():
            for idx, hunk in enumerate(editmatch.parse_patch(patch), 1):
                plan.append((eol.join(hunk.old_lines), eol.join(hunk.new_lines), 1))
            if not plan:
                raise ToolError("补丁里没有可应用的块（需要 `*** Update File:` 与 `@@` 分块）")
        else:
            raise ToolError("需要 patch 或 edits 之一")

        applied: list[dict[str, Any]] = []
        current = text
        for idx, (old, new, cnt) in enumerate(plan, 1):
            if not old.strip():
                # 纯插入块：定位不到上下文就拒绝，避免乱插入
                raise ToolError(f"第 {idx} 块缺少可定位的原文（补丁里要有上下文行或 - 行）")
            m = editmatch.find_span(current, old)
            if not m:
                raise ToolError(
                    f"第 {idx} 块没有匹配上（已应用 {len(applied)} 块，**本次未写入任何改动**）。"
                    + editmatch.first_diff(current, old))
            if not m.unique:
                raise ToolError(
                    f"第 {idx} 块在文件中出现 {m.count} 次，无法唯一定位：请把上下文行写长一些"
                    f"（本次未写入任何改动）")
            fixed_new = new
            if m.strategy != editmatch.STRATEGY_EXACT:
                # 容错匹配：新片段按文件实际缩进重排（tab 文件别被写成空格）
                fixed_new = editmatch.reindent(new, m.matched, eol)
            if cnt == 0:
                current = current.replace(m.matched, fixed_new)
            else:
                current = current[:m.start] + fixed_new + current[m.end:]
            applied.append({"index": idx, "match": m.strategy,
                            "line": current[:m.start].count(eol) + 1})

        if current == text:
            return {"path": path, "changed": False, "applied": applied,
                    "note": "应用后内容没有变化（补丁的 new 与 old 等价）"}
        before = self._hash(target)
        self._snap("apply_patch", path, target)
        with target.open("w", encoding="utf-8", newline="") as f:
            f.write(current)
        after = self._hash(target)
        fuzzy = [a["index"] for a in applied if a["match"] != editmatch.STRATEGY_EXACT]
        out: dict[str, Any] = {
            "path": path, "changed": before != after, "blocks": len(applied),
            "applied": applied, "fuzzy_blocks": fuzzy,
            "bytes": len(current.encode("utf-8")),
            "file_event": {"tool": "apply_patch", "path": path,
                           "before_hash": before, "after_hash": after, "result": "ok"},
        }
        out.update(self._diff_payload(path, text, current))
        if fuzzy:
            out["note"] = (f"有 {len(fuzzy)} 块是容错匹配（空白/缩进差异），已按文件缩进校正；"
                           "后续可用 read_text 复核")
        return out

    async def convert_indentation(self, path: str, mode: str = "tabs2spaces",
                                  width: int = 4, line_start: int = 0,
                                  line_count: int = 0) -> dict[str, Any]:
        """把**行首**缩进在制表符与空格之间确定性地转换（一次搞定）。

        为什么需要单列一个工具：真实任务里模型要"把制表符统一换成 4 个空格"，
        只能用 replace_text 一处一处改（默认 count=1），改一处、读一次、
        发现还有、再改……一个 377 步的任务里几十步都耗在这上面，还换了 6 次
        Copilot 会话（用户原话："一直在重复替换空格/制表的动作"）。
        这个工具只动**行首空白**（行内、字符串里的制表符一律不碰），
        一次调用完成全文件或指定行区间，并回报改了多少行。
        """
        mode = str(mode or "tabs2spaces").strip().lower()
        if mode in ("tab2space", "tabs2space", "expand", "spaces"):
            mode = "tabs2spaces"
        elif mode in ("space2tab", "spaces2tab", "compress", "tabs"):
            mode = "spaces2tabs"
        if mode not in ("tabs2spaces", "spaces2tabs"):
            raise ToolError("mode 只能是 tabs2spaces 或 spaces2tabs")
        try:
            width = max(1, min(int(width), 16))
            line_start = int(line_start or 0)
            line_count = int(line_count or 0)
        except (TypeError, ValueError):
            raise ToolError("width / line_start / line_count 必须是整数")
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        try:
            with target.open("r", encoding="utf-8", errors="strict", newline="") as f:
                raw = f.read()
        except UnicodeDecodeError as exc:
            raise ToolError(f"文件不是有效的 UTF-8 文本，拒绝改写（防止乱码写回）: {exc}")
        crlf = "\r\n" in raw
        lines = raw.replace("\r\n", "\n").split("\n")
        lo = max(0, (line_start - 1) if line_start > 0 else 0)
        hi = len(lines) if not line_count else min(len(lines), lo + max(0, line_count))
        changed = 0
        out = list(lines)
        for i in range(lo, hi):
            body = lines[i]
            # 只取行首空白（空格与制表符），其余原样保留
            m = re.match(r"[ \t]*", body)
            indent = m.group(0) if m else ""
            rest = body[len(indent):]
            if mode == "tabs2spaces":
                if "\t" not in indent:
                    continue
                # 制表位按 width 展开（4 空格制表位）
                new_indent = indent.expandtabs(width)
            else:
                if " " not in indent:
                    continue
                n = len(indent)
                new_indent = "\t" * (n // width) + " " * (n % width)
            if new_indent != indent:
                out[i] = new_indent + rest
                changed += 1
        if not changed:
            return {"path": path, "changed_lines": 0, "changed": False,
                    "hint": "行首缩进已经符合目标格式，无需改动"}
        text = "\n".join(out)
        if crlf:
            text = text.replace("\n", "\r\n")
        before = self._hash(target)
        self._snap("convert_indentation", path, target)
        with target.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
        after = self._hash(target)
        return {"path": path, "mode": mode, "width": width,
                "changed_lines": changed, "changed": before != after,
                "line_range": [lo + 1, hi],
                "file_event": {"tool": "convert_indentation", "path": path,
                               "before_hash": before, "after_hash": after, "result": "ok"}}

    async def indent_lines(self, path: str, line_start: int = 1, line_count: int = 1,
                           delta: int = 0) -> dict[str, Any]:
        """给指定行区间**增减行首空格**（delta 为正加缩进、为负减缩进）。

        为什么不能靠 replace_text：缩进要写在参数字符串里，而 Copilot 的回答经
        HTML 渲染后**行首/连续空格会被折叠成一个**,于是 old == new、写入无变化、
        模型以为没生效又改一次（真实时间线里 15 次空操作、空转几十步）。
        这里用**数字**表达缩进，不依赖字符串里的空白，一次调用确定生效。
        """
        try:
            line_start = int(line_start or 1)
            line_count = int(line_count or 1)
            delta = int(delta or 0)
        except (TypeError, ValueError):
            raise ToolError("line_start / line_count / delta 必须是整数")
        if line_start < 1:
            raise ToolError("line_start 从 1 开始")
        if line_count < 1:
            raise ToolError("line_count 至少 1")
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        try:
            with target.open("r", encoding="utf-8", errors="strict", newline="") as f:
                raw = f.read()
        except UnicodeDecodeError as exc:
            raise ToolError(f"文件不是有效的 UTF-8 文本，拒绝改写（防止乱码写回）: {exc}")
        crlf = "\r\n" in raw
        lines = raw.replace("\r\n", "\n").split("\n")
        lo = line_start - 1
        hi = min(len(lines), lo + line_count)
        if lo >= len(lines):
            raise ToolError(f"line_start={line_start} 超出文件行数（共 {len(lines)} 行）")
        changed = 0
        for i in range(lo, hi):
            body = lines[i]
            if not body.strip():
                continue                      # 空行不动
            m = re.match(r"[ \t]*", body)
            indent = m.group(0) if m else ""
            rest = body[len(indent):]
            n = len(indent.expandtabs(4))
            n2 = max(0, n + delta)
            if n2 == n and "\t" not in indent:
                continue
            new_indent = " " * n2
            if new_indent + rest != body:
                lines[i] = new_indent + rest
                changed += 1
        if not changed:
            return {"path": path, "changed_lines": 0, "changed": False,
                    "hint": "这些行的缩进已经是目标值，无需改动"}
        text = "\n".join(lines)
        if crlf:
            text = text.replace("\n", "\r\n")
        before = self._hash(target)
        self._snap("indent_lines", path, target)
        with target.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
        after = self._hash(target)
        return {"path": path, "changed_lines": changed, "delta": delta,
                "changed": before != after,
                "file_event": {"tool": "indent_lines", "path": path,
                               "before_hash": before, "after_hash": after, "result": "ok"}}

    async def make_directory(self, path: str) -> dict[str, Any]:
        target = self._path(path)
        if not target.exists():
            self._snap("make_directory", path, target)
        target.mkdir(parents=True, exist_ok=True)
        return {"path": path, "created": True}

    async def copy_path(self, source: str, destination: str) -> dict[str, Any]:
        src, dst = self._path(source), self._path(destination)
        if not src.exists():
            raise ToolError(f"源路径不存在: {source}")
        if dst == src or src in dst.parents:
            raise ToolError("禁止把目录复制/移动到它自己的子目录里")
        self._snap("copy_path", destination, dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        return {"source": source, "destination": destination, "copied": True}

    async def move_path(self, source: str, destination: str) -> dict[str, Any]:
        src, dst = self._path(source), self._path(destination)
        if not src.exists():
            raise ToolError(f"源路径不存在: {source}")
        if dst == src or src in dst.parents:
            raise ToolError("禁止把目录复制/移动到它自己的子目录里")
        # 源会被移走、目标是新增内容：两端都要有原像才能回滚
        self._snap("move_path", source, src)
        self._snap("move_path", destination, dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return {"source": source, "destination": destination, "moved": True}

    async def delete_path(self, path: str) -> dict[str, Any]:
        if not self.allow_delete:
            raise ToolError("delete_path 已在 config.json 中禁用")
        target = self._path(path)
        if not target.exists():
            return {"path": path, "deleted": False, "reason": "not_found"}
        if target == self.root:
            raise ToolError("禁止删除工作区根目录")
        self._snap("delete_path", path, target)
        shutil.rmtree(target) if target.is_dir() else target.unlink()
        return {"path": path, "deleted": True}
