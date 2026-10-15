# -*- coding: utf-8 -*-
"""跑一遍运行时不变量检查（提交前/CI 可用，退出码非 0 表示有契约被破坏）。

    python scripts/invariants.py            # 人读输出
    python scripts/invariants.py --json     # 机器可读
"""
import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from cbp.invariants import check_all, registered  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    failures = check_all()
    names = registered()
    if args.json:
        print(json.dumps({"total": len(names), "failed": failures}, ensure_ascii=False, indent=1))
    else:
        print(f"不变量检查：{len(names)} 条，失败 {len(failures)} 条")
        for name in names:
            mark = "✗" if name in failures else "✓"
            print(f"  {mark} {name}")
            if name in failures:
                print(f"      {failures[name]}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
