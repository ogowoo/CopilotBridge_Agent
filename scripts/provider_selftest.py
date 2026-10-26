# -*- coding: utf-8 -*-
"""提供商连通性自检：一条命令定位"浏览器能访问、服务说不可用"。

为什么需要它：浏览器走 Windows **系统代理**（WinINET），而 Python 的 httpx 默认
只读 HTTP_PROXY/HTTPS_PROXY 环境变量、不看系统代理。于是常见现象是——浏览器能
打开 API 站点，服务却报"提供商不可用"，且 httpx 的超时/连接异常 str() 常为空串，
界面上只显示"服务暂时不可用（第 1 次）："什么都看不到。

用法:
    python scripts/provider_selftest.py                 # 自检全部已配置提供商
    python scripts/provider_selftest.py --provider deepseek
    python scripts/provider_selftest.py --direct        # 绕过代理直连测试
    python scripts/provider_selftest.py --no-proxy      # 只用直连（对比用）

输出包含：解析后的 base_url、Key 是否就绪、系统代理、以及"经代理/直连"两条
路径各自的真实结果（HTTP 状态或异常类型与消息）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from gantry import providers as P  # noqa: E402


def mask(value: str) -> str:
    if not value:
        return "(未设置)"
    return value[:4] + "…" + value[-4:] if len(value) > 10 else "***"


def load_cfg() -> dict:
    try:
        return json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"读取 config.json 失败: {exc}")
        return {}


async def probe(provider: P.ApiProvider, label: str, use_proxy: bool) -> bool:
    proxy = provider.proxy_url() if use_proxy else None
    try:
        t0 = time.monotonic()
        async with provider._client(use_proxy=use_proxy) as client:
            resp = await client.post("/chat/completions", json={
                "model": provider.models[0] if provider.models else "test",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            })
        dt = time.monotonic() - t0
        code = resp.status_code
        body = resp.text[:300].replace("\n", " ")
        ok = code < 400
        mark = "✅" if ok else "❌"
        print(f"  {label}: {mark} HTTP {code}  {dt:.1f}s  经代理={proxy or '直连'}")
        if not ok:
            print(f"      响应: {body}")
        elif code == 200:
            print("      (API 正常响应，服务不应报不可用)")
        return ok
    except Exception as exc:            # 网络/超时：把类名与消息都打出来
        mark = "❌"
        detail = str(exc).strip() or "(空消息)"
        print(f"  {label}: {mark} {type(exc).__name__}: {detail}  经代理={proxy or '直连'}")
        return False


async def main() -> int:
    ap = argparse.ArgumentParser(description="提供商 API 连通性自检")
    ap.add_argument("--provider", help="只测某个提供商（默认全部已配置的）")
    ap.add_argument("--direct", action="store_true", help="只走直连（不使用任何代理）")
    ap.add_argument("--no-proxy", action="store_true", help="只走配置/系统代理")
    args = ap.parse_args()

    cfg = load_cfg()
    provs = P.build_providers(cfg)
    sys_proxy = P.system_proxy()
    print("=== 环境 ===")
    print(f"系统代理（注册表/环境变量）: {sys_proxy or '(无)'}")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        val = os.environ.get(key)
        if val:
            print(f"  环境变量 {key} = {val}")

    names = [args.provider] if args.provider else list(provs)
    rc = 0
    for name in names:
        p = provs.get(name)
        if not p:
            print(f"\n=== {name} === 未在 config.json 的 providers 段或内置预设中")
            rc = 1
            continue
        print(f"\n=== {name} ===")
        print(f"  base_url : {p.base_url or '(未设置)'}")
        print(f"  请求地址 : {p.base_url}/chat/completions")
        print(f"  模型     : {p.models or '(未配置，--provider 时无法探测)'}")
        print(f"  Key      : env={p.api_key_env or '(未指定)'} 值={mask(p.api_key)}"
              f"  就绪={'是' if p.configured else '否'}")
        print(f"  代理策略 : 显式={p.proxy or '(无)'} 用系统代理={p.use_system_proxy}")
        if not p.configured:
            print("  ⚠ 未就绪：缺 base_url 或 API Key（先设好环境变量再测）")
            rc = 1
            continue
        if not p.models:
            print("  ⚠ 未配置 models，无法探测（补上 models 后重试）")
            rc = 1
            continue
        results = []
        if not args.direct:
            results.append(await probe(p, "经代理 ", True))
        if not args.no_proxy:
            # 直连：显式 proxy 也要绕开，便于区分"代理故障"还是"站点不可达"
            saved = p.proxy
            p.proxy = None
            p.use_system_proxy = False
            try:
                results.append(await probe(p, "直连   ", False))
            finally:
                p.proxy = saved
        if results and not any(results):
            print("  → 两条路径都失败：确认网络/防火墙，或用 curl 对比：")
            print(f"     curl.exe -sS -o NUL -w \"%{{http_code}}\" {p.base_url}/chat/completions")
            rc = 1
        elif results and results[0] is False and len(results) > 1 and results[1]:
            print("  → 代理不通、直连可用：建议在该提供商配置里加 \"use_system_proxy\": false")
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
