# -*- coding: utf-8 -*-
"""英文模式下的设置页「动画」分组：不许露中文（label + title 都查）。"""
import json
import re
import sys

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    for lang in ("en", "tw"):
        ctx = b.new_context(viewport={"width": 1280, "height": 860})
        pg = ctx.new_page()
        pg.add_init_script(f"try{{localStorage.setItem('gantry-lang','{lang}')}}catch(e){{}}")
        pg.goto("http://127.0.0.1:8765/ui", wait_until="domcontentloaded")
        pg.wait_for_timeout(2200)
        pg.evaluate("() => window.openSettings()")
        pg.wait_for_timeout(900)
        out = pg.evaluate("""() => {
          const rows = [...document.querySelectorAll('#settingsBody .animrow')];
          return rows.map(r => ({
            label: (r.querySelector('span') || {}).textContent || '',
            val: (r.querySelector('.animval') || {}).textContent || '',
            title: r.getAttribute('title') || '',
          }));
        }""")
        cjk = re.compile(r"[\u4e00-\u9fff]")
        bad = [x for x in out
               if lang == "en" and (cjk.search(x["label"]) or cjk.search(x["title"]))]
        print(f"[{lang}] 行数={len(out)}", json.dumps(out[:3], ensure_ascii=False))
        print(f"[{lang}] 中文残留:", bad or "无")
        ctx.close()
    b.close()
