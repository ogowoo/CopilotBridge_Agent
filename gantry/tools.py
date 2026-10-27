

import difflib
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
             "write_text_b64", "append_text_b64", "replace_text",
             "replace_lines", "apply_patch", "python_symbols", "replace_python_symbol",
             "make_directory",
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
             "self_replace_text", "self_validate", "self_unittest", "self_rollback",
             "self_find_files", "self_search_text"),
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

class ToolsCoreMixin:
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
        # 长期记忆：跨会话累积的事实/偏好/结论（gantry/memory.py）
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
        # 白名单**始终**构造（不要跟在 allow_self_upgrade 后面）：
        # 危险区开关是即时生效的——如果构造时空白名单，运行时把开关打开
        # 会发现"允许了但什么文件都不让碰"（self_find_files 实测就是这个）。
        self.self_whitelist = tuple(config.get("self_upgrade_paths") or (
            "gantry/", "webui/", "scripts/", "docs/", "tests/", "skills/",
            "app.py", "start.py", "install.py", "requirements.txt",
            "AGENT.md", "README.md", "config.example.json",
        ))
        # 代码类文件禁止"一大段明文塞进单个 JSON 动作"：模型在 JSON 里塞长代码
        # 会被转义/截断搞坏（实测），所以代码类超过这个字符数必须走 b64 分块。
        # 文档类（md/txt/log）不受限：写 20KB 报告本来就该明文分块，不该逼模型
        # 去产 Base64（历史上正是这个压力把它推去用 PowerShell heredoc，然后踩编码坑）。
        self.code_exts = (".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
                          ".json", ".html", ".htm", ".css", ".scss", ".yml", ".yaml",
                          ".toml", ".ini", ".cfg", ".sh", ".ps1", ".bat", ".sql",
                          ".xml", ".vue", ".svelte")
        self.plain_code_limit = int(config.get("plain_code_limit", 6000))

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
            "python_symbols": self.python_symbols,
            "replace_python_symbol": self.replace_python_symbol,
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
                "self_find_files": self.self_find_files,
                "self_search_text": self.self_search_text,
            })

    def clone(self) -> "ToolRegistry":
        """复制一份注册表给**并行任务**用（root / 沙箱 / 计划模式 / 记忆作用域互不干扰）。

        浅拷贝 __dict__ 即可：钩子（state_getter / subagent_fn / snapshot_hook…）
        都是无状态的函数，共享安全；可变容器在任务启动时会重新赋值。

        **但工具函数必须重新绑定到新实例**：`_tools` 里存的是 `self.read_text` 这类
        bound method，浅拷贝后仍指向**原注册表**——于是 `clone.set_root(会话工作区)`
        只改了克隆的 root，工具执行时用的还是原 root（真实事故：选了工作区，Agent
        却在默认 `workspace/` 里读写，Copilot 回答"当前工作目录是 …\workspace"）。
        """
        import copy as _copy

        new = object.__new__(ToolRegistry)
        for k, v in self.__dict__.items():
            new.__dict__[k] = _copy.copy(v) if isinstance(v, (dict, list, set)) else v
        for name, fn in list(new.__dict__.get("_tools", {}).items()):
            if getattr(fn, "__self__", None) is self:
                try:
                    new._tools[name] = getattr(new, getattr(fn, "__name__", name))
                except Exception:
                    pass
        return new

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


    def tool_list(self) -> dict[str, Any]:
        """列出全部可用工具（含当前未在提示词中列出的组），供按需发现。"""
        allitems = self._descriptions_all()
        buckets: dict[str, list[str]] = {}
        for x in sorted(allitems, key=lambda y: y["name"]):
            buckets.setdefault(tool_group(x["name"]), []).append(x["name"])
        return {"total": len(allitems), "groups": buckets,
                "note": "提示词里只列了与当前任务相关的工具；需要其它工具时直接调用即可。"}





















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
        # 按提示词语言输出描述与参数提示（zh 原样；en 换译本；tw 按字转换）
        try:
            from . import prompt_lang, tool_i18n
            cfg = getattr(self, "config", None) or {}
            lang = prompt_lang.effective_lang(
                cfg.get("prompt_language") if isinstance(cfg, dict) else None)
            items = tool_i18n.localize_items(items, lang)
        except Exception:
            pass
        return items

    def _descriptions_all(self) -> list[dict[str, Any]]:
        items = [
            {"name": "list_directory", "description": "列出工作区目录", "arguments": {"path": "相对路径，默认 ."}},
            {"name": "find_files", "description": "按 glob 查找文件", "arguments": {"pattern": "如 **/*.log"}},
            {"name": "read_text", "description": "读取 UTF-8 文本；默认读全文件（上限 max_read_chars）。超大文件用 offset/limit 按字符分段，或 line_start/line_count 按行读（带行号，便于精确改某几行）", "arguments": {"path": "相对路径", "offset": "字符偏移，默认0", "limit": "本次最多返回字符数，默认120000", "line_start": "按行读：起始行号，1 起（传了就忽略 offset）", "line_count": "按行读：读多少行，默认到文件末尾", "reveal_whitespace": "把空格显示成 ·、制表符显示成 →（查缩进必用）"}},
            {"name": "write_text", "description": "写入 UTF-8 明文；**只用于新建文件**。已存在的文件必须先 read_text，再用 apply_patch/replace_text/replace_lines 局部修改；确实要整体重写才传 overwrite=true。文档类（.md/.txt/.log）可整篇写，代码类仅限 ≤1500 字符（更长用 b64）", "arguments": {"path": "相对路径", "content": "文本", "overwrite": "仅整体重写现有文件时传 true（默认 false 会拒绝覆盖）"}},
            {"name": "append_text", "description": "追加 UTF-8 明文；文档类分块续写用它，代码类仅限 ≤1500 字符（更长用 append_text_b64）", "arguments": {"path": "相对路径", "content": "文本"}},
            {"name": "write_text_b64", "description": "Base64 解码后写入 UTF-8；**只用于新建文件**。修改现有文件必须先 read_text，再用 apply_patch/replace_text/replace_lines；确实要整体重写才传 overwrite=true", "arguments": {"path": "相对路径", "content_b64": "UTF-8 内容的 Base64", "overwrite": "仅整体重写现有文件时传 true（默认 false 会拒绝覆盖）"}},
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
            {"name": "python_symbols", "description": "按 AST 列出 .py 文件里的函数/类及其行区间（含 Class.method 限定名）；改整个函数/类前先用它定位", "arguments": {"path": "相对路径"}},
            {"name": "replace_python_symbol", "description": "按 AST 定位并整段替换一个 Python 函数/类；比文本匹配更稳，改整段实现优先用它", "arguments": {"path": "相对路径", "name": "符号名，如 Class.method 或 func", "content": "新的完整符号源码（含 def/class 行与缩进）"}},
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
                {"name": "self_find_files", "description": "【自升级】列举**服务安装目录**下的文件（排查本服务的问题时用它定位；工作区里没有服务代码）", "arguments": {"pattern": "glob，如 gantry/*.py，默认 **/*.py"}},
                {"name": "self_search_text", "description": "【自升级】在**服务安装目录**里搜文本/正则（用户报的是本服务的按钮/功能/报错时，先用它定位；工作区工具搜不到服务代码）", "arguments": {"query": "文本或正则", "path": "目录，默认 .", "max_results": "默认 50"}},
                {"name": "self_read_text", "description": "【自升级】读取服务自身代码（安装目录相对路径，白名单内）", "arguments": {"path": "如 gantry/agent.py", "offset": "字符偏移，默认0", "limit": "默认全文件"}},
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

    # 常见的"编出来的工具名" → 真工具名。模型的命名习惯很稳定（见下），
    # 命中别名或编辑距离足够近时，报错里直接给出正确的那个。
    TOOL_ALIASES: dict[str, str] = {
        "run_tests": "run_unittest", "run_test": "run_unittest",
        "pytest": "run_unittest", "unittest": "run_unittest",
        "check_syntax": "validate_python", "syntax_check": "validate_python",
        "py_compile": "validate_python", "compile": "validate_python",
        "execute_command": "pwsh", "run_command": "pwsh", "shell": "pwsh",
        "bash": "pwsh", "cmd": "pwsh", "terminal": "pwsh",
        "execute_python": "run_python", "run_script": "run_python", "python": "run_python",
        "read_file": "read_text", "cat": "read_text", "open_file": "read_text",
        "write_file": "write_text", "save_file": "write_text",
        "edit_file": "replace_text", "str_replace": "replace_text",
        "apply_diff": "apply_patch", "patch": "apply_patch",
        "ls": "list_directory", "dir": "list_directory", "list_files": "list_directory",
        "find": "find_files", "glob": "find_files", "search": "grep",
        "todo": "todo_write", "tasks": "todo_write",
    }

    def _unknown_tool_hint(self, name: str) -> str:
        """未知工具的报错：给出最接近的真工具 + 可用工具清单（可操作，而不是只说错了）。"""
        available = sorted(self._tools)
        guess = self.TOOL_ALIASES.get(str(name).strip().lower())
        if not guess:
            near = difflib.get_close_matches(str(name), available, n=1, cutoff=0.6)
            if near:
                guess = near[0]
        head = f"没有名为 {name} 的工具。"
        if guess and guess in self._tools:
            head += f"如果你想做的是这件事，用 **{guess}**（参数见它的描述）。"
        listing = "、".join(available[:24]) + ("…" if len(available) > 24 else "")
        return head + f" 当前可用工具：{listing}。不确定用哪个就先 read_text/list_directory 看现状。"

    async def execute(self, name: str, arguments: dict[str, Any] | None) -> Any:
        if not self.allow_web_copilot_tools and name in self.web_copilot_tools:
            raise ToolError(
                f"{name} 需要 Copilot 网页会话，当前会话用的是 API 提供商模型（如 deepseek/…），"
                "该工具不可用。请改用 write_text_b64/append_text_b64 分块写入新文件、"
                "replace_text 修改现有内容。")
        fn = self._tools.get(name)
        if not fn:
            # 模型换过几次会话后常"忘记工具名"，凭空写出 run_tests / check_syntax /
            # execute_command 之类（代码注释里记过这个坑，实盘会连撞几步过不了验证门）。
            # 这里不是简单报错，而是把最接近的真工具和可用清单一起给它。
            raise ToolError(self._unknown_tool_hint(name))
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



























    # ── Office 文档工具（openpyxl / python-docx / python-pptx / pypdf）────









    # ── Outlook COM 工具（pywin32，需本机 Outlook 已登录）────────────────


# ── 组装（第四批解耦）：核心 + 各功能 mixin，方法面与拆分前完全一致 ──
from .tools_files import FilesMixin  # noqa: E402
from .tools_exec import ExecMixin  # noqa: E402
from .tools_office import OfficeMixin  # noqa: E402
from .tools_outlook import OutlookMixin  # noqa: E402
from .tools_skills import SkillsMixin  # noqa: E402
from .tools_memory import MemoryMixin  # noqa: E402
from .tools_self import SelfUpgradeMixin  # noqa: E402

class ToolRegistry(ToolsCoreMixin, FilesMixin, ExecMixin, OfficeMixin, OutlookMixin, SkillsMixin, MemoryMixin, SelfUpgradeMixin):
    """全部工具的唯一入口（mixin 组装；用法与拆分前一致）。"""

