"""tools_exec.py — 代码执行与校验工具（ToolRegistry 的 mixin）：validate_python / run_unittest / run_code（程序化批量调用只读工具）/ run_python。

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


class ExecMixin:
    async def validate_python(self, path: str = ".") -> dict[str, Any]:
        target = self._path(path)
        files = [target] if target.is_file() else sorted(target.rglob("*.py"))
        if not files:
            raise ToolError(f"未找到 Python 文件: {path}")
        failures = []
        for item in files:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "py_compile", str(item), cwd=str(self.root),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await proc.communicate()
            if proc.returncode:
                failures.append({"path": str(item.relative_to(self.root)),
                                 "error": stderr.decode("utf-8", "replace")[-4000:]})
        return {"passed": not failures, "checked": len(files), "failures": failures}

    async def run_unittest(self, path: str = "tests", pattern: str = "test_*.py") -> dict[str, Any]:
        target = self._path(path)
        if not target.is_dir():
            raise ToolError(f"测试目录不存在: {path}")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "unittest", "discover", "-s", str(target), "-p", pattern, "-v",
            cwd=str(self.root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=max(self.python_timeout, 120))
        except asyncio.TimeoutError:
            proc.kill(); await proc.wait(); raise ToolError("单元测试执行超时")
        output = (stdout + stderr).decode("utf-8", "replace")[-30000:]
        return {"passed": proc.returncode == 0, "exit_code": proc.returncode, "output": output}

    async def run_code(self, code: str) -> dict[str, Any]:
        """在一段代码里批量调用**只读**工具（P2-13）。

        适合"读多个文件再汇总"这类分析任务：工具循环要 N 个远端回合，这里 1 个回合即可。
        白名单只含只读工具；超时/调用次数/回传大小均有硬限额（见 config）。
        """
        if not self.allow_run_code:
            raise ToolError("run_code 已在 config.json 中禁用（allow_run_code=false）")
        from .run_code import run_code as _impl

        out = await _impl(self, code, timeout=self.run_code_timeout,
                          max_calls=self.run_code_max_calls,
                          max_output_chars=self.run_code_max_output)
        if not out.get("ok"):
            raise ToolError(f"run_code 失败：{out.get('error')}")
        out["note"] = (f"本次共代理 {out.get('executed', 0)} 次只读工具调用"
                       f"（尝试 {out.get('calls', 0)} 次）")
        return out

    async def run_python(self, code: str) -> dict[str, Any]:
        if not self.allow_python:
            raise ToolError("run_python 已在 config.json 中禁用")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", code,
            cwd=str(self.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.python_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ToolError("Python 执行超时")
        return {"exit_code": proc.returncode, "stdout": stdout.decode("utf-8", "replace")[-20000:], "stderr": stderr.decode("utf-8", "replace")[-20000:]}
