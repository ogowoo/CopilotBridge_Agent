"""workspace_files.py — 工作区文件与目录操作：写文件、建目录、系统目录选择对话框、目录浏览

从 bridge.py 拆出（第四批解耦）。约定：模块级可变状态与配置仍在 bridge.py，
这里经 ``b.xxx`` 读写；本模块内部互相调用保持直接调用。本模块不定义 FastAPI 端点。
"""
from __future__ import annotations

import asyncio, base64, collections, contextlib, json, os, re, shutil, time, uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import HTTPException

from . import bridge as b


def save_workspace_file(rel: str, data: bytes) -> str:
    """把二进制写进工作区（插件用；复用路径沙箱），返回相对路径。"""
    target = b.AGENT_TOOLS.sandbox_path(str(rel or ""))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    try:
        return str(target.relative_to(b.AGENT_TOOLS.root))
    except ValueError:
        return str(target)


def create_directory(parent: str, name: str) -> dict:
    """在 parent 下创建子目录（工作目录选择器的「新建目录」）。

    只允许**单层目录名**（拒绝分隔符/..），避免借选择器在任意位置递归建树。
    """
    parent_path = str(parent or "").strip() or str(b.AGENT_TOOLS.root)
    name = str(name or "").strip()
    if not name:
        raise b.ToolError("目录名不能为空")
    if any(ch in name for ch in ("/", "\\", ":", "*", "?", '"', "<", ">", "|")):
        raise b.ToolError("目录名不能包含路径分隔符或非法字符")
    if name in (".", ".."):
        raise b.ToolError("目录名非法")
    base = Path(parent_path)
    if not base.is_dir():
        raise b.ToolError(f"父目录不存在: {parent_path}")
    target = base / name
    if target.exists():
        raise b.ToolError(f"目录已存在: {target}")
    try:
        target.mkdir()
    except OSError as exc:
        raise b.ToolError(f"创建目录失败: {exc}") from exc
    return {"path": str(target), "name": name, "parent": str(base)}


# ── 工作目录选择器：系统原生对话框（对应 DSH 的 native 后端）──────────
async def pick_directory_native(start: str | None = None) -> dict:
    """弹系统文件夹对话框（Windows），返回用户选中的路径。

    对应 DSH 的 `dsh-host-directory-picker-native`（它用 COM IFileOpenDialog；
    这里用 PowerShell 的 FolderBrowserDialog，效果等价且无需 ctypes 手写 COM）。
    对话框在服务所在桌面弹出：本地用户能看到；非交互会话会超时并让前端回退到
    browse 模式（与 DSH 的 auto 行为一致）。
    """
    import subprocess as _sp

    base = str(start or "").strip() or str(b.AGENT_TOOLS.root)
    safe = base.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog;"
        f"$f.Description = '选择工作目录';"
        f"$f.SelectedPath = '{safe}';"
        "$f.ShowNewFolderButton = $true;"
        "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK)"
        " { Write-Output ('CBP_PICK=' + $f.SelectedPath) }")
    exe = shutil.which("powershell") or "powershell"
    loop = asyncio.get_running_loop()

    def _run() -> tuple[int, str]:
        proc = _sp.run([exe, "-NoProfile", "-STA", "-Command", script],
                       capture_output=True, text=True, timeout=15,
                       creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    try:
        rc, out = await loop.run_in_executor(None, _run)
    except Exception as exc:
        raise b.ToolError(f"无法打开系统目录对话框（{exc}）；可改用应用内浏览") from exc
    m = re.search(r"CBP_PICK=(.+)", out or "")
    if not m:
        return {"path": "", "cancelled": True}
    chosen = m.group(1).strip()
    if not chosen or not Path(chosen).is_dir():
        return {"path": "", "cancelled": True}
    return {"path": str(Path(chosen)), "cancelled": False}


# ── 工作目录选择器：目录浏览（只列子目录，供 WebUI 选择器）──────────
def browse_directories(path: str | None = None) -> dict:
    """列出目录下的**子目录**，供 WebUI 的工作目录选择器。

    不传 path 时从当前工作区开始（默认 workspace）；不列隐藏/依赖目录，
    单层最多 300 项（防止 huge 目录把界面撑爆）。
    """
    start = str(path or "").strip()
    base = Path(start).expanduser() if start else Path(b.AGENT_TOOLS.root)
    try:
        base = base.resolve()
    except Exception:
        base = Path(b.AGENT_TOOLS.root).resolve()
    if not base.is_dir():
        base = Path(b.AGENT_TOOLS.root).resolve()
    skip = {"__pycache__", "node_modules", "edgeprofile", ".git", "site-packages"}
    entries: list[dict] = []
    try:
        for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            try:
                if not child.is_dir():
                    continue
                name = child.name
                if name.startswith((".", "$")) or name.lower() in skip:
                    continue
                entries.append({"name": name, "path": str(child)})
            except OSError:
                continue
            if len(entries) >= 300:
                break
    except (PermissionError, OSError):
        pass
    parent = base.parent if base.parent != base else None
    return {"path": str(base), "parent": str(parent) if parent else "",
            "entries": entries, "default": str(b.AGENT_TOOLS.root),
            "is_default": str(base) == str(b.AGENT_TOOLS.root)}
