"""扩展工具集（解耦模块，便于独立升级/更新/移除）。

**为什么单独成文件**：`cbp/tools.py` 是文件读写这类"核心工具"，改动风险高；
本模块放"可选、可独立演进"的工具，通过 `register_extra_tools(registry, config)`
挂到 ToolRegistry 上（与 bridge.py 注册 copilot_attach 的方式一致）。
删掉本文件 + 取消 bridge 里的一行调用即可整体下线，不影响核心工具。

参考 DSH（DeepSeek Harness）的工具面，按组提供、可逐组开关
（config.agent.tools_extra 下的布尔项）。

**归属规则（新增工具时先看这里）**：
- 文件读写/验证这类**核心工具** → `cbp/tools.py`（它们依赖快照、回滚、审批等核心设施）
- 其余**可选/实验性/需要额外依赖**的工具 → 本模块，用 `register(..., approval=...)` 声明审批
- 本模块超过 3 组（search/web/shell/plan/vision/ask 已 6 组）就拆成 `tools_extra/` 子包，
  每组一个文件；拆之前保持单文件，避免过早复杂化。

- `search`  `grep`                   按内容正则搜索（find_files 只按文件名）
- `web`     `web_fetch`              抓网页正文（HTML→纯文本）
- `shell`   `pwsh`                   执行 PowerShell 命令
            `job_list/job_output/job_kill`  后台任务（pwsh 的 run_in_background）
- `plan`    `todo_write`             任务清单
            `present`               交付物声明
- `vision`  `read_image`             图片元数据 + 可选交给 Copilot 视觉描述
- `ask`     `ask_user_question`      任务中途向用户提问（阻塞等待回答）

安全默认：
- `pwsh` 默认开启但**需要一次性审批**（approuval_policy=ask 时 WebUI 逐次授权）；
  `allow_shell=false` 可整体关闭。
- `web_fetch` 默认拒绝内网/本地地址（SSRF），`allow_private_web=true` 放开。
- `read_image`/`ask_user_question` 的智能部分依赖桥接层注入的回调；没有回调时
  优雅降级（只返回元数据 / 明确报错），不假装成功。
"""

from __future__ import annotations

import asyncio
import html as html_mod
import ipaddress
import json
import re
import shutil
import socket
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

try:  # httpx 是 FastAPI 的既有依赖；缺失时 web 组降级为 urllib
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


class ExtraToolError(RuntimeError):
    """扩展工具的可预期错误（与核心 ToolError 同语义，桥接层会转成工具错误结果）。"""


_MAX_GREP_FILE_BYTES = 1_000_000      # 单文件超过 1MB 不搜（二进制/超大文件）
_MAX_GREP_FILES = 5000                # 一次最多扫 5000 个文件
_MAX_OUTPUT = 20000                   # 命令输出截断阈值
_DEFAULT_QUESTION_TIMEOUT = 600       # ask_user_question 默认等待秒数


# ────────────────────────── 工具实现 ──────────────────────────

def similar_task_hint(task: str, siblings: list[dict], threshold: float = 0.55) -> str:
    """新子任务与已有子代理高度相似时，提示"用 subagent_send 追问"。

    审计依据：pyshot 任务的 3 个只读分析子代理（57/48/49 步）结论高度重叠，
    本可以一个跑完、另外两个在原上下文里追问——省两次冷启动与汇总。
    """
    def grams(text: str) -> set[str]:
        s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or "")).lower()
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 3 else set()

    mine = grams(task)
    if not mine:
        return ""
    best, best_score = None, 0.0
    for sib in siblings or []:
        other = grams(sib.get("description") or sib.get("title") or "")
        if not other:
            continue
        # 用"包含度"而非 Jaccard：新要求常是原描述的子集（补充一句）
        score = len(mine & other) / max(1, min(len(mine), len(other)))
        if score > best_score:
            best, best_score = sib, score
    if best and best_score >= threshold:
        desc = str(best.get("description") or best.get("title") or "")[:60]
        return (f"已有子代理 {best.get('id')}「{desc}」做过高度相似的任务"
                f"（相似度 {best_score:.0%}，状态 {best.get('state')}）："
                "用 subagent_send 在原上下文里继续/追问通常更快；确需独立视角再开新会话。")
    return ""


