# -*- coding: utf-8 -*-
"""新窗口必须"真的就绪"再交给任务：否则新标签页还在加载/SSO 跳转，
任务发出去没反应，等 180 秒超时（用户实测：开任务后窗口闪退/没反应）。

`_new_copilot_page` 里补：
1. goto 之后等 `wait_copilot_ready()`（SSO/ADFS 链路走完、输入框可用）；
2. 就绪后开一个新对话，避免继承首页残留状态；
3. 记日志（第几个窗口 + 是否就绪）。
"""
import subprocess
from pathlib import Path

p = Path("cbp/bridge.py")
s = p.read_text(encoding="utf-8")

old = '''    new_page = await ctx.new_page()
    try:
        await new_page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    except Exception as exc:
        log(f"新窗口 #{index} 打开 Copilot 失败: {exc}")
    log(f"已开第 {index} 个自动化窗口（多窗口并行）")
    return new_page'''
assert old in s, "_new_copilot_page 锚点"
new = '''    new_page = await ctx.new_page()
    try:
        await new_page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    except Exception as exc:
        log(f"新窗口 #{index} 打开 Copilot 失败: {exc}")
    # 新标签页要**等它真的可用**再交给任务：SSO/ADFS 回跳可能要几十秒，
    # 早交出去会导致"消息发出去没反应 → 180 秒超时"。
    tok = _PAGE_CTX.set(new_page)
    try:
        ready = await wait_copilot_ready(timeout=90)
        if ready:
            # 首页可能残留上次的对话状态：开一个新对话再做任务
            try:
                await new_chat()
            except Exception as exc:
                log(f"新窗口 #{index} 新建对话失败（继续用当前页）: {exc}")
        else:
            log(f"新窗口 #{index} 未就绪（可能仍在登录/跳转），任务可能失败")
    finally:
        _PAGE_CTX.reset(tok)
    log(f"已开第 {index} 个自动化窗口（多窗口并行，就绪={ready}）")
    return new_page'''
s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8", newline="\n")
print("_new_copilot_page：等就绪 + 新建对话")

r = subprocess.run(["python", "-m", "py_compile", "cbp/bridge.py"], capture_output=True, text=True)
print("编译:", "OK" if r.returncode == 0 else (r.stdout + r.stderr)[-400:])
