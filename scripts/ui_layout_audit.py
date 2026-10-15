# -*- coding: utf-8 -*-
"""UI 布局审计：在多种视口宽度下**实测**溢出、遮挡、固定元素碰撞，并截图存档。

**为什么需要**：布局问题靠读 CSS 很容易漏（`position:fixed` 相互遮挡、窄屏横向溢出、
滚动归属错位都只在真实渲染下才暴露）。本脚本用 Playwright 真跑一遍，把几何量出来。

用法:
    python scripts/ui_layout_audit.py                # 默认 7 种宽度，截图存 data/ui-audit/
    python scripts/ui_layout_audit.py --widths 1440x900,900x700
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "data" / "ui-audit"

MEASURE_JS = r"""
() => {
  const de = document.documentElement;
  const res = {inner: window.innerWidth, scrollW: de.scrollWidth, bodyScrollW: document.body.scrollWidth,
               offenders: [], overlaps: [], clippedText: [], scrollOwners: [], byDesignTruncated: []};
  const vw = window.innerWidth, vh = window.innerHeight;

  const describe = el => ({
    tag: el.tagName.toLowerCase(),
    id: el.id || '',
    cls: (typeof el.className === 'string' ? el.className : '').slice(0, 40),
    text: (el.textContent || '').trim().slice(0, 24)
  });

  // 1) 横向溢出：右边界超出视口或左边界为负
  document.querySelectorAll('body *').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.position === 'fixed') return;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    if (r.right > vw + 1 || r.left < -1) {
      res.offenders.push({...describe(el), left: Math.round(r.left), right: Math.round(r.right),
                          w: Math.round(r.width)});
    }
  });

  // 2) 滚动归属：谁在横向/纵向真的能滚
  document.querySelectorAll('body *').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none') return;
    if ((el.scrollWidth > el.clientWidth + 2 || el.scrollHeight > el.clientHeight + 2)
        && /auto|scroll/.test(cs.overflow + cs.overflowX + cs.overflowY)) {
      res.scrollOwners.push({...describe(el), sw: el.scrollWidth, cw: el.clientWidth,
                             sh: el.scrollHeight, ch: el.clientHeight});
    }
  });

  // 3) 固定元素碰撞（两两求交，含与视口边界的关系）
  const fixed = [...document.querySelectorAll('body *')].filter(el => {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' || cs.display === 'none' || cs.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4 && parseFloat(cs.opacity || '1') > 0.1;
  }).map(el => ({el, ...describe(el), r: el.getBoundingClientRect(), z: getComputedStyle(el).zIndex}));
  for (let i = 0; i < fixed.length; i++) {
    for (let j = i + 1; j < fixed.length; j++) {
      const a = fixed[i].r, b = fixed[j].r;
      const ox = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
      const oy = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
      if (ox > 2 && oy > 2) {
        res.overlaps.push({a: fixed[i].id || fixed[i].cls, b: fixed[j].id || fixed[j].cls,
                           overlap: Math.round(ox) + 'x' + Math.round(oy),
                           za: fixed[i].z, zb: fixed[j].z});
      }
    }
  }
  res.fixedList = fixed.map(f => ({id: f.id || f.cls, z: f.z,
                                   box: [Math.round(f.r.left), Math.round(f.r.top),
                                         Math.round(f.r.right), Math.round(f.r.bottom)]}));

  // 4) 文字被裁剪（元素内文字超出其可视宽度且未换行）
  document.querySelectorAll('#sessionView *, #sessions *, .kpi, .btn, .stag, .sid, h1, h2, h3, label').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || !el.textContent.trim()) return;
    // 省略号截断是**设计内行为**（会话标题/长 ID 必须截断），单列到 byDesignTruncated；
    // 只把"既没省略号又被裁掉"的算作真问题。
    if (el.scrollWidth > el.clientWidth + 2 && cs.overflow !== 'visible' && cs.whiteSpace !== 'normal') {
      const item = {...describe(el), sw: el.scrollWidth, cw: el.clientWidth};
      if (cs.textOverflow === 'ellipsis') res.byDesignTruncated.push(item);
      else res.clippedText.push(item);
    }
  });
  return res;
}
"""


def audit(widths: list[tuple[int, int]], url: str, stress: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {"url": url, "stress": stress, "viewport_results": []}
    problems: list[str] = []

    with sync_playwright() as p:
        browser = None
        for kwargs in ({"channel": "msedge"}, {}, {"channel": "chrome"}):
            try:
                browser = p.chromium.launch(headless=True, **kwargs)
                break
            except Exception as exc:
                last = exc
        if browser is None:
            print(f"无法启动浏览器：{last}")
            return 2
        page = browser.new_page(viewport={"width": widths[0][0], "height": widths[0][1]})

        for w, h in widths:
            page.set_viewport_size({"width": w, "height": h})
            page.goto(url, wait_until="domcontentloaded")
            # 选一个真实会话，让 sessionView / KPI / 标签页都渲染出来（否则量的是空状态）
            sid = page.evaluate(
                "async()=>{try{const r=await fetch('/v1/agent/sessions');const d=await r.json();"
                "return (d.sessions&&d.sessions[0]&&d.sessions[0].id)||''}catch(e){return ''}}")
            if sid:
                page.evaluate(f"load({json.dumps(sid)})")
            page.wait_for_timeout(900)
            if stress:
                # 压力场景：把所有 fixed 浮层同时打开（它们各自独立出现时都正常，
                # 只有叠在一起才能暴露遮挡）。全部用只读的本地函数，不发业务请求。
                page.evaluate("""()=>{try{showBoot('needs_login',false,'压力测试');}catch(e){}
                  try{showConnBanner('压力测试：无法连接服务',false);}catch(e){}
                  try{showApprovalBar({tool:'delete_path',args_hash:'stress'});}catch(e){}
                  try{updateRetryCountdown({retry_resume_at:Math.floor(Date.now()/1000)+3600,
                                            retry_attempt:2,busy:false});}catch(e){}
                  try{toast('压力测试提示','ok');}catch(e){}
                  // 限流小标（收起态）也一起打开：它与授权横幅同在底部，窄屏会撞
                  try{const b=document.getElementById('retryCountdown');if(b)b.style.display='none';
                      const cp=document.getElementById('retryChip');if(cp){cp.style.display='block';
                      cp.textContent='⏳ 57:59'}}catch(e){}
                  try{syncFixedLanes();}catch(e){}}""")
                page.wait_for_timeout(500)
            m = page.evaluate(MEASURE_JS)
            m["width"] = w
            m["height"] = h
            m["selected_session"] = bool(sid)
            report["viewport_results"].append(m)
            page.screenshot(path=str(OUT / f"w{w}x{h}.png"), full_page=False)

            if m["scrollW"] > w + 1:
                problems.append(f"{w}x{h}: 文档横向溢出 scrollWidth={m['scrollW']} > {w}")
            if m["offenders"]:
                top = m["offenders"][:4]
                problems.append(f"{w}x{h}: {len(m['offenders'])} 个元素超出视口（例："
                                + "; ".join(f"{o['tag']}#{o['id'] or o['cls']}[{o['left']}..{o['right']}]"
                                            for o in top) + ")")
            for ov in m["overlaps"]:
                problems.append(f"{w}x{h}: 固定元素重叠 {ov['a']} × {ov['b']} ({ov['overlap']}, "
                                f"z={ov['za']}/{ov['zb']})")
            if m.get("byDesignTruncated"):
                report.setdefault("by_design", []).append(
                    {"width": w, "count": len(m["byDesignTruncated"])})
            if m["clippedText"]:
                problems.append(f"{w}x{h}: {len(m['clippedText'])} 处文字被裁剪（例："
                                + "; ".join(f"{c['id'] or c['cls']}:{c['text']}" for c in m["clippedText"][:3]) + ")")

        browser.close()

    (OUT / "layout-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    print(f"实测 {len(widths)} 种视口；截图与报告在 {OUT}")
    if problems:
        print(f"\n发现 {len(problems)} 条布局问题：")
        for x in problems:
            print("  ✗", x)
    else:
        print("\n未发现横向溢出、固定元素重叠或文字裁剪。")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/ui")
    ap.add_argument("--stress", action="store_true",
                    help="同时打开全部固定浮层，专门测遮挡")
    ap.add_argument("--widths", default="1600x900,1440x900,1280x800,1024x768,900x700,768x1024,480x900")
    args = ap.parse_args()
    widths = []
    for item in args.widths.split(","):
        w, _, h = item.strip().partition("x")
        widths.append((int(w), int(h)))
    return audit(widths, args.url, args.stress)


if __name__ == "__main__":
    raise SystemExit(main())