class _Extra:
    """扩展工具的实现集合；每个公开方法对应一个工具。"""

    def __init__(self, registry: Any, config: dict[str, Any],
                 vision_fn: Callable[[str, str], Awaitable[dict]] | None = None,
                 ask_fn: Callable[[str, list[str]], Awaitable[dict]] | None = None,
                 session_getter: Callable[[], str] | None = None,
                 subagent_fn: Callable[..., Awaitable[dict]] | None = None,
                 ralph_fn: Callable[..., Awaitable[dict]] | None = None,
                 workflow_fn: Callable[..., Awaitable[dict]] | None = None,
                 subagent_send_fn: Callable[..., Awaitable[dict]] | None = None,
                 subagent_fork_fn: Callable[..., Awaitable[dict]] | None = None,
                 subagent_list_fn: Callable[..., Awaitable[dict]] | None = None) -> None:
        self.reg = registry
        self.config = config or {}
        self.vision_fn = vision_fn
        self.ask_fn = ask_fn
        # 当前会话 id：todo/present/提问都按会话隔离，避免并发会话互相看到
        self.session_getter = session_getter
        # 子代理编排引擎（桥接层注入：真正跑子会话的逻辑在 bridge.run_subagent 等）
        self.subagent_fn = subagent_fn
        self.ralph_fn = ralph_fn
        self.subagent_send_fn = subagent_send_fn
        self.subagent_fork_fn = subagent_fork_fn
        self.subagent_list_fn = subagent_list_fn
        self.workflow_fn = workflow_fn
        self.allow_shell = bool(self.config.get("allow_shell", True))
        self.allow_web = bool(self.config.get("allow_web", True))
        self.allow_private_web = bool(self.config.get("allow_private_web", False))
        self.shell_timeout = float(self.config.get("shell_timeout_seconds", 120))
        self.question_timeout = float(
            self.config.get("question_timeout_seconds", _DEFAULT_QUESTION_TIMEOUT))
        self.jobs: dict[str, dict[str, Any]] = {}
        # 按会话隔离（原先全局共享：两个会话并发会互相看到对方的清单）
        self._todos: dict[str, list[dict[str, Any]]] = {}
        self._deliverables: dict[str, list[dict[str, Any]]] = {}
        self._pending_questions: dict[str, dict[str, Any]] = {}

    def _sid(self) -> str:
        """当前会话 id（取不到时用 'default'，测试与单会话场景仍然可用）。"""
        try:
            sid = self.session_getter() if self.session_getter else ""
        except Exception:
            sid = ""
        return str(sid or "default")

    @property
    def todos(self) -> list[dict[str, Any]]:
        return self._todos.get(self._sid(), [])

    @property
    def deliverables(self) -> list[dict[str, Any]]:
        return self._deliverables.get(self._sid(), [])

    @property
    def pending_question(self) -> dict[str, Any] | None:
        return self._pending_questions.get(self._sid())

    # ── 通用 ──
    def _path(self, path: str) -> Path:
        """复用核心注册表的路径沙箱。"""
        return self.reg.sandbox_path(path)

    def _rel(self, target: Path) -> str:
        try:
            return str(target.resolve().relative_to(self.reg.root))
        except (ValueError, OSError):
            return str(target)

    # ── search：按内容搜索 ──
    async def grep(self, pattern: str, path: str = ".", include: str = "",
                   max_results: int = 100) -> dict[str, Any]:
        """按内容正则搜索工作区文件，返回 文件:行号:内容。"""
        pattern = str(pattern or "").strip()
        if not pattern:
            raise ExtraToolError("pattern 不能为空")
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise ExtraToolError(f"正则表达式无效: {exc}") from exc
        limit = max(1, min(int(max_results or 100), 500))
        target = self._path(path or ".")
        files: list[Path] = []
        if target.is_file():
            files = [target]
        elif target.is_dir():
            for item in target.rglob(include or "*"):
                if item.is_file():
                    files.append(item)
                    if len(files) >= _MAX_GREP_FILES:
                        break
        else:
            raise ExtraToolError(f"路径不存在: {path}")

        matches: list[dict[str, Any]] = []
        truncated = False
        for f in files:
            try:
                if f.stat().st_size > _MAX_GREP_FILE_BYTES:
                    continue
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for no, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    matches.append({"path": self._rel(f), "line": no, "text": line[:300]})
                    if len(matches) >= limit:
                        truncated = True
                        break
            if truncated:
                break
        return {"pattern": pattern, "count": len(matches), "matches": matches,
                "truncated": truncated, "scanned_files": len(files)}

    # ── web：抓网页正文 ──
    @staticmethod
    def _host_is_private(url: str) -> bool:
        try:
            host = urllib.parse.urlparse(url).hostname or ""
        except Exception:
            return True
        if not host:
            return True
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except (ValueError, IndexError):
                continue
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast):
                return True
        return False

    @staticmethod
    def _html_to_text(raw: str) -> str:
        """HTML → 纯文本（去脚本/样式/标签，解实体；无第三方依赖）。"""
        text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
        text = re.sub(r"(?s)<!--.*?-->", " ", text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|section|article)>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html_mod.unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    async def web_fetch(self, url: str, max_chars: int = 20000) -> dict[str, Any]:
        """抓取 HTTP(S) URL 并返回纯文本（默认拒绝内网地址）。"""
        if not self.allow_web:
            raise ExtraToolError("web_fetch 已禁用（agent.tools_extra.web=false）")
        url = str(url or "").strip()
        if not re.match(r"^https?://", url, re.I):
            raise ExtraToolError("只支持 http(s) URL")
        if not self.allow_private_web and self._host_is_private(url):
            raise ExtraToolError(
                "拒绝访问内网/本地地址（SSRF 防护）；如确需访问请设 agent.tools_extra.allow_private_web=true")
        cap = max(1000, min(int(max_chars or 20000), 60000))
        headers = {"User-Agent": "CopilotBridgeAgent/1.0 (+local agent)"}
        try:
            if httpx is not None:
                async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                    resp = await client.get(url, headers=headers)
                status = resp.status_code
                ctype = resp.headers.get("content-type", "")
                raw = resp.text
                final_url = str(resp.url)
            else:  # 极简降级：无 httpx 时用 urllib（阻塞在线程里）
                import urllib.request

                def _fetch() -> tuple[int, str, str, str]:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=30) as r:
                        return (r.status, r.headers.get("content-type", ""),
                                r.read(2_000_000).decode("utf-8", "replace"), r.geturl())

                status, ctype, raw, final_url = await asyncio.to_thread(_fetch)
        except Exception as exc:
            raise ExtraToolError(f"抓取失败: {exc}") from exc
        low = (ctype or "").lower()
        if "html" in low or raw.lstrip()[:1] == "<":
            text = self._html_to_text(raw)
        elif "json" in low:
            try:
                text = json.dumps(json.loads(raw), ensure_ascii=False, indent=1)
            except Exception:
                text = raw
        else:
            text = raw
        total = len(text)
        return {"url": final_url, "status": status, "content_type": ctype,
                "chars": total, "truncated": total > cap, "text": text[:cap]}

    # ── shell：执行命令 + 后台任务 ──
    def _shell_exe(self) -> str:
        for name in ("pwsh", "powershell"):
            exe = shutil.which(name)
            if exe:
                return exe
        raise ExtraToolError("找不到 pwsh/powershell 可执行文件")

    def _spawn(self, command: str, cwd: Path) -> asyncio.subprocess.Process:
        exe = self._shell_exe()
        return asyncio.create_subprocess_exec(
            exe, "-NoProfile", "-NonInteractive", "-Command", command,
            cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    @staticmethod
    async def _kill_tree(proc: Any) -> None:
        """杀掉整个进程树。

        Windows 上 `proc.kill()` 只结束直接子进程（pwsh.exe），它启动的
        孙进程（python/git/pip）会继续持有 stdout 管道——`communicate()` 永远
        等不到 EOF，于是"超时"形同虚设（实测：一条 pip/测试命令把任务卡了 8 分钟
        还没返回，界面上只显示"等待 Copilot 回答"）。
        """
        import os
        import subprocess as sp
        try:
            if os.name == "nt":
                await asyncio.create_subprocess_exec(
                    "taskkill", "/T", "/F", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
            else:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    async def pwsh(self, command: str, workdir: str = "", timeout_seconds: float | None = None,
                   run_in_background: bool = False) -> dict[str, Any]:
        """执行 PowerShell 命令。run_in_background=true 时返回 job_id（用 job_output 取输出）。"""
        if not self.allow_shell:
            raise ExtraToolError("pwsh 已禁用（agent.allow_shell=false）")
        command = str(command or "").strip()
        if not command:
            raise ExtraToolError("command 不能为空")
        cwd = self._path(workdir) if workdir else self.reg.root
        if not cwd.is_dir():
            raise ExtraToolError(f"工作目录不存在: {workdir}")
        timeout = float(timeout_seconds or self.shell_timeout)
        try:
            proc = await self._spawn(command, cwd)
        except ExtraToolError:
            raise
        except Exception as exc:
            raise ExtraToolError(f"启动命令失败: {exc}") from exc

        if run_in_background:
            job_id = uuid.uuid4().hex[:10]
            job: dict[str, Any] = {"id": job_id, "command": command, "cwd": str(cwd),
                                   "proc": proc, "out": [], "err": [],
                                   "done": False, "exit_code": None, "started": time.time()}
            self.jobs[job_id] = job

            async def _pump(stream: Any, buf: list[str]) -> None:
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        return
                    buf.append(chunk.decode("utf-8", "replace"))

            asyncio.create_task(_pump(proc.stdout, job["out"]))
            asyncio.create_task(_pump(proc.stderr, job["err"]))

            async def _wait() -> None:
                job["exit_code"] = await proc.wait()
                job["done"] = True

            asyncio.create_task(_wait())
            return {"job_id": job_id, "command": command, "status": "running",
                    "hint": "用 job_output 取输出，job_kill 终止"}

        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._kill_tree(proc)
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=5)
            except Exception:
                out_b, err_b = b"", b""
            partial = (out_b or b"").decode("utf-8", "replace")[-800:]
            tail = (err_b or b"").decode("utf-8", "replace")[-400:]
            msg = (f"命令超时（>{timeout:.0f}s），已终止整个进程树: {command[:160]}\n"
                   "换策略：拆小命令、加更精确的筛选参数，或用 run_in_background=true 后 job_output 轮询。")
            if partial:
                msg += f"\n超时前输出（末尾）：\n{partial}"
            if tail:
                msg += f"\nstderr：\n{tail}"
            raise ExtraToolError(msg)
        except Exception as exc:
            raise ExtraToolError(f"执行失败: {exc}") from exc
        out = (out_b or b"").decode("utf-8", "replace")
        err = (err_b or b"").decode("utf-8", "replace")
        return {"exit_code": proc.returncode, "stdout": out[:_MAX_OUTPUT],
                "stderr": err[:_MAX_OUTPUT],
                "truncated": len(out) > _MAX_OUTPUT or len(err) > _MAX_OUTPUT,
                "command": command}

    def _job_text(self, job: dict[str, Any], since: int = 0) -> dict[str, Any]:
        out = "".join(job["out"])
        err = "".join(job["err"])
        return {"job_id": job["id"], "command": job["command"], "status":
                "done" if job["done"] else "running",
                "exit_code": job["exit_code"],
                "stdout": out[since:since + _MAX_OUTPUT],
                "stderr": err[since:since + _MAX_OUTPUT],
                "chars": len(out) + len(err)}

    async def job_list(self) -> dict[str, Any]:
        """列出后台任务。"""
        return {"jobs": [{"job_id": j["id"], "command": j["command"][:120],
                          "status": "done" if j["done"] else "running",
                          "exit_code": j["exit_code"]}
                         for j in self.jobs.values()]}

    async def job_output(self, job_id: str, wait: bool = False,
                         timeout_seconds: float = 30) -> dict[str, Any]:
        """读取后台任务输出（wait=true 时等到结束或超时）。"""
        job = self.jobs.get(str(job_id))
        if not job:
            raise ExtraToolError(f"没有这个后台任务: {job_id}")
        if wait and not job["done"]:
            deadline = time.monotonic() + float(timeout_seconds)
            while time.monotonic() < deadline and not job["done"]:
                await asyncio.sleep(0.2)
        return self._job_text(job)

    async def job_kill(self, job_id: str) -> dict[str, Any]:
        """终止后台任务。"""
        job = self.jobs.get(str(job_id))
        if not job:
            raise ExtraToolError(f"没有这个后台任务: {job_id}")
        if not job["done"]:
            try:
                job["proc"].kill()
            except ProcessLookupError:
                pass
        return {"job_id": job["id"], "status": "killed", "exit_code": job["exit_code"]}

    # ── plan：任务清单 / 交付物 ──
    async def todo_write(self, todos: list[Any]) -> dict[str, Any]:
        """写入任务清单（整体替换；最多 50 项）。"""
        if not isinstance(todos, list):
            raise ExtraToolError("todos 必须是数组")
        clean: list[dict[str, Any]] = []
        for item in todos[:50]:
            if isinstance(item, dict):
                content = str(item.get("content") or "").strip()
                status = str(item.get("status") or "pending").strip()
            else:
                content = str(item).strip()
                status = "pending"
            if not content:
                continue
            if status not in ("pending", "in_progress", "completed"):
                status = "pending"
            clean.append({"content": content[:200], "status": status})
        self._todos[self._sid()] = clean
        # 同步进会话状态：内存字典重启即丢，而且界面要靠 state.todos 显示"计划 x/y"
        self._save_state(todos=clean)
        return {"todos": clean, "count": len(clean),
                "pending": sum(1 for t in clean if t["status"] != "completed")}

    async def present(self, files: list[Any]) -> dict[str, Any]:
        """声明交付物（最多 4 个；只记录，不复制文件）。"""
        if not isinstance(files, list):
            raise ExtraToolError("files 必须是数组")
        items: list[dict[str, Any]] = []
        for item in files[:4]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            target = self._path(path)
            items.append({"path": path, "description": str(item.get("description") or "")[:200],
                          "exists": target.is_file(), "bytes":
                          target.stat().st_size if target.is_file() else 0})
        self._deliverables[self._sid()] = items
        return {"presented": items, "count": len(items)}

    # ── vision：图片 ──
    @staticmethod
    def _image_meta(target: Path) -> dict[str, Any]:
        """不依赖 Pillow 的轻量图片元数据（格式/尺寸/大小）。"""
        data = target.read_bytes()[:64]
        fmt, dims = "", None
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            fmt = "png"
            if len(data) >= 24:
                dims = (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
        elif data[:2] == b"\xff\xd8":
            fmt = "jpeg"          # 尺寸解析较重，跳过（交给 Copilot 视觉）
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            fmt = "gif"
            if len(data) >= 10:
                dims = (int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little"))
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            fmt = "webp"
        else:
            fmt = target.suffix.lstrip(".").lower() or "unknown"
        return {"format": fmt, "dimensions": dims, "bytes": target.stat().st_size}

    async def read_image(self, path: str, question: str = "",
                         analyze: bool = True) -> dict[str, Any]:
        """读取图片元数据；analyze=true 时交给 Copilot 视觉描述（需要回调）。"""
        target = self._path(path)
        if not target.is_file():
            raise ExtraToolError(f"文件不存在: {path}")
        if target.stat().st_size > 20 * 1024 * 1024:
            raise ExtraToolError("图片超过 20MB，请先压缩")
        meta = self._image_meta(target)
        result: dict[str, Any] = {"path": path, **meta}
        if analyze:
            if self.vision_fn is None:
                result["error"] = ("当前通道没有视觉能力（需要 Copilot 网页会话）；"
                                   "已返回元数据，如需描述请改用 copilot_attach 上传")
            else:
                try:
                    ans = await self.vision_fn(path, question or
                                               "描述这张图片的内容；如果里面有文字，请提取文字。")
                    result["analysis"] = ans
                except Exception as exc:
                    result["error"] = f"视觉分析失败: {exc}"
        return result

    # ── team：子代理 / 迭代 / 编排 ──
    def _steps(self, max_steps: int) -> int:
        """max_steps<=0 时取配置 agent.subagent_max_steps（默认 80，夹 10~200）。"""
        try:
            n = int(max_steps or 0)
        except Exception:
            n = 0
        if n > 0:
            return max(3, min(n, 200))
        try:
            from . import bridge as _b
            return _b.subagent_max_steps()
        except Exception:
            return 80

    async def subagent(self, task: str, description: str = "", model: str = "",
                       max_steps: int = 0) -> dict[str, Any]:
        """把独立子任务交给子代理（独立会话/独立上下文），返回结果。"""
        max_steps = self._steps(max_steps)
        if self.subagent_fn is None:
            raise ExtraToolError("当前通道不支持子代理")
        task = str(task or "").strip()
        if not task:
            raise ExtraToolError("task 不能为空")
        return await self.subagent_fn(task, description, model, max_steps)

    async def subagent_many(self, tasks: list | None = None, model: str = "",
                            max_steps: int = 0, stop_on_error: bool = False) -> dict[str, Any]:
        """一次派多个**互不依赖**的子任务（扇出），全部跑完再返回汇总。

        为什么需要：模型要派 3 个子代理就得连写 3 次 `subagent`，弱模型往往直接
        自己串行扫完了（实测 Kimi 就这么干）。扇出把它压成**一次调用**。
        **API 提供商模型真并行**（上限 `agent.subagent_parallel`，默认 4）；网页
        Copilot 通道仍然顺序（只有一个 page，并发会互相踩页）。
        """
        max_steps = self._steps(max_steps)
        if self.subagent_fn is None:
            raise ExtraToolError("当前通道不支持子代理")
        items = [t for t in (tasks or []) if t]
        if not items:
            raise ExtraToolError("tasks 不能为空（数组，每项 {task, description} 或直接字符串）")
        if len(items) > 6:
            raise ExtraToolError(f"一次最多 6 个子任务（收到 {len(items)} 个）")
        # 先把每项校验/归一化：并行时不能一半已启动才发现格式错
        plan: list[tuple[int, str, str]] = []
        for idx, raw in enumerate(items, 1):
            if isinstance(raw, str):
                task, label = raw, ""
            elif isinstance(raw, dict):
                task = str(raw.get("task") or "").strip()
                label = str(raw.get("description") or raw.get("label") or "").strip()
            else:
                raise ExtraToolError(f"第 {idx} 项格式不对（应为字符串或 {task/description} 对象）")
            if not task:
                raise ExtraToolError(f"第 {idx} 项 task 为空")
            plan.append((idx, task, label))
        results, t0 = [], time.time()
        # 复用提示：父会话下已有高度相似的子代理时，提醒模型可用 subagent_send 追问
        _sib_hint = ""
        try:
            from . import bridge as _b2
            _parent = str(_b2.current_session() or "")
            _sibs = [s for s in _b2.AGENT_SESSIONS.list(include_archived=True)
                     if str(s.get("parent_id") or "") == _parent
                     and str(s.get("state")) != "cancelled"]
            for _i, _task, _l in plan:
                _h = similar_task_hint(_task, _sibs)
                if _h:
                    _sib_hint = _h
                    break
        except Exception:
            _sib_hint = ""
        try:
            from . import bridge as _b
            _mdl = model or str((_b.AGENT_SESSIONS.get(_b.current_session()) or {}).get("model") or "")
            _parallel = _b.model_is_provider(_mdl)
            # 提供商：真并行；网页通道：并行数 = 已开的自动化窗口数（默认 1 = 顺序）
            _cap = _b.subagent_parallel() if _parallel else _b.web_subagent_parallel()
        except Exception:
            _parallel, _cap = False, 1
        if _parallel and len(plan) > 1:
            sem = asyncio.Semaphore(_cap)

            async def run_one(idx: int, task: str, label: str) -> dict:
                async with sem:
                    try:
                        out = await self.subagent_fn(task, label, model, max_steps)
                        # 子代理失败时 _drive_child 是**带 error 返回**而不是抛异常，
                        # 只看异常会把失败算成功（实测：子代理 500 却 ok=true）
                        err = out.get("error")
                        return {"index": idx, "task": task[:120], "ok": not err,
                                "agent_id": out.get("agent_id"), "steps": out.get("steps"),
                                "content": out.get("content") or "",
                                **({"error": err} if err else {})}
                    except Exception as exc:              # noqa: BLE001 单项失败要能继续
                        return {"index": idx, "task": task[:120], "ok": False,
                                "error": f"{type(exc).__name__}: {exc}"}

            results = list(await asyncio.gather(*(run_one(i, t, l) for i, t, l in plan)))
        else:
            for idx, task, label in plan:
                try:
                    out = await self.subagent_fn(task, label, model, max_steps)
                    err = out.get("error")
                    results.append({"index": idx, "task": task[:120], "ok": not err,
                                    "agent_id": out.get("agent_id"), "steps": out.get("steps"),
                                    "content": out.get("content") or "",
                                    **({"error": err} if err else {})})
                except Exception as exc:                      # noqa: BLE001 单项失败要能继续
                    results.append({"index": idx, "task": task[:120], "ok": False,
                                    "error": f"{type(exc).__name__}: {exc}"})
                    if stop_on_error:
                        break
        ok = [r for r in results if r["ok"]]
        _out = {"requested": len(items), "succeeded": len(ok), "failed": len(results) - len(ok),
                "seconds": round(time.time() - t0, 1), "parallel": bool(_parallel),
                "concurrency": _cap, "results": results}
        if _sib_hint:
            _out["hint"] = _sib_hint
        return _out

    async def subagent_send(self, session_id: str = "", message: str = "",
                            model: str = "", max_steps: int = 0,
                            agent_id: str = "") -> dict[str, Any]:
        """继续一个已有子代理会话（同一上下文追加一轮）。

        `agent_id` 是 `subagent` 返回值里的字段名，`session_id` 是工具参数名；
        两个都收，避免模型按返回值字段名传参时报参数错误。
        """
        if self.subagent_send_fn is None:
            raise ExtraToolError("当前通道不支持继续子代理")
        max_steps = self._steps(max_steps)      # 不传就取 agent.subagent_max_steps
        target = str(session_id or agent_id or "").strip()
        if not target:
            raise ExtraToolError("需要 session_id（子代理返回的 agent_id）")
        return await self.subagent_send_fn(target, str(message or ""), model, max_steps)

    async def subagent_fork(self, session_id: str, description: str = "",
                            keep_messages: int = 60) -> dict[str, Any]:
        """从某个会话分叉出新的子代理（复制最近对话）。"""
        if self.subagent_fork_fn is None:
            raise ExtraToolError("当前通道不支持分叉子代理")
        return await self.subagent_fork_fn(str(session_id or ""), description, keep_messages)

    async def subagent_list(self, parent_id: str = "", limit: int = 50) -> dict[str, Any]:
        """列出子代理会话。"""
        if self.subagent_list_fn is None:
            raise ExtraToolError("当前通道不支持列出子代理")
        return await self.subagent_list_fn(str(parent_id or ""), limit)

    async def ralph(self, objective: str, max_rounds: int = 5, model: str = "") -> dict[str, Any]:
        """目标迭代：每轮用全新子代理推进目标，直到声明完成或用尽轮次。"""
        if self.ralph_fn is None:
            raise ExtraToolError("当前通道不支持 ralph")
        return await self.ralph_fn(objective, max_rounds, model)

    async def workflow(self, spec: dict, model: str = "") -> dict[str, Any]:
        """按阶段编排多个子代理（阶段按顺序推进；阶段内子代理的具体执行仍由 subagent 决定）。"""
        if self.workflow_fn is None:
            raise ExtraToolError("当前通道不支持 workflow")
        return await self.workflow_fn(spec, model)

    # ── goal：跨轮目标 ──
    def _session_state(self) -> dict[str, Any]:
        getter = getattr(self.reg, "state_getter", None)
        try:
            return (getter() if getter else {}) or {}
        except Exception:
            return {}

    def _save_state(self, **kw: Any) -> None:
        saver = getattr(self.reg, "state_saver", None)
        if not saver:
            return
        try:
            saver(**kw)
        except Exception as exc:
            # 不能静默：曾经因为 sessions 表没有 goal 列，写库抛错被吞，
            # 表现成"create_goal 成功但目标从未落库"（update_goal 随即说没有目标）
            print(f"[tools_extra] 会话状态写入失败 {list(kw)}: {exc}", flush=True)

    async def create_goal(self, objective: str, max_rounds: int = 10) -> dict[str, Any]:
        """创建一个目标；Agent 在返回 final 前会自动检查，未完成则继续下一轮。"""
        obj = str(objective or "").strip()
        if not obj:
            raise ExtraToolError("objective 不能为空")
        rounds = max(1, min(int(max_rounds or 10), 50))
        goal = {"objective": obj[:500], "rounds": 0, "max_rounds": rounds,
                "status": "active", "created": time.time()}
        self._save_state(goal=goal)
        return {"goal": goal,
                "hint": f"目标已创建（最多 {rounds} 轮）；未完成时返回 final 会自动续轮，"
                        "完成时用 update_goal(complete) 结束"}

    async def get_goal(self) -> dict[str, Any]:
        """读取当前目标与进度。"""
        goal = self._session_state().get("goal")
        if not isinstance(goal, dict):
            return {"goal": None, "detail": "当前会话没有目标"}
        return {"goal": goal}

    async def update_goal(self, action: str, objective: str = "") -> dict[str, Any]:
        """更新目标状态：complete / blocked / pause / resume / edit。"""
        act = str(action or "").strip().lower()
        goal = self._session_state().get("goal")
        if not isinstance(goal, dict):
            raise ExtraToolError("当前会话没有目标")
        if act == "edit":
            obj = str(objective or "").strip()
            if not obj:
                raise ExtraToolError("edit 需要 objective")
            goal["objective"] = obj[:500]
        elif act in ("complete", "blocked", "active"):
            goal["status"] = act
        elif act in ("pause", "paused"):
            goal["status"] = "paused"
        elif act in ("resume", "active"):
            goal["status"] = "active"
        else:
            raise ExtraToolError(f"不支持的 action: {action}（可用 complete/blocked/pause/resume/edit）")
        self._save_state(goal=goal)
        return {"goal": goal}

    # ── ask：向用户提问 ──
    async def ask_user_question(self, question: str, options: list[Any] | None = None) -> dict[str, Any]:
        """向用户提问并阻塞等待回答（超时返回 unanswered）。"""
        question = str(question or "").strip()
        if not question:
            raise ExtraToolError("question 不能为空")
        opts = [str(o).strip() for o in (options or []) if str(o).strip()][:6]
        if self.ask_fn is None:
            raise ExtraToolError("当前通道不支持向用户提问（需要 WebUI 会话）")
        sid = self._sid()
        self._pending_questions[sid] = {"question": question, "options": opts, "asked": time.time()}
        try:
            return await self.ask_fn(question, opts)
        finally:
            self._pending_questions.pop(sid, None)


# ────────────────────────── 注册入口 ──────────────────────────

_GROUPS: dict[str, tuple[str, ...]] = {
    "search": ("grep",),
    "web": ("web_fetch",),
    "shell": ("pwsh", "job_list", "job_output", "job_kill"),
    "plan": ("todo_write", "present"),
    "vision": ("read_image",),
    "ask": ("ask_user_question",),
    "goal": ("create_goal", "get_goal", "update_goal"),
    "team": ("subagent", "subagent_many", "subagent_send", "subagent_fork",
             "subagent_list", "ralph", "workflow"),
}

_DESCRIPTIONS: dict[str, tuple[str, dict[str, Any]]] = {
    "grep": ("按内容正则搜索工作区文件（find_files 只按文件名），返回 文件:行号:内容",
             {"pattern": "正则表达式", "path": "搜索目录或文件，默认 .",
              "include": "文件名 glob 过滤，例如 *.py", "max_results": "最多返回条数，默认 100"}),
    "web_fetch": ("抓取 HTTP(S) URL 并返回纯文本（HTML 自动去标签；默认拒绝内网地址）",
                  {"url": "http(s) URL", "max_chars": "最多返回字符数，默认 20000"}),
    "pwsh": ("执行 PowerShell 命令（默认需要一次性审批）；run_in_background=true 时返回 job_id",
             {"command": "要执行的命令", "workdir": "工作目录（相对工作区），默认工作区根",
              "timeout_seconds": "超时秒数，默认 120", "run_in_background": "后台执行，默认 false"}),
    "job_list": ("列出后台任务", {}),
    "job_output": ("读取后台任务输出（wait=true 等到结束或超时）",
                   {"job_id": "任务 id", "wait": "是否等待完成", "timeout_seconds": "等待上限秒数，默认 30"}),
    "job_kill": ("终止后台任务", {"job_id": "任务 id"}),
    "todo_write": ("写入任务清单（整体替换，最多 50 项）；长任务用它跟踪进度",
                   {"todos": "数组；每项 {content, status=pending|in_progress|completed}"}),
    "present": ("声明交付物（最多 4 个文件）。只登记供界面展示：文件本来就在本地工作区，"
                "不会复制或上传，回答里要说「已生成到 <工作区相对路径>」，不要说「可下载」",
                {"files": "数组；每项 {path, description}"}),
    "read_image": ("读取图片元数据（格式/尺寸/大小）；analyze=true 时交给 Copilot 视觉描述",
                   {"path": "图片相对路径", "question": "要问 Copilot 的问题（可选）",
                    "analyze": "是否调用视觉分析，默认 true"}),
    "ask_user_question": ("任务中途向用户提问并等待回答（适合需要用户决策/补充信息时，避免瞎猜）",
                          {"question": "要问用户的问题", "options": "可选项列表（可选）"}),
    "create_goal": ("创建一个跨轮目标：未完成时返回 final 会自动续轮（最多 max_rounds 轮），完成后用 update_goal 结束",
                    {"objective": "目标（具体、可判定完成）", "max_rounds": "最多续轮次数，默认 10"}),
    "get_goal": ("读取当前目标与进度", {}),
    "update_goal": ("更新目标状态：complete 完成 / blocked 卡住 / pause 暂停 / resume 恢复 / edit 改目标",
                    {"action": "complete|blocked|pause|resume|edit", "objective": "action=edit 时的新目标"}),
    "subagent": ("把一段自足的任务交给子代理（独立会话、独立上下文），用于隔离上下文或并行推进调研/分析/局部改造；返回它的最终结果与步数，不返回中间步骤",
                 {"task": "完整的自包含任务：它看不到本次对话，目标/路径/验收标准/返回格式都要写全", "description": "3-5 词标签，仅用于展示",
                  "model": "子代理模型（默认跟随父会话）", "max_steps": "子任务步数上限，默认取 agent.subagent_max_steps（通常 80），上限 200"}),
    "subagent_many": ("一次派多个互不依赖的子任务（API 提供商模型并行跑、上限 agent.subagent_parallel；网页 Copilot 通道顺序；全部跑完再返回汇总）；"
                      "把任务拆成 2~6 个独立子目标时用它，一次调用顶多次 subagent",
                      {"tasks": "数组，每项 {task:'自足的任务描述', description:'3-5 词标签'}",
                       "model": "子代理模型（默认跟随父会话）", "max_steps": "每个子任务步数上限，默认取 agent.subagent_max_steps（通常 80），上限 200",
                       "stop_on_error": "某一项失败后是否停止后续，默认 false"}),
    "subagent_send": ("在已有子代理会话里追加一轮：它记得之前的上下文，只写新增的要求",
                       {"session_id": "子代理会话 ID（即 subagent 返回的 agent_id；也可用 agent_id 传）",
                        "message": "要追加的话", "model": "模型（默认沿用子会话）",
                        "max_steps": "本轮步数上限，默认取 agent.subagent_max_steps"}),
    "subagent_fork": ("从当前会话分叉出子代理：它继承已完成的对话，适合试第二条路线/另一版实现；"
                      "任务里只写新增要求，别复述已有上下文",
                      {"session_id": "源会话 ID（默认当前会话）", "description": "3-5 词标签，仅用于展示",
                       "keep_messages": "复制最近多少条消息，默认 60"}),
    "subagent_list": ("列出子代理会话（可按父会话过滤）与状态/步数",
                      {"parent_id": "父会话 ID（可选）", "limit": "最多条数，默认 50"}),
    "ralph": ("目标迭代：每轮用全新子代理推进目标，直到子代理声明 GOAL_COMPLETE 或用尽轮次",
              {"objective": "目标", "max_rounds": "最多轮次，默认 5", "model": "模型（默认跟随父会话）"}),
    "workflow": ("按阶段编排多个子代理（阶段内顺序；子代理本身在 API 提供商模型上可并行）",
                 {"spec": "对象 {name, phases:[{title, agents:[{prompt,label}]}]}", "model": "模型"}),
}


_APPROVAL_TOOLS: frozenset[str] = frozenset({"pwsh"})   # 破坏性：注册时声明，核心代码不再特判


def register_extra_tools(registry: Any, config: dict[str, Any] | None = None,
                         vision_fn: Callable[[str, str], Awaitable[dict]] | None = None,
                         ask_fn: Callable[[str, list[str]], Awaitable[dict]] | None = None,
                         session_getter: Callable[[], str] | None = None,
                         subagent_fn: Callable[..., Awaitable[dict]] | None = None,
                         ralph_fn: Callable[..., Awaitable[dict]] | None = None,
                         workflow_fn: Callable[..., Awaitable[dict]] | None = None,
                         subagent_send_fn: Callable[..., Awaitable[dict]] | None = None,
                         subagent_fork_fn: Callable[..., Awaitable[dict]] | None = None,
                         subagent_list_fn: Callable[..., Awaitable[dict]] | None = None
                         ) -> "_Extra":
    """把扩展工具注册到 ToolRegistry；返回实现对象（桥接层可读 todos/deliverables）。

    逐组开关：config["tools_extra"] = {"search": true, "web": true, ...}；
    未配置的组默认开启（vision/ask 还需要传入回调才真正可用）。
    """
    cfg = config or {}
    impl = _Extra(registry, cfg, vision_fn=vision_fn, ask_fn=ask_fn,
                  session_getter=session_getter,
                  subagent_fn=subagent_fn, ralph_fn=ralph_fn, workflow_fn=workflow_fn,
                  subagent_send_fn=subagent_send_fn, subagent_fork_fn=subagent_fork_fn,
                  subagent_list_fn=subagent_list_fn)
    groups = cfg.get("tools_extra") if isinstance(cfg.get("tools_extra"), dict) else {}
    enabled: set[str] = set()
    for group, names in _GROUPS.items():
        if groups.get(group, True):
            enabled.update(names)
    for name in sorted(enabled):
        fn = getattr(impl, name)
        desc, args = _DESCRIPTIONS.get(name, (name, {}))
        registry.register(name, fn, desc, args, approval=name in _APPROVAL_TOOLS)
    impl.registered = sorted(enabled)
    return impl
