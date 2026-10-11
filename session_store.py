from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class SessionStore:
    STATES = {"ready", "running", "paused", "waiting", "completed", "failed", "cancelled", "expired", "archived"}

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._init_db()

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _init_db(self):
        with self.lock, self._connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, original_task TEXT NOT NULL,
              state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
              workspace TEXT NOT NULL, model TEXT NOT NULL,
              current_url TEXT, conversation_index INTEGER NOT NULL DEFAULT 0,
              conversation_urls TEXT NOT NULL DEFAULT '[]', total_steps INTEGER NOT NULL DEFAULT 0,
              conversation_steps INTEGER NOT NULL DEFAULT 0, estimated_tokens INTEGER NOT NULL DEFAULT 0,
              estimated_chars INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '{}',
              recent_steps TEXT NOT NULL DEFAULT '[]', modified_files TEXT NOT NULL DEFAULT '{}',
              pending TEXT NOT NULL DEFAULT '[]', last_error TEXT,
              cancel_requested INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
              compact_requested INTEGER NOT NULL DEFAULT 0, rollover_requested INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS steps (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
              step_no INTEGER NOT NULL, conversation_index INTEGER NOT NULL,
              created REAL NOT NULL, tool TEXT, status TEXT NOT NULL,
              reason TEXT, payload TEXT NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
              created REAL NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
              conversation_index INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created);
            CREATE TABLE IF NOT EXISTS file_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
              created REAL NOT NULL, tool TEXT NOT NULL, path TEXT NOT NULL,
              before_hash TEXT, after_hash TEXT, result TEXT NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            -- 每个 Copilot 会话结束时的实测数据：回合数/估算Token/字符/结束原因/模型。
            -- M365 Copilot 的实际上限不公开且动态变化，这些样本用于校准本地阈值。
            CREATE TABLE IF NOT EXISTS conv_stats (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
              conversation_index INTEGER NOT NULL, model TEXT,
              steps INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,
              chars INTEGER NOT NULL DEFAULT 0, ended_by TEXT, url TEXT,
              created REAL NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_steps_session ON steps(session_id, step_no);
            CREATE INDEX IF NOT EXISTS idx_files_session ON file_events(session_id, created);
            """)

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        item = dict(row)
        for key in ("conversation_urls", "summary", "recent_steps", "modified_files", "pending"):
            try: item[key] = json.loads(item[key] or "null")
            except Exception: item[key] = [] if key in {"conversation_urls", "recent_steps", "pending"} else {}
        for key in ("cancel_requested", "archived", "compact_requested", "rollover_requested"):
            item[key] = bool(item[key])
        return item

    def create(self, sid: str, task: str, workspace: str, model: str, title: str | None = None):
        now = time.time()
        title = (title or task.splitlines()[0] or sid)[:120]
        with self.lock, self._connect() as con:
            con.execute("""INSERT OR IGNORE INTO sessions
                (id,title,original_task,state,created,updated,workspace,model)
                VALUES(?,?,?,?,?,?,?,?)""", (sid,title,task,"ready",now,now,workspace,model))
        return self.get(sid)

    def get(self, sid: str):
        with self.lock, self._connect() as con:
            return self._decode(con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())

    def list(self, include_archived: bool = False):
        query = "SELECT * FROM sessions" + ("" if include_archived else " WHERE archived=0") + " ORDER BY updated DESC"
        with self.lock, self._connect() as con:
            return [self._decode(x) for x in con.execute(query)]

    def patch(self, sid: str, **values):
        allowed = {"title","state","workspace","model","current_url","conversation_index","conversation_urls",
                   "total_steps","conversation_steps","estimated_tokens","estimated_chars","summary","recent_steps",
                   "modified_files","pending","last_error","cancel_requested","archived","compact_requested","rollover_requested"}
        values = {k:v for k,v in values.items() if k in allowed}
        if "state" in values and values["state"] not in self.STATES: raise ValueError("invalid state")
        for key in ("conversation_urls","summary","recent_steps","modified_files","pending"):
            if key in values: values[key] = json.dumps(values[key], ensure_ascii=False)
        for key in ("cancel_requested","archived","compact_requested","rollover_requested"):
            if key in values: values[key] = int(bool(values[key]))
        values["updated"] = time.time()
        with self.lock, self._connect() as con:
            con.execute("UPDATE sessions SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?", (*values.values(), sid))
        return self.get(sid)

    def add_step(self, sid: str, step_no: int, conversation_index: int, step: dict[str, Any]):
        with self.lock, self._connect() as con:
            con.execute("INSERT INTO steps(session_id,step_no,conversation_index,created,tool,status,reason,payload) VALUES(?,?,?,?,?,?,?,?)",
                        (sid,step_no,conversation_index,time.time(),step.get("tool"),step.get("status","unknown"),step.get("reason"),json.dumps(step,ensure_ascii=False)))

    def steps(self, sid: str, limit: int = 5000):
        with self.lock, self._connect() as con:
            rows=con.execute("SELECT payload FROM steps WHERE session_id=? ORDER BY step_no LIMIT ?",(sid,limit))
            return [json.loads(x[0]) for x in rows]

    def add_message(self, sid: str, role: str, content: str, conversation_index: int = 0):
        if role not in {"user", "assistant", "system"}:
            raise ValueError("invalid role")
        with self.lock, self._connect() as con:
            con.execute("INSERT INTO messages(session_id,created,role,content,conversation_index) VALUES(?,?,?,?,?)",
                        (sid,time.time(),role,content,int(conversation_index)))

    def messages(self, sid: str, limit: int = 500):
        with self.lock, self._connect() as con:
            # 按自增 id 排序：同一轮 user/assistant 消息的 created 几乎相同，
            # ORDER BY created 可能把回复排到提问前面。
            rows=con.execute("SELECT id,created,role,content,conversation_index FROM messages WHERE session_id=? ORDER BY id LIMIT ?",(sid,limit))
            return [dict(x) for x in rows]

    def add_file_event(self, sid: str, event: dict[str, Any]):
        with self.lock, self._connect() as con:
            con.execute("INSERT INTO file_events(session_id,created,tool,path,before_hash,after_hash,result) VALUES(?,?,?,?,?,?,?)",
                        (sid,time.time(),event.get("tool",""),event.get("path",""),event.get("before_hash"),event.get("after_hash"),event.get("result","unknown")))

    def file_events(self, sid: str):
        with self.lock, self._connect() as con:
            return [dict(x) for x in con.execute("SELECT * FROM file_events WHERE session_id=? ORDER BY created",(sid,))]

    def add_conv_stat(self, sid: str, stat: dict[str, Any]):
        with self.lock, self._connect() as con:
            con.execute("INSERT INTO conv_stats(session_id,conversation_index,model,steps,tokens,chars,ended_by,url,created) VALUES(?,?,?,?,?,?,?,?,?)",
                        (sid, int(stat.get("conversation_index", 0)), stat.get("model"),
                         int(stat.get("steps", 0)), int(stat.get("tokens", 0)), int(stat.get("chars", 0)),
                         stat.get("ended_by"), stat.get("url"), time.time()))

    def conv_stats(self, sid: str):
        with self.lock, self._connect() as con:
            return [dict(x) for x in con.execute("SELECT * FROM conv_stats WHERE session_id=? ORDER BY id", (sid,))]

    def delete(self, sid: str):
        with self.lock, self._connect() as con:
            return con.execute("DELETE FROM sessions WHERE id=?",(sid,)).rowcount > 0

    def recover_interrupted(self):
        with self.lock, self._connect() as con:
            con.execute("UPDATE sessions SET state='paused', last_error='服务中断，可从检查点恢复', updated=? WHERE state IN ('running','waiting')",(time.time(),))

    def interrupted_sessions(self):
        """上次服务中断（而非用户主动暂停）的会话，供 auto_resume 恢复。"""
        with self.lock, self._connect() as con:
            rows = con.execute(
                "SELECT * FROM sessions WHERE archived=0 AND state='paused' AND last_error='服务中断，可从检查点恢复' ORDER BY updated").fetchall()
            return [self._decode(x) for x in rows]

    def run_maintenance(self, session_timeout_seconds=None, retention_days=None, max_sessions=None):
        """周期性会话清理：超时会话置为 expired；历史完成的会话按保留期归档；
        未归档会话数量超过上限时，从最久未更新的开始归档。返回各项处理数量。"""
        now = time.time()
        result = {"expired": 0, "archived_by_retention": 0, "archived_by_cap": 0}
        with self.lock, self._connect() as con:
            if session_timeout_seconds:
                result["expired"] = con.execute(
                    "UPDATE sessions SET state='expired', last_error='会话长时间未活动，已过期', updated=?"
                    " WHERE archived=0 AND state IN ('ready','waiting') AND ? - updated > ?",
                    (now, now, float(session_timeout_seconds))).rowcount
            if retention_days:
                cutoff = now - float(retention_days) * 86400
                result["archived_by_retention"] = con.execute(
                    "UPDATE sessions SET archived=1, state='archived', updated=?"
                    " WHERE archived=0 AND state IN ('completed','cancelled','failed','expired') AND updated < ?",
                    (now, cutoff)).rowcount
            if max_sessions:
                rows = con.execute("SELECT id FROM sessions WHERE archived=0 ORDER BY updated DESC").fetchall()
                excess = len(rows) - int(max_sessions)
                if excess > 0:
                    ids = [r[0] for r in rows[-excess:]]
                    con.execute(
                        f"UPDATE sessions SET archived=1, state='archived', updated=? WHERE id IN ({','.join('?' * len(ids))})",
                        (now, *ids))
                    result["archived_by_cap"] = len(ids)
        return result
