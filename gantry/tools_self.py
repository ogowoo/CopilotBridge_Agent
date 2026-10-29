"""tools_self.py — 自升级工具（ToolRegistry 的 mixin）：读写/替换服务自身代码、全量校验、自身单测与回滚（默认关闭，需 allow_self_upgrade）。

从 tools.py 拆出（第四批解耦）：方法体原样搬入 mixin，运行时行为不变。
"""
from __future__ import annotations

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


class SelfUpgradeMixin:
    # ── 自升级（修改服务自身代码）────────────────────────────────────
    def _self_rel(self, candidate: Path) -> str:
        return candidate.relative_to(self.global_root).as_posix()

    def _self_path(self, value: str = ".") -> Path:
        if not self.allow_self_upgrade:
            raise ToolError("自升级已在 config.json 中禁用（allow_self_upgrade=false）")
        if not self.global_root:
            raise ToolError("未设置服务安装目录，无法自升级")
        candidate = (self.global_root / value).resolve()
        try:
            rel = self._self_rel(candidate)
        except ValueError:
            raise ToolError("拒绝访问服务安装目录之外的路径")
        for deny in self.self_denylist:
            if deny.endswith("/"):
                if rel.startswith(deny) or rel == deny.rstrip("/"):
                    raise ToolError(f"自升级禁止触碰: {rel}")
            elif rel == deny:
                raise ToolError(f"自升级禁止触碰: {rel}")
        allowed = (*self.self_whitelist, "plugins/")
        if not any(rel == w.rstrip("/") or rel.startswith(w) for w in allowed):
            raise ToolError(f"路径不在自升级白名单内: {rel}")
        return candidate

    def _mark_self_upgrade(self, rel: str) -> None:
        """标记"服务代码已改动，需重启生效"（同时作为自我保护线索）。"""
        try:
            marker = self.global_root / "data" / ".self_upgrade_pending"
            marker.parent.mkdir(parents=True, exist_ok=True)
            with marker.open("a", encoding="utf-8") as f:
                f.write(f"{time.time()}\t{rel}\n")
        except Exception:
            pass

    async def self_find_files(self, pattern: str = "**/*.py") -> dict[str, Any]:
        """glob 列举服务目录下的文件（如 `gantry/*.py`）；模糊任务的"先定位"第一步。"""
        # 根目录只校验"开关已开 + 目录已设置"：白名单是给具体文件的，
        # 拿 "." 去过白名单会被拒（"路径不在自升级白名单内: ."）。逐文件再校验。
        if not self.allow_self_upgrade:
            raise ToolError("自升级已在 config.json 中禁用（allow_self_upgrade=false）")
        if not self.global_root:
            raise ToolError("未设置服务安装目录，无法自升级")
        root = self.global_root
        pat = str(pattern or "**/*.py").strip() or "**/*.py"
        if pat.startswith(("/", "\\")) or ".." in pat.split("/"):
            raise ToolError("pattern 不能是绝对路径，也不能含 ..")
        out = []
        for f in sorted(root.glob(pat)):
            if not f.is_file():
                continue
            rel = self._self_rel(f)
            try:
                self._self_path(rel)     # 过白名单/黑名单
            except ToolError:
                continue
            out.append(rel)
            if len(out) >= 300:
                break
        return {"pattern": pat, "count": len(out), "files": out,
                "truncated": len(out) >= 300}

    async def self_search_text(self, query: str, path: str = ".",
                               max_results: int = 50) -> dict[str, Any]:
        """在服务目录里搜文本/正则：返回 file:line + 该行内容。

        模糊任务的关键工具：用户说"那个按钮/那个报错"，先搜出在哪，再决定改哪。
        """
        q = str(query or "").strip()
        if not q:
            raise ToolError("query 不能为空")
        if not self.allow_self_upgrade:
            raise ToolError("自升级已在 config.json 中禁用（allow_self_upgrade=false）")
        if not self.global_root:
            raise ToolError("未设置服务安装目录，无法自升级")
        base = self._self_path(path) if str(path or ".").strip() not in ("", ".") else self.global_root
        try:
            rx = re.compile(q)
        except re.error:
            rx = re.compile(re.escape(q))
        cap = max(1, min(int(max_results or 50), 200))
        hits = []
        files = sorted(base.rglob("*")) if base.is_dir() else [base]
        for f in files:
            if not f.is_file() or f.suffix.lower() not in (".py", ".js", ".html", ".css",
                                                           ".md", ".json", ".toml", ".txt"):
                continue
            rel = self._self_rel(f)
            try:
                self._self_path(rel)
            except ToolError:
                continue
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8",
                                                     errors="replace").splitlines(), 1):
                    if rx.search(line):
                        hits.append({"file": rel, "line": i, "text": line.strip()[:200]})
                        if len(hits) >= cap:
                            break
            except Exception:
                continue
            if len(hits) >= cap:
                break
        return {"query": q, "count": len(hits), "hits": hits,
                "truncated": len(hits) >= cap}

    async def self_read_text(self, path: str, offset: int = 0, limit: int = 0,
                             line_start: int = 0, line_count: int = 0,
                             reveal_whitespace: bool = False) -> dict[str, Any]:
        """读服务自身文件。

        两种读法（与普通 `read_text` 对齐——实测模型会**默认** self_read_text 也支持
        `line_start`/`line_count`，传了就直接 TypeError 丢掉一步，task-mvs4un2x 第 6 步）：
          · `line_start` + `line_count`：按**行**读（1 起），返回带行号的 `lines`；
          · `offset` + `limit`：按**字符**读（老接口，保持兼容）。
        """
        target = self._self_path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        try:
            with target.open("r", encoding="utf-8", errors="replace", newline="") as f:
                text = f.read()
        except UnicodeDecodeError as exc:
            raise ToolError(f"文件不是有效的 UTF-8 文本: {exc}")
        offset = max(0, self._int_arg(offset, 0, "offset"))
        limit = max(0, self._int_arg(limit, 0, "limit"))
        line_start = max(0, self._int_arg(line_start, 0, "line_start"))
        line_count = max(0, self._int_arg(line_count, 0, "line_count"))
        lines = text.replace("\r\n", "\n").split("\n")
        total_lines = len(lines) - (1 if lines and lines[-1] == "" else 0)

        if line_start:
            if line_start > total_lines:
                return {"path": path, "lines": "", "line_start": line_start,
                        "line_count": 0, "total_lines": total_lines, "truncated": False,
                        "note": f"文件只有 {total_lines} 行"}
            want = line_count or (total_lines - line_start + 1)
            want = max(1, min(want, 2000, self.max_read // 40 or 1))
            lo = line_start - 1
            hi = min(total_lines, lo + want)

            def _vis(t: str) -> str:
                return t.replace("\t", "→").replace(" ", "·")

            body = "\n".join(
                ((f"{i + 1}: " + _vis(lines[i])) if reveal_whitespace else f"{i + 1}: {lines[i]}")
                for i in range(lo, hi))
            res = {"path": path, "lines": body, "line_start": line_start,
                   "line_count": hi - lo, "total_lines": total_lines,
                   "truncated": hi < total_lines, "numbered": True}
            if reveal_whitespace:
                res["whitespace"] = "空格=·  制表符=→"
            return res

        total = len(text)
        cap = min(limit, self.max_read) if limit else self.max_read
        end = min(total, offset + cap)
        return {"path": path, "content": text[offset:end], "offset": offset,
                "total_chars": total, "total_lines": total_lines,
                "truncated": end < total,
                "next_offset": end if end < total else None}

    async def _self_write(self, path: str, content: str, overwrite: bool) -> dict[str, Any]:
        if len(content) > self.max_write:
            raise ToolError("写入内容超过限制")
        target = self._self_path(path)
        rel = self._self_rel(target)
        self._snap("self_write_text_b64" if overwrite else "self_append_text_b64", rel, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = self._hash(target)
        with target.open("w" if overwrite else "a", encoding="utf-8", newline="") as f:
            f.write(content)
        self._mark_self_upgrade(rel)
        tool = "self_write_text_b64" if overwrite else "self_append_text_b64"
        return {"path": path, "written_chars": len(content), "tool": tool,
                "note": "服务自身代码已改动，需重启服务生效；验证通过前请勿宣称完成",
                "file_event": {"tool": tool, "path": rel, "before_hash": before,
                               "after_hash": self._hash(target), "result": "ok"}}

    async def self_write_text_b64(self, path: str, content_b64: str) -> dict[str, Any]:
        return await self._self_write(path, self._decode_b64(content_b64), True)

    async def self_append_text_b64(self, path: str, content_b64: str) -> dict[str, Any]:
        return await self._self_write(path, self._decode_b64(content_b64), False)

    def _nearest_block(self, text: str, old: str) -> dict[str, Any]:
        """找与 `old` 最相近的连续行窗口：替换失败时把**真实原文**递回给模型。

        真事（task-mvs4un2x 第 68/70/71/74/76 步）：模型凭记忆拼 `old`，差一个空格就
        "未找到要替换的原文本"，它只能再 self_read_text 一次再拼——一连烧 5 步。
        这里直接把最接近的行区间与原文给它，一步就能改对。
        """
        lines = text.replace("\r\n", "\n").split("\n")
        old_lines = old.replace("\r\n", "\n").split("\n")
        target = "\n".join(x.strip() for x in old_lines if x.strip())
        if not target:
            return {}
        best = (0.0, 0, 0)
        n = len(lines)
        max_take = min(max(len(old_lines) + 3, 3), 60)
        for start in range(n):
            for take in range(1, max_take + 1):
                if start + take > n:
                    break
                cand = "\n".join(x.strip() for x in lines[start:start + take] if x.strip())
                if not cand:
                    continue
                r = difflib.SequenceMatcher(None, cand, target).ratio()
                if r > best[0]:
                    best = (r, start, take)
            if best[0] > 0.95:
                break
        ratio, start, take = best
        if ratio <= 0:
            return {}
        snippet = "\n".join(f"{start + 1 + i}: {lines[start + i]}" for i in range(take))
        return {"ratio": round(ratio, 3), "line_start": start + 1,
                "line_count": take, "snippet": snippet[:1500]}

    async def self_replace_text(self, path: str, old: str, new: str, count: int = 1) -> dict[str, Any]:
        if not old:
            raise ToolError("old 不能为空")
        if len(new) > self.max_write:
            raise ToolError("替换内容超过限制")
        count = max(1, min(self._int_arg(count, 1, "count"), 100))
        target = self._self_path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        try:
            with target.open("r", encoding="utf-8", errors="strict", newline="") as f:
                text = f.read()
        except UnicodeDecodeError as exc:
            raise ToolError(f"文件不是有效的 UTF-8 文本: {exc}")
        rel = self._self_rel(target)
        found = text.count(old)
        matched = old
        if found == 0:
            # 宽容匹配：换行符（CRLF/LF）与行首行尾空白差异不该让替换失败——
            # 模型读到的片段常被 JSON 转义/缩进重排过。
            normalized = text.replace("\r\n", "\n")
            cand = old.replace("\r\n", "\n")
            if normalized.count(cand):
                matched = cand
                text = normalized
                found = normalized.count(cand)
            else:
                stripped = [ln.strip() for ln in cand.split("\n") if ln.strip()]
                if stripped:
                    pat = "\r?\n".join(
                        r"[ \t]*" + re.escape(ln) + r"[ \t]*" for ln in stripped)
                    hits = list(re.finditer(pat, text))
                    if len(hits) == 1:
                        found = 1
                        matched = hits[0].group(0)
        if found == 0:
            near = self._nearest_block(text, old)
            hint = ""
            if near:
                hint = (f"\n最接近的原文在第 {near['line_start']}–"
                        f"{near['line_start'] + near['line_count'] - 1} 行"
                        f"（相似度 {near['ratio']}）：\n{near['snippet']}\n"
                        f"请照抄上面这段当作 old（可先 self_read_text "
                        f"line_start={near['line_start']} line_count={near['line_count']} 确认）")
            raise ToolError(
                "未找到要替换的原文本。" + hint + "\n"
                "提示：old 必须与文件内容**逐字一致**（含空白与换行）；不确定就先用 "
                "self_read_text 的 line_start/line_count 看带行号的原文。")
        if matched != old and found:
            self._snap("self_replace_text", rel, target)
            before = self._hash(target)
            with target.open("w", encoding="utf-8", newline="") as f:
                f.write(text.replace(matched, new, count))
            self._mark_self_upgrade(rel)
            return {"path": path, "replaced": min(found, count),
                    "note": "服务自身代码已改动，需重启服务生效",
                    "match": "宽容匹配（空白/换行差异）",
                    "file_event": {"tool": "self_replace_text", "path": rel,
                                   "before_hash": before, "after_hash": self._hash(target),
                                   "result": "ok"}}
        self._snap("self_replace_text", rel, target)
        before = self._hash(target)
        with target.open("w", encoding="utf-8", newline="") as f:
            f.write(text.replace(old, new, count))
        self._mark_self_upgrade(rel)
        return {"path": path, "replaced": min(found, count),
                "note": "服务自身代码已改动，需重启服务生效",
                "file_event": {"tool": "self_replace_text", "path": rel, "before_hash": before,
                               "after_hash": self._hash(target), "result": "ok"}}

    def _self_py_files(self) -> list[Path]:
        skip = ("data", "EdgeProfile", ".git", "__pycache__", "for debug")
        out = []
        for p in self.global_root.rglob("*.py"):
            rel = p.relative_to(self.global_root)
            if rel.parts and rel.parts[0] in skip:
                continue
            out.append(p)
        return sorted(out)

    async def self_validate(self) -> dict[str, Any]:
        """全量校验：py_compile + 前端 id 引用完整性，是自升级的硬门之一。

        第二条是为了拦"改了 JS 逻辑却没加对应元素"这类假完成（task-mvs4un2x 实测：
        自升级代理加了读 `setTeamScopeHeartbeat` 的代码，却忘了把复选框加进设置页，
        py_compile/unittest 全绿照样宣布完成）。
        """
        if not self.allow_self_upgrade:
            raise ToolError("自升级已在 config.json 中禁用")
        files = self._self_py_files()
        if not files:
            raise ToolError("未找到 Python 文件")
        failures = []
        for item in files:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "py_compile", str(item), cwd=str(self.global_root),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await proc.communicate()
            if proc.returncode:
                failures.append({"path": str(item.relative_to(self.global_root)),
                                 "error": stderr.decode("utf-8", "replace")[-2000:]})
        ui_refs = {"skipped": True, "missing": []}
        try:
            from .ui_refs import check_ui_id_refs
            ui_refs = check_ui_id_refs(self.global_root)
            for miss in ui_refs.get("missing") or []:
                failures.append({
                    "path": "webui/app.js",
                    "error": (f"引用了不存在的元素 id #{miss['id']}（行 {miss['lines']}）——"
                              "JS 里 getElementById/querySelector 的元素必须在 index.html "
                              "或动态生成里真实存在（见 gantry/ui_refs.py）")})
        except Exception as exc:                         # noqa: BLE001
            failures.append({"path": "webui/ui_refs",
                             "error": f"前端 id 引用检查自身失败: {exc}"})
        return {"passed": not failures, "checked": len(files), "failures": failures,
                "ui_refs": ui_refs}

    async def self_unittest(self, pattern: str = "test_*.py") -> dict[str, Any]:
        """在服务安装目录跑单元测试，是自升级的第二个硬门。"""
        if not self.allow_self_upgrade:
            raise ToolError("自升级已在 config.json 中禁用")
        tests_dir = self.global_root / "tests"
        if not tests_dir.is_dir():
            raise ToolError("服务安装目录下没有 tests 目录")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "unittest", "discover", "-s", str(tests_dir), "-p", pattern, "-v",
            cwd=str(self.global_root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                    timeout=max(self.python_timeout, 180))
        except asyncio.TimeoutError:
            proc.kill(); await proc.wait(); raise ToolError("自升级单元测试超时")
        output = (stdout + stderr).decode("utf-8", "replace")[-30000:]
        return {"passed": proc.returncode == 0, "exit_code": proc.returncode, "output": output}

    async def self_rollback(self) -> dict[str, Any]:
        """回滚本次任务对服务自身代码的改动（原像来自快照）。"""
        fn = getattr(self, "self_rollback_fn", None)
        if not fn:
            raise ToolError("当前没有可用的自升级回滚通道（未绑定快照作用域）")
        result = fn()
        return result if isinstance(result, dict) else {"result": result}
