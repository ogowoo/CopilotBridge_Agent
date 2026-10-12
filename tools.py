

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
        self.allow_office = bool(config.get("allow_office", True))
        self.allow_outlook = bool(config.get("allow_outlook", False))
        self.allow_outlook_send = bool(config.get("allow_outlook_send", False))
        self.python_timeout = float(config.get("python_timeout_seconds", 20))
        # 工作区根目录白名单：配置后 set_root 只能切到这些根目录之下，
        # 防止 API 把工具根目录移到 C:\ 之类的地方使路径沙箱形同虚设。
        self.workspace_roots = [
            Path(p).expanduser().resolve()
            for p in (config.get("workspace_roots") or [])
        ]
        self._extra_descriptions: list[dict[str, Any]] = []
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
            "read_excel": self.read_excel,
            "write_excel": self.write_excel,
            "read_docx": self.read_docx,
            "write_docx": self.write_docx,
            "read_pptx": self.read_pptx,
            "read_pdf": self.read_pdf,
            "outlook_unread": self.outlook_unread,
            "outlook_read": self.outlook_read,
            "outlook_search": self.outlook_search,
            "outlook_send": self.outlook_send,
        }

    def register(self, name: str, fn: Callable[..., Awaitable[Any]],
                 description: str, arguments: dict[str, Any]) -> None:
        """注册外部工具（如桥接层的 Copilot 文件上传 copilot_attach）。"""
        self._tools[name] = fn
        self._extra_descriptions.append(
            {"name": name, "description": description, "arguments": arguments})

    def set_root(self, value: str | Path, create: bool = False) -> str:
        target = Path(value).expanduser()
        target = target.resolve() if target.is_absolute() else (self.root / target).resolve()
        if self.workspace_roots and not any(target == r or r in target.parents
                                            for r in self.workspace_roots):
            raise ToolError("目标目录不在 workspace_roots 白名单内")
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
            {"name": "read_text", "description": "读取 UTF-8 文本；文件较大时用 offset 分段读取（每次建议≤5000字符）", "arguments": {"path": "相对路径", "offset": "字符偏移，默认0", "limit": "本次最多返回字符数，默认120000"}},
            {"name": "write_text", "description": "覆盖写入 UTF-8 文本；仅用于新文件或完整短文件", "arguments": {"path": "相对路径", "content": "文本"}},
            {"name": "append_text", "description": "追加普通 UTF-8 文本", "arguments": {"path": "相对路径", "content": "文本"}},
            {"name": "write_text_b64", "description": "Base64 解码后覆盖写入 UTF-8；代码、JSON、HTML、Markdown 必须用此工具", "arguments": {"path": "相对路径", "content_b64": "UTF-8 内容的 Base64"}},
            {"name": "append_text_b64", "description": "Base64 解码后追加 UTF-8；代码、JSON、HTML、Markdown 必须用此工具", "arguments": {"path": "相对路径", "content_b64": "UTF-8 内容的 Base64"}},
            {"name": "replace_text", "description": "在现有文件中精确替换短文本，修改代码时优先使用", "arguments": {"path": "相对路径", "old": "原文本", "new": "新文本", "count": "替换次数，默认1"}},
            {"name": "make_directory", "description": "创建目录", "arguments": {"path": "相对路径"}},
            {"name": "copy_path", "description": "复制文件或目录", "arguments": {"source": "相对路径", "destination": "相对路径"}},
            {"name": "move_path", "description": "移动文件或目录", "arguments": {"source": "相对路径", "destination": "相对路径"}},
            {"name": "validate_python", "description": "检查 Python 文件或目录语法；仅在文件全部写完后执行（写了一半的文件必然失败，不是缺陷）", "arguments": {"path": "相对路径，默认 ."}},
            {"name": "run_unittest", "description": "执行 unittest 测试发现；仅在文件全部写完后执行", "arguments": {"path": "测试目录，默认 tests", "pattern": "默认 test_*.py"}},
        ]
        if self.allow_delete:
            items.append({"name": "delete_path", "description": "删除路径", "arguments": {"path": "相对路径"}})
        if self.allow_python:
            items.append({"name": "run_python", "description": "运行短 Python 计算；禁止用于修改项目文件", "arguments": {"code": "短 Python 代码"}})
        if self.allow_office:
            items += [
                {"name": "read_excel", "description": "读取 Excel 工作表内容", "arguments": {"path": "相对路径", "sheet": "工作表名，默认全部", "max_rows": "每表最多行数，默认 200"}},
                {"name": "write_excel", "description": "向 Excel 写入二维行数据（不存在则创建）", "arguments": {"path": "相对路径", "sheet": "工作表名，默认当前表", "rows": "二维数组", "start_cell": "起始单元格，默认 A1"}},
                {"name": "read_docx", "description": "提取 Word 文档段落与表格文本", "arguments": {"path": "相对路径"}},
                {"name": "write_docx", "description": "创建或追加 Word 文档段落", "arguments": {"path": "相对路径", "paragraphs": "字符串数组", "mode": "append 或 overwrite，默认 append"}},
                {"name": "read_pptx", "description": "提取 PPT 每页文字与备注", "arguments": {"path": "相对路径"}},
                {"name": "read_pdf", "description": "提取 PDF 每页文字", "arguments": {"path": "相对路径"}},
            ]
        if self.allow_outlook:
            items += [
                {"name": "outlook_unread", "description": "列出 Outlook 收件箱未读邮件", "arguments": {"max_count": "最多条数，默认 20"}},
                {"name": "outlook_read", "description": "按 entry_id 读取邮件正文", "arguments": {"entry_id": "来自 outlook_unread/outlook_search"}},
                {"name": "outlook_search", "description": "按主题关键词搜索收件箱邮件", "arguments": {"keyword": "关键词", "max_count": "默认 20"}},
            ]
            if self.allow_outlook_send:
                items.append({"name": "outlook_send", "description": "发送邮件（会真实发出，谨慎）", "arguments": {"to": "收件人", "subject": "主题", "body": "正文", "cc": "抄送，可选"}})
        items += self._extra_descriptions
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
            try:
                stat = item.stat()  # 悬空符号链接会抛错，跳过而不是拖垮整列
                items.append({"name": item.name, "type": "directory" if item.is_dir() else "file", "size": stat.st_size})
            except OSError:
                items.append({"name": item.name, "type": "unknown", "size": 0})
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

    async def read_text(self, path: str, offset: int = 0, limit: int = 0) -> dict[str, Any]:
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        offset = max(0, self._int_arg(offset, 0, "offset"))
        limit = max(0, self._int_arg(limit, 0, "limit"))
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
        before=self._hash(target)
        # newline=""：原样写入，Windows 下不把 \n 静默转成 \r\n
        with target.open("w", encoding="utf-8", newline="") as f: f.write(content)
        after=self._hash(target)
        return {"path":path,"written_chars":len(content),"file_event":{"tool":"write_text","path":path,"before_hash":before,"after_hash":after,"result":"ok"}}

    async def append_text(self, path: str, content: str) -> dict[str, Any]:
        if len(content) > self.max_write:
            raise ToolError("追加内容超过限制")
        target = self._path(path)
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
        try:
            count = max(1, min(int(count), 100))
        except (TypeError, ValueError):
            raise ToolError("count 必须是整数")
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        # 严格解码 + newline=""：非 UTF-8 文件拒绝替换（避免乱码写回），
        # CRLF 文件保持 CRLF，不做换行符静默转换。
        try:
            with target.open("r", encoding="utf-8", errors="strict", newline="") as f:
                text = f.read()
        except UnicodeDecodeError as exc:
            raise ToolError(f"文件不是有效的 UTF-8 文本，拒绝替换（防止乱码写回）: {exc}")
        found = text.count(old)
        if found == 0:
            raise ToolError("未找到要替换的原文本，请先 read_text 后使用精确短片段")
        replaced = min(found, count)
        before=self._hash(target)
        with target.open("w", encoding="utf-8", newline="") as f: f.write(text.replace(old,new,count))
        after=self._hash(target)
        return {"path":path,"replaced":replaced,"remaining_matches":max(0,found-replaced),"file_event":{"tool":"replace_text","path":path,"before_hash":before,"after_hash":after,"result":"ok"}}

    async def make_directory(self, path: str) -> dict[str, Any]:
        target = self._path(path)
        target.mkdir(parents=True, exist_ok=True)
        return {"path": path, "created": True}

    async def copy_path(self, source: str, destination: str) -> dict[str, Any]:
        src, dst = self._path(source), self._path(destination)
        if not src.exists():
            raise ToolError(f"源路径不存在: {source}")
        if dst == src or src in dst.parents:
            raise ToolError("禁止把目录复制/移动到它自己的子目录里")
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

    # ── Office 文档工具（openpyxl / python-docx / python-pptx / pypdf）────

    def _office_allowed(self):
        if not self.allow_office:
            raise ToolError("Office 工具已在 config.json 中禁用（allow_office=false）")

    @staticmethod
    def _int_arg(value, default: int, name: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ToolError(f"{name} 必须是整数")

    async def read_excel(self, path: str, sheet: str = "", max_rows: int = 200) -> dict[str, Any]:
        self._office_allowed()
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        try:
            import openpyxl
        except ImportError:
            raise ToolError("缺少依赖 openpyxl，请运行 pip install openpyxl")
        limit = max(1, min(self._int_arg(max_rows, 200, "max_rows"), 2000))
        wb = openpyxl.load_workbook(target, data_only=True, read_only=True)
        try:
            if sheet and sheet not in wb.sheetnames:
                raise ToolError(f"工作表不存在: {sheet}，可用: {wb.sheetnames}")
            out = {}
            for name in ([sheet] if sheet else wb.sheetnames):
                ws = wb[name]
                rows, total = [], 0
                for r in ws.iter_rows(values_only=True):
                    total += 1
                    if len(rows) < limit:
                        rows.append(["" if c is None else str(c) for c in r])
                out[name] = {"rows": rows, "total_rows": total, "truncated": total > limit}
            return {"path": path, "sheets": out}
        finally:
            wb.close()

    async def write_excel(self, path: str, rows: list, sheet: str = "", start_cell: str = "A1") -> dict[str, Any]:
        self._office_allowed()
        if not isinstance(rows, list) or not rows:
            raise ToolError("rows 必须是非空二维数组")
        try:
            import openpyxl
            from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
        except ImportError:
            raise ToolError("缺少依赖 openpyxl，请运行 pip install openpyxl")
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = self._hash(target)
        try:
            wb = openpyxl.load_workbook(target) if target.is_file() else openpyxl.Workbook()
        except Exception as exc:
            raise ToolError(f"无法读取 Excel 文件（可能不是有效的 xlsx）: {exc}")
        try:
            if sheet:
                ws = wb[sheet] if sheet in wb.sheetnames else wb.create_sheet(sheet)
            else:
                ws = wb.active
            try:
                col_s, row_s = coordinate_from_string(start_cell)
                col0 = column_index_from_string(col_s)
            except Exception:
                raise ToolError(f"无效的起始单元格: {start_cell}")
            written = 0
            for ri, row in enumerate(rows):
                if not isinstance(row, list):
                    row = [row]
                for ci, value in enumerate(row):
                    ws.cell(row=row_s + ri, column=col0 + ci, value=value)
                    written += 1
            wb.save(target)
            title = ws.title
        finally:
            wb.close()
        return {"path": path, "sheet": title, "cells_written": written,
                "file_event": {"tool": "write_excel", "path": path,
                               "before_hash": before, "after_hash": self._hash(target), "result": "ok"}}

    async def read_docx(self, path: str) -> dict[str, Any]:
        self._office_allowed()
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        try:
            import docx
        except ImportError:
            raise ToolError("缺少依赖 python-docx，请运行 pip install python-docx")
        doc = docx.Document(target)
        parts = []
        for p in doc.paragraphs:
            if p.text.strip():
                parts.append(p.text)
        for ti, table in enumerate(doc.tables, 1):
            parts.append(f"[表格 {ti}]")
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        text = "\n".join(parts)
        return {"path": path, "paragraphs": len(doc.paragraphs), "tables": len(doc.tables),
                "content": text[:self.max_read], "truncated": len(text) > self.max_read}

    async def write_docx(self, path: str, paragraphs: list, mode: str = "append") -> dict[str, Any]:
        self._office_allowed()
        if not isinstance(paragraphs, list) or not paragraphs:
            raise ToolError("paragraphs 必须是非空字符串数组")
        if mode not in ("append", "overwrite"):
            raise ToolError("mode 只能是 append 或 overwrite")
        try:
            import docx
        except ImportError:
            raise ToolError("缺少依赖 python-docx，请运行 pip install python-docx")
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = self._hash(target)
        if mode == "overwrite" or not target.is_file():
            doc = docx.Document()
        else:
            doc = docx.Document(target)
        for text in paragraphs:
            doc.add_paragraph(str(text))
        doc.save(target)
        return {"path": path, "mode": mode, "paragraphs_added": len(paragraphs),
                "file_event": {"tool": "write_docx", "path": path,
                               "before_hash": before, "after_hash": self._hash(target), "result": "ok"}}

    async def read_pptx(self, path: str) -> dict[str, Any]:
        self._office_allowed()
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        try:
            from pptx import Presentation
        except ImportError:
            raise ToolError("缺少依赖 python-pptx，请运行 pip install python-pptx")
        prs = Presentation(target)
        slides = []
        for i, slide in enumerate(prs.slides, 1):
            texts = [shape.text for shape in slide.shapes if shape.has_text_frame and shape.text.strip()]
            notes = ""
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                notes = slide.notes_slide.notes_text_frame.text.strip()
            slides.append({"slide": i, "text": "\n".join(texts), "notes": notes})
        total_chars = sum(len(s["text"]) + len(s["notes"]) for s in slides)
        return {"path": path, "slides": slides, "slide_count": len(slides),
                "truncated": total_chars > self.max_read}

    async def read_pdf(self, path: str) -> dict[str, Any]:
        self._office_allowed()
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ToolError("缺少依赖 pypdf，请运行 pip install pypdf")
        reader = PdfReader(str(target))
        pages = []
        used = 0
        truncated = False
        for i, page in enumerate(reader.pages, 1):
            if used >= self.max_read:
                truncated = True
                break
            text = (page.extract_text() or "").strip()
            used += len(text)
            pages.append({"page": i, "text": text})
        return {"path": path, "page_count": len(reader.pages), "pages_read": len(pages),
                "pages": pages, "truncated": truncated or len(pages) < len(reader.pages)}

    # ── Outlook COM 工具（pywin32，需本机 Outlook 已登录）────────────────

    async def _outlook_run(self, fn):
        if not self.allow_outlook:
            raise ToolError("Outlook 工具已在 config.json 中禁用（allow_outlook=false）")

        def runner():
            try:
                import pythoncom
                import win32com.client
            except ImportError:
                raise ToolError("缺少依赖 pywin32，请运行 pip install pywin32")
            pythoncom.CoInitialize()
            try:
                app = win32com.client.Dispatch("Outlook.Application")
                ns = app.GetNamespace("MAPI")
                return fn(ns)
            except ToolError:
                raise
            except Exception as exc:
                raise ToolError(self._explain_outlook_error(exc))
            finally:
                pythoncom.CoUninitialize()

        # COM 弹安全确认框或 Outlook 脱机时会永久阻塞，加超时保护
        timeout = max(self.python_timeout, 30) * 2
        for attempt in (1, 2):
            try:
                return await asyncio.wait_for(asyncio.to_thread(runner), timeout=timeout)
            except ToolError:
                raise
            except Exception as exc:
                if attempt == 1 and self._busy_hresult(exc):
                    await asyncio.sleep(2.0)  # Outlook 繁忙瞬时拒绝：退后重试一次
                    continue
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                    raise ToolError("调用 Outlook 超时（40 秒无响应）——Outlook 可能弹出了安全确认框、"
                                    "处于脱机或卡死状态；请在本机桌面确认 Outlook 后重试")
                raise ToolError(self._explain_outlook_error(exc))

    @staticmethod
    def _busy_hresult(exc) -> bool:
        # RPC_E_CALL_REJECTED / RPC_E_SERVERCALL_RETRYLATER：Outlook 正忙
        try:
            code = int(exc.args[0]) & 0xFFFFFFFF
        except Exception:
            return False
        return code in (0x80010001, 0x80010102)

    @staticmethod
    def _explain_outlook_error(exc) -> str:
        """把 COM 异常翻译成可操作的诊断信息——'已登录但不可用'几乎都是这几类。"""
        hresult = None
        try:
            hresult = int(exc.args[0]) & 0xFFFFFFFF
        except Exception:
            pass
        known = {
            0x80040154: "未注册 Outlook COM 类——本机未安装 Outlook/Office",
            0x80080005: "Outlook 拒绝本次自动化调用——最常见于 Outlook 与服务的运行权限不一致"
                        "（一个管理员运行、一个非管理员运行）或不同 Windows 用户；"
                        "Outlook 与服务需在相同用户、相同提权状态下运行",
            0x8000401A: "当前会话无法启动 Outlook（非交互式会话）——请在已登录用户的桌面会话中运行服务",
            0x80070005: "访问被拒绝——Outlook 安全策略或杀毒软件拦截了 COM 自动化",
            0x80010105: "与 Outlook 的 RPC 通信失败——Outlook 可能卡死，重启 Outlook 后重试",
        }
        if hresult:
            hint = known.get(hresult, "未识别的 COM 错误，请把本段发给维护者")
            return f"Outlook 不可用：{hint}（0x{hresult:08X}: {exc}）"
        return f"Outlook 不可用（未安装、未启动或未登录账号）: {exc}"

    @staticmethod
    def _mail_brief(item) -> dict[str, Any]:
        return {"entry_id": item.EntryID, "subject": str(item.Subject or ""),
                "sender": str(item.SenderName or ""), "received": str(item.ReceivedTime),
                "unread": bool(item.UnRead)}

    async def outlook_unread(self, max_count: int = 20) -> dict[str, Any]:
        limit = max(1, min(self._int_arg(max_count, 20, "max_count"), 100))

        def work(ns):
            items = ns.GetDefaultFolder(6).Items.Restrict("[Unread] = true")  # 6=olFolderInbox
            items.Sort("[ReceivedTime]", True)
            out = []
            for item in items:
                if len(out) >= limit:
                    break
                out.append(self._mail_brief(item))
            return out

        mails = await self._outlook_run(work)
        return {"unread_returned": len(mails), "items": mails}

    async def outlook_read(self, entry_id: str) -> dict[str, Any]:
        if not entry_id:
            raise ToolError("entry_id 不能为空")

        def work(ns):
            item = ns.GetItemFromID(entry_id)
            body = str(item.Body or "")
            brief = self._mail_brief(item)
            brief["body"] = body[:20000]
            brief["truncated"] = len(body) > 20000
            return brief

        return await self._outlook_run(work)

    async def outlook_search(self, keyword: str, max_count: int = 20) -> dict[str, Any]:
        if not keyword:
            raise ToolError("keyword 不能为空")
        limit = max(1, min(self._int_arg(max_count, 20, "max_count"), 100))
        safe = str(keyword).replace("'", "''")

        def work(ns):
            items = ns.GetDefaultFolder(6).Items.Restrict(
                f"@SQL=\"urn:schemas:httpmail:subject\" LIKE '%{safe}%'")
            items.Sort("[ReceivedTime]", True)
            out = []
            for item in items:
                if len(out) >= limit:
                    break
                out.append(self._mail_brief(item))
            return out

        mails = await self._outlook_run(work)
        return {"keyword": keyword, "returned": len(mails), "items": mails}

    async def outlook_send(self, to: str, subject: str, body: str, cc: str = "") -> dict[str, Any]:
        if not self.allow_outlook_send:
            raise ToolError("发送邮件已在 config.json 中禁用（allow_outlook_send=false）")
        if not to or not subject:
            raise ToolError("to 和 subject 不能为空")

        def work(ns):
            app = ns.Application
            mail = app.CreateItem(0)  # 0=olMailItem
            mail.To = to
            if cc:
                mail.CC = cc
            mail.Subject = subject
            mail.Body = body
            mail.Send()
            return True

        await self._outlook_run(work)
        return {"sent": True, "to": to, "subject": subject}
