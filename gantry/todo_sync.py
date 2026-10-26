# -*- coding: utf-8 -*-
"""把工作区里的 TODO 文件同步成会话的待办清单。

背景（用户反馈）：Agent 生成了 `pyshot_todo.md`（23KB，带 `- [ ]` 复选框清单），
但 CBP 的待办面板只认 `todo_write` 工具调用——文件里写了什么它根本不知道。
于是"按 todo 完成整改"这种指令只能靠模型自觉，模型不写 `todo_write` 面板就一直是 0/N。

设计要点
- **只读文件、不改文件**；解析 `- [ ]` / `- [x]` 复选框（也认 `*`、`1.`、`[X]`）。
- **不覆盖已有进度**：同一项（内容归一后相同）如果已经是 completed，保持 completed。
- **按 mtime 去重**：文件没变就不重复解析（每回合调用一次也很便宜）。
- 文件被删/找不到时不动现有清单（不能把用户看得到的东西清空）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

MAX_ITEMS = 50
TODO_NAME_RE = re.compile(r"todo", re.I)
CHECKBOX_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s*\[([ xX✓✔-])\]\s*(.+?)\s*$")
HEADING_RE = re.compile(r"^\s*#{2,6}\s+(.+?)\s*$")
# 真实文件（workspace/pyshot_todo.md）把复选框写在标题里：`### [ ] P0-1 …`
HEADING_CHECK_RE = re.compile(r"^\s*#{1,6}\s*\[([ xX✓✔-])\]\s*(.+?)\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.+?)\s*$")

# 进程内缓存：sid -> (文件路径, mtime)。重启后失效无所谓（会再解析一次）。
_FILE_STATE: dict[str, tuple[str, float]] = {}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def find_todo_file(workspace: str | Path) -> Path | None:
    """在工作区根目录里找 TODO 清单文件（取最新改动的那个）。"""
    root = Path(workspace)
    if not root.is_dir():
        return None
    cands: list[Path] = []
    try:
        for p in root.iterdir():
            if p.is_file() and p.suffix.lower() in {".md", ".markdown", ".txt"} \
                    and TODO_NAME_RE.search(p.name):
                cands.append(p)
    except OSError:
        return None
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]


def parse_todos(text: str, limit: int = MAX_ITEMS) -> list[dict[str, Any]]:
    """从 Markdown 文本里抽复选框清单。

    标题行只作为上下文（不加进内容，避免清单被节标题淹没），
    但如果某项本身没写清楚（很短），把最近的标题拼上去帮助辨认。
    """
    todos: list[dict[str, Any]] = []
    heading = ""
    seen: set[str] = set()
    for line in str(text or "").splitlines():
        hc = HEADING_CHECK_RE.match(line)
        if hc:
            # `### [x] P0-1 建立测试基线`：标题即一项，状态取方框里的字符
            content = _clean_item(hc.group(2))
            key = _norm(content)
            if content and key not in seen:
                seen.add(key)
                todos.append({
                    "content": content[:200],
                    "status": "completed" if hc.group(1).lower() in {"x", "✓", "✔"} else "pending",
                })
                if len(todos) >= limit:
                    break
            continue
        hm = HEADING_RE.match(line)
        if hm:
            heading = hm.group(1).strip()
            continue
        m = CHECKBOX_RE.match(line)
        if not m:
            continue
        mark, content = m.group(1), m.group(2).strip()
        content = _clean_item(content)
        if not content:
            continue
        if len(content) <= 12 and heading:
            content = f"{heading}：{content}"
        key = _norm(content)
        if key in seen:
            continue
        seen.add(key)
        status = "completed" if mark.lower() in {"x", "✓", "✔"} else "pending"
        todos.append({"content": content[:200], "status": status})
        if len(todos) >= limit:
            break
    if not todos and not re.search(r"(?m)^\s*#{1,6}\s", str(text or "")):
        # 没有复选框、也没有标题的**裸清单**才退一步取 `- 项`。
        # 有标题的 TODO 文件里 `- **任务目标**：…` 是说明文字，不能当待办
        # （实测把 pyshot_todo.md 的说明段抓成了 50 条假待办）。
        for line in str(text or "").splitlines():
            bm = BULLET_RE.match(line)
            if not bm:
                continue
            content = _clean_item(bm.group(1))
            key = _norm(content)
            if not content or key in seen or len(content) > 200:
                continue
            seen.add(key)
            todos.append({"content": content, "status": "pending"})
            if len(todos) >= limit:
                break
    return todos


def _clean_item(text: str) -> str:
    """去掉 Markdown 强调/勾/优先级方括号里的噪声，保留可读文本。"""
    s = str(text or "").strip()
    s = re.sub(r"^\[(?: |x|X|✓|✔|-)\]\s*", "", s)
    s = s.replace("**", "").replace("`", "")
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" -—:：")


def merge_todos(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把文件里的清单合并进会话现有清单。

    规则：
    - 文件里有的项：已有同项 → 保留更"靠后"的状态（completed 不会被文件里的 `[ ]` 拉回）；
    - 旧清单里已经 completed、但文件里没了的项 → 保留（别让用户看到完成项凭空消失）；
    - 其余旧项丢弃（文件是新的计划）。
    """
    old_by_key: dict[str, dict[str, Any]] = {}
    for t in old or []:
        k = _norm(t.get("content"))
        if k:
            old_by_key[k] = t
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for t in new:
        k = _norm(t.get("content"))
        prev = old_by_key.get(k)
        item = dict(t)
        if prev:
            used.add(k)
            if prev.get("status") == "completed":
                item["status"] = "completed"
            elif prev.get("status") == "in_progress" and item.get("status") == "pending":
                item["status"] = "in_progress"
            if prev.get("agent_id"):
                item["agent_id"] = prev["agent_id"]
        out.append(item)
    for k, t in old_by_key.items():
        if k in used:
            continue
        if t.get("status") == "completed":
            out.append(dict(t))
    return out[:MAX_ITEMS]


def sync_todos_from_files(session_store: Any, sid: str, force: bool = False) -> bool:
    """把工作区 TODO 文件同步进会话待办；返回是否真的更新了。

    只在文件 mtime 变化时解析（`force=True` 强制一次，供测试/收尾用）。
    任何异常都被吞掉：同步失败绝不能影响任务本身。
    """
    try:
        state = session_store.get(sid)
        if not state:
            return False
        ws = state.get("workspace") or ""
        path = find_todo_file(ws)
        if path is None:
            return False
        mtime = path.stat().st_mtime
        prev = _FILE_STATE.get(sid)
        if not force and prev and prev[0] == str(path) and abs(prev[1] - mtime) < 1e-6:
            return False
        todos = parse_todos(path.read_text(encoding="utf-8", errors="replace"))
        if not todos:
            return False
        merged = merge_todos(state.get("todos") or [], todos)
        if merged == (state.get("todos") or []):
            _FILE_STATE[sid] = (str(path), mtime)
            return False
        session_store.patch(sid, todos=merged)
        _FILE_STATE[sid] = (str(path), mtime)
        return True
    except Exception:
        return False


def reset_cache(sid: str | None = None) -> None:
    """测试用：清掉 mtime 缓存。"""
    if sid is None:
        _FILE_STATE.clear()
    else:
        _FILE_STATE.pop(sid, None)
