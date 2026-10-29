# -*- coding: utf-8 -*-
"""跨模块共享检查（ESM 迁移期护栏）。

背景：原 `webui/app.js` 是单体，顶层 `let`/`const`/`function` 全在一个作用域里，各块随便用。
拆成 ES 模块后，每个文件自成作用域：
  * **函数**跨模块调用 → 必须 `export` 且在 `js/main.js` 里 `window.X = X`（老调用点不改）；
  * **可变状态**（var/let/const 的数据）跨模块读写 → 必须放 `webui/js/globals.js`
    （经典脚本，顶层 var/const 既挂 window 也在全局词法环境里，模块裸名可达）。
否则严格模式下直接 ReferenceError，而且常被 try/catch 吞掉，表现为"列表空白"。

输出：
  A. 需要搬进 globals.js 的名字（状态类，跨模块裸引用）
  B. 需要 export + window 绑定的函数（跨模块裸调用但没绑定）
退出码 1 = 有 A/B 未处理。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBUI = ROOT / "webui"
JS = WEBUI / "js"

DECL = re.compile(
    r"^(?:export\s+)?(async\s+)?(?P<kind>var|let|const|function|class)\s+(?P<name>[A-Za-z_$][\w$]*)(?P<rest>[^\n]*)",
    re.M,
)

# 浏览器/语言内建，不需要声明
BUILTINS = {
    "window", "document", "console", "fetch", "location", "localStorage", "sessionStorage",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval", "requestAnimationFrame",
    "cancelAnimationFrame", "Promise", "Error", "TypeError", "Object", "Array", "String",
    "Number", "Boolean", "Math", "JSON", "Date", "Set", "Map", "RegExp", "Symbol", "Intl",
    "URL", "URLSearchParams", "Blob", "File", "FileReader", "FormData", "Image", "Audio",
    "AudioContext", "webkitAudioContext", "AbortController", "EventSource", "TextEncoder",
    "TextDecoder", "MutationObserver", "IntersectionObserver", "ResizeObserver", "CustomEvent",
    "Event", "KeyboardEvent", "MouseEvent", "DragEvent", "ClipboardEvent", "navigator",
    "history", "performance", "alert", "confirm", "prompt", "getComputedStyle", "matchMedia",
    "structuredClone", "queueMicrotask", "isNaN", "isFinite", "parseInt", "parseFloat",
    "encodeURIComponent", "decodeURIComponent", "undefined", "NaN", "Infinity",
    "applyI18n", "applyLangButton", "tr", "LANGS", "I18N", "curLang",
}


def strip_noise(src: str) -> str:
    """把注释/字符串/模板文本替换成等长空格（换行保留），只留真代码。

    为什么不用几个正则：`'…'` 的正则会把模板字面量里的引号错配，一口气吃掉后面几百行
    （teamcard.js 的 `${lastSteps…}` 就这样被吃掉，扫描器漏报过一次）。模板里的 `${…}`
    才是真代码，必须留下；模板文本本身要屏蔽。
    """
    out: list[str] = []
    i, n = 0, len(src)
    stack: list[str] = []          # "tmpl"=模板文本 / "code"=${ 里的代码 / "brace"=嵌套花括号

    def blank(text: str) -> None:
        for ch in text:
            out.append("\n" if ch == "\n" else " ")

    while i < n:
        c = src[i]
        if stack and stack[-1] == "tmpl":
            if c == "\\":
                blank(src[i:i + 2]); i += 2; continue
            if c == "`":
                stack.pop(); blank("`"); i += 1; continue
            if src.startswith("${", i):
                stack.append("code"); blank("${"); i += 2; continue
            blank(c); i += 1; continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(src[i:j]); i = j; continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(src[i:j]); i = j; continue
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                if j < n and src[j] == "\n":   # 未闭合：别吃掉整个文件
                    break
                j += 1
            j = min(j + 1, n)
            blank(src[i:j]); i = j; continue
        if c == "`":
            stack.append("tmpl"); blank("`"); i += 1; continue
        if c == "{" and stack and stack[-1] == "code":
            stack.append("brace"); out.append("{"); i += 1; continue
        if c == "}" and stack and stack[-1] == "brace":
            stack.pop(); out.append("}"); i += 1; continue
        if c == "}" and stack and stack[-1] == "code":
            stack.pop(); blank("}"); i += 1; continue
        out.append(c); i += 1
    return "".join(out)


ARROW = re.compile(r"^\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)")


def decls(src: str) -> dict[str, tuple[str, str]]:
    """name -> (kind, full first line)；`const f=(x)=>…` 记作 function。"""
    out: dict[str, tuple[str, str]] = {}
    for m in DECL.finditer(src):
        kind, name, rest = m.group("kind"), m.group("name"), m.group("rest")
        line = f"{kind} {name}{rest}".strip()
        if kind in ("var", "let", "const") and ARROW.match(rest):
            kind = "function"
        out[name] = (kind, line)
        if kind in ("var", "let", "const"):
            for part in re.split(r",(?![^\[\]{}()]*[\]}\)])", rest):
                nm = re.match(r"\s*([A-Za-z_$][\w$]*)\s*=", part)
                if nm:
                    out[nm.group(1)] = (kind, f"{kind} {nm.group(1)}{part}")
    return out


def toplevel_window_calls(masked: str, bound: set[str], imported: set[str]) -> list[tuple[int, str]]:
    """找出"模块顶层、**语句开头**就直接调用只靠 window 绑定的函数"。

    ES 模块顶层语句在 import 阶段就执行，而 `window.X = X` 是 main.js 主体（所有 import
    之后）才绑的 → 此时裸名 `X()` 抛 ReferenceError；若外面裹 try/catch 就是**静默不执行**
    （真实事故：`try{syncComposer()}catch(e){}` → 空状态出现两个输入框；更早还有 mountTeamCard）。

    判定很窄，只认这几种行首形式（宁可漏报，不要误报）：
        X(...)                try{X(...)}…           try { X(...) }
    明确**不算**的（它们之后才执行 / 只是引用）：
        window.addEventListener(…, ()=>X())      setTimeout(X, 100)      foo(X)
    顶层花括号里的调用（`if (...){ … }`）也漏报——那需要真解析，暂不做。
    """
    head = re.compile(r"^\s*(?:try\s*\{)?\s*")
    out: list[tuple[int, str]] = []
    depth = 0
    for i, line in enumerate(masked.split("\n"), 1):
        start_depth = depth
        depth += line.count("{") - line.count("}")
        if start_depth != 0:
            continue
        body = head.sub("", line)
        for name in sorted(bound):
            if name in imported:
                continue                       # 本模块 import 了，直接调用没问题
            if not re.match(re.escape(name) + r"\s*\(", body):
                continue
            if "=>" in line.split(name)[0]:
                continue                       # 箭头回调里，延迟执行
            out.append((i, name))
            break
    return out


def analyze(webui: Path | None = None) -> dict[str, list[str]]:
    """返回 {"state": [...], "func": [...], "dup": [...]}；空 = 合规。"""
    global JS, WEBUI
    if webui is not None:
        WEBUI = Path(webui)
        JS = WEBUI / "js"
    files = sorted(JS.glob("*.js"))
    raw = {p.name: p.read_text(encoding="utf-8") for p in files}
    clean = {n: strip_noise(s) for n, s in raw.items()}
    declared = {n: decls(s) for n, s in clean.items()}
    # 经典脚本 i18n.js 也会裸引用这些名字（切语言时重渲染），算消费者不算所有者
    i18n_path = WEBUI / "i18n.js"
    if i18n_path.is_file():
        clean["i18n.js(classic)"] = strip_noise(i18n_path.read_text(encoding="utf-8"))
    exported = {
        n: set(re.findall(r"export\s*\{([^}]*)\}", s, re.S)[0].replace("\n", " ").split(","))
        if re.search(r"export\s*\{", s) else set()
        for n, s in clean.items()
    }
    for n, s in clean.items():
        for m in re.finditer(r"export\s+(?:async\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)", s):
            exported[n].add(m.group(1))
    exported = {n: {x.strip().split(" as ")[-1].strip() for x in v if x.strip()} for n, v in exported.items()}
    imported = {n: set() for n in clean}
    for n, s in clean.items():
        for m in re.finditer(r"import\s*\{([^}]*)\}", s):
            for part in m.group(1).split(","):
                if part.strip():
                    imported[n].add(part.strip().split(" as ")[-1].strip())

    main_src = clean.get("main.js", "")
    bound = set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", main_src))
    globals_src = clean.get("globals.js", "")
    gnames = set(decls(globals_src)) if globals_src else set()

    state_needed: dict[str, list[str]] = {}
    func_problems: list[str] = []
    # import 语句本身不是"引用"：`import { X } from './y.js'` 里的 X 是声明
    refsrc = {n: re.sub(r"^import\s+.*$", "", s, flags=re.M) for n, s in clean.items()}
    for owner, names in sorted(declared.items()):
        if owner in ("globals.js",):
            continue
        for name, (kind, _line) in sorted(names.items()):
            if name in gnames or len(name) < 3:
                continue
            pat = re.compile(r"(?<![\w.$])" + re.escape(name) + r"(?![\w$])")
            refs: list[tuple[str, int]] = []
            for other, src in refsrc.items():
                if other == owner:
                    continue
                m = pat.search(src)
                if m:
                    refs.append((other, src[: m.start()].count("\n") + 1))
            if not refs:
                continue
            where = ", ".join(f"{o}:{ln}" for o, ln in refs[:3])
            if kind in ("function", "class"):
                if name in bound:
                    continue            # 已绑 window：任何模块裸调用都能解析
                bad = [o for o, _ln in refs if name not in imported.get(o, set())]
                if bad:
                    func_problems.append(f"{owner}:{name} ← {where}（未 export/window 绑定；这些文件也没 import：{bad[:3]}）")
            else:
                # 常量可以 export + import（只读）；可变状态必须进 globals.js。
                # 这里只报"消费者既没 import、也不在 globals.js"的名字。
                bad = [o for o, _ln in refs if name not in imported.get(o, set())]
                if bad:
                    state_needed.setdefault(name, []).append(
                        f"{owner}({kind}) ← {where}；这些文件没 import：{bad[:3]}")

    dupe_list = []
    for name in sorted(set().union(*[set(d) for d in declared.values()]) if declared else set()):
        owners = [f"{f}:{declared[f][name][0]}" for f in sorted(declared) if name in declared[f]
                  and f not in ("globals.js", "main.js")]
        if len({o.split(":")[0] for o in owners}) > 1:
            dupe_list.append(f"同名重复声明：{name} 在 {', '.join(owners)}")

    # D. `window.<名>` 指向的是**模块私有**变量（另一个模块写 window.x，本模块读 x → 两份状态）
    windowref: list[str] = []
    win_pat = re.compile(r"window\.([A-Za-z_$][\w$]*)\s*(?:=|\+\+|--)?")
    for owner, names in sorted(declared.items()):
        if owner == "globals.js":
            continue
        for name in sorted(names):
            if name in gnames or len(name) < 4:
                continue
            # 函数/类用 window.X 调是**正常姿势**（主入口把函数绑到 window，事件路径就用它）；
            # 这条规则只针对"状态变量被当成全局用 → 两份状态"。
            if names[name][0] in ("function", "class"):
                continue
            for other, src in refsrc.items():
                if other in (owner, "globals.js", "main.js"):
                    continue        # main.js 就是"把函数绑到 window"的地方，不算误用
                if win_pat.search(src) and re.search(r"\bwindow\." + re.escape(name) + r"\b", src):
                    windowref.append(f"window.{name}（声明在 {owner} 私有）被 {other} 当全局用 → 两份状态")
                    break

    # E. 模块顶层调用"只靠 window 绑定"的函数（import 阶段还没绑上）
    toplevel: list[str] = []
    for owner, src in refsrc.items():
        if owner in ("globals.js", "main.js"):
            continue
        own = set(declared.get(owner, {}))
        for line, name in toplevel_window_calls(src, bound, imported.get(owner, set()) | own):
            toplevel.append(f"{owner}:{line} 顶层调用 {name}()（只由 window 绑定，import 阶段还不存在）")

    return {
        "state": [f"{n}: " + "; ".join(w) for n, w in sorted(state_needed.items())],
        "func": func_problems,
        "toplevel": toplevel,
        "dup": dupe_list,
        "windowref": windowref,
    }


def main() -> int:
    r = analyze()
    code = 0
    if r["dup"]:
        code = 1
        print("C. 同名重复声明（两份实现会各改各的，必须只留一份）：")
        for d in r["dup"]:
            print("  " + d)
    if r["state"]:
        code = 1
        print("A. 需要搬进 webui/js/globals.js 的共享状态（%d 个）：" % len(r["state"]))
        for d in r["state"]:
            print("  " + d)
    if r["func"]:
        code = 1
        print("B. 跨模块调用但未 export/未绑 window 的函数：")
        for p in r["func"]:
            print("  " + p)
    if r["windowref"]:
        code = 1
        print("D. window.<名> 指向模块私有变量（两份状态，必须搬进 globals.js）：")
        for p in r["windowref"]:
            print("  " + p)
    if r.get("toplevel"):
        code = 1
        print("E. 模块顶层调用了「只靠 window 绑定」的函数（import 阶段 ReferenceError）：")
        for p in r["toplevel"]:
            print("  " + p)
    if not code:
        print("OK：跨模块共享都合规（状态在 globals.js，函数已 export + window 绑定）")
    return code


if __name__ == "__main__":
    sys.exit(main())
