# -*- coding: utf-8 -*-
"""生成一页版「快速使用卡片」图片：简体 / 繁體 / English 各一张。

设计：单张竖版卡片，左侧 5 步流程（安装 → 登录 → 启动 → 跑任务），
右侧「界面速览 + 常用操作」，底部常见问题。用 Playwright 渲染 HTML 再截图（2x）。
用法：python scripts/make_quickstart_card.py
输出：docs/quickstart-zh.png / quickstart-zh-TW.png / quickstart-en.png
"""
from __future__ import annotations

import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs"

CSS = """
* { box-sizing: border-box; }
body { margin: 0; font-family: "Microsoft YaHei", "Segoe UI", system-ui, sans-serif;
  background: #eef2f7; color: #1b2430; }
.card { width: 1240px; padding: 34px 38px 30px; background: #eef2f7; }
.head { display: flex; align-items: baseline; gap: 14px; margin-bottom: 6px; }
.logo { width: 44px; height: 44px; border-radius: 12px; background: #0b7a5a;
  display: flex; align-items: center; justify-content: center; color: #fff;
  font-weight: 800; font-size: 19px; letter-spacing: -.5px; }
h1 { font-size: 30px; margin: 0; letter-spacing: -.4px; }
.sub { color: #5b6b7e; font-size: 15px; margin: 2px 0 22px; }
.grid { display: grid; grid-template-columns: 1.06fr .94fr; gap: 22px; align-items: start; }
.step { display: flex; gap: 12px; background: #fff; border: 1px solid #dfe6ef;
  border-radius: 14px; padding: 14px 16px; margin-bottom: 12px; }
.num { flex: none; width: 26px; height: 26px; border-radius: 50%; background: #2563eb;
  color: #fff; font-size: 14px; font-weight: 700; display: flex; align-items: center;
  justify-content: center; margin-top: 1px; }
.step h3 { margin: 0 0 6px; font-size: 16.5px; }
.step p { margin: 0 0 6px; font-size: 13.5px; color: #41505f; line-height: 1.55; }
code { font-family: "Cascadia Mono", Consolas, monospace; font-size: 12.8px;
  background: #f3f6fa; border: 1px solid #e2e9f1; border-radius: 6px;
  padding: 1px 6px; color: #16324f; }
pre { margin: 6px 0 0; background: #0f1621; color: #d7e3f4; border-radius: 10px;
  padding: 10px 12px; font-family: "Cascadia Mono", Consolas, monospace;
  font-size: 12.6px; line-height: 1.65; white-space: pre; overflow: hidden; }
pre .c { color: #7c8ba1; }
.panel { background: #fff; border: 1px solid #dfe6ef; border-radius: 14px;
  padding: 14px 16px; margin-bottom: 12px; }
.panel h3 { margin: 0 0 10px; font-size: 16px; }
table { width: 100%; border-collapse: collapse; font-size: 13.2px; }
td { padding: 6px 0; border-top: 1px solid #eef2f7; vertical-align: top; line-height: 1.5;
  word-break: keep-all; overflow-wrap: anywhere; }
tr:first-child td { border-top: 0; }
td.k { color: #2563eb; font-weight: 600; width: 42%; padding-right: 12px; }
.faq { margin-top: 4px; background: #fff; border: 1px solid #dfe6ef; border-radius: 14px;
  padding: 14px 16px; }
.faq h3 { margin: 0 0 8px; font-size: 16px; }
.faq table td { font-size: 13px; }
.faq td.k { color: #b45309; }
.foot { margin-top: 14px; color: #7a8798; font-size: 12.5px; display: flex;
  justify-content: space-between; }
.tag { display: inline-block; background: #e8f0ff; color: #2563eb; border-radius: 6px;
  padding: 1px 7px; font-size: 12px; font-weight: 600; margin-left: 6px; }
.warn { color: #b45309; }
"""

