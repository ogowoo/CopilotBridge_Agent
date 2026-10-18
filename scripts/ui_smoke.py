# -*- coding: utf-8 -*-
"""WebUI 冒烟测试：用真实浏览器加载 /ui，捕获控制台错误并验证关键行为。

**为什么需要**：`webui/` 是 87KB 单文件、无构建步骤、无前端测试。以前改 UI 只能靠肉眼，
改动与缺陷都无法回归。本脚本用 Playwright 真开一次页面，把"能加载、无报错、关键交互不抛异常"
变成可执行的检查（CI 可跑）。

用法:
    python scripts/ui_smoke.py                # 默认 127.0.0.1:8765，跑完退出码 0/1
    python scripts/ui_smoke.py --keep-open    # 出错时保留浏览器窗口便于排查
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def run(url: str, keep_open: bool) -> int:
    from playwright.sync_api import sync_playwright

    problems: list[str] = []
    console_errors: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as p:
        browser = None
        for kwargs in ({"channel": "msedge"}, {}, {"channel": "chrome"}):
            try:
                browser = p.chromium.launch(headless=True, **kwargs)
                break
            except Exception as exc:  # noqa: PERF203
                last = exc
        if browser is None:
            print(f"无法启动浏览器：{last}")
            return 2

        page = browser.new_page()
        page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}")
                if m.type in ("error",) else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        # 带 token 加载：验证 SSE 会把 token 附到 query（P1 缺陷的直接回归）
        page.add_init_script("localStorage.setItem('cbp-token','SMOKE-TOKEN-123');")
        bad_responses: list[str] = []
        page.on("response", lambda r: bad_responses.append(f"{r.status} {r.url}")
                if r.status >= 400 else None)
        sse_urls: list[str] = []
        page.on("request", lambda r: sse_urls.append(r.url) if "/events" in r.url else None)

        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)          # 让首屏轮询/SSE 起一轮

        # 1) 无 JS 异常
        if page_errors:
            problems.append("页面抛异常: " + "; ".join(page_errors[:3]))
        if console_errors:
            problems.append("控制台 error: " + "; ".join(console_errors[:3]))
        # CSP 专项：收紧 script-src 后，任何漏改的内联脚本/内联事件处理器都会在这里现形
        csp_hits = [t for t in console_errors + page_errors
                    if "Content Security Policy" in t or "Refused to execute" in t
                    or "violates the following" in t]
        if csp_hits:
            problems.append("CSP 违规（还有内联脚本/处理器未迁移）: " + "; ".join(csp_hits[:3]))
        if bad_responses:
            problems.append("4xx/5xx 资源: " + "; ".join(sorted(set(bad_responses))[:4]))

        # 2) 关键全局存在（lastMessages 曾未声明 → 搜索必抛 ReferenceError）
        kind = page.evaluate("typeof lastMessages")
        if kind == "undefined":
            problems.append("lastMessages 未声明（对话搜索会抛 ReferenceError）")

        # 3) 未选会话时点搜索不应抛异常
        try:
            page.evaluate("toggleChatFind()")
        except Exception as exc:
            problems.append(f"未选会话时 toggleChatFind() 抛异常: {exc}")

        # 3b) 安全交互回归：验证事件委托确实把点击送到了 ACTIONS 里的实现
        #     先加载一个会话：#sessionView 在未选会话时是 display:none，标签/工具栏点不到
        sid_for_ui = page.evaluate(
            "async()=>{try{const d=await (await fetch('/v1/agent/sessions')).json();"
            "return (d.sessions&&d.sessions[0]&&d.sessions[0].id)||''}catch(e){return ''}}")
        if sid_for_ui:
            page.evaluate(f"load({json.dumps(sid_for_ui)})")
            page.wait_for_timeout(900)
        #     （收紧 CSP 后内联处理器全部移除，名字写错会静默失效）
        def act(name):
            return page.evaluate(f"document.querySelector('[data-act=\\'{name}\\']')!==null")

        def safe_click(selector, label):
            try:
                page.click(selector, timeout=4000)
                return True
            except Exception as exc:
                problems.append(f"点击 {label}（{selector}）失败: {str(exc)[:80]}")
                return False
        theme_before = page.evaluate("document.documentElement.dataset.theme||'dark'")
        safe_click('[data-act="toggleTheme"]', "主题切换")
        page.wait_for_timeout(300)
        theme_after = page.evaluate("document.documentElement.dataset.theme||'dark'")
        if theme_after == theme_before:
            problems.append("点击 主题切换（data-act=toggleTheme）无效：事件委托未生效")
        else:
            safe_click('[data-act="toggleTheme"]', "主题切换(还原)")   # 切回
        if not act("liveTab"):
            problems.append("找不到 data-act=liveTab（标签按钮未迁移）")
        else:
            safe_click('[data-act="liveTab"][data-arg="chat"]', "继续对话标签")
            page.wait_for_timeout(300)
            if not page.evaluate("document.getElementById('chat').classList.contains('on')||"
                                 "getComputedStyle(document.getElementById('chat')).display!=='none'"):
                problems.append("点击 继续对话 标签未切换")
        # 3c) 扩大覆盖：逐个验证「委托 → ACTIONS → 实现」链路（只用只读或纯 UI 动作）
        #     toggleRail：侧栏折叠开关
        rail_before = page.evaluate("document.documentElement.dataset.rail||''")
        safe_click('[data-act="toggleRail"]', "侧栏折叠")
        page.wait_for_timeout(300)
        if page.evaluate("document.documentElement.dataset.rail||''") == rail_before:
            problems.append("点击 侧栏折叠（toggleRail）无效果")
        else:
            safe_click('[data-act="toggleRail"]', "侧栏折叠(还原)")
        #     四个标签逐个切
        for t in ("activity", "answer", "prompt", "chat"):
            if safe_click(f'[data-act="liveTab"][data-arg="{t}"]', f"标签 {t}"):
                page.wait_for_timeout(250)
                if not page.evaluate(f"document.getElementById('tab{t.capitalize()}')"
                                     f".classList.contains('on')"):
                    problems.append(f"标签 {t} 点击后未选中")
        #     toggleChatFind：查找条显隐
        #     判据：状态**翻转**（不能用"点击后应为 flex"——本脚本前面的
        #     "未选会话时 toggleChatFind() 不抛异常" 那一步已经把查找条打开了）。
        #     另外判 inline style 而非 computedStyle：面板未激活时 computed 恒为 none。
        bar_before = page.evaluate("(()=>{const b=document.getElementById('chatFindBar');"
                                   "return b?(b.style.display||'none'):null})()")
        safe_click('[data-act="toggleChatFind"]', "对话查找")
        page.wait_for_timeout(300)
        bar_after = page.evaluate("(()=>{const b=document.getElementById('chatFindBar');"
                                  "return b?(b.style.display||'none'):null})()")
        if bar_before == bar_after:
            problems.append(f"点击 对话查找（toggleChatFind）无变化（{bar_before} → {bar_after}）")
        #     showPromptView：初始提示词是 GET，只读安全
        if safe_click('[data-act="showPromptView"]', "初始提示词"):
            page.wait_for_timeout(1200)
            # 实测它把内容塞进 #liveBody 并打开 liveMask（不是 prompt 面板）
            if not page.evaluate("document.getElementById('liveMask').classList.contains('open')"):
                problems.append("点击 初始提示词（showPromptView）未打开查看弹窗")
            elif page.evaluate("document.getElementById('liveBody').innerHTML.length") < 20:
                problems.append("点击 初始提示词后弹窗内容为空")
            else:
                safe_click('[data-act="closeLive"]', "关闭提示词弹窗")
        #     expandLive / closeLive：放大弹窗开关
        if act("expandLive"):
            # 每个面板各有一个"放大"按钮，必须先切到可见面板再点（Playwright 的 :visible）
            safe_click('[data-act="liveTab"][data-arg="chat"]', "切到对话(为放大)")
            page.wait_for_timeout(250)
            safe_click('[data-act="expandLive"]:visible', "放大")
            page.wait_for_timeout(400)
            if not page.evaluate("document.getElementById('liveMask').classList.contains('open')"):
                problems.append("点击 放大（expandLive）未打开弹窗")
            else:
                safe_click('[data-act="closeLive"]', "关闭放大弹窗")
        if act("closeManual"):
            safe_click('[data-act="openManual"]', "使用手册"); page.wait_for_timeout(400)
            opened = page.evaluate("document.getElementById('manualMask').classList.contains('open')")
            if not opened:
                problems.append("点击 使用手册（data-act=openManual）未打开弹窗")
            else:
                safe_click('[data-act="closeManual"]', "关闭手册"); page.wait_for_timeout(300)
                if page.evaluate("document.getElementById('manualMask').classList.contains('open')"):
                    problems.append("点击 关闭（data-act=closeManual）未关闭弹窗")
        # 3d) 审批横幅回归：点「暂不授权」后，同一个待授权**不得**被下一次刷新弹回来
        #     （真事：原来"暂不授权"只做 display:none，applyRuntime 每秒再调一次 → 一直弹）
        #     注意必须在**一次 evaluate 内原子完成**：真实会话的 1s runtime 刷新会带着
        #     非 waiting 的真实状态调用 showApprovalBar(null)，会把注入的合成待授权重置掉
        #     （第一次写成多步就出现过 flaky）。
        res = page.evaluate("""() => {
          const disp = () => { const b = document.getElementById('approvalBar');
            return b ? (b.style.display || 'missing') : 'missing'; };
          const clickNo = () => { const b = [...document.querySelectorAll('#approvalBar button')]
            .find(x => x.textContent.includes('暂不')); b && b.click(); };
          showApprovalBar({tool: 'smoke_probe', args_hash: '1'});
          const shown = disp();
          clickNo();
          const afterDismiss = disp();
          showApprovalBar({tool: 'smoke_probe', args_hash: '1'});   // 同一次待授权（模拟每秒刷新）
          const afterRepeat = disp();
          showApprovalBar({tool: 'smoke_probe', args_hash: '2'});   // 换参数哈希 = 新待授权
          const afterNew = disp();
          showApprovalBar(null);                                     // 收尾
          return {shown, afterDismiss, afterRepeat, afterNew};
        }""")
        if res["shown"] != "flex":
            problems.append(f"showApprovalBar 未显示横幅（授权入口失效）: {res['shown']}")
        elif res["afterDismiss"] != "none":
            problems.append(f"点「暂不授权」后横幅未隐藏: {res['afterDismiss']}")
        elif res["afterRepeat"] != "none":
            problems.append("「暂不授权」没被记住：同一待授权又被弹出来了（用户报的'一直弹'）")
        elif res["afterNew"] == "none":
            problems.append("新的待授权未重新提示（拒绝记忆范围过大）")

        # 4) SSE 必须带 token（配置 api_token 时否则恒 401）
        if sse_urls and not any("token=" in u for u in sse_urls):
            problems.append(f"SSE 未附加 token: {sse_urls[0][:120]}")

        # 5) 关键元素存在
        for sel in ("#sessions", "#moreBtn", "#workspace", "#task", "#tabChat", "#chatlog"):
            if not page.query_selector(sel):
                problems.append(f"缺少关键元素 {sel}")

        if problems:
            print("UI 冒烟失败：")
            for x in problems:
                print("  ✗", x)
            if keep_open:
                page.wait_for_timeout(120000)
        else:
            print("UI 冒烟通过：无 JS 异常、关键全局与元素齐全、SSE 带 token")
        browser.close()
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/ui")
    ap.add_argument("--keep-open", action="store_true")
    args = ap.parse_args()
    return run(args.url, args.keep_open)


if __name__ == "__main__":
    raise SystemExit(main())
