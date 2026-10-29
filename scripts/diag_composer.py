# -*- coding: utf-8 -*-
"""诊断：空状态（未选会话）下底部 followup 输入框为什么还显示。"""
import json
import sys

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

PROBE = """() => {
  const comp = document.querySelector('.main > .composer');
  const empty = document.getElementById('emptyState');
  const main = document.querySelector('.main');
  const cs = comp ? getComputedStyle(comp) : null;
  return {
    hasComposer: !!comp,
    composerInlineDisplay: comp ? comp.style.display : null,
    composerComputedDisplay: cs ? cs.display : null,
    emptyInlineDisplay: empty ? empty.style.display : null,
    emptyComputedDisplay: empty ? getComputedStyle(empty).display : null,
    mainClass: main ? main.className : null,
    sessionView: (document.getElementById('sessionView') || {}).style ?
                 document.getElementById('sessionView').style.display : null,
    taskBoxVisible: (() => { const t = document.getElementById('task');
      return t ? getComputedStyle(t).display !== 'none' : null; })(),
    followupVisible: (() => { const f = document.getElementById('followup');
      return f ? getComputedStyle(f).display !== 'none' : null; })(),
    current: window.current,
  };
}"""

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    pg = b.new_context(viewport={"width": 1500, "height": 1000}).new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
    pg.goto("http://127.0.0.1:8765/ui", wait_until="domcontentloaded")
    pg.wait_for_timeout(3000)
    print("启动后:", json.dumps(pg.evaluate(PROBE), ensure_ascii=False))
    pg.evaluate("() => { try{ window.syncComposer() }catch(e){ window.__e=String(e) } }")
    pg.wait_for_timeout(300)
    print("手动 syncComposer 后:", json.dumps(pg.evaluate(PROBE), ensure_ascii=False))
    print("syncComposer 是否可调用:", pg.evaluate("() => typeof window.syncComposer"))
    print("JS 错误:", errs[:4] or "无")
    b.close()
