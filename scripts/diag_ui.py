# -*- coding: utf-8 -*-
"""诊断探针：当前页面的会话列表/时间线/工具记录是否真的画出来了（含 console 错误）。"""
import sys

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    pg = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)[:300]))
    pg.on("console", lambda m: errs.append("console." + m.type + ": " + m.text[:300]) if m.type == "error" else None)
    pg.goto("http://127.0.0.1:8765/ui", wait_until="domcontentloaded")
    pg.wait_for_timeout(4000)
    out = pg.evaluate("""() => ({
      sessCache: (window.sessCache||[]).length,
      sessRows: document.querySelectorAll('#sessions .sess, #sessions > *').length,
      sessListText: (document.getElementById('sessList')||{}).innerText ? document.getElementById('sessList').innerText.slice(0,80) : '',
      subagentCount: window.subagentCount,
      expandedIsSet: window.expandedSess instanceof Set,
      toolLogSteps: window.toolLogSteps && window.toolLogSteps.length,
      current: window.current,
      lastMessages: (window.lastMessages||[]).length,
      showSubagents: window.showSubagents,
      serviceStopped: window.serviceStopped,
    })""")
    print(out)
    print("错误:", errs[:6] or "无")
    b.close()
