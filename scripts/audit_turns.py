# -*- coding: utf-8 -*-
"""审计用：回合配对 / 延迟分布 / 会话切换点（报告第 8 节的数据来源）。

用法：python scripts/audit_turns.py [session_id]
"""
import json
import sqlite3
import sys
from pathlib import Path

SID = sys.argv[1] if len(sys.argv) > 1 else "task-mvfc11u8"
DB = Path("data/agent.db")
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=10)
con.row_factory = sqlite3.Row
cur = con.cursor()

rows = cur.execute("SELECT kind, payload, created FROM events WHERE session_id=? ORDER BY id",
                   (SID,)).fetchall()
begins = [(r["created"], json.loads(r["payload"] or "{}")) for r in rows if r["kind"] == "turn_begin"]
dones = [(r["created"], json.loads(r["payload"] or "{}")) for r in rows if r["kind"] == "turn_done"]
print(f"turn_begin={len(begins)}  turn_done={len(dones)}  差={len(begins) - len(dones)}")

pairs, i, unpaired = [], 0, 0
for t, p in begins:
    j = i
    while j < len(dones) and dones[j][0] < t:
        j += 1
    if j < len(dones) and dones[j][0] - t < 600:
        pairs.append((t, dones[j][0] - t))
        i = j + 1
    else:
        unpaired += 1
print(f"可配对 {len(pairs)}  未配对 {unpaired}")
if pairs:
    lat = sorted(x[1] for x in pairs)
    print(f"回合耗时（秒）: 中位 {lat[len(lat)//2]:.0f}  平均 {sum(lat)/len(lat):.0f}  "
          f"P90 {lat[int(len(lat)*0.9)]:.0f}  最长 {lat[-1]:.0f}")

print("\n会话切换点：")
last, steps = 0, 0
for r in rows:
    p = json.loads(r["payload"] or "{}")
    if r["kind"] == "step_recorded":
        steps += 1
        continue
    if r["kind"] != "turn_begin":
        continue
    ci = p.get("conversation_index")
    if ci is not None and ci != last:
        print(f"  conv {last} → {ci}（切换前已记录 {steps} 条步骤）")
        last = ci
con.close()
