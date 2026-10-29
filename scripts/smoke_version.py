# -*- coding: utf-8 -*-
"""验证：版本号回到侧栏 + 页签标题（`/` 已 302 到 /ui/ 的那次拉取之后本来丢了）。"""
import json
import sys

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    pg = b.new_context(viewport={"width": 1200, "height": 800}).new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)[:150]))
    pg.goto("http://127.0.0.1:8765/ui", wait_until="domcontentloaded")
    pg.wait_for_timeout(2800)
    info = pg.evaluate("""() => {
      const el = document.getElementById('appVersion') || {};
      return {title: document.title, chip: el.textContent || '', hidden: !!el.hidden};
    }""")
    print(json.dumps(info, ensure_ascii=False))
    ok = ("preview.31" in info["chip"]) and (not info["hidden"]) and ("preview.31" in info["title"])
    print("结论:", "版本号正常" if ok else "⚠ 仍不正常")
    print("JS 错误:", errs[:3] or "无")
    b.close()
    sys.exit(0 if ok else 1)
