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
            CREATE TABLE IF NOT EXISTS step_dispatch (
  session_id TEXT NOT NULL,
  conversation_index INTEGER NOT NULL DEFAULT 0,
  step_no INTEGER NOT NULL,
  tool TEXT,
  args_hash TEXT,
  status TEXT NOT NULL DEFAULT 'inflight',
  created REAL NOT NULL,
  PRIMARY KEY (session_id, conversation_index, step_no)
);
CREATE TABLE IF NOT EXISTS approvals (
  session_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  args_hash TEXT NOT NULL,
  created REAL NOT NULL,
  consumed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, tool, args_hash)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  created REAL NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  session_id TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  created REAL NOT NULL
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
  status TEXT NOT NULL DEFAULT 'done',
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
            # 迁移：回合表补 status（inflight/done），用于"已派发但结果未知"的判定
            try:
                con.execute("ALTER TABLE turn_results ADD COLUMN status TEXT NOT NULL DEFAULT 'done'")
            except Exception:
                pass  # 列已存在或表尚未创建
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
                con.execute("ALTER TABLE sessions ADD COLUMN pending_steps TEXT")
            except Exception:
                pass  # 列已存在
            try:
                con.execute("ALTER TABLE sessions ADD COLUMN approval_pending TEXT")
            except Exception:
                pass  # 列已存在
            try:
                con.execute("ALTER TABLE sessions ADD COLUMN initial_prompt TEXT")
            except Exception:
                pass  # 列已存在
            # 迁移：消息携带的附件（粘贴的图片/截图）。只存 data/uploads 下的
            # **文件名**（JSON 数组），不存绝对路径，服务目录搬家也不受影响；
            # 前端经 GET /v1/uploads/{name} 取图（鉴权中间件统一管）。
            try:
                con.execute("ALTER TABLE messages ADD COLUMN attachments TEXT")
            except Exception:
                pass  # 列已存在
            # 迁移：会话标签（自由文本，用于列表分组/筛选；空串视为未打标签）
            try:
                con.execute("ALTER TABLE sessions ADD COLUMN tag TEXT")
            except Exception:
                pass  # 列已存在
            # 迁移：子代理的父子关系与短标签（子代理面板按 parent_id 列出）
            # 迁移：目标（goal 模式）与计划（todo_write）——此前 patch 白名单里有 goal
            # 但**表里没有这一列**，写库直接抛错又被吞掉，导致"create_goal 成功但目标
            # 从未落库"（update_goal 随后报"当前会话没有目标"）。
            for _col, _decl in (("parent_id", "TEXT"), ("description", "TEXT"),
                                ("goal", "TEXT"), ("todos", "TEXT"),
                                ("plan", "TEXT"),
                                ("plan_mode", "INTEGER NOT NULL DEFAULT 0")):
                try:
                    con.execute(f"ALTER TABLE sessions ADD COLUMN {_col} {_decl}")
                except Exception:
                    pass  # 列已存在

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        item = dict(row)
        for key in ("conversation_urls", "summary", "recent_steps", "modified_files", "pending",
                    "pending_steps", "approval_pending", "goal", "todos", "plan"):
            try: item[key] = json.loads(item[key] or "null")
            except Exception: item[key] = [] if key in {"conversation_urls", "recent_steps", "pending", "pending_steps", "todos"} else None
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
        # attachment_count：该会话带图片附件的消息数，供会话列表显示 📎 角标。
        query = ("SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id "
                 "AND m.attachments IS NOT NULL AND m.attachments NOT IN ('[]','')) AS attachment_count "
                 "FROM sessions s" + ("" if include_archived else " WHERE s.archived=0")
                 + " ORDER BY s.updated DESC")
        with self.lock, self._connect() as con:
            return [self._decode(x) for x in con.execute(query)]

    def list_subagents(self, parent_id: str | None = None, limit: int = 50):
        """列出子代理会话（parent_id 非空），可选按父会话过滤。

        子代理面板据此显示"有哪些子代理、在跑什么、进度到哪"。
        """
        sql = ("SELECT * FROM sessions WHERE parent_id IS NOT NULL AND parent_id != '' "
               + ("AND parent_id=? " if parent_id else "")
               + "ORDER BY updated DESC LIMIT ?")
        args = ((parent_id,) if parent_id else ()) + (int(limit),)
        with self.lock, self._connect() as con:
            return [self._decode(x) for x in con.execute(sql, args)]

    def patch(self, sid: str, **values):
        allowed = {"title","state","workspace","model","current_url","conversation_index","conversation_urls",
                   "total_steps","conversation_steps","estimated_tokens","estimated_chars","summary","recent_steps",
                   "modified_files","pending","last_error","cancel_requested","archived","compact_requested",
                   "rollover_requested","conv_tokens","conv_chars","gate_state",
                   "retry_resume_at","retry_attempt","rate_limited_since","initial_prompt",
            "pending_steps","approval_pending","tag","goal","todos","plan","plan_mode",
            "parent_id","description"}
        values = {k:v for k,v in values.items() if k in allowed}
        if "state" in values and values["state"] not in self.STATES: raise ValueError("invalid state")
        for key in ("conversation_urls","summary","recent_steps","modified_files","pending",
                "pending_steps","approval_pending","goal","todos","plan"):
            if key in values: values[key] = json.dumps(values[key], ensure_ascii=False)
        if "plan_mode" in values:
            values["plan_mode"] = int(bool(values["plan_mode"]))
        for key in ("cancel_requested","archived","compact_requested","rollover_requested"):
            if key in values: values[key] = int(bool(values[key]))
        values["updated"] = time.time()
        with self.lock, self._connect() as con:
            prev_state = None
            prev_files: dict = {}
            # 这三个默认值必须在块首给全：任何分支（含"会话不存在"）都会走到后面的
            # 事件发出逻辑，缺一个就是 UnboundLocalError（本轮就踩了，19 个测试同时红）。
            prev_pending: list = []
            prev_pending_steps: list = []
            if any(k in values for k in ("state", "modified_files", "pending", "pending_steps")):
                row = con.execute("SELECT state, modified_files, pending, pending_steps "
                                  "FROM sessions WHERE id=?", (sid,)).fetchone()
                if row:
                    prev_state = row["state"]
                    try:
                        prev_files = json.loads(row["modified_files"] or "{}")
                    except Exception:
                        prev_files = {}
                    try:
                        prev_pending = json.loads(row["pending"] or "[]")
                    except Exception:
                        prev_pending = []
                    try:
                        prev_pending_steps = json.loads(row["pending_steps"] or "[]")
                    except Exception:
                        prev_pending_steps = []
            con.execute("UPDATE sessions SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?", (*values.values(), sid))
            # 状态迁移是"日志即真相"最关键的一类事件：只记**真实变化**（重复写同值不记），
            # 并带上 from/to，便于重放与审计。事件复用同一连接（见 append_event 注释）。
            if "state" in values and prev_state != values["state"]:
                self.append_event(sid, "state",
                                  {"from": prev_state, "to": values["state"]}, con=con)
            if "modified_files" in values:
                try:
                    cur_files = json.loads(values["modified_files"] or "{}")
                except Exception:
                    cur_files = {}
                added = sorted(set(cur_files) - set(prev_files))
                removed = sorted(set(prev_files) - set(cur_files))
                if added or removed:
                    self.append_event(sid, "files_modified",
                                      {"added": added[:50], "removed": removed[:50],
                                       "total": len(cur_files)}, con=con)
            for key, prev_list in (("pending", prev_pending), ("pending_steps", prev_pending_steps)):
                if key not in values:
                    continue
                try:
                    cur_list = json.loads(values[key] or "[]")
                except Exception:
                    cur_list = []
                if len(cur_list) != len(prev_list):
                    # 队列进出（用户插话被消费 / 新插话入队）——只记数量变化，不记正文
                    self.append_event(sid, "queue",
                                      {"queue": key, "before": len(prev_list), "after": len(cur_list)},
                                      con=con)
        return self.get(sid)

    def add_step(self, sid: str, step_no: int, conversation_index: int, step: dict[str, Any]):
        with self.lock, self._connect() as con:
            self.append_event(sid, "step_recorded",
                              {"step_no": int(step_no), "tool": step.get("tool"),
                               "status": step.get("status", "unknown")}, con=con)
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

    DEDUPE_WINDOW = 5.0      # 同内容 assistant 消息在这个窗口内视为重复

    def add_message(self, sid: str, role: str, content: str, conversation_index: int = 0,
                    attachments: list[str] | None = None):
        if role not in {"user", "assistant", "system"}:
            raise ValueError("invalid role")
        atts = json.dumps([str(a) for a in (attachments or [])], ensure_ascii=False) if attachments else None
        # 兜底去重：同一条回答被两条代码路径各写一次时，只留第一条。
        # 实测事故：队列 worker 与 run_agent 各写一次 → 同一秒同内容两条
        # （message id 181/182），界面里回答显示两遍。
        if role == "assistant":
            with self.lock, self._connect() as con:
                last = con.execute(
                    "SELECT content, created FROM messages WHERE session_id=? "
                    "AND conversation_index=? AND role='assistant' "
                    "ORDER BY id DESC LIMIT 1",
                    (sid, conversation_index)).fetchone()
            if (last and (last["content"] or "").strip() == (content or "").strip()
                    and abs(time.time() - float(last["created"] or 0)) <= self.DEDUPE_WINDOW):
                return None
        with self.lock, self._connect() as con:
            cur = con.execute("INSERT INTO messages(session_id,created,role,content,conversation_index,attachments) VALUES(?,?,?,?,?,?)",
                              (sid, time.time(), role, content, int(conversation_index), atts))
            # 事件只记元信息（角色/长度/对话序号），正文留在 messages 表：
            # 事件表要保证轻量，重放对话时按 id 关联即可。
            self.append_event(sid, "message",
                              {"role": role, "chars": len(content or ""),
                               "conversation_index": int(conversation_index),
                               "message_id": cur.lastrowid}, con=con)
            return cur.lastrowid

    def search_messages(self, keyword: str, limit: int = 30) -> list[dict]:
        """跨会话搜索对话正文；返回命中消息 + 其会话标题/状态（供点开跳转）。

        LIKE 通配符要转义：用户搜 "100%" 或 "_" 不应变成通配（否则结果莫名其妙）。
        命中片段在 Python 侧截取（SQLite 没有内置的子串定位/高亮）。
        """
        kw = str(keyword or "")
        if not kw:
            return []
        esc = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{esc}%"
        with self.lock, self._connect() as con:
            rows = con.execute(
                "SELECT m.id, m.session_id, m.role, m.content, m.created, s.title, s.state "
                "FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE m.content LIKE ? ESCAPE '\\' AND s.archived = 0 "
                "ORDER BY m.created DESC LIMIT ?", (pattern, int(limit)))
            out = []
            low = kw.lower()
            for mid, sid, role, content, created, title, state in rows:
                text = str(content or "")
                idx = text.lower().find(low)
                if idx < 0:
                    idx = 0
                start = max(0, idx - 60)
                snippet = text[start:start + 240]
                out.append({"message_id": mid, "session_id": sid, "role": role,
                            "created": created, "title": title, "state": state,
                            "snippet": ("…" if start > 0 else "") + snippet
                                       + ("…" if start + 240 < len(text) else "")})
            return out

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
            rows=con.execute("SELECT id,created,role,content,conversation_index,attachments FROM messages WHERE session_id=? ORDER BY id LIMIT ?",(sid,limit))
            out = []
            for x in rows:
                d = dict(x)
                raw = d.pop("attachments", None)
                try:
                    d["attachments"] = json.loads(raw) if raw else []
                except Exception:
                    d["attachments"] = []
                out.append(d)
            return out

    def add_file_event(self, sid: str, event: dict[str, Any]):
        # 这才是 Agent 任务里文件改动的**真实写路径**（messages 表只服务 WebUI 对话，
        # Agent 的对话在 steps 表；之前我把事件挂在 patch(modified_files=) 上，
        # 而那条路径在 Agent 流程里根本不会被写到——真机验证才发现）。
        self.append_event(sid, "file_event",
                          {"tool": event.get("tool", ""), "path": event.get("path", ""),
                           "result": event.get("result", ""),
                           "after_hash": str(event.get("after_hash") or "")[:12]})
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

    def append_audit(self, kind: str, sid: str, payload: dict[str, Any] | None = None) -> None:
        """全局审计流水（**不随会话删除**）：只会记"发生了什么"，不记内容。

        为什么单独一张表：会话删除会级联清掉 `events`（隐私优先），但"某会话何时被删"
        这种审计事实不该随之消失。因此只保留 kind + session_id + 少量元信息。
        """
        try:
            with self.lock, self._connect() as con:
                con.execute("INSERT INTO audit(kind,session_id,payload,created) VALUES(?,?,?,?)",
                            (str(kind)[:60], str(sid)[:120],
                             json.dumps(payload or {}, ensure_ascii=False, default=str)[:4000],
                             time.time()))
        except Exception:
            pass

    def audit_log(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.lock, self._connect() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),))]

    def append_event(self, sid: str, kind: str, payload: dict[str, Any] | None = None,
                     con: sqlite3.Connection | None = None) -> None:
        """追加一条**事件**（P2-10 第一期：与现有列并行写，读取路径暂不改变）。

        目的：为"日志即唯一真相、状态可由日志重建"铺路。现在两边都写，任何一方出问题
        都不会影响现有行为；等事件覆盖足够全面后，再考虑把读取路径切到投影上。
        失败绝不影响主流程（事件是旁路，不是前置条件）。
        """
        if not sid or not kind:
            return
        row = (sid, str(kind)[:60], json.dumps(payload or {}, ensure_ascii=False, default=str)[:20000],
               time.time())
        try:
            if con is not None:
                # 复用调用方连接：**必须**这样做——调用方通常已持有写事务，另开连接会
                # 因 SQLite 写锁互斥而阻塞到超时（实测每条事件等 30 秒且最终丢失）。
                con.execute("INSERT INTO events(session_id,kind,payload,created) VALUES(?,?,?,?)", row)
            else:
                with self.lock, self._connect() as own:
                    own.execute("INSERT INTO events(session_id,kind,payload,created) VALUES(?,?,?,?)", row)
        except Exception:
            pass

    def events(self, sid: str, limit: int = 500, kinds: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        with self.lock, self._connect() as con:
            if kinds:
                marks = ",".join("?" for _ in kinds)
                rows = con.execute(f"SELECT * FROM events WHERE session_id=? AND kind IN ({marks}) "
                                   f"ORDER BY id LIMIT ?", (sid, *kinds, int(limit))).fetchall()
            else:
                rows = con.execute("SELECT * FROM events WHERE session_id=? ORDER BY id LIMIT ?",
                                   (sid, int(limit))).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                try:
                    item["payload"] = json.loads(item.get("payload") or "{}")
                except Exception:
                    item["payload"] = {}
                out.append(item)
            return out

    def begin_turn(self, sid: str, conv_index: int, prompt_hash: str, steps_before: int) -> None:
        """**发请求之前**登记"这一回合即将派发"（DSH 的 checkpoint-before-dispatch 思路）。

        崩溃/中断后若看到 status='inflight'，说明该回合已发出但结果未知——此时既不能
        当作没发生（可能已产生副作用），也不能直接重发（可能重复副作用），只能让模型
        先做只读核对。这正是"日志即真相"的最小可用形态。
        """
        with self.lock, self._connect() as con:
            con.execute(
                "INSERT INTO turn_results(session_id,conversation_index,prompt_hash,answer,steps_after,status,created) "
                "VALUES(?,?,?,'',?,'inflight',?) ON CONFLICT(session_id,conversation_index,prompt_hash) "
                "DO UPDATE SET steps_after=excluded.steps_after, status='inflight', created=excluded.created",
                (sid, int(conv_index), prompt_hash, int(steps_before), time.time()))
            self.append_event(sid, "turn_begin", {"prompt_hash": prompt_hash, "steps_before": int(steps_before),
                                                  "conversation_index": int(conv_index)}, con=con)

    def grant_approval(self, sid: str, tool: str, args_hash: str) -> None:
        """用户下达一次性授权（allowed-once）。没有 allow-always、也没有撤销存储。"""
        with self.lock, self._connect() as con:
            con.execute("INSERT INTO approvals(session_id,tool,args_hash,created,consumed) "
                        "VALUES(?,?,?,?,0) ON CONFLICT(session_id,tool,args_hash) "
                        "DO UPDATE SET created=excluded.created, consumed=0",
                        (sid, tool, args_hash, time.time()))
            self.append_event(sid, "approval_granted", {"tool": tool, "args_hash": args_hash}, con=con)

    def grant_session_approval(self, sid: str, tool: str) -> None:
        """本会话级授权：该工具在本会话内的后续调用都放行（args_hash='*'）。

        比 approval_policy=never 安全（只影响单个会话 + 单个工具，会话结束即失效），
        比逐参数 allowed-once 省事（模型换个命令措辞不再反复弹授权）。
        """
        with self.lock, self._connect() as con:
            con.execute("INSERT INTO approvals(session_id,tool,args_hash,created,consumed) "
                        "VALUES(?,?,?,?,0) ON CONFLICT(session_id,tool,args_hash) "
                        "DO UPDATE SET created=excluded.created, consumed=0",
                        (sid, tool, "*", time.time()))
            self.append_event(sid, "approval_granted", {"tool": tool, "scope": "session"}, con=con)

    def copy_session_approvals(self, parent_sid: str, child_sid: str) -> int:
        """把父会话的**会话级**授权（args_hash='*'）继承给子会话。

        子代理是同一任务的一部分：用户在父会话里授权了 pwsh，子代理不该再弹一次。
        只复制会话级授权（逐参数的一次性授权不继承——那是针对具体那次调用的）。
        """
        with self.lock, self._connect() as con:
            rows = con.execute(
                "SELECT tool FROM approvals WHERE session_id=? AND args_hash='*' AND consumed=0",
                (parent_sid,)).fetchall()
            for row in rows:
                con.execute(
                    "INSERT INTO approvals(session_id,tool,args_hash,created,consumed) "
                    "VALUES(?,?,?,?,0) ON CONFLICT(session_id,tool,args_hash) "
                    "DO UPDATE SET created=excluded.created, consumed=0",
                    (child_sid, row["tool"], "*", time.time()))
            return len(rows)

    def take_approval(self, sid: str, tool: str, args_hash: str) -> bool:
        """取用授权：先精确匹配（消费式），再回退到本会话级（不消费）。

        fail-closed：两种都查不到才返回 False。
        """
        with self.lock, self._connect() as con:
            # ① 会话级授权（不消费，整场有效）
            row = con.execute("SELECT consumed FROM approvals WHERE session_id=? AND tool=? AND args_hash='*'",
                              (sid, tool)).fetchone()
            if row and int(row["consumed"] or 0) == 0:
                con.execute("UPDATE approvals SET consumed=0 WHERE session_id=? AND tool=? AND args_hash='*'",
                            (sid, tool))
                return True
            # ② 精确匹配的 allowed-once
            row = con.execute("SELECT consumed FROM approvals WHERE session_id=? AND tool=? AND args_hash=?",
                              (sid, tool, args_hash)).fetchone()
            if not row or int(row["consumed"] or 0) == 1:
                self.append_event(sid, "approval_denied", {"tool": tool, "args_hash": args_hash}, con=con)
                return False
            con.execute("UPDATE approvals SET consumed=1 WHERE session_id=? AND tool=? AND args_hash=?",
                        (sid, tool, args_hash))
            self.append_event(sid, "approval_taken", {"tool": tool, "args_hash": args_hash}, con=con)
            return True

    def pending_approvals(self, sid: str) -> list[dict[str, Any]]:
        with self.lock, self._connect() as con:
            rows = con.execute("SELECT * FROM approvals WHERE session_id=? AND consumed=0 ORDER BY created",
                               (sid,)).fetchall()
            return [dict(r) for r in rows]

    def begin_step(self, sid: str, conv_index: int, step_no: int,
                   tool: str, args_hash: str) -> None:
        """**工具派发之前**登记（DSH 的 checkpoint-before-dispatch）。

        与 turn_results 一样放在独立表：steps 是 append-only 且 step_no 会复用，
        往里插一条 inflight 会让时间线出现重复步骤。若进程在工具执行后、结果落库前
        崩溃，这里会留下一条 status='inflight' 的记录——那正是"副作用结果未知"的
        唯一可靠线索，恢复时应据此只允许只读/幂等重试。
        """
        with self.lock, self._connect() as con:
            con.execute(
                "INSERT INTO step_dispatch(session_id,conversation_index,step_no,tool,args_hash,status,created) "
                "VALUES(?,?,?,?,?,'inflight',?) ON CONFLICT(session_id,conversation_index,step_no) "
                "DO UPDATE SET tool=excluded.tool, args_hash=excluded.args_hash, "
                "status='inflight', created=excluded.created",
                (sid, int(conv_index), int(step_no), tool, args_hash, time.time()))
            self.append_event(sid, "step_begin", {"step_no": int(step_no), "tool": tool,
                                                  "args_hash": args_hash,
                                                  "conversation_index": int(conv_index)}, con=con)

    def finish_step(self, sid: str, conv_index: int, step_no: int, status: str = "done") -> None:
        with self.lock, self._connect() as con:
            con.execute("UPDATE step_dispatch SET status=? WHERE session_id=? AND conversation_index=? AND step_no=?",
                        (status, sid, int(conv_index), int(step_no)))
            self.append_event(sid, "step_done", {"step_no": int(step_no), "status": status,
                                                 "conversation_index": int(conv_index)}, con=con)

    def pending_steps(self, sid: str) -> list[dict[str, Any]]:
        """未收尾（结果未知）的工具步骤：恢复时用它决定"能不能重放"。"""
        with self.lock, self._connect() as con:
            rows = con.execute(
                "SELECT * FROM step_dispatch WHERE session_id=? AND status='inflight' ORDER BY step_no",
                (sid,)).fetchall()
            return [dict(r) for r in rows]

    def save_turn(self, sid: str, conv_index: int, prompt_hash: str,
                  answer: str, steps_after: int) -> None:
        """回合成功产出结果：标为 done，恢复时可据此跳过重发。"""
        with self.lock, self._connect() as con:
            con.execute(
                "INSERT INTO turn_results(session_id,conversation_index,prompt_hash,answer,steps_after,status,created) "
                "VALUES(?,?,?,?,?,'done',?) ON CONFLICT(session_id,conversation_index,prompt_hash) "
                "DO UPDATE SET answer=excluded.answer, steps_after=excluded.steps_after, "
                "status='done', created=excluded.created",
                (sid, int(conv_index), prompt_hash, (answer or "")[:4000], int(steps_after), time.time()))
            self.append_event(sid, "turn_done", {"prompt_hash": prompt_hash, "steps_after": int(steps_after),
                                                 "answer_chars": len(answer or "")}, con=con)

    def get_turn(self, sid: str, conv_index: int, prompt_hash: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM turn_results WHERE session_id=? AND conversation_index=? AND prompt_hash=?",
                (sid, int(conv_index), prompt_hash)).fetchone()
            return dict(row) if row else None

    def delete(self, sid: str):
        self.append_audit("session_deleted", sid, {"reason": "explicit"})
        """删除会话及其全部从属数据。

        只删 sessions 行会让 messages/steps/file_events/conv_stats 永久残留
        （会话已不可达，等于不可回收的垃圾数据）。
        """
        with self.lock, self._connect() as con:
            # 级联：父会话删掉后，子代理会话没有归属（界面上只能浮在顶层），一并删除。
            # 只删一层：子代理不允许再开子代理（深度上限 1）。
            kids = [r[0] for r in con.execute(
                "SELECT id FROM sessions WHERE parent_id=?", (sid,)).fetchall()]
            for kid in kids:
                con.execute("DELETE FROM sessions WHERE id=?", (kid,))
                for table in ("messages", "steps", "file_events", "conv_stats",
                              "turn_results", "step_dispatch", "approvals", "events"):
                    try:
                        con.execute(f"DELETE FROM {table} WHERE session_id=?", (kid,))
                    except Exception:
                        pass
            existed = con.execute("DELETE FROM sessions WHERE id=?", (sid,)).rowcount > 0
            for table in ("messages", "steps", "file_events", "conv_stats", "turn_results", "step_dispatch", "approvals", "events"):
                try:
                    con.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
                except Exception:
                    pass  # 老库可能缺表：删主记录成功即视为成功
            return existed

    def cancel_running_children(self, parent_sid: str, note: str = "父任务已结束") -> int:
        """父会话结束时，把仍在 running/waiting 的子会话标记为 cancelled。

        实测：主任务早已 `completed`，子代理却还在"等 Copilot 回答"（步骤还在涨），
        面板上显示 "1 running"。子会话与父会话同属一个任务，父任务收尾后没人会去
        收它们的结果，留着既占浏览器又会继续消耗配额。
        """
        if not parent_sid:
            return 0
        with self.lock, self._connect() as con:
            cur = con.execute(
                "UPDATE sessions SET state='cancelled', cancel_requested=1, "
                "last_error=?, updated=? WHERE parent_id=? AND state IN ('running','waiting')",
                (note, time.time(), parent_sid))
            return cur.rowcount

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
