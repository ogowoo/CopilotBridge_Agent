# -*- coding: utf-8 -*-
"""浏览器验证（续 2）：先打开一个会话（卡片才会真正布局），再量示波器参数效果。"""
import json
import sys

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    pg = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
    pg.goto("http://127.0.0.1:8765/ui", wait_until="domcontentloaded")
    pg.wait_for_timeout(2200)
    pg.evaluate("() => { const r = document.querySelector('#sessions .sess'); if (r) r.click(); }")
    pg.wait_for_timeout(2600)

    res = pg.evaluate("""() => {
      const card = document.getElementById('teamCard');
      const cv = document.getElementById('teamScope');
      if (!card || !cv) return {err: '缺 #teamCard / #teamScope'};
      card.classList.add('busy');
      card.style.setProperty('height', '150px', 'important');
      card.style.setProperty('width', '640px', 'important');
      card.style.setProperty('display', 'block', 'important');
      cv.style.setProperty('display', 'block', 'important');
      const ctx = cv.getContext('2d');
      const probe = () => {
        const W = cv.width, H = cv.height;
        const d = ctx.getImageData(0, 0, W, H).data;
        let lo = 1e9, hi = -1e9, maxA = 0, thick = 0;
        const col = Math.floor(W / 2);
        for (let y = 0; y < H; y++) {
          for (let x = 0; x < W; x += 2) {
            const a = d[(y * W + x) * 4 + 3];
            if (a > 12) { if (y < lo) lo = y; if (y > hi) hi = y; if (a > maxA) maxA = a; }
          }
          if (d[(y * W + col) * 4 + 3] > 12) thick++;
        }
        return {span: hi >= lo ? hi - lo : 0, maxA, thick, W, H};
      };
      const draw = (amp, line, op) => {
        localStorage.setItem('gantry-teamscope-amp', String(amp));
        localStorage.setItem('gantry-teamscope-line', String(line));
        localStorage.setItem('gantry-teamscope-opacity', String(op));
        document.dispatchEvent(new CustomEvent('gantry-animprefs'));
        window.scopeFrame();
        return probe();
      };
      return {a: draw(0.4, 1.8, 1.0), b: draw(1.6, 1.8, 1.0),
              c: draw(1.6, 4.0, 1.0), d: draw(1.6, 1.8, 0.25)};
    }""")
    print("示波器测量:", json.dumps(res, ensure_ascii=False))
    a, bb, c, d = res.get("a", {}), res.get("b", {}), res.get("c", {}), res.get("d", {})
    checks = {
        "画布真的有尺寸": (bb.get("W", 0) > 100 and bb.get("H", 0) > 50),
        "振幅变大 → 跨度变大": bb.get("span", 0) > a.get("span", 0) * 1.5,
        "线变粗 → 厚度变大": c.get("thick", 0) > bb.get("thick", 0),
        "透明度降低 → 峰值 alpha 变小": d.get("maxA", 999) < c.get("maxA", 0),
    }
    for k, v in checks.items():
        print(("  ✓ " if v else "  ✗ ") + k)
    print("JS 错误:", errs[:3] or "无")
    b.close()
    sys.exit(0 if all(checks.values()) else 1)
