# -*- coding: utf-8 -*-
"""`@路径` 引用展开（DSH 的 @文件 体验）：聊天里 @工作区里的文件，发消息时把内容带上。

- `@相对路径` 解析到会话工作区（**拒绝越界**：`..`、绝对路径、盘符一律不认）；
- 只展开**文本类**文件（复用 fileview 的扩展名表），目录/二进制跳过并提示；
- 保护：单文件 ≤ 20k 字符、合计 ≤ 60k、最多 4 个文件；
- 代码围栏（```）里的 @ 不展开（那是示例文本，不是引用）。
"""
from __future__ import annotations

import re
from pathlib import Path

from .fileview import TEXT_EXT

# @ 后面跟路径字符（中英文/点/斜杠/横线/下划线），到空白为止
_AT_RE = re.compile(r"(?<![\w/`])@([^\s@]{2,160})")
_FENCE_RE = re.compile(r"```.*?```", re.S)

MAX_FILES = 4
MAX_EACH = 20000
MAX_TOTAL = 60000
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".idea", "data", ".spill"}


def _resolve(root: Path, rel: str) -> Path | None:
    rel = rel.strip().replace("\\", "/")
    if not rel or rel.startswith(("/", "~")) or ".." in rel.split("/") or ":" in rel:
        return None
    cand = (root / rel).resolve()
    try:
        cand.relative_to(root)
    except ValueError:
        return None
    return cand


def expand_at_mentions(text: str, root: Path, max_files: int = MAX_FILES,
                       max_each: int = MAX_EACH, max_total: int = MAX_TOTAL):
    """返回 (展开后的文本, 引用清单)。root 必须是已 resolve 的目录。"""
    if not text or "@" not in text:
        return text, []
    # 拆出围栏，只在围栏外展开
    parts, last = [], 0
    for m in _FENCE_RE.finditer(text):
        parts.append((text[last:m.start()], True))
        parts.append((m.group(0), False))
        last = m.end()
    parts.append((text[last:], True))

    refs, total = [], 0
    seen = set()

    def _expand(seg: str) -> str:
        nonlocal total
        def sub(m: re.Match) -> str:
            nonlocal total
            rel = m.group(1)
            target = _resolve(root, rel)
            if target is None or not target.is_file():
                return m.group(0)
            if target.suffix.lower() not in TEXT_EXT:
                return m.group(0)
            key = str(target)
            if key in seen or len(refs) >= max_files:
                return m.group(0)
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return m.group(0)
            content = content[:max_each]
            if total + len(content) > max_total:
                room = max_total - total
                if room < 200:
                    return m.group(0)
                content = content[:room]
            total += len(content)
            seen.add(key)
            rel_show = target.relative_to(root).as_posix()
            refs.append({"path": rel_show, "chars": len(content)})
            return (f"@{rel_show}\n【引用文件 {rel_show}（{len(content)} 字符）】\n"
                    f"```\n{content}\n```")
        return _AT_RE.sub(sub, seg)

    out = "".join(_expand(seg) if ok else seg for seg, ok in parts)
    return out, refs


def search_files(root: Path, query: str, limit: int = 50) -> list[str]:
    """@ 弹层用的文件检索：按**相对路径子串**匹配（大小写不敏感），跳过噪声目录。"""
    root = Path(root).resolve()
    q = (query or "").strip().lower().replace("\\", "/")
    if not root.is_dir():
        return []
    out = []
    try:
        walker = root.rglob("*")
        for f in walker:
            if len(out) >= limit * 4:      # 先多收一点再排序截断
                break
            try:
                if not f.is_file():
                    continue
                rel = f.relative_to(root).as_posix()
            except ValueError:
                continue
            if any(part in _SKIP_DIRS for part in rel.split("/")):
                continue
            if rel.startswith("."):
                continue
            if q and q not in rel.lower():
                continue
            out.append(rel)
    except OSError:
        return []
    out.sort(key=lambda r: (r.lower().find(q) if q else 0, len(r), r))
    return out[:limit]
