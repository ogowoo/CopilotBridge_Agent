# -*- coding: utf-8 -*-
"""重启后：恢复 pyshot 任务，并确认 todo 从文件同步进来了。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
SID = "task-mvfc11u8"


def get(p, timeout=60):
    with urllib.request.urlopen(BASE + p, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def post(p, payload=None, timeout=420):
    req = urllib.request.Request(BASE + p, data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


s = get(f"/v1/agent/sessions/{SID}")["session"]
print("恢复前:", s.get("state"), "| steps:", s.get("total_steps"), "| err:", str(s.get("last_error"))[:40])
print("todos:", len(s.get("todos") or []), "条")
for t in (s.get("todos") or [])[:5]:
    print(f"  [{t.get('status','?'):<11}] {str(t.get('content'))[:52]}")

print("\n恢复任务…")
try:
    r = post(f"/v1/agent/sessions/{SID}/actions/resume")
    print("resume:", json.dumps({k: r.get(k) for k in ("state", "runner_started", "note")}, ensure_ascii=False))
except Exception as exc:
    print("resume 失败:", str(exc)[:150])

for i in range(12):
    time.sleep(10)
    s = get(f"/v1/agent/sessions/{SID}")["session"]
    rt = get(f"/v1/agent/sessions/{SID}/runtime")
    todos = s.get("todos") or rt.get("todos") or []
    done = sum(1 for t in todos if t.get("status") == "completed")
    print(f"[{(i+1)*10:>3}s] state={s.get('state'):<9} steps={s.get('total_steps'):<4} "
          f"todo {done}/{len(todos)} phase={str(rt.get('phase'))[:10]}")
    if s.get("state") in ("completed", "failed", "cancelled"):
        break