# ── 文案：三语 ────────────────────────────────────────────────
TEXT = {
    "zh": {
        "title": "CopilotBridge Agent · 快速使用",
        "sub": "Windows + Edge + Python 3.11+ ｜ 从零到第一个任务约 5 分钟",
        "steps": [
            ("安装（4 条命令）", [
                ("", ["git clone https://github.com/ogowoo/CopilotBridge_Agent.git",
                      "cd CopilotBridge_Agent",
                      "python install.py            # 装依赖 + Playwright 浏览器",
                      "copy config.example.json config.json"]),
            ]),
            ("登录 Copilot（只做一次）", [
                ("", ["python scripts/login.py"]),
                ("p", "弹出的 Edge 里登录 M365 账号 —— 必须是有订阅的账号；"
                      "看到推荐账号就点「使用其他账户」。登录态存 EdgeProfile/，之后不用再登。"),
            ]),
            ("启动", [
                ("", ["python start.py"]),
                ("p", "打开 http://127.0.0.1:8765/ui ；顶部显示「Copilot 就绪」即可用。"),
            ]),
            ("跑第一个任务", [
                ("p", "① 写任务：分析 workspace 里的项目并生成 TODO　② 选模型（copilot-auto 够用）"
                      "　③ 选工作目录　④ 选权限（默认「工作区内修改」）　⑤ 点「Agent 执行」→「开始」"),
            ]),
        ],
        "ui_title": "界面速览",
        "ui": [("实时流", "对话 + 工具结果 + 状态行（completed · 步数 · Token）"),
               ("步骤时间线", "每步的工具/参数/结果，可展开 JSON、筛选、导出"),
               ("待办 / 子代理", "实时流下方两条，点横条展开；待办随进度自动勾选"),
               ("左下角 ⚙", "设置：主题/语言、执行参数、权限档位、浏览器、危险区")],
        "ops_title": "常用操作",
        "ops": [("暂停 / 继续", "任务条「暂停」→「恢复」从检查点续跑"),
                ("边跑边改要求", "输入框直接发（Ctrl+J 立即生效，不必等这轮跑完）"),
                ("换模型", "任务条右侧下拉；限流挂起时用挂起卡片里的「换模型继续」"),
                ("看它改了什么", "步骤时间线 →「查看结果」；更多菜单可「回滚任务改动」"),
                ("删任务 / 重启", "More → 删除任务；设置页 → 危险区 → 重启服务")],
        "faq_title": "常见问题",
        "faq": [("Copilot 未就绪", "点「重试浏览器」；仍不行重跑 python scripts/login.py"),
                ("卡在「等待服务」", "网页限流：自动挂起、到点继续；也可换提供商模型立刻继续"),
                ("端口被占用", "改 config.json 的 server.port 再启动"),
                ("想用 DeepSeek/Kimi", "$env:DEEPSEEK_API_KEY=\"sk-...\" 后选 deepseek/deepseek-chat")],
        "foot_left": "详细说明：README.md ｜ docs/QUICKSTART.md ｜ docs/TECHNICAL.md",
        "foot_right": "权限三档：仅可查看 / 工作区内修改（默认） / 完全权限",
    },
    "zh-TW": {
        "title": "CopilotBridge Agent · 快速上手",
        "sub": "Windows + Edge + Python 3.11+ ｜ 從零到第一個任務約 5 分鐘",
        "steps": [
            ("安裝（4 條指令）", [
                ("", ["git clone https://github.com/ogowoo/CopilotBridge_Agent.git",
                      "cd CopilotBridge_Agent",
                      "python install.py            # 安裝相依套件 + Playwright 瀏覽器",
                      "copy config.example.json config.json"]),
            ]),
            ("登入 Copilot（只需一次）", [
                ("", ["python scripts/login.py"]),
                ("p", "在彈出的 Edge 中登入 M365 帳號 —— 必須是有訂閱的帳號；"
                      "看到建議帳號就點「使用其他帳戶」。登入狀態存於 EdgeProfile/，之後不必再登。"),
            ]),
            ("啟動", [
                ("", ["python start.py"]),
                ("p", "開啟 http://127.0.0.1:8765/ui ；頂端顯示「Copilot 就緒」即可使用。"),
            ]),
            ("跑第一個任務", [
                ("p", "① 寫任務：分析 workspace 裡的專案並產生 TODO　② 選模型（copilot-auto 夠用）"
                      "　③ 選工作目錄　④ 選權限（預設「工作區內修改」）　⑤ 點「Agent 執行」→「開始」"),
            ]),
        ],
        "ui_title": "介面速覽",
        "ui": [("即時流", "對話 + 工具結果 + 狀態列（completed · 步數 · Token）"),
               ("步驟時間軸", "每步的工具/參數/結果，可展開 JSON、篩選、匯出"),
               ("待辦 / 子代理", "即時流下方兩列，點橫條展開；待辦會隨進度自動勾選"),
               ("左下角 ⚙", "設定：主題/語言、執行參數、權限級別、瀏覽器、危險區")],
        "ops_title": "常用操作",
        "ops": [("暫停 / 繼續", "任務列「暫停」→「恢復」從檢查點續跑"),
                ("邊跑邊改需求", "輸入框直接送（Ctrl+J 立即生效，不必等這輪跑完）"),
                ("換模型", "任務列右側下拉；限流掛起時用掛起卡片裡的「換模型繼續」"),
                ("看它改了什麼", "步驟時間軸 →「查看結果」；更多選單可「回滾任務改動」"),
                ("刪任務 / 重啟", "More → 刪除任務；設定頁 → 危險區 → 重啟服務")],
        "faq_title": "常見問題",
        "faq": [("Copilot 未就緒", "點「重試瀏覽器」；仍不行請重跑 python scripts/login.py"),
                ("卡在「等待服務」", "網頁限流：自動掛起、到點繼續；也可換提供商模型立刻繼續"),
                ("連接埠被佔用", "改 config.json 的 server.port 再啟動"),
                ("想用 DeepSeek/Kimi", "$env:DEEPSEEK_API_KEY=\"sk-...\" 後選 deepseek/deepseek-chat")],
        "foot_left": "詳細說明：README.md ｜ docs/QUICKSTART.md ｜ docs/TECHNICAL.md",
        "foot_right": "權限三級：僅可檢視 / 工作區內修改（預設） / 完全權限",
    },
    "en": {
        "title": "CopilotBridge Agent · Quick start",
        "sub": "Windows + Edge + Python 3.11+ ｜ zero to first task in about 5 minutes",
        "steps": [
            ("Install (4 commands)", [
                ("", ["git clone https://github.com/ogowoo/CopilotBridge_Agent.git",
                      "cd CopilotBridge_Agent",
                      "python install.py            # deps + Playwright browser",
                      "copy config.example.json config.json"]),
            ]),
            ("Sign in to Copilot (once)", [
                ("", ["python scripts/login.py"]),
                ("p", "Sign in to your M365 account in the Edge window — it must be a subscribed "
                      "account; if Windows suggests another one, pick \"Use another account\". "
                      "The session is stored in EdgeProfile/ and reused afterwards."),
            ]),
            ("Start", [
                ("", ["python start.py"]),
                ("p", "Open http://127.0.0.1:8765/ui ; when the top bar says \"Copilot ready\" you are good."),
            ]),
            ("Run your first task", [
                ("p", "1) Describe the task (e.g. analyse the project under workspace and produce a TODO)  "
                      "2) pick a model (copilot-auto is fine)  3) pick the working directory  "
                      "4) pick permissions (default \"Modify workspace\")  5) click Agent run → Start"),
            ]),
        ],
        "ui_title": "What you see",
        "ui": [("Live stream", "chat + tool results + a status line (completed · steps · tokens)"),
               ("Step timeline", "every step's tool/args/result; expandable JSON, filters, export"),
               ("Todos / Subagents", "two rows under the stream; click a row to expand; todos tick themselves"),
               ("Bottom-left ⚙", "settings: theme/language, execution, permission level, browser, danger zone")],
        "ops_title": "Everyday actions",
        "ops": [("Pause / resume", "task bar \"Pause\" → \"Resume\" continues from the checkpoint"),
                ("Change your mind", "just type in the composer (Ctrl+J injects immediately)"),
                ("Switch model", "model dropdown on the task bar; when rate-limited use \"Switch model & continue\""),
                ("See what changed", "step timeline → \"View result\"; More menu → roll back task changes"),
                ("Delete / restart", "More → Delete task; Settings → Danger zone → Restart service")],
        "faq_title": "Troubleshooting",
        "faq": [("Copilot not ready", "click \"Retry browser\"; if it persists re-run python scripts/login.py"),
                ("Stuck at \"waiting\"", "web rate limit: it suspends and resumes automatically, "
                                        "or pick a provider model to continue now"),
                ("Port already in use", "change server.port in config.json and restart"),
                ("Use DeepSeek/Kimi", "set $env:DEEPSEEK_API_KEY=\"sk-...\" then pick deepseek/deepseek-chat")],
        "foot_left": "Details: README.md ｜ docs/QUICKSTART.md ｜ docs/TECHNICAL.md",
        "foot_right": "Permission levels: View only / Modify workspace (default) / Full access",
    },
}


