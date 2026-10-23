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
        if not any(rel == w.rstrip("/") or rel.startswith(w) for w in self.self_whitelist):
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

    async def self_read_text(self, path: str, offset: int = 0, limit: int = 0) -> dict[str, Any]:
        target = self._self_path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        offset = max(0, self._int_arg(offset, 0, "offset"))
        limit = max(0, self._int_arg(limit, 0, "limit"))
        total = len(text)
        cap = min(limit, self.max_read) if limit else self.max_read
        end = min(total, offset + cap)
        return {"path": path, "content": text[offset:end], "offset": offset,
                "total_chars": total, "truncated": end < total,
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
        found = text.count(old)
        if found == 0:
            raise ToolError("未找到要替换的原文本，请先 self_read_text 确认原文")
        rel = self._self_rel(target)
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
        """对服务安装目录做全量语法检查（py_compile），是自升级的硬门之一。"""
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
        return {"passed": not failures, "checked": len(files), "failures": failures}

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
