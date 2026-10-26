"""CBP 插件系统（与 DSH 插件语义兼容）。

**为什么是"语义兼容"而不是"跑 DSH 插件"**：DSH 插件是 npm 包 + Cordis 插件
（JS 的 `apply(ctx)`）在 Node 里跑；CBP 是 Python 服务。能对齐的是**契约**：
同形的清单、同一套贡献点（tools / providers / api / ui / events）、同样的
加载→apply→卸载生命周期、同样的"id + disabled + config"启停语义。做不到的是
直接执行 JS 插件代码——那需要内嵌 Node 运行时，不在本系统目标内。

**清单格式（与 DSH 的 `package.json` → `"dsh"` 字段对齐）**：

`plugins/<name>/plugin.json`（也接受 `package.json` 里放 `"cbp"` 字段）：

    {
      "name": "web-search",
      "version": "0.1.0",
      "description": "联网搜索",
      "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},   // 可选：DSH 元数据
      "cbp": {
        "entry": "plugin.py",
        "permissions": ["tools", "api"],
        "contributes": {"tools": ["web_search"], "api": ["GET /api/search"]},
        "config": {"backend": "duckduckgo"}
      }
    }

**入口模块**（`plugin.py`）：

    def setup(ctx):     # 加载时调用；ctx 相当于 DSH/Cordis 的 ctx
        ctx.register_tool("web_search", handler, "说明", {"query": "..."})
        ctx.on("session_end", callback)

    def teardown(ctx):  # 卸载时调用（可选）
        ...

**ctx 能力**
- `ctx.name / ctx.config / ctx.dir / ctx.log()`
- `ctx.register_tool(name, fn, description, arguments, approval=False, group="plugin")`
- `ctx.register_provider(name, cfg)` —— 追加/覆盖 API 提供商预设
- `ctx.register_api(method, path, handler)` —— 挂到 `/{plugin}/api/{path}`
- `ctx.on(event, handler)` —— 订阅 `session_start` / `step_done` / `session_end`
- `ctx.state` / `ctx.save_state(**kw)` —— 插件自己的持久化小状态
- `ctx.read_text(rel)` / `ctx.write_text(rel, text)` —— 限定在插件目录内的文件访问

**启停**：`data/plugins.json` 记录 `{name: {"enabled": bool}}`；禁用即卸载
（工具/提供商/路由/事件一起摘掉），启用即重新 apply——不重启服务。
"""

from __future__ import annotations

import re
import asyncio
import shutil
import subprocess
import tempfile
import zipfile
import importlib.util
import inspect
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

PLUGIN_DIR_NAME = "plugins"
MANIFEST_NAMES = ("plugin.json", "package.json")
# 插件声明式面板支持的渲染方式（插件只给数据，前端负责渲染——插件不注入 HTML）
PANEL_VIEWS = ("kv", "list", "table", "json", "markdown")

ALLOWED_PERMISSIONS = {"tools", "providers", "api", "ui", "events", "storage", "files"}
PERMISSION_LABELS = {
    "tools": "注册 Agent 工具",
    "providers": "追加/覆盖模型提供商",
    "api": "注册 HTTP 接口",
    "ui": "声明前端面板",
    "events": "订阅会话事件",
    "storage": "读写插件状态",
    "files": "读写插件目录内文件",
}


class PluginError(RuntimeError):
    """插件加载/校验/执行的可预期错误。"""


@dataclass
class PluginManifest:
    name: str
    version: str = "0.1.0"
    description: str = ""
    entry: str = "plugin.py"
    permissions: list[str] = field(default_factory=list)
    contributes: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    dsh: dict[str, Any] = field(default_factory=dict)     # DSH 兼容元数据（只读透传）
    # 插件自带译文：{"en": {"中文原文": "English"}, "tw": {...}}
    # 平台不知道第三方插件写了哪些中文，让它自己在清单里带上最不失真。
    i18n: dict[str, dict[str, str]] = field(default_factory=dict)
    dir: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "description": self.description,
                "entry": self.entry, "permissions": self.permissions,
                "contributes": self.contributes, "config": self.config, "dsh": self.dsh,
                "i18n": self.i18n}


