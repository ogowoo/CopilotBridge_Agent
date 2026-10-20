# -*- coding: utf-8 -*-
"""审计第三步：把浪费最多的三类失败摘出原文——
① list_directory 的 repeat_guard（92 次）到底重复在哪；
② service_unavailable（105 次）的真实原因；
③ invalid_action（37 次）模型当时输出了什么。
"""
import collections
import json
import re
import sqlite3
from pathlib import Path

SID = "task-mvfc11u8"
con = sqlite3.connect(f"file:{(Path('data/agent.db')).as_posix()}?mode=ro", uri=True, timeout=10)
con.row_factory = sqlite3.Row
cur = con.cursor()

# ① step_begin 里的参数（只看 list_directory）+ step_recorded 的状态
begins = {}
for r in cur.execute("SELECT id, kind, payload FROM events WHERE session_id=? AND kind='step_begin'"
                     " ORDER BY id", (SID,)):
    try:
        p = json.loads(r["payload"] or "{}")
    except Exception:
        continue
    begins[p.get("step_no")] = p

recs = []
for r in cur.execute("SELECT kind, payload FROM events WHERE session_id=? AND kind='step_recorded'"
                     " ORDER BY id", (SID,)):
    try:
        p = json.loads(r["payload"] or "{}")
    except Exception:
        continue
    recs.append(p)

print("=== list_directory 的 args_hash 分布（同 hash 重复=真重复）===")
ld = [b for b in begins.values() if b.get("tool") == "list_directory"]
print(f"  list_directory 步数: {len(ld)}")
h = collections.Counter(b.get("args_hash") for b in ld)
print(f"  不同 args_hash: {len(h)}  最高频: {h.most_common(5)}")

print("\n=== ② service_unavailable / invalid_action 的 reason（来自 recent_steps）===")
row = cur.execute("SELECT recent_steps FROM sessions WHERE id=?", (SID,)).fetchone()
steps = json.loads(row["recent_steps"] or "[]")
print(f"  recent_steps 只保留 {len(steps)} 条（滚动窗口）")
idx = collections.Counter()
for s in steps:
    st = str(s.get("status"))
    idx[st] += 1
print("  窗口内状态:", dict(idx))

def show(status, n=4, key_extra=None):
    print(f"\n--- {status} 样本 ---")
    k = 0
    for s in steps:
        if str(s.get("status")) != status:
            continue
        print(f"  step {s.get('step')} tool={s.get('tool')} reason={str(s.get('reason'))[:170]}")
        if s.get("result") is not None:
            print(f"      result={json.dumps(s.get('result'), ensure_ascii=False)[:200]}")
        k += 1
        if k >= n:
            break

for st in ("repeat_guard", "service_unavailable", "invalid_action", "no_progress_warning"):
    show(st, 3)

con.close()
