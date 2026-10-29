# -*- coding: utf-8 -*-
"""冒烟：任何状态下"可输入的输入框"只允许有一个。

用户实测（截图）：未选会话时，中间的新任务框和底部的跟进框**同时出现**，看不出该用哪个。
根因是启动时 `syncComposer()` 没跑起来（模块执行期 window.syncComposer 还没绑定，
异常被空 catch 吞掉；空状态又默认可见、不产生 style 变化，观察器也不触发）。

这里按"真正可见的 textarea 数量"判定，而不是查某个元素存在与否：
  ① 空状态：#emptyState 里的 #task 可见，#followup 不可见 → 1 个；
  ② 选中会话：反过来 → 1 个；
  ③ 切回空状态（点「＋ 新任务」）→ 又回到 1 个。
"""
import json
import sys

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

COUNT = """() => {
  const vis = (el) => {
    if (!el) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4;
  };
  const tas = [...document.querySelectorAll('textarea')];
  return {
    visible: tas.filter(vis).map(t => t.id),
    task: vis(document.getElementById('task')),
    followup: vis(document.getElementById('followup')),
    composerBox: vis(document.querySelector('.main > .composer')),
    mainClass: (document.querySelector('.main') || {}).className || '',
    emptyState: vis(document.getElementById('emptyState')),
  };
}"""

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    ctx = b.new_context(viewport={"width": 1500, "height": 1000})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
    pg.goto("http://127.0.0.1:8765/ui", wait_until="domcontentloaded")
    pg.wait_for_timeout(3000)

    empty = pg.evaluate(COUNT)
    pg.screenshot(path="data/ui-audit/composer-empty.png")

    pg.evaluate("() => { const r = document.querySelector('#sessions .sess'); if (r) r.click(); }")
    pg.wait_for_timeout(2800)
    session = pg.evaluate(COUNT)
    pg.screenshot(path="data/ui-audit/composer-session.png")

    pg.evaluate("() => { try{ window.openTask() }catch(e){} }")
    pg.wait_for_timeout(900)
    back = pg.evaluate(COUNT)

    print("① 空状态  :", json.dumps(empty, ensure_ascii=False))
    print("② 选中会话:", json.dumps(session, ensure_ascii=False))
    print("③ 回到空态:", json.dumps(back, ensure_ascii=False))

    checks = {
        "空状态只有 1 个输入框": len(empty["visible"]) == 1 and empty["visible"] == ["task"],
        "空状态底部框已收起": empty["composerBox"] is False,
        "会话里只有 1 个输入框": len(session["visible"]) == 1 and session["visible"] == ["followup"],
        "会话里底部框可见": session["composerBox"] is True,
        "回到空态又只剩 1 个": len(back["visible"]) == 1 and back["visible"] == ["task"],
        "无 JS 异常": not errs,
    }
    for k, v in checks.items():
        print(("  ✓ " if v else "  ✗ ") + k)
    if errs:
        print("JS 错误:", errs[:4])
    b.close()
    sys.exit(0 if all(checks.values()) else 1)
