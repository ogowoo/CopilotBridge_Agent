# -*- coding: utf-8 -*-
"""清理"泄漏的 Playwright 浏览器"。

实测：连续跑几天 UI 审计/截图脚本后，累积了 **21 个** Playwright 拉起的 msedge
（`--user-data-dir=%TEMP%\\playwright_*`），既吃内存也让人以为"有什么程序在捣乱"。
泄漏来自脚本在异常/超时路径上没走到 `browser.close()`。

这里按"年龄"回收：只杀超过 N 分钟仍存活的 Playwright 浏览器，正在跑的脚本不受影响。
用法：
    python scripts/reap_browsers.py            # 只报告
    python scripts/reap_browsers.py --kill     # 真杀
    python scripts/reap_browsers.py --kill --minutes 5
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

PLAYWRIGHT_PROFILE = ("playwright",)


def _ps(script: str) -> str:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                         capture_output=True, text=True)
    return (out.stdout or "").strip()


def list_browsers() -> list[dict]:
    """返回仍在运行的 Playwright 浏览器（PID + 年龄分钟 + 命令行片段）。"""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" "
        "| Where-Object { $_.CommandLine -match 'playwright_' } "
        "| ForEach-Object { $age = ((Get-Date) - $_.CreationDate).TotalMinutes; "
        "\"$($_.ProcessId)`t$([math]::Round($age,1))`t$($_.CommandLine.Substring(0,[Math]::Min(120,$_.CommandLine.Length)))\" }"
    )
    rows = []
    for line in _ps(script).splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].isdigit():
            rows.append({"pid": int(parts[0]), "age_min": float(parts[1]),
                         "cmd": parts[2]})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kill", action="store_true")
    ap.add_argument("--minutes", type=float, default=15.0,
                    help="只回收存活超过这个分钟数的浏览器（默认 15）")
    args = ap.parse_args()

    rows = list_browsers()
    stale = [r for r in rows if r["age_min"] >= args.minutes]
    print(f"Playwright 浏览器进程: {len(rows)} 个，其中超过 {args.minutes:g} 分钟的: {len(stale)} 个")
    for r in sorted(stale, key=lambda x: -x["age_min"])[:10]:
        print(f"  PID {r['pid']:<7} {r['age_min']:>6.1f} 分钟  {r['cmd'][:80]}")
    if not stale:
        return 0
    if not args.kill:
        print("\n（只报告。加 --kill 才会回收）")
        return 0
    killed = 0
    for r in stale:
        try:
            os.kill(r["pid"], 9)
            killed += 1
        except OSError:
            pass
    time.sleep(1)
    print(f"\n已回收 {killed} 个；剩余 Playwright 浏览器: {len(list_browsers())} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
