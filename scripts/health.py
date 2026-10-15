# -*- coding: utf-8 -*-
"""会话健康巡检：事件日志 vs 数据库列的一致性（P2-10 第二期的运行时验收）。

**为什么需要**：投影与列"在测试里一致"不等于"在真实流量里一直一致"。第三期要把读取
路径切到投影之前，必须先用真实会话证明**从不出现不一致**。本脚本就是这件事的度量工具：
对所有（或指定）会话跑投影并与列交叉核对，输出统计与不一致明细。

用法:
    python scripts/health.py                    # 巡检全部会话（默认最近 200 个）
    python scripts/health.py --limit 50         # 只看最近 50 个
    python scripts/health.py --session task-x   # 只查一个会话
    python scripts/health.py --json             # 机器可读，便于接 CI/巡检
退出码：0=全部一致；1=发现不一致或读取失败（可接 CI）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from cbp.projection import compare_with_columns, project  # noqa: E402


def load_sessions(con: sqlite3.Connection, limit: int, only: str | None) -> list[str]:
    if only:
        row = con.execute("SELECT id FROM sessions WHERE id=?", (only,)).fetchone()
        return [row[0]] if row else []
    rows = con.execute("SELECT id FROM sessions ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
    return [r[0] for r in rows]


def check_session(con: sqlite3.Connection, sid: str) -> dict:
    events = []
    for r in con.execute("SELECT * FROM events WHERE session_id=? ORDER BY id LIMIT 20000", (sid,)):
        item = dict(r)
        try:
            item["payload"] = json.loads(item.get("payload") or "{}")
        except Exception:
            item["payload"] = {}
        events.append(item)
    turns = [dict(r) for r in con.execute(
        "SELECT prompt_hash,status,steps_after FROM turn_results WHERE session_id=?", (sid,))]
    steps = [dict(r) for r in con.execute(
        "SELECT step_no,tool,status FROM step_dispatch WHERE session_id=?", (sid,))]
    proj = project(events)
    problems = compare_with_columns(proj, turns, steps)
    # 归类：**早于事件功能上线**的老会话（有检查点列但 0 事件）不算不一致——
    # 否则 CI 会永远红，指标也就失去意义。只有"有事件却对不上"才是真缺口。
    legacy = not events and (turns or steps)
    if legacy:
        problems = []
    return {"session_id": sid, "events": len(events), "turns": len(turns), "steps": len(steps),
            "current_state": proj.get("current_state"),
            "unknown_kinds": proj.get("unknown_kinds") or {},
            "legacy": legacy, "problems": problems}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(BASE / "data" / "agent.db"))
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--session", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.is_file():
        print(f"数据库不存在: {db}")
        return 1
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    sids = load_sessions(con, args.limit, args.session or None)
    reports = [check_session(con, sid) for sid in sids]
    con.close()

    bad = [r for r in reports if r["problems"]]
    legacy = [r for r in reports if r["legacy"]]
    unknown = {}
    for r in reports:
        for k, n in (r["unknown_kinds"] or {}).items():
            unknown[k] = unknown.get(k, 0) + n

    if args.json:
        print(json.dumps({"checked": len(reports), "mismatched": len(bad),
                          "legacy_without_events": len(legacy),
                          "unknown_kinds": unknown, "reports": reports},
                         ensure_ascii=False, indent=1))
    else:
        print(f"巡检会话 {len(reports)} 个：一致 {len(reports) - len(bad) - len(legacy)}，"
              f"不一致 {len(bad)}，早于事件功能（0 事件，跳过）{len(legacy)}")
        total_events = sum(r["events"] for r in reports)
        print(f"事件总数 {total_events}；有事件的会话 "
              f"{sum(1 for r in reports if r['events'])} 个")
        if unknown:
            print(f"⚠ 出现未识别事件类型（投影会忽略，但应确认是否漏实现）：{unknown}")
        for r in bad[:10]:
            print(f"  ✗ {r['session_id']}（事件 {r['events']}）: " + "; ".join(r["problems"][:3]))
        if bad:
            print("提示：不一致说明事件覆盖有缺口（或历史会话早于事件功能上线）——"
                  "前者要补事件发点，后者可忽略。")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
