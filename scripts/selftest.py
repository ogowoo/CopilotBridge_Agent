# -*- coding: utf-8 -*-
"""新版本服务自检：只读端点 + 一次真实 Copilot 发送 + 文件直读，做完清理。

不写入用户已有会话：所有动作都在一个临时会话里做，结束后删除。
"""
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
SID = "diag-selftest-1"
results = []


def call(path, payload=None, method="GET", timeout=300):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data,
                               headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body or b"{}")
            except json.JSONDecodeError:
                return resp.status, body[:200].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode("utf-8", "replace")
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"[:200]


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))


# ① 只读端点
st, root = call("/")
check("GET / 返回版本", st == 200 and root.get("version") == "10.3.0-preview.3",
      f"{st} version={root.get('version') if isinstance(root, dict) else root}")

st, h = call("/health")
check("GET /health", st == 200 and h.get("status") == "ok",
      f"{st} browser={h.get('browser_state')} ready={h.get('copilot_ready')}")

st, models = call("/v1/models")
ids = [m.get("id") for m in (models.get("data") or [])] if isinstance(models, dict) else []
check("GET /v1/models", st == 200 and len(ids) > 5,
      f"{st} 模型 {len(ids)} 个；sol-think-deeper 可用="
      f"{any(m.get('id') == 'copilot-gpt-5-6-sol-think-deeper' and m.get('available') for m in (models.get('data') or []))}")

st, up = call("/openapi.json")
check("GET /openapi.json", st == 200 and isinstance(up, dict), f"{st}")

# ② 文件直读（走 run_agent 入口拦截，不调 Copilot）
st, res = call("/v1/agent/completions",
               {"model": "copilot-auto", "session_id": SID,
                "workspace": "C:\\Tools\\Test4All\\CopilotBridge_Agent\\workspace\\wgime",
                "messages": [{"role": "user", "content": "给我看下 docs/wgime_todo_roadmap.md"}]},
               "POST", 300)
content = ""
if isinstance(res, dict):
    content = ((res.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
check("文件直读 → 文件卡片", "```cbpfile" in content, f"{st} 长度 {len(content)}")

st, sess = call(f"/v1/agent/sessions/{SID}")
steps = len(sess.get("steps") or []) if isinstance(sess, dict) else -1
check("文件直读不进 Agent 循环", steps == 0, f"步骤 {steps}")

# ③ 真实 Copilot 回合
st, res2 = call("/v1/agent/completions",
                {"model": "copilot-quick-response", "session_id": SID + "-chat",
                 "workspace": "C:\\Tools\\Test4All\\CopilotBridge_Agent\\workspace\\wgime",
                 "messages": [{"role": "user", "content": "只回一个词：ping"}]},
                "POST", 420)
txt = ""
if isinstance(res2, dict):
    txt = ((res2.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
check("真实 Copilot 发送/接收", st == 200 and bool(txt.strip()),
      f"{st} 回答={txt.strip()[:40]!r}")

# ④ 清理临时会话
for sid in (SID, SID + "-chat"):
    st, r = call(f"/v1/agent/sessions/{sid}", method="DELETE", timeout=120)
    print(f"  清理 {sid}: {st}")

bad = [x for x in results if not x[1]]
print(f"\n通过 {len(results) - len(bad)}/{len(results)}")
raise SystemExit(0 if not bad else 1)
