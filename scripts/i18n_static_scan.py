# -*- coding: utf-8 -*-
"""不变量：index.html 里的中文文本/属性都要有英文词条（含隐藏弹窗）。

真事：删除结果弹窗的「删除结果」「复制结果 JSON」「没有远程删除明细。」等文案只在
弹窗**打开时**才可见，运行态审计（i18n_coverage）不打开它就永远看不到 → 英文界面
里冒出中文。这里静态扫 index.html 的所有文本节点与属性，要求 i18n.js 有对应键。
"""
import re
import sys
from pathlib import Path

# 从本文件位置推导安装目录：**绝不能写死绝对路径**——写死的那版在别人机器上
# 直接 FileNotFoundError，导致「静态文案都有英文键」这个不变量在任何非作者机器上
# 必然失败（实测：同步后全套测试 1 例失败，错误里还带着作者机器的路径）。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def check() -> list[str]:
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    i18n = (ROOT / "webui" / "i18n.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'"([^"\n]{1,120})"\s*:', i18n))
    cjk = re.compile(r"[\u4e00-\u9fff]")
    # 只扫 <body> 里的可见文案：去掉 <script>/<style> 与 HTML 注释
    body = html.split("<body", 1)[-1]
    body = re.sub(r"<script[\s\S]*?</script>", " ", body)
    body = re.sub(r"<style[\s\S]*?</style>", " ", body)
    body = re.sub(r"<!--[\s\S]*?-->", " ", body)
    # 先去掉标记为"数据/动态"的子树（与运行态审计同一套 data-i18n-skip 语义）
    for _ in range(6):        # 嵌套标记反复剔除几轮
        body = re.sub(r"<([a-zA-Z][\w-]*)\b[^>]*data-i18n-skip[^>]*>[\s\S]*?</\1>", " ", body)
    # 文本节点
    text = re.sub(r"<[^>]+>", "\n", body)
    bad = []
    for line in text.split("\n"):
        t = line.strip()
        if not t or not cjk.search(t):
            continue
        if t in keys:
            continue
        bad.append(t[:40])
    # 属性
    body_attr = re.sub(r"<[^>]*data-i18n-skip[^>]*>", " ", body)
    for m in re.finditer(r'(title|placeholder|aria-label|alt|value)="([^"]*)"', body_attr):
        val = m.group(2)
        if cjk.search(val) and val not in keys and "{" not in val:
            bad.append(f'{m.group(1)}="{val[:30]}"')
    return bad


if __name__ == "__main__":
    missing = check()
    print(f"index.html 里没有英文词条的中文：{len(missing)} 处")
    for m in missing:
        print("  ", m)
    sys.exit(1 if missing else 0)