def _read_manifest(plugin_dir: Path) -> PluginManifest | None:
    """读插件清单：plugin.json 优先，其次 package.json 的 `cbp` 字段。"""
    for fname in MANIFEST_NAMES:
        path = plugin_dir / fname
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PluginError(f"{plugin_dir.name}: 清单不是合法 JSON（{fname}）: {exc}") from exc
        if not isinstance(raw, dict):
            raise PluginError(f"{plugin_dir.name}: 清单必须是对象")
        # package.json 用 "cbp" 字段（与 DSH 的 "dsh" 字段对齐）；plugin.json 允许平铺
        block = raw.get("cbp")
        if not isinstance(block, dict):
            block = raw if fname == "plugin.json" else None
        if block is None:
            return None
        name = str(block.get("name") or raw.get("name") or plugin_dir.name).strip()
        if not name:
            name = plugin_dir.name
        perms = [str(p) for p in (block.get("permissions") or [])]
        unknown = [p for p in perms if p not in ALLOWED_PERMISSIONS]
        if unknown:
            raise PluginError(f"{name}: 未知权限 {unknown}（可用: {sorted(ALLOWED_PERMISSIONS)}）")
        return PluginManifest(
            name=name,
            version=str(block.get("version") or raw.get("version") or "0.1.0"),
            description=str(block.get("description") or raw.get("description") or ""),
            entry=str(block.get("entry") or "plugin.py"),
            i18n={str(k): {str(a): str(bb) for a, bb in (v or {}).items()}
                  for k, v in (block.get("i18n") or {}).items()
                  if isinstance(v, dict) and k in ("en", "tw")},
            permissions=perms,
            contributes=dict(block.get("contributes") or {}),
            config=dict(block.get("config") or {}),
            dsh=dict(raw.get("dsh") or {}),
            dir=plugin_dir,
        )
    return None


