"""长期记忆：跨会话累积的事实、偏好、约定与踩坑结论。

与 AGENT.md / skills 的分工：
- `AGENT.md`：需要**每回合全文遵守**的硬约定（短、稳定、人工维护）。
- `skills/`：可复用的"怎么做"（过程性知识，按需 skill_read 取全文）。
- **记忆（本模块）**：零散事实与经验（"这个仓库的测试要 15 秒""用户偏好中文""上次
  某接口会被限流"）。数量大、单条短、需要**检索**而不该整篇注入。

设计要点：
- 与 agent.db 分开存放（`data/memory.db`），互不影响迁移与备份；
- 作用域 global / workspace：工作区记忆只在该工作区任务里可见，global 对所有任务可见；
- 检索不依赖 embedding：FTS5 可用时用 FTS，否则退回关键词打分（零依赖、确定性）；
- 去重：同作用域下文本归一化相同视为同一条，写入即更新并累计 hits；
- 使用计数：被检索命中并注入提示时 use_count+1，长期零命中的低价值记忆可被淘汰；
- 有条数上限，超出时按 (pinned, use_count, updated) 排序淘汰。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,              -- global | ws:<hash>
  kind TEXT NOT NULL DEFAULT 'fact',-- fact | preference | decision | pitfall | convention
  text TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '[]',
  source_session TEXT,
  pinned INTEGER NOT NULL DEFAULT 0,
  use_count INTEGER NOT NULL DEFAULT 0,
  last_used REAL,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  norm TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_scope ON memories(scope, updated);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_norm ON memories(scope, norm);
"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())[:400]


def _terms(query: str) -> list[str]:
    """把查询切成检索词：英文/数字按词；中文按**二元组**切。

    不能按单字切中文：查询里的"一下"会命中"买一送一"这类无关记忆，
    实测会把噪声注入提示词。二元组（bigram）无需分词器即可大幅提高选择性。
    """
    q = str(query or "").lower()
    terms = re.findall(r"[a-z0-9_\.\-]{2,}", q)
    for chunk in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(chunk) == 1:
            terms.append(chunk)
        else:
            terms.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return terms


class MemoryStore:
    MAX_PER_SCOPE = 500

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._init()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        try:
            with self._connect() as con:
                con.executescript(SCHEMA)
        except Exception:
            pass

    # ── 写入 ──────────────────────────────────────────────────────
    def remember(self, scope: str, text: str, kind: str = "fact",
                 tags: list[str] | None = None, source_session: str | None = None,
                 pinned: bool = False) -> dict[str, Any]:
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("记忆内容不能为空")
        if len(text) > 2000:
            raise ValueError("单条记忆不超过 2000 字符（过长请写入 AGENT.md 或技能）")
        kind = str(kind or "fact").strip().lower()
        if kind not in {"fact", "preference", "decision", "pitfall", "convention"}:
            kind = "fact"
        norm = _norm(text)
        now = time.time()
        with self._connect() as con:
            row = con.execute("SELECT id, use_count, kind, source_session FROM memories WHERE scope=? AND norm=?",
                              (scope, norm)).fetchone()
            if row:
                # 重复写入视为更新：默认 kind=fact 不应把已标注的 preference/pitfall 覆盖掉
                keep_kind = kind if kind != "fact" else row["kind"]
                # 去重时一并更新 source_session：记录"最后写入者"，便于溯源与
                # 断言归属（否则旧条目仍指向很久以前的会话）
                _prev_sid = row["source_session"] if "source_session" in row.keys() else None
                con.execute("UPDATE memories SET text=?, kind=?, tags=?, updated=?, "
                            "pinned=MAX(pinned, ?), source_session=? WHERE id=?",
                            (text, keep_kind, json.dumps(tags or [], ensure_ascii=False), now,
                             1 if pinned else 0, source_session or _prev_sid, row["id"]))
                mid, created, updated = row["id"], False, True
                kind = keep_kind
            else:
                cur = con.execute(
                    "INSERT INTO memories(scope,kind,text,tags,source_session,pinned,use_count,"
                    "created,updated,norm) VALUES(?,?,?,?,?,?,0,?,?,?)",
                    (scope, kind, text, json.dumps(tags or [], ensure_ascii=False),
                     source_session, 1 if pinned else 0, now, now, norm))
                mid, created, updated = cur.lastrowid, True, False
            self._trim(con, scope)
        return {"id": mid, "created": created, "updated": updated, "scope": scope,
                "kind": kind, "text": text, "pinned": bool(pinned)}

    def _trim(self, con: sqlite3.Connection, scope: str) -> None:
        """超上限时淘汰：先保 pinned，再按使用次数与更新时间。"""
        total = con.execute("SELECT COUNT(*) c FROM memories WHERE scope=?", (scope,)).fetchone()["c"]
        if total <= self.MAX_PER_SCOPE:
            return
        victims = con.execute(
            "SELECT id FROM memories WHERE scope=? ORDER BY pinned ASC, use_count ASC, updated ASC "
            "LIMIT ?", (scope, total - self.MAX_PER_SCOPE)).fetchall()
        con.executemany("DELETE FROM memories WHERE id=?", [(v["id"],) for v in victims])

    # ── 检索 ──────────────────────────────────────────────────────
    def search(self, query: str, scopes: list[str], limit: int = 8,
               kinds: list[str] | None = None, touch: bool = True) -> list[dict[str, Any]]:
        """关键词打分检索；pinned 记忆始终优先返回（不受查询命中与否影响）。"""
        scopes = [s for s in (scopes or []) if s]
        if not scopes:
            return []
        limit = max(1, min(int(limit or 8), 50))
        placeholders = ",".join("?" for _ in scopes)
        sql = f"SELECT * FROM memories WHERE scope IN ({placeholders})"
        args: list[Any] = list(scopes)
        if kinds:
            sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
            args.extend(kinds)
        with self._connect() as con:
            rows = [dict(r) for r in con.execute(sql, args)]
            pinned = [r for r in rows if r["pinned"]]
            pool = rows if not query else rows
            terms = _terms(query)
            scored = []
            for r in pool:
                if r["pinned"]:
                    continue
                hay = (r["text"] + " " + " ".join(json.loads(r["tags"] or "[]"))).lower()
                if not terms:
                    score = 0.0
                else:
                    hits = sum(1 for t in terms if t in hay)
                    if not hits:
                        continue
                    score = hits / max(len(terms), 1)
                    # 越新、越常被用到的记忆略微加权
                    score += min(r["use_count"], 10) * 0.01
                scored.append((score, r["updated"], r))
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            picked = pinned[:3] + [r for _s, _u, r in scored]
            picked = picked[:limit]
            if touch and picked:
                now = time.time()
                con.executemany("UPDATE memories SET use_count=use_count+1, last_used=? WHERE id=?",
                                [(now, r["id"]) for r in picked])
        for r in picked:
            try:
                r["tags"] = json.loads(r["tags"] or "[]")
            except Exception:
                r["tags"] = []
        return picked

    def list(self, scopes: list[str], limit: int = 100, query: str = "") -> list[dict[str, Any]]:
        scopes = [s for s in (scopes or []) if s]
        if not scopes:
            return []
        placeholders = ",".join("?" for _ in scopes)
        with self._connect() as con:
            rows = [dict(r) for r in con.execute(
                f"SELECT * FROM memories WHERE scope IN ({placeholders}) "
                "ORDER BY pinned DESC, updated DESC LIMIT ?", (*scopes, max(1, min(limit, 500))))]
        if query:
            terms = _terms(query)
            rows = [r for r in rows if any(t in r["text"].lower() for t in terms)] if terms else rows
        for r in rows:
            try:
                r["tags"] = json.loads(r["tags"] or "[]")
            except Exception:
                r["tags"] = []
        return rows

    def set_pinned(self, mid: int, pinned: bool, scopes: list[str] | None = None) -> bool:
        """置顶/取消置顶：pinned 记忆在检索时恒定返回（适合极重要的硬约定）。"""
        with self._connect() as con:
            if scopes:
                placeholders = ",".join("?" for _ in scopes)
                cur = con.execute(
                    f"UPDATE memories SET pinned=?, updated=? WHERE id=? AND scope IN ({placeholders})",
                    (1 if pinned else 0, time.time(), int(mid), *scopes))
            else:
                cur = con.execute("UPDATE memories SET pinned=?, updated=? WHERE id=?",
                                  (1 if pinned else 0, time.time(), int(mid)))
            return cur.rowcount > 0

    def forget(self, mid: int, scopes: list[str] | None = None) -> bool:
        with self._connect() as con:
            if scopes:
                placeholders = ",".join("?" for _ in scopes)
                cur = con.execute(f"DELETE FROM memories WHERE id=? AND scope IN ({placeholders})",
                                  (int(mid), *scopes))
            else:
                cur = con.execute("DELETE FROM memories WHERE id=?", (int(mid),))
            return cur.rowcount > 0

    def stats(self, scopes: list[str]) -> dict[str, Any]:
        scopes = [s for s in (scopes or []) if s]
        if not scopes:
            return {"total": 0, "by_kind": {}, "pinned": 0, "unused": 0}
        placeholders = ",".join("?" for _ in scopes)
        with self._connect() as con:
            rows = [dict(r) for r in con.execute(
                f"SELECT kind, COUNT(*) c, SUM(pinned) p, SUM(CASE WHEN use_count=0 THEN 1 ELSE 0 END) u "
                f"FROM memories WHERE scope IN ({placeholders}) GROUP BY kind", scopes)]
        return {"total": sum(r["c"] for r in rows),
                "by_kind": {r["kind"]: r["c"] for r in rows},
                "pinned": sum(r["p"] or 0 for r in rows),
                "unused": sum(r["u"] or 0 for r in rows)}
