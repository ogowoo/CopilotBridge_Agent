# -*- coding: utf-8 -*-
"""WebUI 冒烟测试：用真实浏览器加载 /ui，捕获控制台错误并验证关键行为。

**为什么需要**：`webui/` 是 87KB 单文件、无构建步骤、无前端测试。以前改 UI 只能靠肉眼，
改动与缺陷都无法回归。本脚本用 Playwright 真开一次页面，把"能加载、无报错、关键交互不抛异常"
变成可执行的检查（CI 可跑）。

用法:
    python scripts/ui_smoke.py                # 默认 127.0.0.1:8765，跑完退出码 0/1
    python scripts/ui_smoke.py --keep-open    # 出错时保留浏览器窗口便于排查
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def run(url: str, keep_open: bool) -> int:
    from playwright.sync_api import sync_playwright

    problems: list[str] = []
    console_errors: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as p:
        browser = None
        for kwargs in ({"channel": "msedge"}, {}, {"channel": "chrome"}):
            try:
                browser = p.chromium.launch(headless=True, **kwargs)
                break
            except Exception as exc:  # noqa: PERF203
                last = exc
        if browser is None:
            print(f"无法启动浏览器：{last}")
            return 2

        page = browser.new_page()
        page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}")
                if m.type in ("error",) else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        # 带 token 加载：验证 SSE 会把 token 附到 query（P1 缺陷的直接回归）
        page.add_init_script("localStorage.setItem('cbp-token','SMOKE-TOKEN-123');")
        bad_responses: list[str] = []
        page.on("response", lambda r: bad_responses.append(f"{r.status} {r.url}")
                if r.status >= 400 else None)
        sse_urls: list[str] = []
        page.on("request", lambda r: sse_urls.append(r.url) if "/events" in r.url else None)

        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)          # 让首屏轮询/SSE 起一轮

        # 1) 无 JS 异常
        if page_errors:
            problems.append("页面抛异常: " + "; ".join(page_errors[:3]))
        if console_errors:
            problems.append("控制台 error: " + "; ".join(console_errors[:3]))
        if bad_responses:
            problems.append("4xx/5xx 资源: " + "; ".join(sorted(set(bad_responses))[:4]))

        # 2) 关键全局存在（lastMessages 曾未声明 → 搜索必抛 ReferenceError）
        kind = page.evaluate("typeof lastMessages")
        if kind == "undefined":
            problems.append("lastMessages 未声明（对话搜索会抛 ReferenceError）")

        # 3) 未选会话时点搜索不应抛异常
        try:
            page.evaluate("toggleChatFind()")
        except Exception as exc:
            problems.append(f"未选会话时 toggleChatFind() 抛异常: {exc}")

        # 4) SSE 必须带 token（配置 api_token 时否则恒 401）
        if sse_urls and not any("token=" in u for u in sse_urls):
            problems.append(f"SSE 未附加 token: {sse_urls[0][:120]}")

        # 5) 关键元素存在
        for sel in ("#sessions", "#sid", "#moreBtn", "#workspace", "#task", "#tabChat", "#chatlog"):
            if not page.query_selector(sel):
                problems.append(f"缺少关键元素 {sel}")

        if problems:
            print("UI 冒烟失败：")
            for x in problems:
                print("  ✗", x)
            if keep_open:
                page.wait_for_timeout(120000)
        else:
            print("UI 冒烟通过：无 JS 异常、关键全局与元素齐全、SSE 带 token")
        browser.close()
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/ui")
    ap.add_argument("--keep-open", action="store_true")
    args = ap.parse_args()
    return run(args.url, args.keep_open)


if __name__ == "__main__":
    raise SystemExit(main())
