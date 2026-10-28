# -*- coding: utf-8 -*-
"""端到端：@ 引用展开 + 发送按钮运行中变停止。"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8765"
SID = "diag-at-stop"


def call(path, payload=None, method="GET", timeout=600):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data,
                               headers={"Content-Type": "application/json"}, method="POST" if payload else "GET")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


# ① @ 展开：发一条带 @ 的消息，看落库的用户消息里有没有【引用文件】块
print("① @ 展开…")
d = call("/v1/agent/completions",
         {"model": "copilot-quick-response", "session_id": SID, "workspace": None,
          "agent_mode": False,
          "messages": [{"role": "user", "content": "@pyshot_todo.md 一句话说这份文件是什么"}]},
         timeout=300)
msgs = call(f"/v1/agent/sessions/{SID}").get("messages") or []
user = [m for m in msgs if m.get("role") == "user"]
expanded = any("【引用文件" in (m.get("content") or "") for m in user)
print("   消息里有引用块:", expanded)
if not expanded:
    print("   首条 user 消息前 200 字:", (user[0]["content"] if user else "")[:200])

# ② 停止按钮：发一个跑一阵的任务，运行中按钮应变 stop，点一下应取消
print("② 停止按钮…")
d2 = call("/v1/agent/completions",
          {"model": "copilot-quick-response", "session_id": SID + "-run", "workspace": None,
           "agent_mode": True,
           "messages": [{"role": "user",
                         "content": "写 12 个短文件 f1..f12.txt，每个 20 字，写完汇报"}]},
          timeout=600)

# 立即开始轮询 busy，趁在跑时看按钮
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    pg = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
    pg.goto(BASE + "/ui", wait_until="domcontentloaded")
    pg.wait_for_timeout(2000)
    pg.evaluate(f"() => {{ try {{ load('{SID}-run') }} catch(e){{}} }}")
    pg.wait_for_timeout(2500)

    stop_seen = False
    for _ in range(40):
        busy = pg.evaluate("""() => {
          const btn=document.getElementById('sendFollowup');
          return {cls:btn.className, act:btn.dataset.act, title:btn.title,
                  busy:!!(window.lastRuntime&&window.lastRuntime.busy)};
        }""")
        if busy["act"] == "stopTask" or "stop" in busy["cls"]:
            stop_seen = True
            print("   运行中按钮是停止态:", busy)
            pg.screenshot(path="data/ui-audit/send-stop.png",
                          clip=pg.evaluate("""() => { const c=document.querySelector('.composer').getBoundingClientRect();
                              return {x:Math.round(c.x),y:Math.round(c.y)-8,width:Math.round(c.width),height:70}; }"""))
            # 点它
            pg.evaluate("() => stopTask()")
            break
        time.sleep(0.6)
    print("   抓到停止态:", stop_seen)

    time.sleep(2)
    st = call(f"/v1/agent/sessions/{SID}-run")
    state = (st.get("session") or {}).get("state")
    print("   点击后状态:", state)

    b.close()

# 清理
for s in (SID, SID + "-run"):
    try:
        call(f"/v1/agent/sessions/{s}", method="DELETE", timeout=120)
    except Exception:
        pass
print("\n通过" if (expanded and stop_seen) else "\n部分失败")
