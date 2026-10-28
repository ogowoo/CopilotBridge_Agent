# -*- coding: utf-8 -*-
"""CSS 重排的**行为不变**证明：逐元素比对计算样式（computed style）。

为什么要它：把规则从一个文件搬到另一个文件，等于改**层叠顺序**。同选择器同权重的冲突
会悄悄改变结果（颜色/间距/圆角/溢出），截图看不出来，布局审计也未必覆盖。

做法：
  1. `--record <dir>`：把 /v1/** 的响应**录下来**，同时把每个场景下每个元素的计算样式存成 JSON；
  2. 改 CSS；
  3. `--compare <dir>`：用**同一份录制**驱动同一个页面，再存一份 JSON，逐元素逐属性比对。

只比"看得见的元素"和一组布局/颜色属性（不比动画/过渡/时间戳），并且把动画禁掉，
保证两次跑的是同一帧。

场景 = 页面状态 × 主题 × 视口：默认 / 打开会话 / 展开调用记录+待办 / 设置页 / 手册 /
插件 / 新任务空态，主题暗+浅，视口 1400×900、900×700、480×900。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8765/ui"

# 只比这些属性：布局 + 颜色 + 排版（不比 transition/animation，避免时间噪声）
PROPS = [
    "display", "position", "top", "right", "bottom", "left", "float", "clear", "z-index",
    "box-sizing", "width", "height", "min-width", "min-height", "max-width", "max-height",
    "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
    "border-top-style", "border-right-style", "border-bottom-style", "border-left-style",
    "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
    "border-top-left-radius", "border-top-right-radius",
    "border-bottom-left-radius", "border-bottom-right-radius",
    "background-color", "background-image", "background-position", "background-size",
    "background-repeat", "color", "opacity", "visibility", "overflow-x", "overflow-y",
    "box-shadow", "outline-style", "outline-width", "outline-color", "outline-offset",
    "font-family", "font-size", "font-weight", "font-style", "line-height", "letter-spacing",
    "text-align", "text-decoration-line", "text-transform", "white-space", "word-break",
    "overflow-wrap", "text-overflow", "text-indent", "vertical-align",
    "flex-direction", "flex-wrap", "flex-grow", "flex-shrink", "flex-basis",
    "justify-content", "align-items", "align-self", "align-content", "order", "gap",
    "row-gap", "column-gap", "grid-template-columns", "grid-template-rows",
    "grid-auto-flow", "grid-column", "grid-row",
    "transform", "content-visibility", "contain-intrinsic-size", "cursor",
    "list-style-type", "table-layout", "border-collapse", "scroll-behavior",
]

SCEN = [
    # (name, theme, viewport, 动作)
    ("default", "", (1400, 900), []),
    ("default_light", "light", (1400, 900), []),
    ("session", "", (1400, 900), ["open_session"]),
    ("session_rows", "", (1400, 900), ["open_session", "rows"]),
    ("session_light", "light", (1400, 900), ["open_session", "rows"]),
    ("settings", "", (1400, 900), ["settings"]),
    ("manual", "", (1400, 900), ["manual"]),
    ("plugins", "", (1400, 900), ["plugins"]),
    ("newtask", "", (1400, 900), ["newtask"]),
    ("narrow", "", (480, 900), []),
    ("narrow_session", "", (480, 900), ["open_session"]),
    ("mid_session", "", (900, 700), ["open_session", "rows"]),
]

ACTIONS = """
async (names) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const click = (sel) => { const el = document.querySelector(sel); if (el) el.click(); };
  for (const n of names) {
    if (n === 'open_session') { click('#sessions .sess'); await sleep(2200); }
    if (n === 'rows') {
      try { window.toggleToolLog(); } catch (e) {}
      await sleep(600);
      try { window.toggleTodos(); } catch (e) {}
      await sleep(600);
    }
    if (n === 'settings') { try { await window.openSettings(); } catch (e) {} await sleep(900); }
    if (n === 'manual') { try { await window.openManual(); } catch (e) {} await sleep(900); }
    if (n === 'plugins') { try { await window.openPlugins(); } catch (e) {} await sleep(900); }
    if (n === 'newtask') { try { window.openTask(); } catch (e) {} await sleep(900); }
  }
}
"""

DUMP = """
(props) => {
  const out = [];
  const root = document.body;
  const walk = (el, path) => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none') return;              // 不渲染的子树整棵跳过
    const rec = { p: path };
    for (const k of props) rec[k] = cs.getPropertyValue(k);
    const rect = el.getBoundingClientRect();
    rec._box = [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)].join(',');
    out.push(rec);
    let i = 0;
    for (const ch of el.children) { walk(ch, path + '/' + ch.tagName.toLowerCase() + '[' + (i++) + ']'); }
  };
  let i = 0;
  for (const ch of root.children) walk(ch, ch.tagName.toLowerCase() + '[' + (i++) + ']');
  return out;
}
"""

FREEZE = """
(() => {
  const st = document.createElement('style');
  st.textContent = '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}';
  document.documentElement.appendChild(st);
  try { localStorage.setItem('gantry-lang', 'zh'); } catch (e) {}
  try { localStorage.setItem('gantry-theme', 'THEME'); } catch (e) {}
})();
"""


def run(mode: str, outdir: Path) -> int:
    outdir.mkdir(parents=True, exist_ok=True)
    respfile = outdir / "responses.json"
    recorded: dict[str, dict] = json.loads(respfile.read_text(encoding="utf-8")) if respfile.is_file() else {}
    replay = mode == "compare" and bool(recorded)
    dump: dict[str, list] = {}

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, channel="msedge")
        for name, theme, (w, h), acts in SCEN:
            ctx = b.new_context(viewport={"width": w, "height": h})
            pg = ctx.new_page()
            pg.add_init_script(FREEZE.replace("THEME", theme))
            if replay:
                def handle(route, request):
                    url = request.url
                    rec = recorded.get(url)
                    if rec is None:
                        route.continue_()
                    else:
                        route.fulfill(status=rec["status"], body=rec["body"],
                                      headers={"content-type": rec.get("type") or "application/json"})
                pg.route("**/v1/**", handle)
                pg.route("**/health", handle)
            elif mode == "record":
                def on_resp(resp):
                    url = resp.url
                    if "/v1/" not in url and not url.endswith("/health"):
                        return
                    ct = resp.headers.get("content-type", "")
                    if "event-stream" in ct:
                        return
                    try:
                        body = resp.text()
                    except Exception:                       # noqa: BLE001
                        return
                    recorded.setdefault(url, {"status": resp.status, "body": body, "type": ct})
                pg.on("response", on_resp)

            pg.goto(BASE, wait_until="domcontentloaded")
            pg.wait_for_timeout(3200)
            pg.evaluate(ACTIONS, acts)
            pg.wait_for_timeout(600)
            dump[name] = pg.evaluate(DUMP, PROPS)
            print(f"  {name:16s} {len(dump[name]):5d} 元素")
            ctx.close()
        b.close()

    (outdir / f"styles_{mode}.json").write_text(json.dumps(dump, ensure_ascii=False), encoding="utf-8")
    if mode == "record":
        respfile.write_text(json.dumps(recorded, ensure_ascii=False), encoding="utf-8")
        print(f"录制 {len(recorded)} 个接口响应 → {respfile}")
    return 0


def compare(baseline: Path, current: Path) -> int:
    A = json.loads((baseline / "styles_record.json").read_text(encoding="utf-8"))
    B = json.loads((current / "styles_compare.json").read_text(encoding="utf-8"))
    total = 0
    shown = 0
    for scen in sorted(A):
        a = {r["p"]: r for r in A[scen]}
        b = {r["p"]: r for r in B.get(scen, [])}
        only_a = set(a) - set(b)
        only_b = set(b) - set(a)
        diffs = []
        for path in sorted(set(a) & set(b)):
            for k, va in a[path].items():
                if k == "p":
                    continue
                vb = b[path].get(k)
                if va != vb:
                    diffs.append((path, k, va, vb))
        total += len(diffs)
        head = f"[{scen}] 差异 {len(diffs)} 处；仅基线有 {len(only_a)} 元素，仅当前有 {len(only_b)} 元素"
        print(head)
        if only_a or only_b:
            print("   仅基线:", sorted(only_a)[:4])
            print("   仅当前:", sorted(only_b)[:4])
        for path, k, va, vb in diffs[:12]:
            print(f"   {path} · {k}: {va!r} → {vb!r}")
            shown += 1
    print(f"\n合计属性差异：{total}（显示 {shown} 条明细）")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    if "--record" in sys.argv:
        sys.exit(run("record", Path(sys.argv[sys.argv.index("--record") + 1])))
    if "--compare" in sys.argv:
        d = Path(sys.argv[sys.argv.index("--compare") + 1])
        run("compare", d)          # 先用同一份录制重跑一遍，落 styles_compare.json
        sys.exit(compare(d, d))
    if "--diff-only" in sys.argv:
        d = Path(sys.argv[sys.argv.index("--diff-only") + 1])
        sys.exit(compare(d, d))
    print(__doc__)
