# -*- coding: utf-8 -*-
"""span 分析：把 data/spans.jsonl 聚合成可读报表（零依赖）。

回答三类问题：
  1) 时间花在哪——远端回合（invoke_agent）vs 本地工具（execute_tool）；
  2) 哪个模型/工具慢或爱出错——P50/P95 与错误率；
  3) 每个会话的开销——回合数、工具数、总时长。

用法:
    python scripts/spans.py                    # 分析全部（默认最近 20000 条）
    python scripts/spans.py --limit 500        # 只看最近 500 条
    python scripts/spans.py --session eval-x   # 只看某个会话
    python scripts/spans.py --json             # 另存 data/evals/spans-report-<时间戳>.json
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DEFAULT = BASE / "data" / "spans.jsonl"


def pct(values: list[float], p: float) -> float:
    """百分位（线性插值）；空列表返回 0。"""
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return float(xs[lo] + (xs[hi] - xs[lo]) * (k - lo))


def agg(node: dict, key: str, ms: float) -> None:
    b = node.setdefault(key, {"count": 0, "errors": 0, "ms": []})
    b["count"] += 1
    b["ms"].append(ms)


def summarize(buckets: dict, extra=None) -> dict:
    out = {}
    for key, b in buckets.items():
        ms = b["ms"]
        row = {"count": b["count"], "errors": b["errors"],
               "p50_ms": round(pct(ms, 0.5), 1), "p95_ms": round(pct(ms, 0.95), 1),
               "avg_ms": round(sum(ms) / len(ms), 1) if ms else 0.0,
               "total_s": round(sum(ms) / 1000, 2)}
        if extra:
            row.update(extra.get(key, {}))
        out[key] = row
    return out


def analyse(recs: list[dict]) -> dict:
    by_model: dict = {}
    by_tool: dict = {}
    by_session: dict = {}
    by_op: dict = {}
    total_remote = total_local = 0.0
    errors = 0

    for r in recs:
        attrs = r.get("attributes") or {}
        op = attrs.get("gen_ai.operation.name") or "?"
        ms = float(r.get("duration_ms") or 0)
        err = 1 if r.get("status") == "error" else 0
        errors += err
        agg(by_op, op, ms)
        if op == "invoke_agent":
            total_remote += ms
            agg(by_model, attrs.get("gen_ai.request.model") or "?", ms)
        elif op == "execute_tool":
            total_local += ms
            b = by_tool.setdefault(attrs.get("gen_ai.tool.name") or "?", {"count": 0, "errors": 0, "ms": [], "chars": []})
            b["count"] += 1
            b["ms"].append(ms)
            b["chars"].append(float(attrs.get("gantry.result_chars") or 0))
        sid = attrs.get("gantry.session_id") or "(unknown)"
        s = by_session.setdefault(sid, {"count": 0, "errors": 0, "ms": [], "turns": 0, "tools": 0})
        s["count"] += 1
        s["errors"] += err
        s["ms"].append(ms)
        if op == "invoke_agent":
            s["turns"] += 1
        elif op == "execute_tool":
            s["tools"] += 1
        # 错误计数写回 op/model/tool 桶
        if err:
            for node, key in ((by_op, op),
                              (by_model, attrs.get("gen_ai.request.model") or "?") if op == "invoke_agent" else (None, None),
                              (by_tool, attrs.get("gen_ai.tool.name") or "?") if op == "execute_tool" else (None, None)):
                if node is not None and key in node:
                    node[key]["errors"] += 1

    tool_extra = {k: {"avg_result_chars": round(sum(v["chars"]) / len(v["chars"]), 1) if v["chars"] else 0}
                  for k, v in by_tool.items()}
    for k, v in by_tool.items():
        v.pop("chars", None)

    return {
        "spans": len(recs),
        "errors": errors,
        "error_rate": round(errors / len(recs), 4) if recs else 0.0,
        "time_split": {"remote_s": round(total_remote / 1000, 2),
                       "local_s": round(total_local / 1000, 2),
                       "remote_share": round(total_remote / (total_remote + total_local), 3)
                       if (total_remote + total_local) else 0.0},
        "by_operation": summarize(by_op),
        "by_model": summarize(by_model),
        "by_tool": summarize(by_tool, tool_extra),
        "by_session": summarize(by_session,
                                {s: {"turns": v["turns"], "tools": v["tools"]} for s, v in by_session.items()}),
    }


def print_report(rep: dict, top: int = 10) -> None:
    print(f"span 总数 {rep['spans']}  错误 {rep['errors']}（{rep['error_rate']*100:.1f}%）")
    ts = rep["time_split"]
    print(f"时间分布: 远端回合 {ts['remote_s']}s / 本地工具 {ts['local_s']}s"
          f"（远端占 {ts['remote_share']*100:.0f}%）\n")

    def table(title: str, data: dict, sort_key: str = "total_s") -> None:
        rows = sorted(data.items(), key=lambda kv: kv[1].get(sort_key, 0), reverse=True)[:top]
        print(title)
        print(f"  {'名称':<34}{'次数':>6}{'错误':>6}{'P50ms':>9}{'P95ms':>9}{'合计s':>9}")
        for name, v in rows:
            print(f"  {str(name)[:33]:<34}{v['count']:>6}{v['errors']:>6}"
                  f"{v['p50_ms']:>9}{v['p95_ms']:>9}{v['total_s']:>9}")
        print()

    table("按模型（远端回合）", rep["by_model"])
    table("按工具（本地执行）", rep["by_tool"])
    table("按会话（总耗时）", rep["by_session"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=str(DEFAULT))
    ap.add_argument("--limit", type=int, default=20000)
    ap.add_argument("--session", default="")
    ap.add_argument("--json", action="store_true", help="另存 JSON 报表到 data/evals/")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    p = Path(args.path)
    if not p.is_file():
        print(f"没有 span 文件: {p}（跑几个任务后即可生成）")
        return 1
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, args.limit):]
    recs = []
    for line in lines:
        try:
            r = json.loads(line)
        except Exception:
            continue
        if args.session and (r.get("attributes") or {}).get("gantry.session_id") != args.session:
            continue
        recs.append(r)
    if not recs:
        print("没有匹配的 span")
        return 1
    rep = analyse(recs)
    print_report(rep, top=args.top)
    if args.json:
        out_dir = BASE / "data" / "evals"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"spans-report-{time.strftime('%Y%m%d-%H%M%S')}.json"
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        print("报表:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