class PluginContext:
    """插件运行时上下文（对应 DSH/Cordis 的 ctx）。"""

    def __init__(self, host: "PluginHost", manifest: PluginManifest) -> None:
        self.host = host
        self.manifest = manifest
        self.name = manifest.name
        self.config = dict(manifest.config)
        self.dir = manifest.dir
        self.registered: dict[str, list[str]] = {"tools": [], "providers": [], "api": [], "events": []}
        self._handlers: list[tuple[str, Callable[..., Any]]] = []

    # ── 工具 ──
    def register_tool(self, name: str, fn: Callable[..., Awaitable[Any]],
                      description: str, arguments: dict[str, Any] | None = None,
                      approval: bool = False) -> None:
        if "tools" not in self.manifest.permissions:
            raise PluginError(f"{self.name}: 未声明 permissions 里的 'tools'，不能注册工具")
        self.host.registry.register(f"{name}", fn, description, dict(arguments or {}),
                                    approval=approval)
        self.registered["tools"].append(name)

    # ── 提供商 ──
    def register_provider(self, name: str, cfg: dict[str, Any]) -> None:
        if "providers" not in self.manifest.permissions:
            raise PluginError(f"{self.name}: 未声明 'providers' 权限")
        self.host.add_provider(name, cfg)
        self.registered["providers"].append(name)

    # ── API 路由 ──
    def register_api(self, method: str, path: str, handler: Callable[..., Any]) -> None:
        if "api" not in self.manifest.permissions:
            raise PluginError(f"{self.name}: 未声明 'api' 权限")
        key = f"{method.upper()} {path.lstrip('/')}"
        self.host.routes[(self.name, method.upper(), path.lstrip("/"))] = handler
        self.registered["api"].append(key)

    # ── 落盘 ──
    def save_file(self, rel: str, data: bytes) -> str:
        """把二进制写进**工作区**（生图等插件用），返回工作区相对路径。"""
        if "files" not in self.manifest.permissions:
            raise PluginError(f"{self.name}: 未声明 'files' 权限")
        return self.host.save_file(rel, data)

    # ── 跑任务 ──
    async def run_task(self, task: str, model: str = "", workspace: str = "",
                       prefix: str = "plugin") -> dict[str, Any]:
        return await self.host.run_task(task, model, workspace, prefix)

    # ── 声明式 UI 面板 ──
    def register_panel(self, panel: dict[str, Any]) -> None:
        """声明一个界面面板：插件提供数据接口，前端按 view 渲染。

        只允许**声明式**内容（kv/list/table/json/markdown），不接受 HTML/JS——
        插件是第三方代码，让它注入标记等于把 XSS 面直接开放给客户端。
        """
        if "ui" not in self.manifest.permissions:
            raise PluginError(f"{self.name}: 未声明 permissions 里的 'ui'，不能注册面板")
        if not isinstance(panel, dict):
            raise PluginError(f"{self.name}: panel 必须是对象")
        pid = str(panel.get("id") or "").strip()
        if not pid:
            raise PluginError(f"{self.name}: panel.id 不能为空")
        view = str(panel.get("view") or "kv").strip().lower()
        if view not in PANEL_VIEWS:
            raise PluginError(f"{self.name}: view 必须是 {list(PANEL_VIEWS)} 之一")
        api = panel.get("api") or {}
        if not isinstance(api, dict) or not str(api.get("path") or "").strip():
            raise PluginError(f"{self.name}: panel.api 需要 path（插件自己的接口）")
        entry = {
            "plugin": self.name,
            "id": pid,
            "title": str(panel.get("title") or pid)[:80],
            "view": view,
            "method": str(api.get("method") or "GET").upper(),
            "path": str(api.get("path") or "").strip(),
            "hint": str(panel.get("hint") or "")[:200],
        }
        self.registered.setdefault("panels", []).append(entry)
        self.host.panels_list = [x for x in self.host.panels_list
                                 if not (x["plugin"] == self.name and x["id"] == pid)]
        self.host.panels_list.append(entry)

    # ── 事件 ──
    def on(self, event: str, handler: Callable[..., Any]) -> None:
        if "events" not in self.manifest.permissions:
            raise PluginError(f"{self.name}: 未声明 'events' 权限")
        self.host.listeners.append((self.name, event, handler))
        self.registered["events"].append(event)

    # ── 状态与文件 ──
    @property
    def state(self) -> dict[str, Any]:
        return self.host.state_for(self.name)

    def save_state(self, **kw: Any) -> None:
        if "storage" not in self.manifest.permissions:
            raise PluginError(f"{self.name}: 未声明 'storage' 权限")
        self.host.save_state(self.name, **kw)

    def read_text(self, rel: str) -> str:
        if "files" not in self.manifest.permissions:
            raise PluginError(f"{self.name}: 未声明 'files' 权限")
        return (self.dir / rel).resolve().read_text(encoding="utf-8")

    def write_text(self, rel: str, text: str) -> None:
        if "files" not in self.manifest.permissions:
            raise PluginError(f"{self.name}: 未声明 'files' 权限")
        target = (self.dir / rel).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def log(self, msg: str) -> None:
        self.host.log(f"[plugin:{self.name}] {msg}")


