# -*- coding: utf-8 -*-
"""ESM 解耦后的"真交互"冒烟：点开会话 → 看时间线/工具记录/待办是否真的画出来。

背景：拆模块时最阴的 bug 是"跨模块共享状态变成模块私有"→ ReferenceError 被 try/catch 吞掉，
界面只是空白（会话列表空、步骤不刷新），静态检查/单测都绿。所以要真的点一遍。
"""
import sys

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    pg = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)[:200]))
    pg.on("console", lambda m: errs.append("console: " + m.text[:200]) if m.type == "error" else None)
    pg.goto("http://127.0.0.1:8765/ui", wait_until="domcontentloaded")
    pg.wait_for_timeout(3500)

    # 点第一个会话
    pg.evaluate("() => { const r=document.querySelector('#sessions .sess'); r && r.click(); }")
    pg.wait_for_timeout(3500)
    after = pg.evaluate("""() => ({
      current: window.current,
      title: (document.getElementById('title')||{}).textContent,
      steps: document.querySelectorAll('#steps .streamstep, #steps > *').length,
      lastSteps: (window.lastSteps||[]).length,
      toolLogHidden: (document.getElementById('toolLog')||{}).hidden,
      messages: (window.lastMessages||[]).length,
      status: (document.getElementById('status')||{}).textContent,
    })""")
    print(after)

    # 展开调用记录 / 待办
    pg.evaluate("() => { try{window.toggleToolLog()}catch(e){window.__e1=String(e)} }")
    pg.wait_for_timeout(1200)
    pg.evaluate("() => { try{window.toggleTodos()}catch(e){window.__e2=String(e)} }")
    pg.wait_for_timeout(800)
    more = pg.evaluate("""() => ({
      e1: window.__e1, e2: window.__e2,
      toolBodyRows: document.querySelectorAll('#toolLogBody .streamstep').length,
      goalVisible: !(document.getElementById('goalBar')||{}).hidden,
      streamStatus: (document.getElementById('streamStatus')||{}).textContent.slice(0,60),
    })""")
    print(more)
    pg.screenshot(path="data/ui-audit/esm-final.png")
    print("错误:", errs[:6] or "无")
    b.close()
