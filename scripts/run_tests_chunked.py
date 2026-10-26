# -*- coding: utf-8 -*-
"""分模块跑测试（每个模块独立进程 + 超时），用于排查"整体跑会卡住"的次序问题。

用法：
    python scripts/run_tests_chunked.py [--timeout 180] [--only 关键字]

输出：每个模块的 OK / FAILED / TIMEOUT，最后汇总。退出码非 0 表示有失败或超时。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
TESTS = BASE / "tests"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=180, help="单个模块超时（秒）")
    ap.add_argument("--only", default="", help="只跑名字含该关键字的模块")
    args = ap.parse_args()

    mods = sorted(p.stem for p in TESTS.glob("test_*.py"))
    if args.only:
        mods = [m for m in mods if args.only in m]
    bad = []
    t0 = time.time()
    for m in mods:
        cmd = [sys.executable, "-m", "unittest", f"tests.{m}"]
        started = time.time()
        try:
            r = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                               timeout=args.timeout)
            out = (r.stderr or "") + (r.stdout or "")
            tail = [ln for ln in out.splitlines() if ln.startswith(("Ran ", "OK", "FAILED"))]
            status = "OK" if r.returncode == 0 else "FAILED"
            if status != "OK":
                bad.append(m)
            print(f"{status:7} {m:46} {time.time() - started:5.1f}s  {' | '.join(tail[-2:])}")
            if status != "OK":
                for ln in out.splitlines():
                    if ln.startswith(("FAIL:", "ERROR:")):
                        print("        ", ln[:120])
        except subprocess.TimeoutExpired:
            bad.append(m)
            print(f"TIMEOUT {m:46} {time.time() - started:5.1f}s  （>{args.timeout}s）")
    print(f"\n共 {len(mods)} 个模块，用时 {time.time() - t0:.1f}s；异常模块：{bad or '无'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
