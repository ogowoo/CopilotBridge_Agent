# -*- coding: utf-8 -*-
"""多语言覆盖审计：在英文模式下把每个界面状态都过一遍，抓出仍然显示中文的文本。

与 `ui_i18n_audit.py` 的区别：那个脚本扫的是**静态外壳**（data-i18n 与静态 DOM），
这个脚本扫的是**运行态**——逐个打开对话框/标签/面板/菜单，按"可见性"判定，
所以能抓到动态注入、条件渲染、以及后端返回的文案。

用法:
    python scripts/i18n_coverage.py              # 打印缺失清单 + 写报告
    python scripts/i18n_coverage.py --zh         # 在中文模式跑一遍（应无缺失，作对照）
    python scripts/i18n_coverage.py --json       # 机器可读

判定规则：
- 只统计**可见**元素（display/visibility/尺寸/被祖先裁剪）
- 跳过数据容器（模型输出、步骤时间线正文、会话标题等），它们不是界面文案
- 每处缺失记录：状态名 + 选择器路径 + 文本 + 是否在按钮/标题/占位符里
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "data" / "i18n-coverage"
CJK = re.compile(r"[\u4e00-\u9fff]")

# 数据容器：内容是模型输出/文件内容/用户名，不是界面文案
SKIP_SELECTORS = [
    "#chatlog", "#steps", "#answer", "#activity", "#prompt", "#queueBox",
    ".chatlog", ".steplist", ".msg", ".sessions", "#sessions",
    "#manualBody", "#pluginsBody",
    "#gsResults", "#toasts .toast", "#bootText", "#connText",
    ".answerbody", ".promptview", "#findInput", ".attachlist",
    # 数据而非界面文案：会话标题、子代理任务描述、记忆正文与统计数字、标签
    "#title", ".sa-desc", ".sa-title", "#memList .mem-text", "#memList .mem-meta span",
]

COLLECT = r"""
(selectors) => {
  const out = [];
  const seen = new Set();
  const vis = el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const skipped = el => selectors.some(sel => {
    try { return el.closest(sel) !== null } catch (e) { return false }
  });
  const path = el => {
    const bits = [];
    let n = el;
    for (let i = 0; i < 4 && n && n.nodeType === 1; i++) {
      bits.unshift(n.tagName.toLowerCase() + (n.id ? '#' + n.id : '') +
                   (n.classList && n.classList.length ? '.' + [...n.classList].slice(0, 2).join('.') : ''));
      n = n.parentElement;
    }
    return bits.join(' > ');
  };
  const push = (text, el, kind) => {
    const t = (text || '').replace(/\s+/g, ' ').trim();
    if (!t || !/[\u4e00-\u9fff]/.test(t)) return;
    const key = kind + '|' + t;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ text: t.slice(0, 90), kind, path: path(el) });
  };
  document.querySelectorAll('body *').forEach(el => {
    if (skipped(el) || !vis(el)) return;
    for (const node of el.childNodes) {
      if (node.nodeType === 3) push(node.textContent, el, 'text');
    }
    for (const attr of ['title', 'placeholder', 'aria-label', 'alt']) {
      if (el.hasAttribute(attr)) push(el.getAttribute(attr), el, attr);
    }
  });
  return out;
}
"""


def states(page):
    """依次打开各个界面状态（每个状态跑一次采集）。"""
    seq = [
        ("首页", "() => { try { closeMaskAll() } catch (e) {} }"),
        ("新任务弹窗", "() => openTask()"),
        ("使用手册", "() => { try{closeDlg()}catch(e){}; openManual() }"),
        ("插件管理", "() => { try{closeManual()}catch(e){}; openPlugins() }"),
        ("插件面板", "() => { try{closePlugins()}catch(e){}; openPanels() }"),
        ("工作区选择", "() => { try{closePanels()}catch(e){}; openWsBrowser() }"),
        ("全局搜索", "() => { try{closeWs()}catch(e){}; toggleGlobalSearch() }"),
        ("更多菜单", "() => { try{toggleGlobalSearch()}catch(e){}; "
                    "document.querySelector('[data-act=openPlugins]'); "
                    "const b=document.querySelector('[data-act=toggleMore]'); if(b) b.click() }"),
        ("记忆面板", "() => { try{closeMore()}catch(e){}; "
                    "const b=document.getElementById('memBtn'); if(b) b.click() }"),
        ("会话更多菜单", "() => { try{closeMemory()}catch(e){}; "
                        "const b=document.querySelector('[data-act=toggleMore]'); if(b) b.click() }"),
        ("新任务填充", "() => { try{closeMore()}catch(e){}; openTask(); "
                      "const t=document.getElementById('taskInput'); if(t){t.value='检查多语言覆盖'} "
                      "const s=document.getElementById('taskStart'); if(s) s.disabled=false }"),
        ("限制条件面板", "() => { try{closeMaskAll()}catch(e){}; "
                        "const b=document.getElementById('sessModel'); if(b) b.focus() }"),
    ]
    for name, js in seq:
        try:
            page.evaluate(js)
        except Exception as exc:            # noqa: BLE001 状态打不开就跳过
            print(f"  （状态「{name}」跳过：{str(exc).splitlines()[0][:60]}）")
            continue
        page.wait_for_timeout(450)
        yield name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/ui")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    findings: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(1800)
        page.evaluate(f"setLang('{args.lang}')")
        page.wait_for_timeout(700)
        # 先选一个真实会话，让会话页/步骤/标签都渲染出来
        sid = page.evaluate(
            "async()=>{try{const r=await fetch('/v1/agent/sessions');const d=await r.json();"
            "return (d.sessions&&d.sessions[0]&&d.sessions[0].id)||''}catch(e){return ''}}")
        if sid:
            page.evaluate(f"load({json.dumps(sid)})")
            page.wait_for_timeout(1200)

        for name in states(page):
            items = page.evaluate(COLLECT, SKIP_SELECTORS)
            for it in items:
                findings.append({"state": name, **it})
            print(f"  {name}: {len(items)} 处")
        # 标签页单独采
        for tab in ("steps", "answer", "activity", "chat"):
            try:
                page.evaluate(f"liveTab('{tab}')")
                page.wait_for_timeout(500)
                items = page.evaluate(COLLECT, SKIP_SELECTORS)
                for it in items:
                    findings.append({"state": f"标签-{tab}", **it})
            except Exception:
                pass
        browser.close()

    uniq: dict[str, dict] = {}
    for f in findings:
        uniq.setdefault(f["text"], f)

    print(f"\n=== 英文模式下仍有中文的界面文案：{len(uniq)} 处 ===")
    for text, f in sorted(uniq.items(), key=lambda kv: kv[1]["state"]):
        print(f"  [{f['state']}] {f['kind']:<12} {text[:60]}")
        print(f"        {f['path']}")
    report = OUT / "report.json"
    report.write_text(json.dumps(list(uniq.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告: {report}")
    if args.json:
        print(json.dumps(list(uniq.values()), ensure_ascii=False, indent=1))
    return 0 if not uniq else 1


if __name__ == "__main__":
    raise SystemExit(main())
