"""工作区快照：记录任务改动前的文件原像，支持整任务回滚（自我升级 failback）。

设计要点：
- **改动前捕获**：工具层在任何写操作前调用 capture()，保存目标路径的
  改动前状态。同一会话内同一路径只捕获一次（保留最早状态），这样
  "回滚这个任务"恢复的是任务开始前的样子，而不是某个中间态。
- **三种状态**：`file`（存原像字节）、`dir`（存整棵目录树）、
  `absent`（当时不存在 —— 回滚时删除它，覆盖 Agent 新建的文件/目录）。
- **不解析内容**：文本/二进制统一按字节复制，xlsx/docx 等也能回滚。
- **有上限**：单文件、总大小、条目数都有上限，超限记入 skipped 而不是
  拖垮工具调用；快照失败永远不阻断写操作本身。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any


class SnapshotStore:
    MAX_FILES = 800                      # 单会话快照条目上限
    MAX_FILE_BYTES = 8 * 1024 * 1024     # 单文件原像上限
    MAX_TOTAL_BYTES = 256 * 1024 * 1024  # 单会话原像总量上限（防止把大工程整体拷走）

    def __init__(self, base: Path, retention: int = 50):
        self.base = Path(base).resolve()
        self.retention = max(1, int(retention))
        # 立即建目录：否则全新安装（还没有任何快照）时，读状态/列作用域的
        # iterdir() 会抛 FileNotFoundError，把 /self/status 与离线回滚打挂。
        try:
            self.base.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def scopes(self, prefix: str = "") -> list[str]:
        """列出可回滚的作用域目录（只含有 manifest.json 的，避免被 prune 的
        半成品目录导致后续 stat()/restore 抛错）。"""
        try:
            dirs = [d for d in self.base.iterdir() if d.is_dir()]
        except OSError:
            return []
        out = []
        for d in dirs:
            if prefix and not d.name.startswith(prefix):
                continue
            if self.manifest_path(d.name).is_file():
                out.append(d.name)
        return out

    # ── 路径与元数据 ──────────────────────────────────────────────
    def _dir(self, sid: str) -> Path:
        safe = re.sub(r"[^0-9A-Za-z._-]+", "-", str(sid)).strip("-")[:80] or "session"
        # "." / ".." / "..." 这类名字会被 _dir 原样保留，使 clear()/restore()
        # 直接作用到 snapshots 根或其父目录（rmtree 掉 data/ 整个目录）：
        # 必须以字母数字开头，并用 resolve 再确认结果是 base 的直接子目录。
        if not re.match(r"^[0-9A-Za-z]", safe):
            safe = "s" + safe
        candidate = (self.base / safe).resolve()
        if candidate.parent != self.base.resolve() or candidate == self.base.resolve():
            safe = "s" + re.sub(r"[^0-9A-Za-z._-]+", "-", str(sid)).strip("-")[:76]
            candidate = (self.base / safe).resolve()
            if candidate.parent != self.base.resolve():
                safe = "session"
        return self.base / safe

    def manifest_path(self, sid: str) -> Path:
        return self._dir(sid) / "manifest.json"

    def exists(self, sid: str) -> bool:
        return self.manifest_path(sid).is_file()

    def load(self, sid: str) -> dict[str, Any]:
        p = self.manifest_path(sid)
        empty = {"session_id": sid, "created": None, "updated": None, "files": {}, "skipped": []}
        if not p.is_file():
            return empty
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data.setdefault("files", {})
            data.setdefault("skipped", [])
            return data
        except Exception:
            return empty

    def _save(self, sid: str, data: dict[str, Any]) -> None:
        data["updated"] = time.time()
        d = self._dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path(sid).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.manifest_path(sid))

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def _tree_bytes(self, root: Path) -> int:
        total = 0
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
        return total

    # ── 捕获 ──────────────────────────────────────────────────────
    def capture(self, sid: str, rel: str, target: Path, tool: str = "",
                root: Path | None = None) -> None:
        """写操作前调用。任何异常都被吞掉：快照是安全网，不能反过来阻断写入。

        root 记录本条原像所属的根目录（工作区/安装目录）。同一 Session 若换过
        工作区，同名相对路径会撞在一起；restore 时按 root 过滤，避免把 A 根的
        内容写回 B 根。
        """
        try:
            self._capture(sid, rel, Path(target), tool, Path(root).resolve() if root else None)
        except Exception:
            pass

    def _capture(self, sid: str, rel: str, target: Path, tool: str,
                 root: Path | None = None) -> None:
        # 快照目录自己不能被快照：工作区等于安装目录时，copytree 会把快照
        # 目录抄进新快照，逐轮指数膨胀。
        try:
            target.resolve().relative_to(self.base)
            return
        except ValueError:
            pass
        data = self.load(sid)
        rel_norm = str(rel).replace("\\", "/").lstrip("/") or "."
        if rel_norm in data["files"]:
            return  # 已有原像：保留最早状态
        if len(data["files"]) >= self.MAX_FILES:
            data["skipped"].append({"path": rel_norm, "reason": "条目数超限"})
            self._save(sid, data)
            return
        # 总量上限（此前 MAX_TOTAL_BYTES 只是定义、从未生效）
        if self._tree_bytes(self._dir(sid) / "files") >= self.MAX_TOTAL_BYTES:
            data["skipped"].append({"path": rel_norm, "reason": "原像总量超限"})
            self._save(sid, data)
            return

        entry: dict[str, Any] = {"path": rel_norm, "tool": tool, "captured": time.time()}
        if root is not None:
            entry["root"] = str(root)
        if target.is_file():
            size = target.stat().st_size
            if size > self.MAX_FILE_BYTES:
                data["skipped"].append({"path": rel_norm, "reason": f"文件 {size} 字节超限"})
                self._save(sid, data)
                return
            stored = f"files/{len(data['files']):04d}_{target.name}"
            dest = self._dir(sid) / stored
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
            entry.update({"kind": "file", "stored": stored, "bytes": size,
                          "before_hash": self._sha256(dest)})
        elif target.is_dir():
            size = self._tree_bytes(target)
            if size > self.MAX_FILE_BYTES:
                data["skipped"].append({"path": rel_norm, "reason": f"目录 {size} 字节超限"})
                self._save(sid, data)
                return
            stored = f"files/{len(data['files']):04d}_{target.name}.dir"
            dest = self._dir(sid) / stored
            shutil.copytree(target, dest, dirs_exist_ok=True)
            entry.update({"kind": "dir", "stored": stored, "bytes": size})
        else:
            entry.update({"kind": "absent"})
        data["files"][rel_norm] = entry
        data["created"] = data.get("created") or time.time()
        self._save(sid, data)

    # ── 查询 ──────────────────────────────────────────────────────
    def summary(self, sid: str) -> dict[str, Any]:
        data = self.load(sid)
        files = data.get("files", {})
        kinds: dict[str, int] = {}
        for e in files.values():
            kinds[e.get("kind", "?")] = kinds.get(e.get("kind", "?"), 0) + 1
        return {"session_id": sid, "exists": bool(files), "created": data.get("created"),
                "updated": data.get("updated"), "counts": kinds,
                "files": [{"path": e["path"], "kind": e.get("kind"), "bytes": e.get("bytes", 0)}
                          for e in files.values()],
                "skipped": data.get("skipped", [])}

    # ── 回滚 ──────────────────────────────────────────────────────
    def restore(self, sid: str, root: Path, only: list[str] | None = None) -> dict[str, Any]:
        """按原像回滚：file/dir 还原内容，absent 删除；返回逐条明细。"""
        data = self.load(sid)
        files = data.get("files", {})
        if not files:
            return {"restored": 0, "deleted": 0, "failed": 0, "results": [],
                    "message": "没有该 Session 的快照记录"}
        root = Path(root).resolve()
        want = {str(x).replace("\\", "/") for x in only} if only else None
        results = []
        restored = deleted = failed = 0
        scope_dir = self._dir(sid).resolve()
        for rel, entry in files.items():
            if want is not None and rel not in want:
                continue
            # 根维度校验：原像来自另一个根（会话中途换过工作区）时不动它，
            # 否则会把 A 根的内容写回 B 根的同名路径。
            stored_root = entry.get("root")
            if stored_root and Path(stored_root) != Path(root):
                results.append({"path": rel, "status": "skipped",
                                "detail": f"原像属于其它根目录 {stored_root}，未回滚"})
                continue
            target = (root / rel).resolve()
            # 沙箱：只允许在工作区根内回滚
            try:
                target.relative_to(root)
            except ValueError:
                results.append({"path": rel, "status": "refused", "error": "工作区外路径"})
                failed += 1
                continue
            kind = entry.get("kind")
            tmp: Path | None = None
            try:
                if kind == "absent":
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                    results.append({"path": rel, "status": "deleted",
                                    "detail": "任务期间新建，回滚已删除"})
                    deleted += 1
                    continue
                # 原像路径校验：清单里的 stored 不能被构造成 ../../ 逃出快照目录
                src = (scope_dir / str(entry.get("stored") or "")).resolve()
                try:
                    src.relative_to(scope_dir)
                except ValueError:
                    raise RuntimeError("快照原像路径非法")
                if not src.exists():
                    raise RuntimeError("快照原像缺失")
                if kind == "dir":
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(src, target, dirs_exist_ok=True)
                else:
                    # 原子还原：直接 copy2 到目标会在失败时留下被截断的文件
                    # （回滚反而把工作区改坏），先写临时文件再替换。
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_name(target.name + ".cbp-restore-tmp")
                    shutil.copy2(src, tmp)
                    os.replace(tmp, target)
                results.append({"path": rel, "status": "restored",
                                "bytes": entry.get("bytes", 0)})
                restored += 1
            except Exception as exc:
                try:
                    if tmp is not None and tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
                results.append({"path": rel, "status": "failed", "error": str(exc)})
                failed += 1
        return {"restored": restored, "deleted": deleted, "failed": failed,
                "results": results, "root": str(root)}

    def clear(self, sid: str) -> bool:
        d = self._dir(sid)
        if not d.exists():
            return False
        shutil.rmtree(d, ignore_errors=True)
        return True

    def prune(self) -> list[str]:
        """按 updated 时间清理超出保留数的会话快照目录。"""
        if not self.base.is_dir():
            return []
        items = [d for d in self.base.iterdir() if d.is_dir()]
        items.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        removed = []
        for d in items[self.retention:]:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
        return removed
