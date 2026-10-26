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

import difflib
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
    MAX_REVS_PER_PATH = 6                # 单路径保留的版本数（最早 + 最近几个，防膨胀）

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
            data.setdefault("history", {})       # 版本历史：rel -> [entry(rev 升序)]
            data.setdefault("turn", 0)           # 当前 Copilot 回合号（版本 = 回合）
            data.setdefault("stored_seq", 0)     # 原像文件名的单调序号
            data.setdefault("next_rev", 1)       # 旧字段，兼容读取
            return data
        except Exception:
            return empty

    def next_turn(self, sid: str) -> int:
        """回合开始信号：版本号 +1。一次 Copilot complete = 一个版本。

        同回合内的多次写操作共享同一版本号（首写才记版本），因此"回滚到
        版本 N"= 撤销第 N 回合（及之后）的文件改动，而不是按单次写操作算。
        """
        data = self.load(sid)
        turn = int(data.get("turn", 0)) + 1
        data["turn"] = turn
        self._save(sid, data)
        return turn

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
        first_capture = rel_norm not in data["files"]
        rev = max(1, int(data.get("turn", 0)))
        history = data.setdefault("history", {})
        if not first_capture:
            # 同回合首写才记版本：一个 Copilot 回合内对同一路径的多次写只算
            # 一次（保留该回合开始前的原像），后续写不再新增版本条目。
            hist = history.get(rel_norm, [])
            if hist and int(hist[-1].get("rev", 0)) == rev:
                return
        if first_capture and len(data["files"]) >= self.MAX_FILES:
            data["skipped"].append({"path": rel_norm, "reason": "条目数超限"})
            self._save(sid, data)
            return
        # 总量上限（此前 MAX_TOTAL_BYTES 只是定义、从未生效）
        if self._tree_bytes(self._dir(sid) / "files") >= self.MAX_TOTAL_BYTES:
            data["skipped"].append({"path": rel_norm, "reason": "原像总量超限"})
            self._save(sid, data)
            return

        seq = int(data.get("stored_seq", 0)) + 1
        data["stored_seq"] = seq
        entry: dict[str, Any] = {"path": rel_norm, "tool": tool, "captured": time.time(),
                                 "rev": rev}
        if root is not None:
            entry["root"] = str(root)
        if target.is_file():
            size = target.stat().st_size
            if size > self.MAX_FILE_BYTES:
                data["skipped"].append({"path": rel_norm, "reason": f"文件 {size} 字节超限"})
                self._save(sid, data)
                return
            stored = f"files/{seq:04d}_{target.name}"
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
            stored = f"files/{seq:04d}_{target.name}.dir"
            dest = self._dir(sid) / stored
            shutil.copytree(target, dest, dirs_exist_ok=True)
            entry.update({"kind": "dir", "stored": stored, "bytes": size})
        else:
            entry.update({"kind": "absent"})

        if first_capture:
            data["files"][rel_norm] = entry          # 最早原像：整任务回滚用
            history[rel_norm] = [entry]
        else:
            hist = history.setdefault(rel_norm, [])
            if len(hist) >= self.MAX_REVS_PER_PATH:
                # 保留最早（hist[0]）与最近几个，丢弃中间最旧的一个
                hist.pop(1)
            hist.append(entry)
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

    # ── 改动统计 / 差异（任务完成后的「改了哪些文件」卡片）────────────
    MAX_DIFF_BYTES = 4 * 1024 * 1024     # 单文件超过这个大小不做行级 diff
    MAX_DIFF_LINES = 40000               # 单文件行数上限（SequenceMatcher 的耗时保护）

    @staticmethod
    def _read_lines(path: Path) -> list[str] | None:
        """读文本行；二进制 / 超大文件返回 None（表示"无法按行比较"）。"""
        try:
            if not path.is_file():
                return None
            if path.stat().st_size > SnapshotStore.MAX_DIFF_BYTES:
                return None
            data = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in data[:4096]:          # 二进制（xlsx/docx/图片）没有"行"的概念
            return None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return text.splitlines()[: SnapshotStore.MAX_DIFF_LINES]

    @staticmethod
    def _numstat(old: list[str], new: list[str]) -> tuple[int, int]:
        """行级增删数（对齐 git numstat 的口径）。"""
        add = dele = 0
        sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag in ("replace", "insert"):
                add += j2 - j1
            if tag in ("replace", "delete"):
                dele += i2 - i1
        return add, dele

    def _entry_target(self, entry: dict[str, Any],
                      default_root: Path | None) -> Path | None:
        root = entry.get("root")
        base = Path(root) if root else (Path(default_root) if default_root else None)
        if base is None:
            return None
        return Path(base) / str(entry.get("path") or "")

    def changes(self, sid: str, default_root: Path | None = None) -> dict[str, Any]:
        """任务开始前 → 现在，每个文件的行级增删统计。

        DSH 在任务完成后会显示「Edited N files +X −Y」；这里用**快照里的原像**
        做 diff（原像是写操作前捕获的，正好是"任务开始前"），不依赖 git。
        二进制文件（xlsx/docx/图片）只列路径、不给行数；未改动的文件直接略过。
        """
        data = self.load(sid)
        files: list[dict[str, Any]] = []
        total_add = total_del = 0
        for rel, entry in (data.get("files") or {}).items():
            target = self._entry_target(entry, default_root)
            if target is None:
                continue
            kind = str(entry.get("kind") or "")
            status = ""
            old_lines: list[str] | None = None
            new_lines: list[str] | None = None
            if kind == "absent":
                if target.exists():
                    status = "added"
                    new_lines = self._read_lines(target)
                else:
                    continue                    # 捕获时不存在、现在也不存在：没发生改动
            elif kind == "file":
                stored = self._dir(sid) / str(entry.get("stored") or "")
                if target.is_file():
                    old_lines = self._read_lines(stored)
                    new_lines = self._read_lines(target)
                    # 内容完全一致（写了又改回去 / 只是 touch）：不列出来
                    if old_lines is not None and new_lines is not None and old_lines == new_lines:
                        continue
                    status = "modified"
                else:
                    status = "deleted"
                    old_lines = self._read_lines(stored)
            elif kind == "dir":
                status = "dir"
            else:
                continue
            add = dele = 0
            binary = False
            if status in ("modified", "added", "deleted"):
                if status != "deleted" and new_lines is None:
                    binary = True
                elif status == "modified" and old_lines is None:
                    binary = True
                else:
                    add, dele = self._numstat(old_lines or [], new_lines or [])
            files.append({"path": rel, "status": status,
                          "additions": add, "deletions": dele, "binary": binary,
                          "kind": kind, "bytes": int(entry.get("bytes") or 0),
                          "tool": str(entry.get("tool") or "")})
            if status in ("modified", "added", "deleted"):
                total_add += add
                total_del += dele
        files.sort(key=lambda x: (-(x["additions"] + x["deletions"]), x["path"]))
        changed = sum(1 for f in files if f["status"] in ("modified", "added", "deleted"))
        return {"session_id": sid, "count": changed, "additions": total_add,
                "deletions": total_del, "files": files,
                "skipped": data.get("skipped", [])}

    def diff(self, sid: str, rel: str,
             default_root: Path | None = None) -> dict[str, Any] | None:
        """单个文件的原像 → 现在的 unified diff；路径必须出现在该会话快照里。

        只允许读**快照记录过的路径**：接口不能变成"任意文件读取"（否则知道 sid 的
        调用方就能读工作区外的文件）。
        """
        want = str(rel or "").replace("\\", "/").lstrip("/")
        data = self.load(sid)
        entry = (data.get("files") or {}).get(want)
        if not entry:
            return None
        target = self._entry_target(entry, default_root)
        if target is None:
            return None
        stored = self._dir(sid) / str(entry.get("stored") or "")
        kind = str(entry.get("kind") or "")
        old_lines: list[str] | None = []
        new_lines: list[str] | None = []
        if kind == "file":
            old_lines = self._read_lines(stored)
        if target.is_file():
            new_lines = self._read_lines(target)
        binary = (kind == "file" and old_lines is None) or (target.is_file() and new_lines is None)
        old_lines = old_lines or []
        new_lines = new_lines or []
        add, dele = self._numstat(old_lines, new_lines)
        text = ""
        if not binary:
            text = "\n".join(difflib.unified_diff(
                [ln + "\n" for ln in old_lines],
                [ln + "\n" for ln in new_lines],
                fromfile=("a/" + want) if kind == "file" else "/dev/null",
                tofile=("b/" + want) if target.exists() else "/dev/null",
                n=3)).rstrip()
        status = "deleted" if not target.exists() else ("added" if kind == "absent" else "modified")
        return {"path": want, "status": status, "additions": add, "deletions": dele,
                "binary": binary, "diff": text[:400000]}

    # ── 回滚 ──────────────────────────────────────────────────────
    def versions(self, sid: str) -> list[dict[str, Any]]:
        """版本点列表：按 rev 升序，每个版本标明它即将改动的路径与时间。"""
        data = self.load(sid)
        hist = data.get("history", {})
        out = []
        for entries in hist.values():
            for e in entries:
                out.append({"rev": e.get("rev"), "captured": e.get("captured"),
                            "path": e.get("path"), "tool": e.get("tool"),
                            "kind": e.get("kind")})
        out.sort(key=lambda x: (x["rev"] or 0, x["path"] or ""))
        return out

    # ── 回滚 ──────────────────────────────────────────────────────
    def _apply_entry(self, root: Path, rel: str, entry: dict[str, Any],
                     scope_dir: Path) -> dict[str, Any]:
        """把单个原像条目回滚到 root。返回状态明细，供 restore / restore_rev 复用。"""
        # 根维度校验：原像来自另一个根（会话中途换过工作区）时不动它。
        stored_root = entry.get("root")
        if stored_root and Path(stored_root) != Path(root):
            return {"path": rel, "status": "skipped",
                    "detail": f"原像属于其它根目录 {stored_root}，未回滚"}
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return {"path": rel, "status": "refused", "error": "工作区外路径"}
        kind = entry.get("kind")
        tmp: Path | None = None
        try:
            if kind == "absent":
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                return {"path": rel, "status": "deleted",
                        "detail": "任务期间新建，回滚已删除"}
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
                # 原子还原：先写临时文件再替换，避免失败留下被截断的文件。
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".gantry-restore-tmp")
                shutil.copy2(src, tmp)
                os.replace(tmp, target)
            return {"path": rel, "status": "restored", "bytes": entry.get("bytes", 0)}
        except Exception as exc:
            try:
                if tmp is not None and tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return {"path": rel, "status": "failed", "error": str(exc)}

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
            r = self._apply_entry(root, rel, entry, scope_dir)
            if r["status"] == "restored":
                restored += 1
            elif r["status"] == "deleted":
                deleted += 1
            elif r["status"] in {"refused", "failed"}:
                failed += 1
            results.append(r)
        return {"restored": restored, "deleted": deleted, "failed": failed,
                "results": results, "root": str(root)}

    def restore_rev(self, sid: str, root: Path, rev: int) -> dict[str, Any]:
        """回滚到指定版本 rev（撤销第 rev 回合之后的改动）。

        版本按 Copilot 回合计数：版本 N = 第 N 回合完成后的状态。回滚到版本
        rev = 恢复每个路径到"第 rev 回合完成后"的原像——即该路径 rev 之后
        首次被改动的那个原像；若 rev 之后没再被改过，保持现状不动。
        """
        data = self.load(sid)
        hist = data.get("history", {})
        if not hist:
            return {"restored": 0, "deleted": 0, "failed": 0, "results": [],
                    "message": "没有该 Session 的快照记录"}
        root = Path(root).resolve()
        scope_dir = self._dir(sid).resolve()
        results = []
        restored = deleted = failed = 0
        for rel, entries in hist.items():
            if not entries:
                continue
            ordered = sorted(entries, key=lambda e: e.get("rev") or 0)
            # 找 rev 之后首次被改动的原像（其内容 = 第 rev 回合完成后的状态）；
            # 没有则说明该路径在第 rev 回合后没再变过，跳过（保持现状）。
            after = next((e for e in ordered if (e.get("rev") or 0) > rev), None)
            if after is None:
                continue
            r = self._apply_entry(root, rel, after, scope_dir)
            if r["status"] == "restored":
                restored += 1
            elif r["status"] == "deleted":
                deleted += 1
            elif r["status"] in {"refused", "failed"}:
                failed += 1
            results.append(r)
        return {"restored": restored, "deleted": deleted, "failed": failed,
                "results": results, "root": str(root), "rev": rev}

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
