# -*- coding: utf-8 -*-
"""复现用户场景：只调 TeamScope 参数 → 点「保存」→ 提示必须不是"没有改动"。

三种情况都要对：
  1. 只改动画参数 → "本地设置已保存（N 项）"
  2. 什么都不改 → "没有改动"
  3. 改动画 + 再点保存一次（没再改） → "没有改动"
（这条会读 toast 的真实文本，不是查源码。）
"""
import json
import sys

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

TOASTS = """() => [...document.querySelectorAll('#toasts .toast')].map(e => e.textContent.trim())"""


def last_toast(pg):
    t = pg.evaluate(TOASTS)
    return t[-1] if t else ""


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    ctx = b.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)[:160]))

    # 干净起点：把动画参数清回默认（模拟"用户第一次打开设置"）
    pg.goto("http://127.0.0.1:8765/ui", wait_until="domcontentloaded")
    pg.evaluate("""() => { ['gantry-teamscope-amp','gantry-teamscope-line','gantry-teamscope-opacity',
      'gantry-teamscope-speed','gantry-breathe-dur','gantry-breathe-strength',
      'gantry-breathe-on','gantry-teamscope-heartbeat'].forEach(k => localStorage.removeItem(k)); }""")
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_timeout(2200)

    pg.evaluate("() => window.openSettings()")
    pg.wait_for_timeout(800)

    # ① 只拖滑杆 → 保存
    pg.evaluate("""() => { const el = document.getElementById('set_scopeAmp');
      el.value = 1.4; el.dispatchEvent(new Event('input', {bubbles: true})); }""")
    pg.wait_for_timeout(200)
    pg.click("#settingsSave")
    pg.wait_for_timeout(500)
    t1 = last_toast(pg)

    # ② 立刻再点一次保存（这次真的没改动）
    pg.evaluate("() => [...document.querySelectorAll('#toasts .toast')].forEach(e => e.remove())")
    pg.click("#settingsSave")
    pg.wait_for_timeout(500)
    t2 = last_toast(pg)

    # ③ 关掉再打开，什么都不动直接保存（快照应重置）
    pg.evaluate("() => window.closeSettings()")
    pg.wait_for_timeout(300)
    pg.evaluate("() => { localStorage.setItem('gantry-teamscope-amp','1.4'); }")
    pg.evaluate("() => window.openSettings()")
    pg.wait_for_timeout(600)
    pg.evaluate("() => [...document.querySelectorAll('#toasts .toast')].forEach(e => e.remove())")
    pg.click("#settingsSave")
    pg.wait_for_timeout(500)
    t3 = last_toast(pg)

    print(json.dumps({"只改动画后保存": t1, "再点保存": t2, "重开后直接保存": t3},
                     ensure_ascii=False))
    checks = {
        "① 不再说没有改动": "没有改动" not in t1 and "已保存" in t1,
        "② 真没改时说没有改动": "没有改动" in t2,
        "③ 快照已重置": "没有改动" in t3,
    }
    for k, v in checks.items():
        print(("  ✓ " if v else "  ✗ ") + k)
    print("JS 错误:", errs[:3] or "无")
    b.close()
    sys.exit(0 if all(checks.values()) else 1)
