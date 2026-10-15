

import asyncio
import base64
import hashlib
import json
import queue
import inspect
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable


class ToolError(RuntimeError):
    pass


def parse_skill_md(dirname: str, text: str) -> tuple[str, str]:
    """SKILL.md 元数据：优先 YAML frontmatter 的 name/description，
    退回第一级标题 + 首个非空段落。description 单行封顶 120 字符。"""
    name, desc, body = dirname, "", text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip() or name
            elif line.startswith("description:"):
                desc = line.split(":", 1)[1].strip()
        body = text[m.end():]
    if not desc:
        hm = re.search(r"^#\s+(.+)$", body, re.M)
        if hm:
            name = hm.group(1).strip() or name
            rest = body[hm.end():]
        else:
            rest = body
        para = next((p.strip() for p in rest.split("\n\n")
                     if p.strip() and not p.strip().startswith("#")), "")
        desc = " ".join(para.split())[:120]
    return name, desc[:120]



# ── 工具分组：按任务裁剪注入的工具清单（P0-1）─────────────────────
# 30 个工具的 schema 每回合全量注入纯属烧 token；单条任务通常只用少数几组。
TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "core": ("list_directory", "find_files", "read_text", "write_text", "append_text",
             "write_text_b64", "append_text_b64", "replace_text", "make_directory",
             "copy_path", "move_path", "validate_python", "run_unittest",
             "skill_list", "skill_read", "skill_create",
             "memory_write", "memory_search", "memory_forget", "tool_list"),
    "delete": ("delete_path",),
    "python": ("run_python",),
    "office": ("read_excel", "write_excel", "read_docx", "write_docx", "read_pptx", "read_pdf"),
    "outlook": ("outlook_unread", "outlook_read", "outlook_search", "outlook_send"),
    "copilot": ("copilot_attach", "copilot_file"),
    "self": ("self_read_text", "self_write_text_b64", "self_append_text_b64",
             "self_replace_text", "self_validate", "self_unittest", "self_rollback"),
}

GROUP_KEYWORDS: dict[str, tuple[str, ...]] = {
    "office": ("excel", "xlsx", "表格", "word", "docx", "文档", "ppt", "pptx", "幻灯片", "pdf", "报表"),
    "outlook": ("邮件", "outlook", "收件箱", "未读", "发信", "mail"),
    "copilot": ("图片", "截图", "扫描件", "附件", "上传给 copilot", "由 copilot", "copilot 生成"),
    "delete": ("删除", "清理", "移除"),
    "python": ("python 脚本", "脚本处理", "批量处理", "计算"),
}


def tool_group(name: str) -> str:
    """工具所属分组；未登记的（含外部注册工具）归入 core，保证不会被裁掉。"""
    for group, members in TOOL_GROUPS.items():
        if name in members:
            return group
    return "core"

