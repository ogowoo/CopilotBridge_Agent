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
               offenders: [], overlaps: [], clippedText: [], scrollOwners: [], byDesignTruncated: [], edgeOverlaps: [], clippedOverflow: []};
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
    if (cs.display === 'none' || cs.visibility === 'hidden') return;   // 固定元素也要查：
    // 实测教训：`#bootBar` 因 CSS 选择器写错（逗号后直接跟 @media，整块被吞）
    // 在窄屏跑到视口右侧 28px 外，而我把 fixed 排除后审计完全看不见。
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

  // 3b) 固定浮层遮挡**内容**元素（此前的盲区：只比了浮层之间，漏掉"浮层压住标题/按钮"）
  const KEY = ['#title','#status','#sidChip','.tabs','#chatlog','#followup','#sendFollowup',
               '#injectBtn','#chatFindToggle','#sessModel','.actions','#steps','#activity','#answer'];
  res.occlusions = [];
  // 元素被滚动容器裁剪后**真正可见**的矩形：不裁剪就会误报。
  // 实测：#sessionView 内部滚动的 .tabs，原始矩形落在 fixed 浮层下面，
  // 但那些像素已被容器裁掉，用户看不到"遮挡"。
  const clipOf = el => {
    let r = el.getBoundingClientRect();
    let p = el.parentElement;
    while (p && p !== document.body) {
      const cs = getComputedStyle(p);
      if (/auto|scroll|hidden/.test(cs.overflowY + cs.overflowX)) {
        const pr = p.getBoundingClientRect();
        r = {left: Math.max(r.left, pr.left), top: Math.max(r.top, pr.top),
             right: Math.min(r.right, pr.right), bottom: Math.min(r.bottom, pr.bottom)};
      }
      p = p.parentElement;
    }
    return r;
  };
  fixed.forEach(f => {
    if (f.r.right < 0 || f.r.left > vw || f.r.bottom < 0 || f.r.top > vh) return;
    KEY.forEach(sel => {
      document.querySelectorAll(sel).forEach(el => {
        const cs2 = getComputedStyle(el);
        if (cs2.display === 'none' || cs2.visibility === 'hidden') return;
        const b = clipOf(el);   // 只比"裁剪后仍可见"的部分
        if (b.width < 4 || b.height < 4) return;
        const ox = Math.max(0, Math.min(f.r.right, b.right) - Math.max(f.r.left, b.left));
        const oy = Math.max(0, Math.min(f.r.bottom, b.bottom) - Math.max(f.r.top, b.top));
        if (ox * oy <= 0) return;
        const ratio = (ox * oy) / (b.width * b.height);
        if (ratio < 0.05) return;    // 5% 以下忽略（轻微擦边）
        // 判据：目标**中心点**是否真的被盖住。仅矩形相交不足以判定遮挡——
        // 目标可能被祖先容器裁剪（矩形伸到可视区之外），那样"重叠"是不可见的。
        const cx = Math.round(b.left + b.width / 2), cy = Math.round(b.top + b.height / 2);
        const hit = document.elementFromPoint(cx, cy);
        const reallyCovered = hit === f.el || (hit && f.el.contains(hit));
        const item = {cover: f.id || f.cls, target: sel, ratio: Math.round(ratio * 100),
                      overlap: Math.round(ox) + 'x' + Math.round(oy)};
        if (reallyCovered) res.occlusions.push(item); else res.edgeOverlaps.push(item);
      });
    });
  });

  // 3c) **被裁掉的溢出**：容器 overflow:hidden 时子元素溢出既没有滚动条、也可能不出现在
  //     offenders 里（bbox 仍在视口内）——这次会话栏溢出 25px 就是这样藏住的
  //     （截图里筛选框被压成一个空椭圆）。
  res.clippedOverflow = [];
  document.querySelectorAll('body *').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    if (!/hidden/.test(cs.overflow + cs.overflowX)) return;
    if (el.clientWidth < 40) return;
    if (cs.textOverflow === 'ellipsis') return;   // 省略号截断是设计内行为（会话标题等）
    if (el.children.length === 0) return;         // 只看**容器**（子元素溢出），不看纯文本叶
    const over = el.scrollWidth - el.clientWidth;
    if (over > 2) res.clippedOverflow.push({...describe(el), over, cw: el.clientWidth});
  });

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