def esc(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def html(lang: str) -> str:
    t = TEXT[lang]
    steps = []
    for idx, (title, blocks) in enumerate(t["steps"], 1):
        inner = []
        for kind, payload in blocks:
            if kind == "p":
                inner.append(f"<p>{esc(payload)}</p>")
            else:
                cmds = "\n".join(
                    f'<span class="c">{esc(c.split("  # ")[0])}</span>' if "  # " in c
                    else esc(c) for c in payload)
                # 注释用暗色：把 # 之后的部分单独着色
                lines = []
                for c in payload:
                    if "  # " in c:
                        cmd, note = c.split("  # ", 1)
                        lines.append(f'{esc(cmd)}<span class="c">  # {esc(note)}</span>')
                    else:
                        lines.append(esc(c))
                inner.append("<pre>" + "\n".join(lines) + "</pre>")
        steps.append(f'<div class="step"><div class="num">{idx}</div><div style="flex:1 1 auto">'
                     f'<h3>{esc(title)}</h3>{"".join(inner)}</div></div>')

    def table(rows, cls=""):
        body = "".join(f'<tr><td class="k">{esc(k)}</td><td>{esc(v)}</td></tr>' for k, v in rows)
        return f'<table class="{cls}">{body}</table>'

    return f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<style>{CSS}</style></head><body><div class="card">
<div class="head"><div class="logo">CB</div><h1>{esc(t['title'])}</h1></div>
<div class="sub">{esc(t['sub'])}</div>
<div class="grid">
  <div>{''.join(steps)}</div>
  <div>
    <div class="panel"><h3>🗂 {esc(t['ui_title'])}</h3>{table(t['ui'])}</div>
    <div class="panel"><h3>⌨ {esc(t['ops_title'])}</h3>{table(t['ops'])}</div>
  </div>
</div>
<div class="faq"><h3>🩺 {esc(t['faq_title'])}</h3>{table(t['faq'], 'faqtable')}</div>
<div class="foot"><span>{esc(t['foot_left'])}</span><span>{esc(t['foot_right'])}</span></div>
</div></body></html>"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        page = browser.new_page(viewport={"width": 1240, "height": 900}, device_scale_factor=2)
        for lang in ("zh", "zh-TW", "en"):
            src = html(lang)
            tmp = OUT / f".quickstart-{lang}.html"
            tmp.write_text(src, encoding="utf-8")
            page.goto(tmp.as_uri(), wait_until="load")
            page.wait_for_timeout(250)
            el = page.query_selector(".card")
            png = OUT / f"quickstart-{lang}.png"
            el.screenshot(path=str(png))
            tmp.unlink(missing_ok=True)
            made.append((png.name, png.stat().st_size))
        browser.close()
    for name, size in made:
        print(f"  {name}: {size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
