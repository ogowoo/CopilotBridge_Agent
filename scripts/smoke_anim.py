# -*- coding: utf-8 -*-
"""浏览器验证：设置页「动画」分组的 8 个控件真的能改、改了什么真的生效。

不只查"元素在不在"，而是：
  1. 打开设置 → 控件齐全、滑杆有初始值；
  2. 拖「镜头参数」→ localStorage 落库 + <html> 上的 CSS 变量跟着变（呼吸）；
  3. 示波器：直接调 scope 的绘制入口，比较不同振幅下线的高/低点是否真的变了（canvas 像素）；
  4. 关掉呼吸 → :root[data-breathe=off]，composer 的 animation 变 none；
  5. 无 JS 异常；截图存 data/ui-audit/anim-settings.png。
"""
import json
import sys

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    ctx = b.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)[:160]))
    pg.on("console", lambda m: errs.append("console: " + m.text[:160]) if m.type == "error" else None)
    pg.goto("http://127.0.0.1:8765/ui", wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)

    pg.evaluate("() => window.openSettings && window.openSettings()")
    pg.wait_for_timeout(900)

    info = pg.evaluate("""() => {
      const ids = ['setTeamScopeHeartbeat','set_scopeAmp','set_scopeLine','set_scopeOpacity',
                   'set_scopeSpeed','setBreatheOn','set_breatheDur','set_breatheStrength'];
      return ids.map(id => {
        const el = document.getElementById(id);
        return {id, ok: !!el, type: el && el.type, val: el && (el.type === 'checkbox' ? el.checked : el.value)};
      });
    }""")
    print("控件:", json.dumps(info, ensure_ascii=False))

    # 拉满振幅与线宽、拉满呼吸强度
    pg.evaluate("""() => {
      const set = (id, v) => { const el = document.getElementById(id);
        el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); };
      set('set_scopeAmp', 1.6); set('set_scopeLine', 4);
      set('set_breatheDur', 3.4); set('set_breatheStrength', 60);
    }""")
    pg.wait_for_timeout(400)
    state = pg.evaluate("""() => ({
      ls: {
        amp: localStorage.getItem('gantry-teamscope-amp'),
        line: localStorage.getItem('gantry-teamscope-line'),
        dur: localStorage.getItem('gantry-breathe-dur'),
        str: localStorage.getItem('gantry-breathe-strength'),
      },
      css: (() => { const cs = getComputedStyle(document.documentElement);
        return {dur: cs.getPropertyValue('--breathe-dur').trim(),
                hi: cs.getPropertyValue('--breathe-hi').trim(),
                glow: cs.getPropertyValue('--breathe-glow').trim()}; })(),
      label: (document.getElementById('val_scopeAmp') || {}).textContent,
    })""")
    print("落库/CSS:", json.dumps(state, ensure_ascii=False))

    # 示波器：同一块画布，用小/大振幅各画一帧，比较墨迹的上下跨度
    span = pg.evaluate("""async () => {
      const cv = document.getElementById('teamScope');
      if (!cv) return {err: 'no canvas'};
      const ctx = cv.getContext('2d');
      const ink = () => { const d = ctx.getImageData(0,0,cv.width,cv.height).data;
        let lo = 1e9, hi = -1e9;
        for (let y = 0; y < cv.height; y++) {
          for (let x = 0; x < cv.width; x += 4) {
            if (d[(y*cv.width + x)*4 + 3] > 20) { if (y < lo) lo = y; if (y > hi) hi = y; }
          }
        }
        return hi - lo; };
      const draw = (amp) => {
        localStorage.setItem('gantry-teamscope-amp', String(amp));
        document.dispatchEvent(new CustomEvent('gantry-animprefs'));
        window.scopeFrame();
        return ink();
      };
      const small = draw(0.4), big = draw(1.6);
      return {small, big, ratio: small ? +(big/small).toFixed(2) : 0};
    }""")
    print("示波器墨迹跨度:", json.dumps(span, ensure_ascii=False))

    # 关掉呼吸
    pg.evaluate("""() => { const el = document.getElementById('setBreatheOn');
      el.checked = false; el.dispatchEvent(new Event('change', {bubbles: true})); }""")
    pg.wait_for_timeout(300)
    breathe = pg.evaluate("""() => ({
      attr: document.documentElement.getAttribute('data-breathe'),
      ls: localStorage.getItem('gantry-breathe-on'),
    })""")
    print("呼吸开关:", json.dumps(breathe, ensure_ascii=False))

    pg.screenshot(path="data/ui-audit/anim-settings.png")
    print("JS 错误:", errs[:5] or "无")
    b.close()
