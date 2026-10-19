# -*- coding: utf-8 -*-
"""主题对比度审计：按 WCAG 实测 7 套主题下的关键文字对比度。

**为什么需要**：审查时发现 `--faint` 在浅色系主题下只有 2.6–3.9（正文要求 ≥4.5），
accent 按钮白字在 6 套主题下 2.0–3.7，`.empty .big` 低到 1.32。这类问题肉眼很难发现
（"看着还行"），但可以用 getComputedStyle + 沿祖先求背景色的方式精确量出来。
本脚本把它变成可回归的检查：改主题变量后跑一次就知道有没有掉回不合格。

用法:
    python scripts/ui_contrast_audit.py            # 7 套主题，返回码 0/1
    python scripts/ui_contrast_audit.py --json     # 输出机器可读结果
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
# 注意：每一项都要**显式**设 dataset.theme（原先 dark 那项传空串、跳过设置，
# 于是量到的是页面默认主题 → dark 列和 light 列数字一模一样，形同没测暗色）。
THEMES = [("dark", "dark"), ("amber", "amber"), ("jade", "jade"), ("light", "light"),
          ("solarized", "solarized"), ("mint", "mint")]

# (选择器, 需求对比度, 说明) —— 4.5 = 正文，3.0 = 大字/非文本
TARGETS = [
    (".kpi span", 4.5, "KPI 标签（--faint）"),
    ("#status", 4.5, "会话状态行"),
    (".sub", 4.5, "路径/副标题"),
    ("code.sid", 4.5, "会话 ID 芯片"),
    (".chatbar .meta", 4.5, "对话区提示"),
    (".empty .big", 3.0, "空态大字（大字阈值 3.0）"),
    (".btn.accent", 4.5, "accent 按钮文字"),
    (".tab.on", 4.5, "选中标签文字"),
    (".toast.ok", 4.5, "成功提示"),
    (".menuList button.danger", 4.5, "危险菜单项"),
    # 下面两条是"展开后才存在"的元素 —— 以前只在暗色下看过，浅色主题里
    # 变量名写错（--term-fg vs --term-text）导致白底白字，肉眼才发现。
    (".streamstep details pre", 4.5, "步骤详情 JSON（展开态）"),
    (".md-codewrap pre", 4.5, "代码块（Markdown）"),
]

JS = r"""
(targets) => {
  const lum = c => {
    const m = c.match(/[\d.]+/g); if (!m) return null;
    const [r, g, b] = m.slice(0, 3).map(x => x / 255);
    const f = v => v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const bgOf = el => {
    let n = el;
    while (n && n !== document.documentElement) {
      const c = getComputedStyle(n).backgroundColor;
      if (c && !/rgba\(0, 0, 0, 0\)|transparent/.test(c)) return c;
      n = n.parentElement;
    }
    return getComputedStyle(document.body).backgroundColor || 'rgb(255,255,255)';
  };
  const ratio = (a, b) => { const x = lum(a), y = lum(b);
    const hi = Math.max(x, y), lo = Math.min(x, y); return Math.round((hi + 0.05) / (lo + 0.05) * 100) / 100; };
  const out = [];
  document.querySelectorAll('details').forEach(d => { d.open = true; });
  for (const [sel, need, label] of targets) {
    let el = document.querySelector(sel);
    if (need === 3.0) {   // 空态大字：临时切到空态才存在
      const ev = document.getElementById('emptyState'), sv = document.getElementById('sessionView');
      if (ev && sv) { ev.style.display = 'flex'; sv.style.display = 'none'; el = document.querySelector(sel); }
    }
    if (!el) { out.push({sel, label, need, ratio: null, note: '元素不存在'}); continue; }
    const cs = getComputedStyle(el);
    const r = ratio(cs.color, bgOf(el));
    out.push({sel, label, need, ratio: r, color: cs.color, bg: bgOf(el),
              fontPx: parseFloat(cs.fontSize), bold: parseInt(cs.fontWeight || '400', 10) >= 600});
  }
  return out;
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/ui")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    report: dict = {}
    bad: list[str] = []
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
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        for name, attr in THEMES:
            page.goto(args.url, wait_until="domcontentloaded")
            page.evaluate(f"document.documentElement.dataset.theme={json.dumps(attr)}")
            # 选一个会话让 KPI/状态/标签等元素真实存在
            sid = page.evaluate("async()=>{try{const d=await (await fetch('/v1/agent/sessions')).json();"
                                "return (d.sessions&&d.sessions[0]&&d.sessions[0].id)||''}catch(e){return ''}}")
            if sid:
                page.evaluate(f"load({json.dumps(sid)})")
            page.evaluate("()=>{try{toast('对比度探针','ok');}catch(e){}}")
            page.wait_for_timeout(500)
            res = page.evaluate(JS, TARGETS)
            report[name] = res
            for item in res:
                if item["ratio"] is None:
                    continue
                if item["ratio"] < item["need"]:
                    bad.append(f"{name}: {item['sel']} 对比度 {item['ratio']} < {item['need']} "
                               f"（{item['label']}）")
        browser.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"{'元素':<28}{'需要':>6} " + "".join(f"{n:>10}" for n, _ in THEMES))
        for i, (sel, need, label) in enumerate(TARGETS):
            row = f"{sel:<28}{need:>6} "
            for name, _ in THEMES:
                r = report[name][i]["ratio"]
                mark = "!" if (r is not None and r < need) else " "
                row += f"{(str(r) if r is not None else '—'):>9}{mark}"
            print(row)
    if bad:
        print(f"\n不合格 {len(bad)} 项：")
        for b in bad:
            print("  ✗", b)
        return 1
    print("\n全部达标（正文 ≥4.5，空态大字 ≥3.0）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
