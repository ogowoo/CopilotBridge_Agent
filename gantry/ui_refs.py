# -*- coding: utf-8 -*-
"""前端 id 引用完整性：JS 里 `getElementById('x')`/`querySelector('#x')` 引用的
元素必须真的存在——index.html 的 `id="x"`，或 app.js 动态生成（innerHTML 模板里的
`id="x"`、`el.id='x'`）。

为什么需要：自升级代理曾加了 `saveSettings` 里读 `setTeamScopeHeartbeat` 的逻辑，
却**忘了把这个复选框加进设置页**就宣布完成（task-mvs4un2x，用户实测）。
py_compile + unittest 都覆盖不了"元素存不存在"——这正是这条静态检查要拦的。

`ALLOWED_MISSING`：有意防御的引用（`getElementById('sid')||{}` 这种兼容旧 DOM 的
兜底），不许静默放行新缺漏——要加白名单必须写明理由。
"""
from __future__ import annotations

import re
from pathlib import Path

# 有意防御的引用（元素可能不存在，代码里已做 ||{} 兜底；不许滥用）
ALLOWED_MISSING: frozenset[str] = frozenset({
    "sid",   # 旧版新任务表单的会话 id 输入框；代码用 (getElementById('sid')||{}).value 兜底
})

_ID_DEF_HTML = re.compile(r"\bid=\"([A-Za-z0-9_-]+)\"|\bid='([A-Za-z0-9_-]+)'")
_ID_DEF_JS_ASSIGN = re.compile(r"\.id\s*=\s*['\"]([A-Za-z0-9_-]+)['\"]")
_ID_DEF_JS_HTML = re.compile(r"\bid=\"([A-Za-z0-9_-]+)\"|\bid='([A-Za-z0-9_-]+)'")
_ID_REF_GET = re.compile(r"getElementById\((?:'|\")([A-Za-z0-9_-]+)(?:'|\")\)")
_ID_REF_QS = re.compile(r"querySelector(?:All)?\((?:'|\")#([A-Za-z0-9_-]+)(?:'|\")\)")


def check_ui_id_refs(root: Path) -> dict:
    """扫描 webui/ 下 index.html 与 app.js，返回引用完整性结果。"""
    root = Path(root)
    html_path = root / "webui" / "index.html"
    js_path = root / "webui" / "app.js"
    if not html_path.is_file() or not js_path.is_file():
        return {"skipped": True, "reason": f"缺少 {html_path} 或 {js_path}"}

    html = html_path.read_text(encoding="utf-8")
    js = js_path.read_text(encoding="utf-8")

    defined: set[str] = set()
    for m in _ID_DEF_HTML.finditer(html):
        defined.add(m.group(1) or m.group(2))
    for m in _ID_DEF_JS_HTML.finditer(js):
        defined.add(m.group(1) or m.group(2))
    defined |= set(_ID_DEF_JS_ASSIGN.findall(js))

    refs: dict[str, list[int]] = {}
    for m in _ID_REF_GET.finditer(js):
        line = js[:m.start()].count("\n") + 1
        refs.setdefault(m.group(1), []).append(line)
    for m in _ID_REF_QS.finditer(js):
        line = js[:m.start()].count("\n") + 1
        refs.setdefault(m.group(1), []).append(line)

    missing = []
    for name in sorted(refs):
        if name not in defined and name not in ALLOWED_MISSING:
            missing.append({"id": name, "lines": refs[name][:5]})

    return {
        "skipped": False,
        "defined": len(defined),
        "refs": len(refs),
        "missing": missing,
        "allowed_missing": sorted(ALLOWED_MISSING),
    }
