# -*- coding: utf-8 -*-
"""审计第二步：从事件日志重建 pyshot 任务的执行轨迹，统计工具/状态/失败模式。

输出：
- 每个工具的使用次数、成功/失败/被拦
- invalid_action（模型输出不合协议）的原文样本
- 工具报错文本聚类（同一类错误出现几次）
- 会话切换（conversation_index）节奏、runturn 次数
- 重复调用（同工具同参数连续出现）
"""
import collections
import json
import sqlite3
import time
from pathlib import Path

SID = "task-mvfc11u8"
DB = Path("data/agent.db")
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=10)
con.row_factory = sqlite3.Row
cur = con.cursor()

rows = cur.execute("SELECT id, kind, payload, created FROM events WHERE session_id=? ORDER BY id",
                   (SID,)).fetchall()
print(f"事件总数: {len(rows)}")

kinds = collections.Counter(r["kind"] for r in rows)
print("\n=== 事件类型分布 ===")
for k, v in kinds.most_common():
    print(f"  {k:<18} {v}")

steps = []
for r in rows:
    if r["kind"] != "step_recorded":
        continue
    try:
        p = json.loads(r["payload"] or "{}")
    except Exception:
        p = {}
    steps.append({"no": p.get("step_no"), "tool": p.get("tool"), "status": p.get("status"),
                  "t": r["created"]})
print(f"\n记录到的步骤: {len(steps)}")

print("\n=== 每个工具的调用与状态 ===")
per = collections.defaultdict(collections.Counter)
for s in steps:
    per[str(s["tool"])][str(s["status"])] += 1
for tool, cnt in sorted(per.items(), key=lambda kv: -sum(kv[1].values())):
    total = sum(cnt.values())
    detail = ", ".join(f"{k}={v}" for k, v in cnt.most_common())
    print(f"  {tool:<14} {total:>4} 次   {detail}")

print("\n=== 状态总览 ===")
print(" ", dict(collections.Counter(str(s["status"]) for s in steps)))

turns = [r for r in rows if r["kind"] == "turn_begin"]
print(f"\n=== 回合 ===")
print(f"  turn_begin: {len(turns)}")
if len(rows) > 1:
    span = rows[-1]["created"] - rows[0]["created"]
    print(f"  总耗时: {span/60:.1f} 分钟（{span/3600:.2f} 小时）")
    print(f"  平均每步: {span/max(1,len(steps)):.1f} 秒")

# 会话切换节奏
switch = [(s["no"], s["t"]) for s in steps]
print("\n=== conversation_index 变化点 ===")
for r in rows:
    if r["kind"] in ("turn_begin",):
        try:
            p = json.loads(r["payload"] or "{}")
        except Exception:
            continue
        if p.get("conversation_index") not in (None, 0):
            pass
conv = cur.execute("SELECT conversation_index, conversation_urls, conv_tokens, conv_chars,"
                   " total_steps FROM sessions WHERE id=?", (SID,)).fetchone()
print("  conversation_index:", conv["conversation_index"],
      "| conv_tokens:", conv["conv_tokens"], "| conv_chars:", conv["conv_chars"])
con.close()