# 工具栏"主操作按钮"的锚定关系：主按钮必须贴容器右边缘、且垂直在工具栏内。
# 实测事故：`.nc-bar` 用 flex-wrap 时主按钮跟着换行漂移，跑到第二行中间；
# 布局审计只查溢出/重叠/裁剪，量不到"换行了但没出界"这种秩序问题。
COMPOSER_ALIGN_JS = r"""
() => {
  const empty = document.getElementById('emptyState');
  const bar = document.querySelector('.nc-bar');
  const send = document.querySelector('.nc-send');
  if (!empty || !bar || !send) return {skipped: 'elements-missing'};
  if (getComputedStyle(empty).display === 'none') return {skipped: 'composer-hidden'};
  const b = bar.getBoundingClientRect(), s = send.getBoundingClientRect();
  if (b.width < 2 || s.width < 2) return {skipped: 'zero-size'};
  return {barLeft: Math.round(b.left), barRight: Math.round(b.right),
          barTop: Math.round(b.top), barBottom: Math.round(b.bottom),
          sendLeft: Math.round(s.left), sendRight: Math.round(s.right),
          sendTop: Math.round(s.top), sendBottom: Math.round(s.bottom)};
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
                  try{syncFixedLanes();}catch(e){}
                  // 打开对话标签：否则 #followup/#sendFollowup/#injectBtn/#chatlog 都是 display:none，
                  // 量不到"浮层压住发送按钮"这类问题（子代理正是开着对话标签量到的）
                  try{liveTab('chat');}catch(e){}
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
            for oc in m.get("occlusions", []):
                problems.append(f"{w}x{h}: 浮层 {oc['cover']} 遮挡 {oc['target']} "
                                f"{oc['ratio']}%（{oc['overlap']}）")
            for ov in m["overlaps"]:
                problems.append(f"{w}x{h}: 固定元素重叠 {ov['a']} × {ov['b']} ({ov['overlap']}, "
                                f"z={ov['za']}/{ov['zb']})")
            for co in m.get("clippedOverflow", []):
                problems.append(f"{w}x{h}: 容器 {co['id'] or co['cls']} 内溢出被裁 {co['over']}px "
                                f"(内容 {co['cw'] + co['over']} / 可用 {co['cw']})")
            if m.get("edgeOverlaps"):
                report.setdefault("edge_overlaps", []).append(
                    {"width": w, "items": m["edgeOverlaps"]})
            if m.get("byDesignTruncated"):
                report.setdefault("by_design", []).append(
                    {"width": w, "count": len(m["byDesignTruncated"])})
            if m["clippedText"]:
                problems.append(f"{w}x{h}: {len(m['clippedText'])} 处文字被裁剪（例："
                                + "; ".join(f"{c['id'] or c['cls']}:{c['text']}" for c in m["clippedText"][:3]) + ")")

            # ── 新会话编辑器：主按钮锚定检查 ──────────────────────────
            try:
                page.evaluate("()=>{try{openTask()}catch(e){}}")
                page.wait_for_timeout(320)
                comp = page.evaluate(COMPOSER_ALIGN_JS)
            except Exception as exc:
                comp = {"skipped": f"eval-failed: {exc}"}
            if not comp.get("skipped"):
                gap = abs(comp["sendRight"] - comp["barRight"])
                if gap > 2:
                    problems.append(
                        f"{w}x{h}: 工具栏主按钮未贴右（容器右 {comp['barRight']} / "
                        f"按钮右 {comp['sendRight']}，差 {gap}px）")
                if comp["sendTop"] < comp["barTop"] - 1 or comp["sendBottom"] > comp["barBottom"] + 1:
                    problems.append(
                        f"{w}x{h}: 工具栏主按钮纵向超出工具栏 "
                        f"(按钮 {comp['sendTop']}..{comp['sendBottom']} / "
                        f"工具栏 {comp['barTop']}..{comp['barBottom']})")
                report.setdefault("composer_align", []).append(
                    {"width": w, "gap": gap,
                     "inside": comp["sendTop"] >= comp["barTop"] - 1
                               and comp["sendBottom"] <= comp["barBottom"] + 1})

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
