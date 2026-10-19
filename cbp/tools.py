

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

from . import editmatch


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
    "core": ("exit_plan_mode", "list_directory", "find_files", "read_text", "write_text", "append_text",
             "apply_patch",
             "write_text_b64", "append_text_b64", "replace_text", "make_directory",
             "copy_path", "move_path", "validate_python", "run_unittest",
             "skill_list", "skill_read", "skill_create",
             "memory_write", "memory_search", "memory_forget", "tool_list",
             # copilot_file 是**写工具**（让 Copilot 生成文件并下载到工作区），原先放在
             # copilot 组，而该组关键字全是"图片/截图/附件/上传给 copilot"——结果任何
             # "生成文件"类任务都会把它裁掉，模型根本看不到这个工具（实测：
             # "生成一个报告文件" -> copilot_file 不在工具面里）。写工具必须常驻，
             # 否则等于把"直接让 Copilot 生成文件"这条路堵死。
             "copilot_file"),
    "delete": ("delete_path",),
    "python": ("run_python",),
    "office": ("read_excel", "write_excel", "read_docx", "write_docx", "read_pptx", "read_pdf"),
    "outlook": ("outlook_unread", "outlook_read", "outlook_search", "outlook_send"),
    # copilot 组只剩"上传文件给 Copilot 分析"，关键字围绕附件/图片
    "copilot": ("copilot_attach",),
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



# 破坏性/不可回滚操作：默认需要用户**一次性**授权（fail-closed）。
# 与 DSH 一致：只有 allowed-once，没有 allow-always、也没有撤销存储——
# 授权用完即焚，下一次同类操作要重新授权。
# ── 沙箱档位（对应 DSH 的三档）──────────────────────────────────────
# read-only            只读：任何会改动文件/系统/外部服务的工具一律拒绝
# workspace-write      默认：写操作只允许在工作区内（workspace_roots 白名单）
# danger-full-access   允许写到工作区之外（仍受自升级护栏约束），供用户显式选择
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

# 变更类工具：read-only 档位下全部拒绝；扩展工具可用 approval=True 声明后同样受管
MUTATING_TOOLS = {
    "write_text", "write_text_b64", "append_text", "append_text_b64", "replace_text",
    "apply_patch", "replace_lines", "convert_indentation", "indent_lines",
    "write_docx", "write_excel", "make_directory", "move_path", "copy_path", "delete_path",
    "run_python", "pwsh", "outlook_send", "save_workspace_file",
    "image_generate", "schedule_add", "schedule_remove", "schedule_run", "memory_write",
}

APPROVAL_REQUIRED_TOOLS = ("delete_path", "outlook_send", "run_python")

# 一次调用可能已经产生副作用、但我们**拿不到结果**的动作：禁止原样重试。
NON_IDEMPOTENT_TOOLS = ("pwsh", "outlook_send", "delete_path", "move_path", "append_text",
                        "append_text_b64", "copy_path")

# 只读动作：它们成功即说明"当前真实状态"已核对过，可以解除上面的封锁。
READ_ONLY_TOOLS = ("read_text", "list_directory", "find_files", "outlook_unread",
                   "read_excel", "read_csv", "read_pdf", "read_docx", "memory_search",
                   "skill_list", "tool_list", "validate_python", "run_unittest")

