# -*- coding: utf-8 -*-
"""验证浏览器里的 favicon 就是那个 G（与托盘同一份 webui/favicon.ico）。"""
import json
import sys

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    pg = b.new_context(viewport={"width": 1200, "height": 800}).new_page()
    reqs = []
    pg.on("response", lambda r: reqs.append((r.status, r.url)) if "favicon" in r.url else None)
    pg.goto("http://127.0.0.1:8765/ui", wait_until="domcontentloaded")
    pg.wait_for_timeout(2200)
    info = pg.evaluate("""() => {
      const l = document.querySelector('link[rel="icon"]');
      return {href: l ? l.getAttribute('href') : null,
              abs: l ? l.href : null,
              type: l ? l.getAttribute('type') : null};
    }""")
    print("link[rel=icon]:", json.dumps(info, ensure_ascii=False))
    print("favicon 请求:", reqs[:3] or "（浏览器可能已缓存）")
    got = pg.evaluate("""async () => {
      const r = await fetch('/ui/favicon.ico', {cache: 'no-store'});
      const buf = await r.arrayBuffer();
      return {status: r.status, bytes: buf.byteLength,
              type: r.headers.get('content-type')};
    }""")
    print("直接取:", json.dumps(got, ensure_ascii=False))
    ok = (info.get("href") == "favicon.ico" and got.get("status") == 200
          and got.get("bytes", 0) > 1000)
    print("结论:", "favicon 就是 G 图标" if ok else "⚠ 不一致")
    b.close()
    sys.exit(0 if ok else 1)
