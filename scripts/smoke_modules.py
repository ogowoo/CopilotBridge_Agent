# -*- coding: utf-8 -*-
"""浏览器冒烟：所有迁出模块的函数都绑到了 window、无 JS 错误。"""
import sys

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

NAMES = [
    "toast", "mdToHtml", "msgEl", "renderMessages", "renderStream", "renderSteps",
    "renderToolLog", "fillToolLogBody", "renderTurnFlow", "toggleTodos", "renderGoalBar",
    "updateStreamStatus", "refreshChatBarMeta", "mountTeamCard", "highlight", "applyFind",
    "trDetail", "svgIcon", "toolLabel", "stepRowEl", "loadTurns", "summarizeAnswer",
    "turnRow", "openChangeDiff", "maybeLoadChanges", "toggleChatFind", "focusHit",
    "findStep", "chatFindInput", "toggleToolLog", "stashView", "restoreView",
    "pinChatBottom", "scrollChatToEnd", "clearGoalSubagents", "showLastPrompt",
    "inferCurrentTodo", "syncTeamCard", "toggleStreamDetail", "startScope", "stopScope",
    "atUpdate", "bindAtMenu", "actionCardHtml", "renderJsonReply", "tryParseJson",
    "jsonViewBox", "codeBlockHtml", "fileCardHtml", "renderAssistantBody",
    "loadChanges", "clearChanges", "renderChangesCard", "renderStepsMaybe",
    "flushStepsIfPending", "stepView", "fillStepTools", "stepMatch", "collapseTransient",
    "nearestScroller",
]

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    pg = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)[:200]))
    pg.goto("http://127.0.0.1:8765/ui", wait_until="domcontentloaded")
    pg.wait_for_timeout(3000)
    missing = [n for n in NAMES if pg.evaluate(f"() => typeof window.{n}") != "function"]
    print("缺失:", missing or "无")
    print("JS 错误:", errors[:4] or "无")
    b.close()
    ok = not missing and not errors
    print("通过" if ok else "不达标")
    raise SystemExit(0 if ok else 1)