class ToolRegistry:
    def __init__(self, root: Path, config: dict[str, Any]):
        # 保留原始配置：扩展工具的开关（tools_extra/allow_shell）与审批判定要读它
        self.config = dict(config or {})
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_read = int(config.get("max_read_chars", 120000))
        self.max_write = int(config.get("max_write_chars", 200000))
        self.allow_delete = bool(config.get("allow_delete", False))
        # 一次性审批策略（fail-closed）：ask=破坏性工具须用户逐次授权；never=不要求。
        self.approval_policy = str(config.get("approval_policy", "ask")).lower()
        # 沙箱档位：read-only / workspace-write / danger-full-access（非法值回落默认）
        self.sandbox_mode = str(config.get("sandbox_mode", "workspace-write")).lower()
        if self.sandbox_mode not in SANDBOX_MODES:
            self.sandbox_mode = "workspace-write"
        # 计划模式：进入后强制只读，直到 exit_plan_mode 显式退出
        self.plan_mode = bool(config.get("plan_mode", False))
        self.approval_required_tools = tuple(
            config.get("approval_required_tools") or APPROVAL_REQUIRED_TOOLS)
        # 外部注册时用 approval=True 声明的破坏性工具（与核心集合分开维护）
        self._approval_extra: set[str] = set()
        # 会话状态钩子（由桥接层注入）：扩展工具读写当前会话状态（goal/todos 等）
        self.state_getter: Callable[[], dict[str, Any]] | None = None
        self.state_saver: Callable[..., Any] | None = None
        self.allow_python = bool(config.get("allow_python", False))
        # 程序化工具调用（P2-13）：在子进程里批量调用**只读**工具，默认关闭。
        self.allow_run_code = bool(config.get("allow_run_code", False))
        self.run_code_timeout = float(config.get("run_code_timeout_seconds", 30))
        self.run_code_max_calls = int(config.get("run_code_max_calls", 50))
        self.run_code_max_output = int(config.get("run_code_max_output_chars", 20000))
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
        # COM/Outlook 超时后"结果未知"的封锁标记：见 _mark_unknown_outcome
        self._com_unknown: dict[str, Any] | None = None
        # 可配置超时（测试与慢机器需要；默认 60 秒）
        self.outlook_timeout = float(config.get("outlook_timeout_seconds", 60))
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
        # 代码类文件禁止"一大段明文塞进单个 JSON 动作"：模型在 JSON 里塞长代码
        # 会被转义/截断搞坏（实测），所以代码类超过这个字符数必须走 b64 分块。
        # 文档类（md/txt/log）不受限：写 20KB 报告本来就该明文分块，不该逼模型
        # 去产 Base64（历史上正是这个压力把它推去用 PowerShell heredoc，然后踩编码坑）。
        self.code_exts = (".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
                          ".json", ".html", ".htm", ".css", ".scss", ".yml", ".yaml",
                          ".toml", ".ini", ".cfg", ".sh", ".ps1", ".bat", ".sql",
                          ".xml", ".vue", ".svelte")
        self.plain_code_limit = int(config.get("plain_code_limit", 1500))

        # "需要 Copilot 网页会话"的工具：provider（API）会话里禁用（见 set_web_copilot_tools）
        self.web_copilot_tools = ("copilot_attach", "copilot_file")
        self.allow_web_copilot_tools = True
        # 并行的前提：会话 id 与"是否允许网页工具"必须**按任务取值**，不能用
        # 全局单值（两个子代理并发时会互相覆盖）。由 bridge 注入 contextvars 读值器。
        self.current_session_id = ""
        self.session_id_fn = None            # () -> str | None
        self.web_copilot_allowed_fn = None    # () -> bool
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
            "replace_lines": self.replace_lines,
            "apply_patch": self.apply_patch,
            "convert_indentation": self.convert_indentation,
            "indent_lines": self.indent_lines,
            "make_directory": self.make_directory,
            "copy_path": self.copy_path,
            "move_path": self.move_path,
            "delete_path": self.delete_path,
            "exit_plan_mode": self.exit_plan_mode,
            "run_python": self.run_python,
            "run_code": self.run_code,
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
                 description: str, arguments: dict[str, Any],
                 approval: bool = False) -> None:
        """注册外部工具（如桥接层的 Copilot 文件上传 copilot_attach）。

        approval=True：该工具是破坏性操作，需一次性审批（由**模块自己声明**，
        核心工具的能力开关仍走 APPROVAL_REQUIRED_TOOLS）。这样扩展工具模块
        不必在核心代码里加 if 分支。
        """
        self._tools[name] = fn
        if approval:
            self._approval_extra.add(name)
        self._extra_descriptions.append(
            {"name": name, "description": description, "arguments": arguments})

    def unregister(self, name: str) -> None:
        """摘掉一个外部注册的工具（插件卸载用）。"""
        self._tools.pop(name, None)
        self._extra_descriptions = [d for d in self._extra_descriptions
                                    if d.get("name") != name]
        self._approval_extra.discard(name)

    def set_web_copilot_tools(self, allowed: bool) -> None:
        """启用/禁用"需要 Copilot 网页会话"的工具（copilot_attach/copilot_file）。

        API 提供商（deepseek/… 等）会话没有 Copilot 网页对话可驱动，这两个工具
        注定失败。实测真实任务里 copilot_file 连续失败 10 次、白烧回合——因此
        provider 会话里既不注入也不执行（执行时给可自救的错误信息）。
        """
        self.allow_web_copilot_tools = bool(allowed)

    def web_copilot_allowed(self) -> bool:
        """当前任务是否允许网页 Copilot 工具。

        优先用注入的读值器（bridge 用 contextvars 按任务隔离）；没有读值器时
        退回全局标志——这样单任务/浏览器通道的行为与以前完全一致。
        """
        fn = self.web_copilot_allowed_fn
        if fn is not None:
            try:
                return bool(fn())
            except Exception:
                pass
        return bool(getattr(self, "allow_web_copilot_tools", True))

    def session_id(self) -> str | None:
        """当前任务的会话 id（同样优先按任务上下文取值）。"""
        fn = self.session_id_fn
        if fn is not None:
            try:
                return fn()
            except Exception:
                pass
        return getattr(self, "current_session_id", None) or None

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
                                     source_session=self.session_id(),
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
        if (self.workspace_roots and self.sandbox_mode != "danger-full-access"
                and not any(target == r or r in target.parents
                            for r in self.workspace_roots)):
            raise ToolError(self._sandbox_denied(
                "FS_SANDBOX_NOT_ALLOWED", str(value), "目标目录不在 workspace_roots 白名单内"))
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
            {"name": "read_text", "description": "读取 UTF-8 文本；默认读全文件（上限 max_read_chars）。超大文件用 offset/limit 按字符分段，或 line_start/line_count 按行读（带行号，便于精确改某几行）", "arguments": {"path": "相对路径", "offset": "字符偏移，默认0", "limit": "本次最多返回字符数，默认120000", "line_start": "按行读：起始行号，1 起（传了就忽略 offset）", "line_count": "按行读：读多少行，默认到文件末尾", "reveal_whitespace": "把空格显示成 ·、制表符显示成 →（查缩进必用）"}},
            {"name": "write_text", "description": "覆盖写入 UTF-8 明文；文档类（.md/.txt/.log）可整篇写，代码类仅限 ≤1500 字符的短文件（更长请用 b64 分块）", "arguments": {"path": "相对路径", "content": "文本"}},
            {"name": "append_text", "description": "追加 UTF-8 明文；文档类分块续写用它，代码类仅限 ≤1500 字符（更长用 append_text_b64）", "arguments": {"path": "相对路径", "content": "文本"}},
            {"name": "write_text_b64", "description": "Base64 解码后覆盖写入 UTF-8；代码、JSON、HTML、Markdown 必须用此工具", "arguments": {"path": "相对路径", "content_b64": "UTF-8 内容的 Base64"}},
            {"name": "append_text_b64", "description": "Base64 解码后追加 UTF-8；代码、JSON、HTML、Markdown 必须用此工具", "arguments": {"path": "相对路径", "content_b64": "UTF-8 内容的 Base64"}},
            {"name": "replace_text", "description": "在现有文件中精确替换短文本，修改代码时优先使用", "arguments": {"path": "相对路径", "old": "原文本", "new": "新文本", "count": "替换次数，默认1"}},
            {"name": "apply_patch", "description": "一次调用完成多处修改（原子：任一失败则不写入）；支持补丁或 edits 列表，四处以上小改用它省回合",
             "arguments": {"path": "相对路径",
                           "patch": "补丁文本：*** Begin Patch / *** Update File: x / @@ 分块，行首 ' '=上下文 '-'=旧 '+'=新",
                           "edits": "或改用数组 [{old,new,count?}]（与 patch 二选一）",
                           "count": "每条替换次数，默认 1（0=全部）"}},
            {"name": "convert_indentation", "description": "把**行首**缩进在制表符/空格之间一次转换完（只动行首，不碰行内与字符串）；修缩进别再用 replace_text 一处一处改", "arguments": {"path": "相对路径", "mode": "tabs2spaces（默认）或 spaces2tabs", "width": "制表位宽度，默认4", "line_start": "只改这一段：起始行，1 起（可省略）", "line_count": "改多少行（可省略）"}},
            {"name": "indent_lines", "description": "给指定行区间**增减行首空格**（delta 正=加缩进，负=减）；修缩进用它，别在 replace_text 里塞空格", "arguments": {"path": "相对路径", "line_start": "起始行，1 起", "line_count": "多少行，默认1", "delta": "加减几个空格（正=加，负=减）"}},
            {"name": "replace_lines", "description": "按行号替换整行区间（1 起，含首尾），保留文件原有换行风格；改缩进/整段替换时用它，比 replace_text 更稳；不需要 shell", "arguments": {"path": "相对路径", "start": "起始行号，1 起", "count": "替换几行，默认1", "content": "新内容（可含多行；传空串表示删除这几行）"}},
            {"name": "make_directory", "description": "创建目录", "arguments": {"path": "相对路径"}},
            {"name": "copy_path", "description": "复制文件或目录", "arguments": {"source": "相对路径", "destination": "相对路径"}},
            {"name": "move_path", "description": "移动文件或目录", "arguments": {"source": "相对路径", "destination": "相对路径"}},
            {"name": "validate_python", "description": "检查 Python 文件或目录语法；仅在文件全部写完后执行（写了一半的文件必然失败，不是缺陷）", "arguments": {"path": "相对路径，默认 ."}},
            {"name": "run_unittest", "description": "执行 unittest 测试发现；仅在文件全部写完后执行", "arguments": {"path": "测试目录，默认 tests", "pattern": "默认 test_*.py"}},
            {"name": "exit_plan_mode",
             "description": "结束计划模式：提交方案并解除只读限制（计划阶段只读探索后调用）",
             "arguments": {"plan": "要执行的方案（Markdown）", "notes": "补充说明（可选）"}},
        ]
        if self.allow_delete:
            items.append({"name": "delete_path", "description": "删除路径", "arguments": {"path": "相对路径"}})
        if self.allow_run_code:
            items.append({"name": "run_code",
                          "description": "在子进程里跑一段 Python，可批量调用只读工具"
                                         "（read_text/list_directory/find_files/skill_list/"
                                         "memory_search/tool_list），最后用 final(值) 回传结果。"
                                         "适合“读多个文件再汇总”：工具循环要 N 个回合，这里 1 个回合。"
                                         "只读、无写入；有超时与调用次数上限",
                          "arguments": {"code": "Python 代码；用 final(结果) 结束"}})
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

    def requires_approval(self, name: str) -> bool:
        """该工具此刻是否需要一次性授权。

        策略 config.approval_policy：ask（默认，要求授权）/ never（不要求，视为用户
        已明确承担风险）。可配 approval_required_tools 覆盖默认集合。
        """
        if self.approval_policy == "never":
            return False
        if name in self._approval_extra:
            # 模块注册时自己声明为破坏性操作；能力开关由该模块负责
            return True
        if name not in self.approval_required_tools:
            return False
        # 能力本身没开时不需要审批（工具根本不该被调用，另有报错路径）。
        if name == "delete_path":
            return self.allow_delete
        if name == "run_python":
            return self.allow_python
        if name == "outlook_send":
            return self.allow_outlook_send
        return True

    def _sandbox_denied(self, code: str, target: str, reason: str) -> str:
        """结构化沙箱拒绝（借 DSH 的 FS_SANDBOX_DENIED 思路）。

        只报"拒绝访问"对模型不可行动——它不知道当前边界在哪、也不知道怎么合法达成
        目的，于是反复试探浪费回合。这里统一给出：错误码、当前模式、原因、可行动方案。
        """
        mode = "self-upgrade" if self.allow_self_upgrade else "workspace-write"
        lines = [
            f"[sandbox: {code}] 拒绝访问: {target}",
            f"当前模式: {mode}（工作区根 {self.root}；"
            + ("服务安装目录内仅白名单路径可写）" if self.allow_self_upgrade
               else "仅工作区内可写，服务安装目录受保护）"),
            f"原因: {reason}",
            "可以这样做: ① 改用工作区内的相对路径（不要用 ../ 或绝对路径）；"
            "② 若确实需要改服务自身代码，请让用户在 config.json 打开 agent.allow_self_upgrade 后重试；"
            "③ 只需要内容时用只读工具读取，或请用户把文件放进工作区。",
        ]
        return "\n".join(lines)

    def _covers_install(self, candidate: Path) -> bool:
        """candidate 是否落在服务安装目录内（danger 档下仍要拦这个）。"""
        if not self.global_root:
            return False
        try:
            install = Path(self.global_root).resolve()
            candidate.relative_to(install)
            return not self.allow_self_upgrade
        except ValueError:
            return False

    def sandbox_path(self, value: str = ".") -> Path:
        """公开的沙箱路径解析（扩展工具模块用，避免访问私有方法）。"""
        return self._path(value)

    def _path(self, value: str = ".") -> Path:
        candidate = (self.root / value).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            # danger-full-access：允许越出工作区（用户显式选择），但不许借此改服务
            # 安装目录——那会绕过自升级护栏与验证闭环。
            if self.sandbox_mode == "danger-full-access" and not self._covers_install(candidate):
                return candidate
            raise ToolError(self._sandbox_denied(
                "FS_SANDBOX_ESCAPE", str(value),
                "解析后的路径落在工作区之外（含 ../ 或绝对路径越界）")) from exc
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
                            raise ToolError(self._sandbox_denied(
                                "FS_SANDBOX_DENYLIST", rel, "该路径属于服务敏感目录（denylist）"))
                    elif rel == deny:
                        raise ToolError(self._sandbox_denied(
                            "FS_SANDBOX_DENYLIST", rel, "该路径属于服务敏感文件（denylist）"))
        return candidate

    # ── "结果未知"封锁（协作式取消的兜底）─────────────────────────────
    # 背景：COM 调用超时无法真正取消（线程里的调用还在跑）。原来的实现直接放弃等待，
    # 于是模型可能再次发起同一动作 → 重复发信/重复删除；更糟的是外层还会重试一次。
    # 现在改为：超时后**记录结果未知**并阶段性封锁不可幂等动作，直到只读动作成功
    # （等价于"先核对现状再说"），绝不静默放弃、也绝不自动重试。

    def _mark_unknown_outcome(self, tool: str, detail: str) -> None:
        self._com_unknown = {"tool": tool, "detail": detail, "since": time.time()}

    def clear_unknown_outcome(self, by: str = "") -> None:
        if self._com_unknown:
            log_line = self._com_unknown.get("tool")
            self._com_unknown = None
            if by:
                print(f"[tools] 结果未知封锁已由只读动作 {by} 解除（涉及 {log_line}）", flush=True)

    def unknown_outcome_error(self, tool: str) -> str:
        info = self._com_unknown or {}
        return ("[TOOL_TIMEOUT_UNKNOWN] 上一次 " + str(info.get("tool") or "工具") +
                " 调用超时，**结果未知**（调用可能仍在后台执行，也可能已经产生副作用）。\n"
                "已禁止原样重试该动作。请先用只读工具核对真实现状（例如 read_text / "
                "list_directory / outlook_unread），确认之后再决定下一步；"
                "若确认没有发生，可再明确重发一次。\n详情：" + str(info.get("detail") or "")[:200])

    async def exit_plan_mode(self, plan: str = "", notes: str = "") -> dict[str, Any]:
        """结束计划模式：把方案写进会话状态并解锁写操作。

        对应 DSH 的 exit_plan_mode：计划阶段只读探索，产出方案后由用户/模型确认再执行。
        """
        text = str(plan or "").strip()
        if not text:
            raise ToolError("exit_plan_mode 需要 plan（要执行的方案）")
        self.plan_mode = False
        saved = {"plan": text[:8000], "notes": str(notes or "")[:2000], "at": time.time()}
        if self.state_saver is not None:
            try:
                self.state_saver(plan=saved, plan_mode=0)
            except Exception:
                pass
        return {"ok": True, "plan_saved": True, "plan": text,
                "hint": "方案已记录。**本轮请直接返回 final** 把方案呈现给用户，"
                        "不要开始执行；用户确认后下一轮再按方案动手。"}

    async def execute(self, name: str, arguments: dict[str, Any] | None) -> Any:
        if not self.allow_web_copilot_tools and name in self.web_copilot_tools:
            raise ToolError(
                f"{name} 需要 Copilot 网页会话，当前会话用的是 API 提供商模型（如 deepseek/…），"
                "该工具不可用。请改用 write_text_b64/append_text_b64 分块写入新文件、"
                "replace_text 修改现有内容。")
        fn = self._tools.get(name)
        if not fn:
            raise ToolError(f"未知工具: {name}")
        # 计划模式 / 只读档位：变更类动作在**执行前**就拒绝（结构化错误，模型可据此
        # 改走只读路径，而不是写完才发现不该写）
        if name in MUTATING_TOOLS:
            if self.plan_mode:
                raise ToolError(self._sandbox_denied(
                    "SANDBOX_PLAN_MODE", name,
                    "当前处于计划模式（只读）；先给出方案并调用 exit_plan_mode 结束计划阶段"))
            if self.sandbox_mode == "read-only":
                raise ToolError(self._sandbox_denied(
                    "SANDBOX_READ_ONLY", name,
                    "当前沙箱档位是 read-only；改用只读工具，或让用户切换档位"))
        if self._com_unknown and name in NON_IDEMPOTENT_TOOLS:
            raise ToolError(self.unknown_outcome_error(name))
        try:
            result = fn(**(arguments or {}))
            # 工具可以同步也可以异步：skill_list 等纯计算工具是同步函数，
            # 无条件 await 会报 "object list can't be used in 'await' expression"，
            # 使这些工具经 Agent 调用时永远失败。
            if inspect.isawaitable(result):
                result = await result
            if name in READ_ONLY_TOOLS:
                self.clear_unknown_outcome(name)
            return result
        except TypeError as exc:
            # 参数错误必须**可行动**：只报 "unexpected keyword argument" 模型无法自救
            # （实测它只会换个路径瞎试，浪费回合）。这里直接列出该工具的合法参数名与
            # 各自含义，并把"路径不要带 workspace/ 前缀"这类高频坑一并点出。
            raise ToolError(self._argument_error_hint(name, fn, arguments, exc)) from exc

    def _argument_error_hint(self, name: str, fn: Any, arguments: dict | None,
                             exc: TypeError) -> str:
        """把 TypeError 翻译成"怎么改"的提示。"""
        given = ", ".join(sorted((arguments or {}).keys())) or "(无)"
        spec = ""
        for item in self.descriptions():
            if item.get("name") == name:
                spec = ", ".join(f"{k}（{v}）" for k, v in (item.get("arguments") or {}).items())
                break
        if not spec:
            try:
                sig = inspect.signature(fn)
                spec = ", ".join(
                    f"{p.name}（{'必填' if p.default is inspect._empty else '可选'}）"
                    for p in sig.parameters.values() if p.kind is not p.VAR_KEYWORD)
            except Exception:
                spec = "(未知)"
        hint = (f"工具参数错误: {exc}\n"
                f"你传的参数: {given}\n"
                f"{name} 的合法参数: {spec}")
        if any(str(k).startswith("workspace/") for k in (arguments or {})):
            hint += ("\n提示: 工具路径**相对于当前工作区根**，不要再加 workspace/ 前缀"
                     "（根目录下直接用 README.md 这样的相对路径）。")
        hint += "\n请按上面的参数名重发本次动作，不要重试同一个错误参数。"
        return hint

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

    async def read_text(self, path: str, offset: int = 0, limit: int = 0,
                        line_start: int = 0, line_count: int = 0,
                        reveal_whitespace: bool = False) -> dict[str, Any]:
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"文件不存在: {path}")
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

    @staticmethod
    def _hash(path: Path) -> str | None:
        if not path.is_file(): return None
        h=hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda:f.read(1048576),b""): h.update(chunk)
        return h.hexdigest()

    def _check_plain_code_size(self, path: str, content: str) -> None:
        """代码类文件不允许"一大段明文塞进单个 JSON 动作"。

        为什么：模型把长代码直接放进 action JSON 时，引号/反斜杠转义和输出截断
        会把内容搞坏（实测）。所以代码类超过 `plain_code_limit`(默认 1500) 时
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

    async def write_text(self, path: str, content: str, via_b64: bool = False) -> dict[str, Any]:
        if len(content) > self.max_write:
            raise ToolError("写入内容超过限制")
        # via_b64=True 时不限明文长度：Base64 分块就是代码类大文件的正路
        if not via_b64:
            self._check_plain_code_size(path, content)
        target = self._path(path)
        self._snap("write_text", path, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        before=self._hash(target)
        # newline=""：原样写入，Windows 下不把 \n 静默转成 \r\n
        with target.open("w", encoding="utf-8", newline="") as f: f.write(content)
        after=self._hash(target)
        return {"path":path,"written_chars":len(content),"file_event":{"tool":"write_text","path":path,"before_hash":before,"after_hash":after,"result":"ok"}}

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

    async def write_text_b64(self, path: str, content_b64: str) -> dict[str, Any]:
        content = self._decode_b64(content_b64)
        result = await self.write_text(path, content, via_b64=True)
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
        before=self._hash(target)
        self._snap("replace_text", path, target)
        with target.open("w", encoding="utf-8", newline="") as f:
            f.write(text.replace(old_used, new_used) if count == 0
                    else text.replace(old_used, new_used, count))
        after=self._hash(target)
        _left = max(0, found - replaced)
        _out = {"path":path,"replaced":replaced,"remaining_matches":_left,"match":strategy,
                "file_event":{"tool":"replace_text","path":path,"before_hash":before,"after_hash":after,"result":"ok"}}
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
        return {"path": path, "replaced_lines": hi - lo, "start": start,
                "inserted_lines": len(new_lines), "total_lines": len(out_lines),
                "changed": before != after,
                "file_event": {"tool": "replace_lines", "path": path,
                               "before_hash": before, "after_hash": after, "result": "ok"}}


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

        # COM 弹安全确认框或 Outlook 脱机时会永久阻塞，加超时保护。
        # 注意：COM 调用**无法真正取消**——超时只表示"我们不再等"，线程里的调用仍在跑。
        # 因此这里用 shield 保护底层 future（不抛弃它）并对它的最终结果挂回调，
        # 超时绝不重试（重试 = 在第一次可能已生效的情况下再来一次）。
        timeout = max(self.outlook_timeout, 1.0)
        worker = _ComWorker.get()
        for attempt in (1, 2):
            fut = worker.submit(runner)
            try:
                return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            except ToolError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                self._mark_unknown_outcome("outlook", f"{timeout:.0f} 秒无响应")

                def _late(done, _t=timeout):
                    """迟到的结果：不改变任务走向，只把真相记下来（不静默抛弃）。"""
                    try:
                        if done.cancelled():
                            return
                        err = done.exception()
                        if err is not None:
                            print(f"[tools] 超时后的 Outlook 调用最终失败：{err}"[:300], flush=True)
                        else:
                            print("[tools] 超时后的 Outlook 调用最终成功——副作用很可能已发生，"
                                  "已禁止原样重试，请先只读核对", flush=True)
                    except Exception:
                        pass

                fut.add_done_callback(_late)
                raise ToolError(
                    f"[TOOL_TIMEOUT] 调用 Outlook 超过 {timeout:.0f} 秒无响应：Outlook 可能弹出了"
                    "安全确认框、处于脱机或卡死状态。**该调用可能仍在后台执行**，因此结果未知："
                    "已禁止原样重试同一动作；请先在本机确认 Outlook 状态，并用只读工具"
                    "（如 outlook_unread）核对是否已经发生，再决定下一步。")
            except Exception as exc:
                if attempt == 1 and self._busy_hresult(exc):
                    await asyncio.sleep(2.0)  # Outlook 繁忙瞬时拒绝：退后重试一次（此时调用确未生效）
                    continue
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
