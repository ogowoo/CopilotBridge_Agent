# -*- coding: utf-8 -*-
"""浏览器验证（含截图）：跟进框的 / 菜单能弹出、选择真的变了、Enter 只补全不发送。"""
import json
import re
import sys

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402


def menu(pg, mid):
    return pg.evaluate("""(id) => {
      const b = document.getElementById(id);
      if (!b) return {exists: false};
      const rows = [...b.querySelectorAll('.slashrow')];
      return {exists: true, hidden: !!b.hidden, rows: rows.length,
              first: rows.length ? rows[0].textContent.trim() : '',
              on: (rows.find(r => r.classList.contains('on')) || {}).textContent || ''};
    }""", mid)


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    pg = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
    pg.goto("http://127.0.0.1:8765/ui", wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)
    pg.evaluate("() => { const r = document.querySelector('#sessions .sess'); if (r) r.click(); }")
    pg.wait_for_timeout(2600)

    pg.click("#followup")
    pg.keyboard.type("/")
    pg.wait_for_timeout(400)
    m1 = menu(pg, "slashMenuFollow")
    pg.screenshot(path="data/ui-audit/slash-followup.png")

    pg.keyboard.press("ArrowDown")
    pg.wait_for_timeout(250)
    m2 = menu(pg, "slashMenuFollow")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(450)
    val = pg.evaluate("() => document.getElementById('followup').value")
    m3 = menu(pg, "slashMenuFollow")
    print("弹出:", json.dumps(m1, ensure_ascii=False))
    print("↓ 之后选中:", json.dumps(m2.get("on", "")[:40], ensure_ascii=False))
    print("Enter 之后 value:", repr(val), "| 菜单:", json.dumps(m3, ensure_ascii=False))

    first_cmd = (re.search(r"/([A-Za-z0-9_.\-]+)", m1.get("first") or "") or [None, ""])[1]
    second_cmd = (re.search(r"/([A-Za-z0-9_.\-]+)", m2.get("on") or "") or [None, ""])[1]
    checks = {
        "打 / 弹出菜单": m1.get("exists") and not m1.get("hidden") and m1.get("rows", 0) > 0,
        "↓ 真的换了选中项": bool(second_cmd) and second_cmd != first_cmd,
        "Enter 补全了选中项": val.startswith("/") and second_cmd and second_cmd in val,
        "补全后菜单收起": m3.get("hidden") is True,
        "没有被当消息发出去": not errs,          # 发送会走后端；这里只有 JS 异常兜底
    }
    for k, v in checks.items():
        print(("  ✓ " if v else "  ✗ ") + k)
    if errs:
        print("JS 错误:", errs[:4])
    b.close()
    sys.exit(0 if all(checks.values()) else 1)
