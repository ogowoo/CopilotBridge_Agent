# -*- coding: utf-8 -*-
"""前端源码取用（测试用）：`webui/*.js`（经典脚本）+ `webui/js/*.js`（ES 模块 + globals）。

app.js 退役后"前端逻辑"分散在 30 多个文件里，测试如果只读其中一个文件，
就会出现"改在 A 文件、断言在 B 文件，测试永远绿"的假覆盖。这里统一拼接。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# 拼接顺序 = 原 webui/app.js 里各块的先后顺序（迁移时按块切出来的）。
# 顺序有语义：老测试习惯"按函数名切一段再看里边写了什么"，顺序变了段就切空了。
ORDER = [
    "globals.js",
    "cards.js", "approvals.js", "subagents.js", "panels.js", "slash.js", "workspace.js",
    "apistate.js", "sessions.js", "codeux.js", "steps.js", "toollog.js", "find.js",
    "msgrender.js", "toast.js", "dialogs.js", "followload.js", "teamcard.js", "atmenu.js",
    "control.js", "manual.js", "composer.js", "themeboot.js", "settings.js", "version.js",
    # 下面三个是拆分过程中新造的文件（原 app.js 里没有对应块）
    "state.js", "scope.js", "main.js",
]


@lru_cache(maxsize=4)
def ui_js(root: str | None = None) -> str:
    """返回前端全部 JS 的拼接源码（先经典脚本，再按原 app.js 顺序拼 js/ 模块）。"""
    base = Path(root) if root else ROOT
    web = base / "webui"
    classic = sorted(web.glob("*.js"))
    moddir = web / "js"
    mods = {p.name: p for p in moddir.glob("*.js")} if moddir.is_dir() else {}
    ordered = [mods.pop(n) for n in ORDER if n in mods]
    ordered += [mods[n] for n in sorted(mods)]        # 新增文件按名字排在最后
    return "\n".join(p.read_text(encoding="utf-8") for p in classic + ordered)


# 样式文件的加载顺序（= 层叠顺序，见 webui/index.html）。原 style.css 已按块拆开。
CSS_ORDER = [
    "tokens.css", "shell.css", "chat.css", "dialogs.css", "themes.css",
    "overlays.css", "composer.css", "layout.css", "settings.css",
    "alerts.css", "refinements.css",
]


@lru_cache(maxsize=4)
def ui_css(root: str | None = None) -> str:
    """返回全部样式的拼接源码（顺序 = index.html 的 <link> 顺序 = 层叠顺序）。

    样式拆成 webui/css/*.css 后，测试若只读其中一个文件，同样会出现
    "改在 A 文件、断言在 B 文件"的假覆盖，所以统一走这里。
    """
    base = Path(root) if root else ROOT
    cssdir = base / "webui" / "css"
    if not cssdir.is_dir():
        return ""
    found = {q.name: q for q in cssdir.glob("*.css")}
    ordered = [found.pop(n) for n in CSS_ORDER if n in found]
    ordered += [found[n] for n in sorted(found)]
    return "\n".join(q.read_text(encoding="utf-8") for q in ordered)


def css_rule(selector: str, root: str | None = None) -> str:
    """取某个选择器（含其所有 `{…}` 块）的源码片段，找不到返回空串。

    例：`css_rule(".todolist")`。比在整份 CSS 里 `assertIn(".todolist")` 精确
    （后者会被注释或别的选择器里的同名字符串骗过）。
    """
    src = ui_css(root)
    out = []
    for m in re.finditer(re.escape(selector) + r"[^{}]*\{", src):
        depth = 1
        for k in range(m.end(), len(src)):
            if src[k] == "{":
                depth += 1
            elif src[k] == "}":
                depth -= 1
                if depth == 0:
                    out.append(src[m.start():k + 1])
                    break
    return "\n".join(out)


def fn_body(name: str, root: str | None = None) -> str:
    """取某个函数的完整函数体（大括号配平）。app.js 退役后按 `index(A)..index(B)` 切段
    很容易切空（B 可能在 A 前面），按函数名取体更稳。"""
    src = ui_js(root)
    i = src.find(f"function {name}(")
    if i < 0:
        raise AssertionError(f"找不到 function {name}（函数被改名/删除了？）")
    j = src.find("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"function {name} 的大括号不配平")
