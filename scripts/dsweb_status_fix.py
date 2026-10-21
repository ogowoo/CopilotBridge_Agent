# -*- coding: utf-8 -*-
"""修 status 的自相矛盾：反代在线但没模型时 ready 必须 false、model_ids 必须空。

原来 `models = res["models"] or list(cfg["models"])` 会把"配置里的兜底清单"
当成"反代实际给的模型"，于是面板显示 ready=true 而 /v1/models 里什么都没有。
"""
import subprocess
from pathlib import Path

p = Path("plugins/deepseek-web/plugin.py")
s = p.read_text(encoding="utf-8")

old = '''    res = await probe(str(cfg["base_url"]), str(cfg["api_key"]))
    models = res["models"] or list(cfg["models"])'''
assert old in s, "status 探测锚点"
new = '''    res = await probe(str(cfg["base_url"]), str(cfg["api_key"]))
    # 反代**实际**给出的模型；配置里的清单只是"建议值"，不能当成已就绪
    live_models = list(res["models"])
    candidates = list(cfg["models"])'''
s = s.replace(old, new, 1)

# 用 live_models 决定 ready / model_ids；重注册也只看 live
s = s.replace('''    if res["ok"] and res["models"]:''', '''    if res["ok"] and live_models:''', 1)
s = s.replace('''                "models": list(res["models"]),''', '''                "models": list(live_models),''', 1)
s = s.replace('''    _LAST.update({"at": time.time(), "ok": res["ok"], "models": models,''',
              '''    _LAST.update({"at": time.time(), "ok": res["ok"], "models": live_models,''', 1)

old2 = '''        "ready": bool(res["ok"] and models),
        "latency_ms": res["latency_ms"],
        "error": res["error"],
        "models_in_proxy": models,
        "model_ids": [f"{prefix}/{m}" for m in models],
        "hint": ("选模型时用 dsweb/<模型名>；反代没起就先把它跑起来"
                 "（docker run -p 8000:8000 … 或 python proxy.py）"),'''
assert old2 in s, "status 返回锚点"
new2 = '''        "ready": bool(res["ok"] and live_models),
        "latency_ms": res["latency_ms"],
        "error": res["error"],
        "models_in_proxy": live_models,
        "model_ids": [f"{prefix}/{m}" for m in live_models],
        "suggested_models": candidates,
        "hint": (f"选模型时用 {prefix}/<模型名>" if (res["ok"] and live_models) else
                 ("反代在线但没有可用模型：打开它的管理面板登录 DeepSeek 账号后刷新"
                  if res["ok"] else
                  f"反代连不上（{cfg['base_url']}）：先把 docker/python 那个反代跑起来")),'''
s = s.replace(old2, new2, 1)
p.write_text(s, encoding="utf-8", newline="\n")
print("plugin: status 区分 live/候选模型")

r = subprocess.run(["python", "-m", "py_compile", "plugins/deepseek-web/plugin.py"],
                   capture_output=True, text=True)
print("编译:", "OK" if r.returncode == 0 else (r.stdout + r.stderr)[-300:])

# 让运行中的服务重新加载插件
import json
import urllib.request


def post(path):
    req = urllib.request.Request("http://127.0.0.1:8765" + path, data=b"{}",
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read() or b"{}")


info = post("/v1/plugins/deepseek-web/reload")
print("reload:", info.get("plugin", {}).get("status"))
with urllib.request.urlopen("http://127.0.0.1:8765/v1/plugins/deepseek-web/api/status",
                            timeout=30) as resp:
    st = json.loads(resp.read())
print("status:", json.dumps({k: st.get(k) for k in ("reachable", "ready", "models_in_proxy",
                                                     "model_ids", "hint")},
                            ensure_ascii=False))