class PluginHost:
    """插件发现/加载/卸载/启停 + 贡献点登记。"""

    def __init__(self, root: Path, *, registry: Any = None,
                 providers: dict[str, Any] | None = None,
                 provider_factory: Callable[[str, dict[str, Any]], Any] | None = None,
                 log_fn: Callable[[str], None] | None = None) -> None:
        self.root = Path(root)
        self.plugin_root = self.root / PLUGIN_DIR_NAME
        self.data_dir = self.root / "data"
        self.registry = registry
        self.providers = providers if providers is not None else {}
        # 由桥接层注入：把配置构造成 ApiProvider 实例再放进 PROVIDERS
        self.provider_factory = provider_factory
        # 由桥接层注入：跑一个新会话的 Agent 任务（定时任务等）
        self.run_task_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None
        # 由桥接层注入：把二进制写进工作区（生图等）
        self.save_file_fn: Callable[[str, bytes], str] | None = None
        self.log_fn = log_fn or (lambda m: print(m.encode("ascii", "backslashreplace").decode("ascii"), flush=True))
        self.routes: dict[tuple[str, str, str], Callable[..., Any]] = {}
        self.listeners: list[tuple[str, str, Callable[..., Any]]] = []
        self.panels_list: list[dict[str, Any]] = []
        self.records: dict[str, dict[str, Any]] = {}   # name -> {manifest, module, ctx, status, error}
        self._state: dict[str, dict[str, Any]] = {}
        self._state_path = self.data_dir / "plugins.json"
        self._load_state()

    # ── 状态持久化 ──
    def _load_state(self) -> None:
        try:
            if self._state_path.is_file():
                self._state = json.loads(self._state_path.read_text(encoding="utf-8")) or {}
        except Exception:
            self._state = {}

    def _save_state_file(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
        except Exception as exc:
            # 落盘失败不致命，但必须留痕：否则"改了插件配置、重启后丢了"会毫无线索
            self.log(f"[plugins] 状态落盘失败: {exc}")

    def state_for(self, name: str) -> dict[str, Any]:
        return dict((self._state.get(name) or {}).get("state") or {})

    def save_state(self, name: str, **kw: Any) -> None:
        entry = self._state.setdefault(name, {})
        st = dict(entry.get("state") or {})
        st.update(kw)
        entry["state"] = st
        self._save_state_file()

    def save_file(self, rel: str, data: bytes) -> str:
        if self.save_file_fn is None:
            raise PluginError("当前宿主没有注入 save_file 能力")
        return self.save_file_fn(rel, data)

    async def run_task(self, task: str, model: str = "", workspace: str = "",
                       prefix: str = "cron") -> dict[str, Any]:
        """跑一个独立 Agent 任务（由桥接层注入实现；定时任务/生图等插件用）。"""
        if self.run_task_fn is None:
            raise PluginError("当前宿主没有注入 run_task 能力")
        return await self.run_task_fn(task, model, workspace, prefix)

    def add_provider(self, name: str, cfg: dict[str, Any]) -> None:
        """注册/覆盖一个 API 提供商（走桥接层注入的工厂）。"""
        if self.provider_factory is not None:
            self.providers[name] = self.provider_factory(name, cfg)
        else:
            self.providers[name] = cfg

    def enabled(self, name: str) -> bool:
        return bool((self._state.get(name) or {}).get("enabled", True))

    def set_enabled(self, name: str, enabled: bool) -> None:
        entry = self._state.setdefault(name, {})
        entry["enabled"] = bool(enabled)
        self._save_state_file()

    def log(self, msg: str) -> None:
        self.log_fn(msg)

    # ── 发现 ──
    def discover(self) -> list[PluginManifest]:
        if not self.plugin_root.is_dir():
            return []
        out: list[PluginManifest] = []
        for d in sorted(p for p in self.plugin_root.iterdir() if p.is_dir()):
            if d.name.startswith((".", "__")):
                continue
            try:
                m = _read_manifest(d)
            except PluginError as exc:
                self.log(f"插件清单无效已跳过：{exc}")
                continue
            if m is not None:
                out.append(m)
        return out

    # ── 加载/卸载 ──
    def load(self, name: str) -> dict[str, Any]:
        rec = self.records.get(name)
        if rec and rec.get("status") == "loaded":
            return self.describe(name)
        manifest = next((m for m in self.discover() if m.name == name), None)
        if manifest is None:
            raise PluginError(f"没有名为 {name} 的插件")
        entry = (manifest.dir / manifest.entry).resolve()
        if not entry.is_file():
            raise PluginError(f"{name}: 入口文件不存在 {manifest.entry}")
        mod_name = f"cbp_plugin_{name.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(mod_name, entry)
        if spec is None or spec.loader is None:
            raise PluginError(f"{name}: 无法加载入口 {manifest.entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise PluginError(f"{name}: 入口执行失败: {exc}") from exc
        ctx = PluginContext(self, manifest)
        self.records[name] = {"manifest": manifest, "module": module, "ctx": ctx,
                              "status": "loaded", "error": None, "loaded_at": time.time()}
        setup = getattr(module, "setup", None)
        if callable(setup):
            try:
                result = setup(ctx)
                if inspect.isawaitable(result):
                    asyncio.get_event_loop().create_task(result) if False else None
            except Exception as exc:
                self._unload(name)
                raise PluginError(f"{name}: setup() 失败: {exc}") from exc
        return self.describe(name)

    def _unload(self, name: str) -> None:
        rec = self.records.pop(name, None)
        if not rec:
            return
        ctx: PluginContext = rec["ctx"]
        module = rec.get("module")
        teardown = getattr(module, "teardown", None)
        if callable(teardown):
            try:
                teardown(ctx)
            except Exception as exc:
                self.log(f"[plugin:{name}] teardown 异常（忽略）: {exc}")
        # 摘工具
        for tool in ctx.registered.get("tools") or []:
            try:
                if self.registry is not None and hasattr(self.registry, "unregister"):
                    self.registry.unregister(tool)
            except Exception:
                pass
        # 摘提供商
        for prov in ctx.registered.get("providers") or []:
            self.providers.pop(prov, None)
        # 摘路由
        for key in list(self.routes):
            if key[0] == name:
                self.routes.pop(key, None)
        # 摘事件
        self.listeners = [x for x in self.listeners if x[0] != name]
        # 摘面板（否则卸载后界面还留着一个点开就 404 的入口）
        self.panels_list = [x for x in self.panels_list if x["plugin"] != name]
        sys.modules.pop(f"cbp_plugin_{name.replace('-', '_')}", None)

    def unload(self, name: str) -> None:
        self._unload(name)

    def reload(self, name: str) -> dict[str, Any]:
        self._unload(name)
        return self.load(name)

    def _known(self, name: str) -> bool:
        return any(m.name == name for m in self.discover())

    def enable(self, name: str) -> dict[str, Any]:
        # 先确认插件真的存在，再落盘状态：否则一个拼错的名字（或 API 误调）会
        # 在 data/plugins.json 里留下"幽灵插件"，列表里显示成 v · 未加载（实测踩到）
        if not self._known(name) and name not in self.records:
            raise PluginError(f"没有名为 {name} 的插件")
        self.set_enabled(name, True)
        try:
            return self.load(name)
        except PluginError as exc:
            self.records.setdefault(name, {})["error"] = str(exc)
            raise

    def disable(self, name: str) -> dict[str, Any]:
        if not self._known(name) and name not in self.records:
            raise PluginError(f"没有名为 {name} 的插件")
        self.set_enabled(name, False)
        self._unload(name)
        return {"name": name, "status": "disabled"}

    def load_all(self) -> None:
        for m in self.discover():
            if not self.enabled(m.name):
                continue
            try:
                self.load(m.name)
            except Exception as exc:
                self.records[m.name] = {"manifest": m, "status": "error",
                                        "error": str(exc), "loaded_at": None}

    # ── 查询 ──
    def panels(self) -> list[dict[str, Any]]:
        """当前已加载插件声明的界面面板。"""
        return [dict(x) for x in self.panels_list]

    # ── 安装 / 卸载 / 注册表（版本化）────────────────────────────────
    def _plugin_dir(self, name: str) -> Path:
        return self.plugin_root / name

    @staticmethod
    def _version_tuple(ver: str) -> tuple[int, ...]:
        parts = []
        for piece in str(ver or "").strip().lstrip("v").split("."):
            num = "".join(ch for ch in piece if ch.isdigit())
            parts.append(int(num) if num else 0)
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])

    def _backup(self, name: str) -> str:
        src = self._plugin_dir(name)
        if not src.is_dir():
            return ""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.data_dir / "plugin-backups" / f"{name}-{stamp}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)
        return str(dest)

    def install(self, source: str, name: str = "", force: bool = False) -> dict[str, Any]:
        """从目录 / zip / git URL 安装插件（版本化，覆盖前自动备份）。

        - 本地目录：复制整目录
        - .zip：解包（拒绝 `..` 路径，防 zip-slip）
        - git URL：`git clone --depth 1`
        安装后立即加载；同名已存在时比较版本：新版覆盖（旧版备份），
        旧版默认拒绝（避免误降级），`force=True` 才覆盖。
        """
        src = str(source or "").strip()
        if not src:
            raise PluginError("source 不能为空")
        self.plugin_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cbp-install-") as tmp:
            tmpdir = Path(tmp)
            if re.match(r"^(https?://|git@)", src):
                git = shutil.which("git")
                if not git:
                    raise PluginError("安装 git 插件需要本机有 git")
                dest = tmpdir / "src"
                proc = subprocess.run([git, "clone", "--depth", "1", src, str(dest)],
                                      capture_output=True, text=True, timeout=180)
                if proc.returncode != 0:
                    raise PluginError(f"git clone 失败: {(proc.stderr or proc.stdout)[:200]}")
                staged = dest
            elif src.lower().endswith(".zip"):
                zpath = Path(src)
                if not zpath.is_file():
                    raise PluginError(f"找不到 zip: {src}")
                staged = tmpdir / "unzip"
                staged.mkdir()
                with zipfile.ZipFile(zpath) as zf:
                    for item in zf.infolist():
                        target = (staged / item.filename).resolve()
                        if staged.resolve() not in target.parents and target != staged.resolve():
                            raise PluginError(f"zip 含越界路径，拒绝安装: {item.filename}")
                    zf.extractall(staged)
            else:
                d = Path(src).expanduser()
                if not d.is_dir():
                    raise PluginError(f"找不到插件目录: {src}")
                staged = d
            manifest = _read_manifest(staged)
            if manifest is None:
                raise PluginError(f"{src} 不是插件（没有 plugin.json / package.json）")
            target_name = str(name or manifest.name).strip()
            if not target_name:
                raise PluginError("无法确定插件名")
            dest = self._plugin_dir(target_name)
            existing = self.records.get(target_name)
            old_ver = ""
            try:
                if dest.is_dir():
                    old = _read_manifest(dest)
                    old_ver = old.version if old else ""
            except Exception:
                old_ver = ""
            new_ver = manifest.version
            backup = ""
            if dest.exists():
                if not force and old_ver and self._version_tuple(new_ver) < self._version_tuple(old_ver):
                    raise PluginError(
                        f"已安装版本 {old_ver} 高于要安装的 {new_ver}；"
                        f"确认要降级请加 force=true")
                self._unload(target_name)
                backup = self._backup(target_name)
                shutil.rmtree(dest)
            shutil.copytree(staged, dest)
        rec = self._state.setdefault(target_name, {})
        rec["installed"] = {"version": new_ver, "source": src, "at": time.time(),
                            "backup": backup}
        rec["enabled"] = True
        self._save_state_file()
        try:
            loaded = self.load(target_name)
        except PluginError as exc:
            raise PluginError(f"安装成功但加载失败: {exc}") from exc
        return {"name": target_name, "version": new_ver, "previous": old_ver,
                "backup": backup, "status": (loaded or {}).get("status", "loaded")}

    def uninstall(self, name: str, keep_backup: bool = True) -> dict[str, Any]:
        """卸载插件：先卸载运行时贡献，再删目录（默认留备份）。"""
        dest = self._plugin_dir(name)
        if not dest.is_dir() and name not in self.records:
            raise PluginError(f"没有名为 {name} 的插件")
        self._unload(name)
        backup = self._backup(name) if keep_backup and dest.is_dir() else ""
        if dest.is_dir():
            shutil.rmtree(dest)
        self._state.pop(name, None)
        self._save_state_file()
        return {"name": name, "removed": True, "backup": backup}

    def registry_view(self) -> dict[str, Any]:
        """注册表视图：已安装插件（版本/来源/安装时间）+ 备份数量。"""
        out = []
        for m in self.discover():
            meta = (self._state.get(m.name) or {}).get("installed") or {}
            out.append({"name": m.name, "version": m.version,
                        "description": m.description,
                        "source": meta.get("source") or "builtin",
                        "installed_at": meta.get("at"),
                        "enabled": self.enabled(m.name),
                        "status": (self.records.get(m.name) or {}).get("status", "not-loaded")})
        backups = []
        bdir = self.data_dir / "plugin-backups"
        if bdir.is_dir():
            backups = sorted(p.name for p in bdir.iterdir() if p.is_dir())[-20:]
        return {"plugins": out, "backups": backups}

    def i18n(self) -> dict[str, dict[str, str]]:
        """汇总所有**已加载**插件自带的译文：{"en": {...}, "tw": {...}}。

        平台不知道第三方插件写了哪些中文，让它自己在清单里带译文最不会失真。
        前端启动/重载插件后取一次，合并进字典即可让 tr() 覆盖插件文案。
        """
        merged: dict[str, dict[str, str]] = {"en": {}, "tw": {}}
        for name, rec in self.records.items():
            if rec.get("status") != "loaded":
                continue
            try:
                man = _read_manifest(self.plugin_root / name)
            except Exception:
                continue
            if not man or not man.i18n:
                continue
            for lang, table in man.i18n.items():
                if lang in merged and isinstance(table, dict):
                    merged[lang].update({str(k): str(v) for k, v in table.items()})
        return merged

    def plugin_function(self, plugin: str, fn_name: str) -> Callable[..., Any] | None:
        """取已加载插件的模块级函数（桥接层复用插件能力用，如 research.deep_research）。"""
        rec = self.records.get(plugin) or {}
        module = rec.get("module")
        fn = getattr(module, fn_name, None) if module is not None else None
        return fn if callable(fn) else None

    def describe(self, name: str) -> dict[str, Any]:
        rec = self.records.get(name) or {}
        manifest: PluginManifest | None = rec.get("manifest")
        ctx: PluginContext | None = rec.get("ctx")
        return {
            "name": name,
            "version": manifest.version if manifest else "",
            "description": manifest.description if manifest else "",
            "status": rec.get("status") or ("disabled" if not self.enabled(name) else "not_loaded"),
            "error": rec.get("error"),
            "enabled": self.enabled(name),
            "permissions": list(manifest.permissions) if manifest else [],
            "contributes": (manifest.contributes if manifest else {}),
            "dsh": (manifest.dsh if manifest else {}),
            "registered": dict(ctx.registered) if ctx else {"tools": [], "providers": [], "api": [], "events": []},
        }

    def list(self) -> list[dict[str, Any]]:
        names = {m.name for m in self.discover()} | set(self.records)
        return [self.describe(n) for n in sorted(names)]

    # ── 事件与路由分发 ──
    def emit(self, event: str, **payload: Any) -> None:
        for _name, ev, handler in list(self.listeners):
            if ev != event:
                continue
            try:
                result = handler(**payload)
                if inspect.isawaitable(result):
                    try:
                        asyncio.get_running_loop().create_task(result)
                    except RuntimeError:
                        pass
            except Exception as exc:
                self.log(f"[plugin:{_name}] 事件 {event} 处理异常: {exc}")

    async def dispatch_api(self, plugin: str, method: str, path: str, request: Any) -> Any:
        # 两种写法都认：插件注册 "search" 或 "api/search"（路由前缀已吃掉 /api/）
        rel = path.lstrip("/")
        handler = (self.routes.get((plugin, method.upper(), rel))
                   or self.routes.get((plugin, method.upper(), "api/" + rel)))
        if handler is None:
            raise PluginError(f"插件 {plugin} 没有注册 {method.upper()} /{path}")
        # 按**签名**决定是否传 request，而不是靠捕获 TypeError 试错：
        # handler 内部抛的 TypeError 会被误当成"参数不匹配"，静默重跑一次并
        # 掩盖真实错误（实测踩到过——真正的 bug 变成"二次调用又炸了"）。
        try:
            params = [p for p in inspect.signature(handler).parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD,
                                    p.VAR_POSITIONAL)]
        except (TypeError, ValueError):
            params = [True]          # 取不到签名（内置/C 函数）时按"接收 request"处理
        result = handler(request) if params else handler()
        if inspect.isawaitable(result):
            result = await result
        return result
