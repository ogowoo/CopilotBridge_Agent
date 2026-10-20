# -*- coding: utf-8 -*-
"""审计第一步：摸清 agent.db 结构，取出 pyshot 任务（task-mvfc11u8）的全部执行数据。"""
import json
import sqlite3
from pathlib import Path

DB = Path("data/agent.db")
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=10)
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=== 表 ===")
for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    name = r["name"]
    try:
        n = cur.execute(f"SELECT COUNT(*) c FROM {name}").fetchone()["c"]
    except Exception as exc:
        n = f"? {exc}"
    print(f"  {name:<22} {n}")

print("\n=== sessions 列 ===")
cols = [r["name"] for r in cur.execute("PRAGMA table_info(sessions)")]
print(cols)

print("\n=== 本任务及子代理 ===")
for r in cur.execute("SELECT id, title, state, total_steps, estimated_tokens, conversation_index,"
                     " parent_id, created, updated FROM sessions WHERE id LIKE 'task-mvfc%'"
                     " OR parent_id='task-mvfc11u8' ORDER BY created"):
    print(f"  {r['id']:<18} {r['state']:<10} steps={r['total_steps']:<4} "
          f"tok={r['estimated_tokens']:<7} conv={r['conversation_index']} "
          f"parent={r['parent_id'] or '-':<16} {(r['title'] or '')[:46]}")
con.close()