class _ComWorker:
    """专用 COM 线程：常驻 + Windows 消息泵，Outlook COM 调用全部串行在它上面执行。

    为什么不用线程池：Outlook COM 从后台线程调用且该线程没有消息泵时，
    需要 UI 交互的 MAPI 调用会以 0x80004004 (E_ABORT, Operation aborted)
    中止。主线程自检能通过是因为控制台主线程自带消息泵；线程池线程没有。
    单 COM 线程 + PumpWaitingMessages 是 Office 自动化从服务/异步代码
    调用的标准做法（同时天然串行化并发 Outlook 操作）。
    """

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def get(cls) -> "_ComWorker":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self._queue: "queue.Queue" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="outlook-com", daemon=True)
        self._thread.start()

    def _run(self):
        pythoncom = None
        try:
            import pythoncom
            pythoncom.CoInitializeEx(0)
        except Exception:
            return  # pywin32 缺失：submit 侧会因超时得到明确错误
        while True:
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                try:
                    pythoncom.PumpWaitingMessages()
                except Exception:
                    pass
                continue
            if item is None:
                break
            fn, loop, fut = item
            try:
                result = fn()
            except Exception as exc:
                self._dispatch(loop, fut, exc=exc)
            else:
                self._dispatch(loop, fut, result=result)
            try:
                pythoncom.PumpWaitingMessages()
            except Exception:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

    @staticmethod
    def _dispatch(loop, fut, result=None, exc=None):
        try:
            if loop.is_closed():
                return
            if exc is not None:
                loop.call_soon_threadsafe(fut.set_exception, exc)
            else:
                loop.call_soon_threadsafe(fut.set_result, result)
        except Exception:
            pass

    def submit(self, fn):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._queue.put((fn, loop, fut))
        return fut


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
        # 技能写入：项目级技能默认允许（只影响当前工作区）；全局级需 allow_self_upgrade
        self.allow_skill_write = bool(config.get("allow_skill_write", True))
        # 长期记忆：跨会话累积的事实/偏好/结论（cbp/memory.py）
        self.allow_memory_write = bool(config.get("allow_memory_write", True))
        self.memory = None            # 由桥接层注入 MemoryStore
        self.memory_scope = "global"  # 当前工作区作用域名，由桥接层按 root 设定
        # Outlook 后端：bridge 层注册的 OWA 网页自动化后端；None 时走 COM（经典 Outlook）
        self.outlook_backend = None
        self.python_timeout = float(config.get("python_timeout_seconds", 20))
        # 工作区根目录白名单：配置后 set_root 只能切到这些根目录之下，
        # 防止 API 把工具根目录移到 C:\ 之类的地方使路径沙箱形同虚设。
        self.workspace_roots = [
            Path(p).expanduser().resolve()
            for p in (config.get("workspace_roots") or [])
        ]
        self._extra_descriptions: list[dict[str, Any]] = []
        self._snapshot_hook: Callable[[str, str, Path], None] | None = None
        # 自升级回滚通道：由桥接层绑定为"恢复服务自身代码快照"的可调用对象
        self.self_rollback_fn: Callable[[], Any] | None = None
        # ── 全局作用域（服务安装目录）与自升级 ───────────────────────────
        # global_root：服务安装目录，存放全局 AGENT.md / skills（对所有工作区生效）
        # allow_self_upgrade：允许 Agent 修改服务自身代码（默认关闭）
        # self_whitelist：自升级可触达的路径前缀（安装目录内相对路径）
        self.global_root: Path | None = None
        self.allow_self_upgrade = bool(config.get("allow_self_upgrade", False))
        self.self_whitelist = tuple(config.get("self_upgrade_paths") or (
            "cbp/", "webui/", "scripts/", "docs/", "tests/", "skills/",
            "app.py", "start.py", "install.py", "requirements.txt",
            "AGENT.md", "README.md", "config.example.json",
        )) if self.allow_self_upgrade else ()
        # "需要 Copilot 网页会话"的工具：provider（API）会话里禁用（见 set_web_copilot_tools）
        self.web_copilot_tools = ("copilot_attach", "copilot_file")
        self.allow_web_copilot_tools = True
        self.self_denylist = ("config.json", "data/", "EdgeProfile/", "for debug/", ".git/", "__pycache__/")
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
            "skill_list": self.skill_list,
            "skill_read": self.skill_read,
            "skill_create": self.skill_create,
            "tool_list": self.tool_list,
            "memory_write": self.memory_write,
            "memory_search": self.memory_search,
            "memory_forget": self.memory_forget,
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
        self._extra_descriptions.append({
            "name": "tool_list", "arguments": {},
            "description": "列出全部可用工具与分组（提示词里只列了与当前任务相关的工具，需要其它工具时可直接调用）"})
        if self.allow_self_upgrade:
            self._tools.update({
                "self_read_text": self.self_read_text,
                "self_write_text_b64": self.self_write_text_b64,
                "self_append_text_b64": self.self_append_text_b64,
                "self_replace_text": self.self_replace_text,
                "self_validate": self.self_validate,
                "self_unittest": self.self_unittest,
                "self_rollback": self.self_rollback,
            })

    def register(self, name: str, fn: Callable[..., Awaitable[Any]],
                 description: str, arguments: dict[str, Any]) -> None:
        """注册外部工具（如桥接层的 Copilot 文件上传 copilot_attach）。"""
        self._tools[name] = fn
        self._extra_descriptions.append(
            {"name": name, "description": description, "arguments": arguments})

    def set_web_copilot_tools(self, allowed: bool) -> None:
        """启用/禁用"需要 Copilot 网页会话"的工具（copilot_attach/copilot_file）。

        API 提供商（deepseek/… 等）会话没有 Copilot 网页对话可驱动，这两个工具
        注定失败。实测真实任务里 copilot_file 连续失败 10 次、白烧回合——因此
        provider 会话里既不注入也不执行（执行时给可自救的错误信息）。
        """
        self.allow_web_copilot_tools = bool(allowed)

    def set_snapshot_hook(self, fn: Callable[[str, str, Path], None] | None) -> None:
        """设置写操作前的快照钩子 fn(tool, relative_path, absolute_target)。

        由桥接层绑定当前 Session 的快照存储，用于"回滚整个任务的改动"。
        钩子内部异常不会影响写入本身。
        """
        self._snapshot_hook = fn

    def _snap(self, tool: str, rel: str, target: Path) -> None:
        hook = getattr(self, "_snapshot_hook", None)
        if not hook:
            return
        try:
            hook(tool, rel, target)
        except Exception:
            pass

    # ── 全局与项目两级 skills ────────────────────────────────────────
    def set_global_root(self, root: "str | Path | None") -> None:
        """设置服务安装目录：全局 AGENT.md / skills 所在处，对所有工作区生效。"""
        self.global_root = Path(root).resolve() if root else None

    def _skill_dirs(self) -> list[tuple[str, Path]]:
        out = []
        if self.global_root:
            d = self.global_root / "skills"
            if d.is_dir():
                out.append(("global", d))
        d = self.root / "skills"
        if d.is_dir():
            out.append(("project", d))
        return out

    def tool_list(self) -> dict[str, Any]:
        """列出全部可用工具（含当前未在提示词中列出的组），供按需发现。"""
        allitems = self._descriptions_all()
        buckets: dict[str, list[str]] = {}
        for x in sorted(allitems, key=lambda y: y["name"]):
            buckets.setdefault(tool_group(x["name"]), []).append(x["name"])
        return {"total": len(allitems), "groups": buckets,
                "note": "提示词里只列了与当前任务相关的工具；需要其它工具时直接调用即可。"}

    def skill_list(self) -> list[dict[str, Any]]:
        """两级技能清单：项目同名技能覆盖全局。"""
        merged: dict[str, dict[str, Any]] = {}
        for scope, d in self._skill_dirs():
            try:
                for sub in sorted(d.iterdir()):
                    if not sub.is_dir():
                        continue
                    md = sub / "SKILL.md"
                    if not md.is_file():
                        continue
                    text = md.read_text(encoding="utf-8", errors="replace")
                    name, desc = parse_skill_md(sub.name, text)
                    merged[name] = {"name": name, "description": desc, "scope": scope,
                                    "path": f"skills/{sub.name}/SKILL.md", "abs": str(md)}
            except Exception:
                continue
        return list(merged.values())[:30]

    async def skill_read(self, name: str) -> dict[str, Any]:
        """读取技能全文。全局技能位于工作区之外，read_text 受沙箱限制读不到，
        必须用本工具读取。"""
        if not name:
            raise ToolError("name 不能为空")
        key = str(name).strip().lower()
        for skill in self.skill_list():
            dirname = Path(skill["path"]).parent.name.lower()
            if skill["name"].lower() == key or dirname == key:
                p = Path(skill["abs"])
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:
                    raise ToolError(f"读取技能失败: {exc}") from exc
                cap = 12000
                return {"name": skill["name"], "scope": skill["scope"], "path": skill["path"],
                        "content": text if len(text) <= cap
                        else text[:cap] + f"\n…[共 {len(text)} 字符，已截断]"}
        raise ToolError(f"未找到技能: {name}（可用 skill_list 查看可用技能）")

    async def skill_create(self, name: str, content: str, description: str = "",
                           scope: str = "project", overwrite: bool = False) -> dict[str, Any]:
        """把任务中验证有效的做法沉淀为技能（SKILL.md）。

        scope=project（默认）写入工作区 skills/，只影响该工作区的任务；
        scope=global 写入服务安装目录 skills/，对所有工作区生效，因此要求
        allow_self_upgrade（与修改服务自身代码同级权限）。

        约定：目录名 = 技能名（小写短横线），SKILL.md 用 YAML frontmatter
        声明 name/description，正文写"什么时候用、怎么做、坑在哪"。
        """
        if not self.allow_skill_write:
            raise ToolError("技能写入已在 config.json 中禁用（allow_skill_write=false）")
        slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
        if not slug:
            raise ToolError("技能名不合法：请用小写字母/数字/短横线，如 webui-theme-change")
        if not content or not str(content).strip():
            raise ToolError("content 不能为空")
        if len(content) > self.max_write:
            raise ToolError("技能内容超过写入上限")
        scope_key = str(scope or "project").strip().lower()
        if scope_key in ("project", "workspace"):
            base = self.root / "skills"
        elif scope_key in ("global", "service"):
            if not self.allow_self_upgrade:
                raise ToolError("写入全局技能需要开启 allow_self_upgrade（影响所有工作区）")
            if not self.global_root:
                raise ToolError("未设置服务安装目录，无法写全局技能")
            base = self.global_root / "skills"
        else:
            raise ToolError("scope 只能是 project 或 global")

        target = (base / slug / "SKILL.md").resolve()
        if not overwrite and target.is_file():
            raise ToolError(f"技能已存在: {slug}（要覆盖请带 overwrite=true，或先用 skill_read 看现状）")

        desc_line = " ".join(str(description or "").split())[:120]
        if not desc_line:
            # 没给描述就从正文提炼：跳过标题/字段标签/过短行，优先取足够长的句子，
            # 避免把「什么时候用」这类小标题当成技能简介。
            candidates = []
            for line in str(content).splitlines():
                s = " ".join(line.split())
                if not s or s.startswith("#") or s.startswith("---") or s.startswith(">"):
                    continue
                if s.endswith(":") or s.endswith("："):
                    continue
                candidates.append(s)
            for cand in candidates:
                if len(cand) >= 8:
                    desc_line = cand[:120]
                    break
            if not desc_line and candidates:
                desc_line = " ".join(candidates[:2])[:120]
        body = str(content).strip()
        text = f"---\nname: {slug}\ndescription: {desc_line}\n---\n\n{body}\n"

        self._snap("skill_create", target.relative_to(self.root).as_posix()
                   if scope_key == "project" else target.relative_to(self.global_root).as_posix(),
                   target)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = self._hash(target)
        with target.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
        parsed_name, parsed_desc = parse_skill_md(slug, text)
        return {"created": True, "name": parsed_name, "slug": slug, "scope": scope_key,
                "path": str(target), "description": parsed_desc, "chars": len(text),
                "note": "技能即时可用（下次 skill_list/任务上下文即出现），无需重启服务",
                "file_event": {"tool": "skill_create", "path": str(target),
                               "before_hash": before, "after_hash": self._hash(target), "result": "ok"}}

    # ── 长期记忆 ────────────────────────────────────────────────────
    def _mem_scopes(self, scope: str = "all") -> list[str]:
        key = str(scope or "all").strip().lower()
        if key.startswith("glob"):
            return ["global"]
        if key.startswith("proj") or key.startswith("work"):
            return [self.memory_scope]
        return ["global", self.memory_scope]

    def _mem(self):
        if self.memory is None:
            raise ToolError("长期记忆未启用（服务未注入 MemoryStore）")
        return self.memory

    async def memory_write(self, text: str, kind: str = "fact", tags: list | None = None,
                           pinned: bool = False, scope: str = "global") -> dict[str, Any]:
        """记录一条长期记忆：跨会话累积的事实、偏好、结论与踩坑。"""
        if not self.allow_memory_write:
            raise ToolError("记忆写入已在 config.json 中禁用（allow_memory_write=false）")
        if isinstance(tags, str):
            tags = [t.strip() for t in re.split(r"[,，]", tags) if t.strip()]
        target = "global" if str(scope or "global").lower().startswith("glob") else self.memory_scope
        try:
            r = self._mem().remember(target, text, kind=kind, tags=list(tags or []),
                                     source_session=getattr(self, "current_session_id", None),
                                     pinned=bool(pinned))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        r["note"] = "已写入长期记忆：后续相关任务会自动检索到（无需重启）"
        return r

    async def memory_search(self, query: str = "", limit: int = 8, scope: str = "all") -> dict[str, Any]:
        """检索长期记忆。query 为空时返回最近更新的若干条。"""
        limit = max(1, min(self._int_arg(limit, 8, "limit"), 30))
        scopes = self._mem_scopes(scope)
        store = self._mem()
        items = (store.search(query, scopes, limit=limit) if str(query or "").strip()
                 else store.list(scopes, limit=limit))
        return {"query": query, "checked_scopes": scopes, "returned": len(items),
                "items": [{"id": m["id"], "kind": m["kind"], "text": m["text"],
                           "tags": m.get("tags") or [], "pinned": bool(m["pinned"]),
                           "use_count": m["use_count"], "scope": m["scope"]} for m in items]}

    async def memory_forget(self, id: int) -> dict[str, Any]:
        """删除一条记忆（只允许删当前任务可见作用域内的）。"""
        mid = self._int_arg(id, 0, "id")
        if mid <= 0:
            raise ToolError("id 必须是正整数")
        ok = self._mem().forget(mid, scopes=["global", self.memory_scope])
        if not ok:
            raise ToolError(f"未找到可删除的记忆 id={mid}（可能属于别的工作区）")
        return {"forgot": mid}

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

    def set_root(self, value: str | Path, create: bool = False) -> str:
        target = Path(value).expanduser()
        target = target.resolve() if target.is_absolute() else (self.root / target).resolve()
        if self.workspace_roots and not any(target == r or r in target.parents
                                            for r in self.workspace_roots):
            raise ToolError("目标目录不在 workspace_roots 白名单内")
        # 权限门：工作区若覆盖服务安装目录，普通文件工具就能直接改服务代码/
        # config.json/data —— 等于绕过 allow_self_upgrade、self 白名单与自升级
        # 验证闭环。因此这种工作区必须显式开启自升级权限才允许。
        if self.global_root and not self.allow_self_upgrade:
            install = Path(self.global_root).resolve()
            try:
                install.relative_to(target)
                raise ToolError(
                    f"工作目录 {target} 覆盖了服务安装目录 {install}："
                    "把工作区设成服务自身目录需要先开启 allow_self_upgrade"
                    "（否则可绕过自升级权限与验证门直接改服务代码），"
                    "请改用独立工作区（如 workspace/）")
            except ValueError:
                pass
        if create:
            target.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            raise ToolError(f"工作目录不存在: {target}")
        if not target.is_dir():
            raise ToolError(f"工作目录不是文件夹: {target}")
        self.root = target
        return str(self.root)

    def descriptions(self, only: "set[str] | None" = None) -> list[dict[str, Any]]:
        """工具清单；only 非空时只返回其中的工具（按任务裁剪，省提示词 token）。

        裁剪只影响"列出来给模型看什么"：执行时不校验 only，模型凭记忆调用未列出的
        工具依旧可用（隐藏≠禁用），避免裁剪把能力砍掉。
        """
        items = self._descriptions_all()
        if only:
            items = [x for x in items if x.get("name") in only]
        return items

    def _descriptions_all(self) -> list[dict[str, Any]]:
        items = [
            {"name": "list_directory", "description": "列出工作区目录", "arguments": {"path": "相对路径，默认 ."}},
            {"name": "find_files", "description": "按 glob 查找文件", "arguments": {"pattern": "如 **/*.log"}},
            {"name": "read_text", "description": "读取 UTF-8 文本；默认读全文件（上限 max_read_chars），文件超大时用 offset/limit 分段（每段可到 20000 字符，别分太碎）", "arguments": {"path": "相对路径", "offset": "字符偏移，默认0", "limit": "本次最多返回字符数，默认120000"}},
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
                {"name": "outlook_search", "description": "按主题关键词和日期搜索指定文件夹（inbox 收件箱/sent 已发送/outbox/deleted/drafts）", "arguments": {"keyword": "主题关键词，可留空", "folder": "文件夹，默认 inbox", "since": "起始日期 YYYY-MM-DD，可留空", "max_count": "默认 20"}},
            ]
            if self.allow_outlook_send:
                items.append({"name": "outlook_send", "description": "发送邮件（会真实发出，谨慎）", "arguments": {"to": "收件人", "subject": "主题", "body": "正文", "cc": "抄送，可选"}})
        if self._skill_dirs():
            items += [
                {"name": "skill_list", "description": "列出可用技能（全局 + 项目两级，项目同名覆盖全局）", "arguments": {}},
                {"name": "skill_read", "description": "读取技能 SKILL.md 全文（全局技能在工作区外，只能用此工具读取）", "arguments": {"name": "技能名"}},
                {"name": "memory_write", "description": "记录一条跨会话有用的长期记忆（事实/偏好/决定/踩坑）。只在确实对后续任务有价值时写，写入即去重合并；scope=global 对所有任务可见，project 仅当前工作区", "arguments": {"text": "一条自足的事实描述（不要写'本次任务'这类指代）", "kind": "fact|preference|decision|pitfall|convention", "tags": "标签数组，可省略", "pinned": "是否常驻提示词（只在极重要时用）", "scope": "global（默认）或 project"}},
                {"name": "memory_search", "description": "检索长期记忆（关键词匹配，返回最相关若干条）", "arguments": {"query": "检索词", "limit": "返回条数，默认 8", "scope": "global|project|all，默认 all"}},
                {"name": "memory_forget", "description": "删除一条过时或错误的长期记忆", "arguments": {"id": "记忆 ID"}},
                {"name": "skill_create", "description": "把任务中验证有效的做法沉淀为技能（SKILL.md）。scope=project 写工作区 skills/（默认）；scope=global 写服务安装目录 skills/（需自升级权限）；已存在需 overwrite=true", "arguments": {"name": "技能名，小写短横线如 webui-theme-change", "content": "技能正文（Markdown：什么时候用/步骤/坑）", "description": "一句话描述，可省略（从正文首行提炼）", "scope": "project（默认）或 global", "overwrite": "是否覆盖已有技能，默认 false"}},
            ]
        if self.allow_self_upgrade:
            items += [
                {"name": "self_read_text", "description": "【自升级】读取服务自身代码（安装目录相对路径，白名单内）", "arguments": {"path": "如 cbp/agent.py", "offset": "字符偏移，默认0", "limit": "默认全文件"}},
                {"name": "self_write_text_b64", "description": "【自升级】覆盖写入服务自身文件；代码必须 Base64 分块（每块≤3000字符），全部写完后用 self_validate + self_unittest 验证", "arguments": {"path": "安装目录相对路径", "content_b64": "Base64"}},
                {"name": "self_append_text_b64", "description": "【自升级】追加写入服务自身文件（分块续写用）", "arguments": {"path": "安装目录相对路径", "content_b64": "Base64"}},
                {"name": "self_replace_text", "description": "【自升级】修改服务自身代码时优先用精确替换", "arguments": {"path": "安装目录相对路径", "old": "原文本", "new": "新文本", "count": "默认1"}},
                {"name": "self_validate", "description": "【自升级】对整个服务安装目录做语法检查；自升级必须通过", "arguments": {}},
                {"name": "self_unittest", "description": "【自升级】在服务安装目录跑单元测试；自升级必须通过", "arguments": {"pattern": "默认 test_*.py"}},
                {"name": "self_rollback", "description": "【自升级】回滚本次任务对服务自身代码的改动（恢复到任务开始前）", "arguments": {}},
            ]
        extra = self._extra_descriptions
        if not self.allow_web_copilot_tools:
            extra = [x for x in extra if x.get("name") not in self.web_copilot_tools]
        items += extra
        return items

    def _path(self, value: str = ".") -> Path:
        candidate = (self.root / value).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ToolError("拒绝访问工作区之外的路径") from exc
        # 纵深防御：工作区覆盖服务安装目录时（已要求 allow_self_upgrade），
        # 敏感路径仍按自升级黑名单拒绝——config.json/data/EdgeProfile 不该
        # 因为"换个工作目录"就变成可写。
        if self.global_root:
            try:
                rel = candidate.relative_to(Path(self.global_root).resolve()).as_posix()
            except ValueError:
                rel = ""
            if rel:
                for deny in self.self_denylist:
                    if deny.endswith("/"):
                        if rel.startswith(deny) or rel == deny.rstrip("/"):
                            raise ToolError(f"该路径属于服务敏感目录，禁止通过工作区工具访问: {rel}")
                    elif rel == deny:
                        raise ToolError(f"该路径属于服务敏感文件，禁止通过工作区工具访问: {rel}")
        return candidate

    async def execute(self, name: str, arguments: dict[str, Any] | None) -> Any:
        if not self.allow_web_copilot_tools and name in self.web_copilot_tools:
            raise ToolError(
                f"{name} 需要 Copilot 网页会话，当前会话用的是 API 提供商模型（如 deepseek/…），"
                "该工具不可用。请改用 write_text_b64/append_text_b64 分块写入新文件、"
                "replace_text 修改现有内容。")
        fn = self._tools.get(name)
        if not fn:
            raise ToolError(f"未知工具: {name}")
        try:
            result = fn(**(arguments or {}))
            # 工具可以同步也可以异步：skill_list 等纯计算工具是同步函数，
            # 无条件 await 会报 "object list can't be used in 'await' expression"，
            # 使这些工具经 Agent 调用时永远失败。
            if inspect.isawaitable(result):
                return await result
            return result
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
        self._snap("write_text", path, target)
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
        old_used, new_used = old, new
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
            raise ToolError("未找到要替换的原文本，请先 read_text 后使用精确短片段"
                            + self._nearest_hint(text, old))
        replaced = min(found, count)
        before=self._hash(target)
        self._snap("replace_text", path, target)
        with target.open("w", encoding="utf-8", newline="") as f: f.write(text.replace(old_used,new_used,count))
        after=self._hash(target)
        return {"path":path,"replaced":replaced,"remaining_matches":max(0,found-replaced),"file_event":{"tool":"replace_text","path":path,"before_hash":before,"after_hash":after,"result":"ok"}}

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
        self._snap("write_excel", path, target)
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
        self._snap("write_docx", path, target)
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
                import win32com.client
            except ImportError:
                raise ToolError("缺少依赖 pywin32，请运行 pip install pywin32")
            # COM 线程初始化由 _ComWorker 统一负责（CoInitializeEx + 消息泵），
            # 这里不再每调用 CoInitialize/CoUninitialize，避免破坏公寓状态。
            try:
                app = win32com.client.Dispatch("Outlook.Application")
                ns = app.GetNamespace("MAPI")
                return fn(ns)
            except ToolError:
                raise
            except Exception as exc:
                raise ToolError(self._explain_outlook_error(exc))

        # COM 弹安全确认框或 Outlook 脱机时会永久阻塞，加超时保护
        timeout = max(self.python_timeout, 30) * 2
        worker = _ComWorker.get()
        for attempt in (1, 2):
            try:
                return await asyncio.wait_for(worker.submit(runner), timeout=timeout)
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
            0x80004004: "操作被中止——Outlook 忙、有模态对话框或安全确认被取消/超时；稍后重试，"
                        "持续出现请重启 Outlook（outlook_search 已改为本地过滤，不再触发此类查询中止）",
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

    def _owa_or_none(self):
        """已注册 OWA 后端时返回它；同时校验开关。"""
        if self.outlook_backend is None:
            return None
        if not self.allow_outlook:
            raise ToolError("Outlook 工具已在 config.json 中禁用（allow_outlook=false）")
        return self.outlook_backend

    async def outlook_unread(self, max_count: int = 20) -> dict[str, Any]:
        limit = max(1, min(self._int_arg(max_count, 20, "max_count"), 100))
        backend = self._owa_or_none()
        if backend is not None:
            return await backend.unread(limit)

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
        backend = self._owa_or_none()
        if backend is not None:
            # OWA 后端没有 entry_id 概念，按主题关键词定位
            return await backend.read(entry_id)

        def work(ns):
            item = ns.GetItemFromID(entry_id)
            body = str(item.Body or "")
            brief = self._mail_brief(item)
            brief["body"] = body[:20000]
            brief["truncated"] = len(body) > 20000
            return brief

        return await self._outlook_run(work)

    async def outlook_search(self, keyword: str = "", folder: str = "inbox",
                             since: str = "", max_count: int = 20) -> dict[str, Any]:
        """按关键词/日期搜索指定邮件文件夹（收件箱、已发送等）。

        不再用 MAPI DASL @SQL 查询：它在部分环境会抛 0x80004004
        (Operation aborted) 且无规律可循；改为本地迭代过滤，行为确定。
        OWA 后端注册时优先走网页版（folder/since 参数暂不支持，忽略）。
        """
        limit = max(1, min(self._int_arg(max_count, 20, "max_count"), 100))
        keyword = str(keyword or "").strip()
        backend = self._owa_or_none()
        if backend is not None:
            if not keyword:
                raise ToolError("OWA 模式下 outlook_search 需要 keyword")
            return await backend.search(keyword, limit)
        folder_key = str(folder or "inbox").strip().lower()
        folder_id = {"inbox": 6, "sent": 5, "outbox": 4, "deleted": 3, "drafts": 16}.get(folder_key)
        if folder_id is None:
            try:
                folder_id = int(folder)
            except (TypeError, ValueError):
                raise ToolError(f"未知文件夹: {folder}（可用 inbox/sent/outbox/deleted/drafts 或 olFolder 数字）")
        keyword = str(keyword or "").strip()
        since_date = None
        if str(since or "").strip():
            try:
                since_date = datetime.strptime(str(since).strip()[:10], "%Y-%m-%d").date()
            except ValueError:
                raise ToolError("since 格式应为 YYYY-MM-DD")

        def work(ns):
            items = ns.GetDefaultFolder(folder_id).Items
            items.Sort("[ReceivedTime]", True)  # 倒序：配合 since 可以提前终止
            out = []
            for item in items:
                if len(out) >= limit:
                    break
                try:
                    when = item.ReceivedTime
                    d = when.date() if when is not None else None
                except Exception:
                    d = None
                if since_date is not None:
                    if d is None or d < since_date:
                        break  # 倒序遍历：更早的条目不可能命中
                if keyword and keyword.lower() not in str(item.Subject or "").lower():
                    continue
                out.append(self._mail_brief(item))
            return out

        mails = await self._outlook_run(work)
        return {"folder": folder_key, "keyword": keyword, "since": since or None,
                "returned": len(mails), "items": mails}

    async def outlook_send(self, to: str, subject: str, body: str, cc: str = "") -> dict[str, Any]:
        if not self.allow_outlook_send:
            raise ToolError("发送邮件已在 config.json 中禁用（allow_outlook_send=false）")
        if not to or not subject:
            raise ToolError("to 和 subject 不能为空")
        # 走 _owa_or_none 而不是直接判 backend：它会一并校验 allow_outlook，
        # 与 COM 路径的权限语义保持一致（否则 allow_outlook=false +
        # allow_outlook_send=true 时仍能通过 OWA 真实发信）。
        backend = self._owa_or_none()
        if backend is not None:
            return await backend.send(to, subject, body, cc)

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
