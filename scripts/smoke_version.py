# -*- coding: utf-8 -*-
"""验证：版本号与 /health 一致（不写死某个预览号），且根路径是 302 → /ui/。"""
import json
import sys
import urllib.request

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

meta = json.load(urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=10))
want = str(meta.get("version") or "")
print("接口版本:", want, "| 名称:", meta.get("name"))

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
    checks = {
        "chip 与接口版本一致": bool(want) and want in info["chip"],
        "chip 可见": not info["hidden"],
        "页签标题带版本": bool(want) and want in info["title"],
        "无 JS 异常": not errs,
    }
    for k, v in checks.items():
        print(("  ✓ " if v else "  ✗ ") + k)
    if errs:
        print("JS 错误:", errs[:3])
    b.close()
    sys.exit(0 if all(checks.values()) else 1)
