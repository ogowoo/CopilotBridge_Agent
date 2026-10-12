# -*- coding: utf-8 -*-
"""conv_stats 阈值校准分析：用实测数据反推 compact/rollover 配置。

用法:
    python scripts/calibrate.py [data/agent.db]   # 从项目根目录运行

原理:
    M365 Copilot 的对话长度上限不公开且动态变化。conv_stats 表在每个
    Copilot 会话结束时记录实测数据（估算 token / 字符 / 步数 / 结束原因）。
    - ended_by 含「长度上限」的样本 = 真实触顶，其 tokens 是上限的下界参考
      （触顶时累计值 >= 实际能承载的量，但已非常接近）
    - ended_by 为「Token/步数/手动触发自动创建新 Copilot 会话」的样本 =
      本地阈值主动切换，其 tokens 分布反映当前配置下的实际使用水位
    建议 rollover_at_tokens = min(触顶样本 tokens) * 0.85，留安全余量；
    compact_at_tokens 保持约为 rollover 的 0.6~0.65（与默认比例一致）。
    没有触顶样本时，用主动切换样本的 P25 作为保守参考。
"""
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/agent.db")
if not DB.exists():
    sys.exit(f"找不到数据库: {DB}（用法: python calibrate.py <agent.db 路径>）")

CEILING_MARK = "长度上限"
AUTO_MARK = "Token/步数/手动"


def pct(values, p):
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


def main():
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM conv_stats ORDER BY id").fetchall()
    if not rows:
        sys.exit("conv_stats 还没有样本：先跑一些 Agent 任务积累数据。")

    groups = defaultdict(lambda: {"ceiling": [], "auto": [], "completed": [], "other": []})
    for r in rows:
        model = r["model"] or "(未知模型)"
        t = r["tokens"] or 0
        by = r["ended_by"] or ""
        if CEILING_MARK in by:
            groups[model]["ceiling"].append(t)
        elif by.startswith(AUTO_MARK):
            groups[model]["auto"].append(t)
        elif by == "completed":
            groups[model]["completed"].append(t)
        else:
            groups[model]["other"].append(t)

    print(f"共 {len(rows)} 个会话样本\n")
    for model, g in sorted(groups.items()):
        n = sum(len(v) for v in g.values())
        print(f"== {model}（{n} 个会话）==")
        for kind, label in [("ceiling", "触顶结束(真实上限参考)"),
                            ("auto", "本地阈值主动切换"),
                            ("completed", "正常完成"),
                            ("other", "其他/失败")]:
            v = g[kind]
            if v:
                print(f"  {label:22s} n={len(v):3d}  min={min(v):7,}  P25={pct(v,.25):7,}  "
                      f"中位={pct(v,.5):7,}  max={max(v):7,}")
            else:
                print(f"  {label:22s} n=  0")
        # 建议
        ceil = g["ceiling"]
        auto = g["auto"]
        if ceil:
            suggest = int(min(ceil) * 0.85)
            basis = f"触顶样本最小值 {min(ceil):,} × 0.85"
        elif auto:
            suggest = int(pct(auto, 0.25))
            basis = f"主动切换样本 P25（暂无触顶样本，偏保守）"
        else:
            suggest = None
            basis = "样本不足"
        if suggest:
            compact = int(suggest * 0.64)
            print(f"  建议 rollover_at_tokens = {suggest:7,}（依据: {basis}）")
            print(f"  建议 compact_at_tokens  = {compact:7,}（rollover × 0.64）")
        else:
            print(f"  暂无法给出建议: {basis}")
        print()

    print("当前 config.json 值可在 agent 段核对；修改后重启服务生效。")
    print("注意: tokens 为本地启发式估算（与 config 阈值同口径），不同模型请分别看。")


if __name__ == "__main__":
    main()
