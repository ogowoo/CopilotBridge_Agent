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
            CREATE TABLE IF NOT EXISTS turn_results (
  session_id TEXT NOT NULL,
  conversation_index INTEGER NOT NULL DEFAULT 0,
  prompt_hash TEXT NOT NULL,
  answer TEXT NOT NULL DEFAULT '',
  steps_after INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL,
  PRIMARY KEY (session_id, conversation_index, prompt_hash)
);
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
            # 迁移：rollover 独立计数器——estimated_* 在压缩时重置（本地上下文），
            # conv_* 只在换会话时清零（反映远端对话真实压力）。
            for col in ("conv_tokens", "conv_chars"):
                try:
                    con.execute(f"ALTER TABLE sessions ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
                except Exception:
                    pass  # 列已存在
            # 迁移：验证闭环旗持久化——暂停/重启后必须记得"代码改了还没验证"，
            # 否则恢复出来的 Agent 直接返回 final 就能绕过验证门。
            try:
                con.execute("ALTER TABLE sessions ADD COLUMN gate_state TEXT")
            except Exception:
                pass  # 列已存在
            # 迁移：服务限流退避的倒计时信息——resume_at 供前端右上角浮窗倒数；
            # rate_limited_since 记录首次限流起点，窗口期内重试都锚定同一终点。
            try:
                con.execute("ALTER TABLE sessions ADD COLUMN retry_resume_at REAL")
            except Exception:
                pass  # 列已存在
            try:
                con.execute("ALTER TABLE sessions ADD COLUMN retry_attempt INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass  # 列已存在
            try:
                con.execute("ALTER TABLE sessions ADD COLUMN rate_limited_since REAL")
            except Exception:
                pass  # 列已存在
            # 迁移：会话的**初始提示词**（首回合发给模型的完整内容，含系统提示词、
            # 工具清单、项目约定）。RUNTIME.prompt_preview 每回合都被覆盖，不落库
            # 就再也看不到"这个任务当初被交待了什么"。
            try:
                con.execute("ALTER TABLE sessions ADD COLUMN initial_prompt TEXT")
            except Exception:
                pass  # 列已存在

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
                   "modified_files","pending","last_error","cancel_requested","archived","compact_requested",
                   "rollover_requested","conv_tokens","conv_chars","gate_state",
                   "retry_resume_at","retry_attempt","rate_limited_since","initial_prompt"}
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
            # 按自增 id 排序：换会话/服务不可用等事件复用 step_no，按 id 才稳定
            rows=con.execute("SELECT payload, created FROM steps WHERE session_id=? ORDER BY id LIMIT ?",(sid,limit))
            result = []
            for payload, created in rows:
                item = json.loads(payload)
                item.setdefault("_time", created)  # epoch 秒，前端格式化显示
                result.append(item)
            return result

    def add_message(self, sid: str, role: str, content: str, conversation_index: int = 0):
        if role not in {"user", "assistant", "system"}:
            raise ValueError("invalid role")
        with self.lock, self._connect() as con:
            cur = con.execute("INSERT INTO messages(session_id,created,role,content,conversation_index) VALUES(?,?,?,?,?)",
                              (sid, time.time(), role, content, int(conversation_index)))
            return cur.lastrowid

    def update_message(self, sid: str, message_id: int, content: str) -> bool:
        """更新一条消息内容（供队列中待处理消息的编辑）。"""
        with self.lock, self._connect() as con:
            cur = con.execute("UPDATE messages SET content=? WHERE id=? AND session_id=?",
                              (content, int(message_id), sid))
            return cur.rowcount > 0

    def delete_message(self, sid: str, message_id: int) -> bool:
        """删除一条消息（供队列中待处理消息的删除）。"""
        with self.lock, self._connect() as con:
            cur = con.execute("DELETE FROM messages WHERE id=? AND session_id=?",
                              (int(message_id), sid))
            return cur.rowcount > 0

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

    def save_turn(self, sid: str, conv_index: int, prompt_hash: str,
                  answer: str, steps_after: int) -> None:
        """记录"这个回合已真正发过并产出结果"：恢复时可据此跳过重发。"""
        with self.lock, self._connect() as con:
            con.execute(
                "INSERT INTO turn_results(session_id,conversation_index,prompt_hash,answer,steps_after,created) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(session_id,conversation_index,prompt_hash) "
                "DO UPDATE SET answer=excluded.answer, steps_after=excluded.steps_after, created=excluded.created",
                (sid, int(conv_index), prompt_hash, (answer or "")[:4000], int(steps_after), time.time()))

    def get_turn(self, sid: str, conv_index: int, prompt_hash: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM turn_results WHERE session_id=? AND conversation_index=? AND prompt_hash=?",
                (sid, int(conv_index), prompt_hash)).fetchone()
            return dict(row) if row else None

    def delete(self, sid: str):
        """删除会话及其全部从属数据。

        只删 sessions 行会让 messages/steps/file_events/conv_stats 永久残留
        （会话已不可达，等于不可回收的垃圾数据）。
        """
        with self.lock, self._connect() as con:
            existed = con.execute("DELETE FROM sessions WHERE id=?", (sid,)).rowcount > 0
            for table in ("messages", "steps", "file_events", "conv_stats", "turn_results"):
                try:
                    con.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
                except Exception:
                    pass  # 老库可能缺表：删主记录成功即视为成功
            return existed

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
                # 数量上限只归档终态会话；paused/ready/waiting 是可恢复状态，归档会丢失
                rows = con.execute(
                    "SELECT id FROM sessions WHERE archived=0 AND state IN ('completed','cancelled','failed','expired')"
                    " ORDER BY updated DESC").fetchall()
                excess = len(rows) - int(max_sessions)
                if excess > 0:
                    ids = [r[0] for r in rows[-excess:]]
                    con.execute(
                        f"UPDATE sessions SET archived=1, state='archived', updated=? WHERE id IN ({','.join('?' * len(ids))})",
                        (now, *ids))
                    result["archived_by_cap"] = len(ids)
        return result
