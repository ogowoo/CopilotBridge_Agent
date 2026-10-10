

import asyncio
import base64
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable


class ToolError(RuntimeError):
    pass


class ToolRegistry:
    def __init__(self, root: Path, config: dict[str, Any]):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_read = int(config.get("max_read_chars", 120000))
        self.max_write = int(config.get("max_write_chars", 200000))
        self.allow_delete = bool(config.get("allow_delete", False))
        self.allow_python = bool(config.get("allow_python", False))
        self.python_timeout = float(config.get("python_timeout_seconds", 20))
        self._tools: dict[str, Callable[..., Awaitable[Any]]] = {
            "list_directory": self.list_directory,
            "find_files": self.find_files,
            "read_text": self.read_text,
            "write_text": self.write_text,
            "append_text": self.append_text,
            "write_text_b64": self.write_text_b64,
            "append_text_b64": self.append_text_b64,
            "replace_text": self.replace_text,
            "make_directory": self.make_directory,
            "copy_path": self.copy_path,
            "move_path": self.move_path,
            "delete_path": self.delete_path,
            "run_python": self.run_python,
            "validate_python": self.validate_python,
            "run_unittest": self.run_unittest,
        }

    def set_root(self, value: str | Path, create: bool = False) -> str:
        target = Path(value).expanduser()
        target = target.resolve() if target.is_absolute() else (self.root / target).resolve()
        if create:
            target.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            raise ToolError(f"工作目录不存在: {target}")
        if not target.is_dir():
            raise ToolError(f"工作目录不是文件夹: {target}")
        self.root = target
        return str(self.root)

    def descriptions(self) -> list[dict[str, Any]]:
        items = [
            {"name": "list_directory", "description": "列出工作区目录", "arguments": {"path": "相对路径，默认 ."}},
            {"name": "find_files", "description": "按 glob 查找文件", "arguments": {"pattern": "如 **/*.log"}},
            {"name": "read_text", "description": "读取 UTF-8 文本", "arguments": {"path": "相对路径"}},
            {"name": "write_text", "description": "覆盖写入 UTF-8 文本；仅用于新文件或完整短文件", "arguments": {"path": "相对路径", "content": "文本"}},
            {"name": "append_text", "description": "追加普通 UTF-8 文本", "arguments": {"path": "相对路径", "content": "文本"}},
            {"name": "write_text_b64", "description": "Base64 解码后覆盖写入 UTF-8；代码、JSON、HTML、Markdown 必须用此工具", "arguments": {"path": "相对路径", "content_b64": "UTF-8 内容的 Base64"}},
            {"name": "append_text_b64", "description": "Base64 解码后追加 UTF-8；代码、JSON、HTML、Markdown 必须用此工具", "arguments": {"path": "相对路径", "content_b64": "UTF-8 内容的 Base64"}},
            {"name": "replace_text", "description": "在现有文件中精确替换短文本，修改代码时优先使用", "arguments": {"path": "相对路径", "old": "原文本", "new": "新文本", "count": "替换次数，默认1"}},
            {"name": "make_directory", "description": "创建目录", "arguments": {"path": "相对路径"}},
            {"name": "copy_path", "description": "复制文件或目录", "arguments": {"source": "相对路径", "destination": "相对路径"}},
            {"name": "move_path", "description": "移动文件或目录", "arguments": {"source": "相对路径", "destination": "相对路径"}},
            {"name": "validate_python", "description": "检查 Python 文件或目录语法；修改 Python 后必须执行", "arguments": {"path": "相对路径，默认 ."}},
            {"name": "run_unittest", "description": "执行 unittest 测试发现；治理完成前必须通过", "arguments": {"path": "测试目录，默认 tests", "pattern": "默认 test_*.py"}},
        ]
        if self.allow_delete:
            items.append({"name": "delete_path", "description": "删除路径", "arguments": {"path": "相对路径"}})
        if self.allow_python:
            items.append({"name": "run_python", "description": "运行短 Python 计算；禁止用于修改项目文件", "arguments": {"code": "短 Python 代码"}})
        return items

    def _path(self, value: str = ".") -> Path:
        candidate = (self.root / value).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ToolError("拒绝访问工作区之外的路径") from exc
        return candidate

    async def execute(self, name: str, arguments: dict[str, Any] | None) -> Any:
        fn = self._tools.get(name)
        if not fn:
            raise ToolError(f"未知工具: {name}")
        try:
            return await fn(**(arguments or {}))
        except TypeError as exc:
            raise ToolError(f"工具参数错误: {exc}") from exc

    async def list_directory(self, path: str = ".") -> dict[str, Any]:
        target = self._path(path)
        if not target.is_dir():
            raise ToolError(f"目录不存在: {path}")
        items = []
        for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            stat = item.stat()
            items.append({"name": item.name, "type": "directory" if item.is_dir() else "file", "size": stat.st_size})
        return {"path": path, "items": items[:1000], "truncated": len(items) > 1000}

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

    async def read_text(self, path: str) -> dict[str, Any]:
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        return {"path": path, "content": text[:self.max_read], "truncated": len(text) > self.max_read}

    @staticmethod
    def _hash(path: Path) -> str | None:
        if not path.is_file(): return None
        h=hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda:f.read(1048576),b""): h.update(chunk)
        return h.hexdigest()

    async def write_text(self, path: str, content: str) -> dict[str, Any]:
        if len(content) > self.max_write:
            raise ToolError("写入内容超过限制")
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        before=self._hash(target); target.write_text(content, encoding="utf-8"); after=self._hash(target)
        return {"path":path,"written_chars":len(content),"file_event":{"tool":"write_text","path":path,"before_hash":before,"after_hash":after,"result":"ok"}}

    async def append_text(self, path: str, content: str) -> dict[str, Any]:
        if len(content) > self.max_write:
            raise ToolError("追加内容超过限制")
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        before=self._hash(target)
        with target.open("a", encoding="utf-8") as f: f.write(content)
        after=self._hash(target)
        return {"path":path,"appended_chars":len(content),"file_event":{"tool":"append_text","path":path,"before_hash":before,"after_hash":after,"result":"ok"}}

    @staticmethod
    def _decode_b64(content_b64: str) -> str:
        try:
            raw = base64.b64decode(content_b64, validate=True)
            return raw.decode("utf-8")
        except Exception as exc:
            raise ToolError(f"Base64 UTF-8 内容无效: {exc}") from exc

    async def write_text_b64(self, path: str, content_b64: str) -> dict[str, Any]:
        content = self._decode_b64(content_b64)
        result = await self.write_text(path, content)
        result["tool"] = "write_text_b64"
        return result

    async def append_text_b64(self, path: str, content_b64: str) -> dict[str, Any]:
        content = self._decode_b64(content_b64)
        result = await self.append_text(path, content)
        result["tool"] = "append_text_b64"
        return result

    async def replace_text(self, path: str, old: str, new: str, count: int = 1) -> dict[str, Any]:
        if not old:
            raise ToolError("old 不能为空")
        if len(new) > self.max_write:
            raise ToolError("替换内容超过限制")
        count = max(1, min(int(count), 100))
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        found = text.count(old)
        if found == 0:
            raise ToolError("未找到要替换的原文本，请先 read_text 后使用精确短片段")
        replaced = min(found, count)
        before=self._hash(target); target.write_text(text.replace(old,new,count),encoding="utf-8"); after=self._hash(target)
        return {"path":path,"replaced":replaced,"remaining_matches":max(0,found-replaced),"file_event":{"tool":"replace_text","path":path,"before_hash":before,"after_hash":after,"result":"ok"}}

    async def make_directory(self, path: str) -> dict[str, Any]:
        target = self._path(path)
        target.mkdir(parents=True, exist_ok=True)
        return {"path": path, "created": True}

    async def copy_path(self, source: str, destination: str) -> dict[str, Any]:
        src, dst = self._path(source), self._path(destination)
        if not src.exists():
            raise ToolError(f"源路径不存在: {source}")
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
        shutil.rmtree(target) if target.is_dir() else target.unlink()
        return {"path": path, "deleted": True}

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
