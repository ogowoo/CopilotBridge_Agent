# -*- coding: utf-8 -*-
"""识别"给我看某个本地文件"的意图，并把文件内容回给界面渲染成卡片。

不依赖 Copilot 也不依赖模型是否理解：能命中工作区里的真实文件就直接读出来。
支持文本（带行号展示）、图片（直接显示）、二进制（给类型/大小 + 下载）。
"""
import re
from pathlib import Path

TEXT_EXT = {".md", ".markdown", ".txt", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx",
            ".jsx", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
            ".cs", ".cpp", ".cc", ".c", ".h", ".hpp", ".ps1", ".psm1", ".sh", ".bat",
            ".sql", ".html", ".htm", ".css", ".scss", ".log", ".xml", ".csv", ".tsv",
            ".rs", ".go", ".java", ".kt", ".gradle", ".dockerfile", ".gitignore",
            ".env", ".props", ".targets", ".sln", ".csproj", ".editorconfig"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
BINARY_EXT = {".pdf", ".zip", ".7z", ".gz", ".tar", ".xlsx", ".xls", ".docx", ".doc",
              ".pptx", ".exe", ".dll", ".so", ".dylib", ".bin", ".ico", ".woff", ".woff2",
              ".ttf", ".otf", ".mp4", ".mp3", ".wav", ".mov"}

VERBS = ("看", "查看", "打开", "加载", "显示", "展示", "读", "输出", "贴出", "打印", "dump", "cat")
NOUNS = ("文件", "内容", "文档", "正文", "源码", "代码", "图片", "图", "截图", "md")

MAX_TEXT_BYTES = 400_000


def _candidates(text: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"[`\"'\u201c\u201d]([^`\"'\u201c\u201d\n]{2,200})[`\"'\u201c\u201d]", text):
        out.append(m.group(1).strip())
    # 尾部边界不能用 \b：中文也是 \w，`xxx.md给` 会匹配不到（实测）
    for m in re.finditer(r"(?:[A-Za-z]:)?[\w./\\\-]*[\w\-]+\.[A-Za-z0-9]{1,10}(?![A-Za-z0-9_])",
                         text):
        seg = m.group(0)
        if ("/" in seg or "\\" in seg or "." in seg) and not seg.startswith("http"):
            out.append(seg.strip())
    seen, uniq = set(), []
    for c in out:
        c = c.strip().strip("\uff0c,\u3002;\uff1b:\uff1a()\uff08\uff09")
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _resolve(root: Path, token: str) -> Path | None:
    t = token.replace("\\", "/").strip().lstrip("./")
    p = root / t
    try:
        if p.is_file():
            return p
    except OSError:
        pass
    name = Path(t).name
    if not name or "." not in name:
        return None
    try:
        hits = [x for x in root.rglob(name) if x.is_file()]
    except OSError:
        return None
    if not hits:
        return None
    hits.sort(key=lambda x: (len(str(x)), str(x)))
    return hits[0]


def _kind(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in BINARY_EXT:
        return "binary"
    return "text"


def maybe_file_view(text: str, root: Path) -> dict | None:
    """命中"给我看这个文件"就返回描述，否则 None。"""
    if not text or not str(text).strip():
        return None
    low = str(text)
    if not any(v in low for v in VERBS):
        return None
    looks_file = any(n in low for n in NOUNS) or bool(
        re.search(r"\.(?:md|txt|py|js|ts|json|ya?ml|toml|cs|cpp|h|ps1|sh|html|css|log|png|jpe?g|gif|webp|pdf|zip|xlsx?)\b",
                  low, re.I))
    if not looks_file:
        return None
    for token in _candidates(low):
        p = _resolve(root, token)
        if not p:
            continue
        kind = _kind(p)
        try:
            size = p.stat().st_size
        except OSError:
            continue
        rel = str(p.relative_to(root)).replace("\\", "/") if str(p).startswith(str(root)) else str(p)
        base = {"path": rel, "kind": kind, "size": size, "name": p.name}
        if kind != "text":
            return base
        try:
            data = p.read_bytes()[:MAX_TEXT_BYTES]
            content = data.decode("utf-8", "replace")
        except OSError:
            continue
        base.update({"content": content,
                     "lines": content.count("\n") + (0 if content.endswith("\n") else 1),
                     "truncated": size > len(data)})
        return base
    return None


def render_message(view: dict, sid: str) -> str:
    """把结果包成界面能识别成卡片的段落。"""
    path = view["path"]
    kind = view.get("kind") or "text"
    kb = max(1, round(view.get("size", 0) / 1024))
    if kind == "image":
        return (f"这是图片 `{path}`（{kb} KB）：\n\n"
                f"```cbpfile image {path}\n```\n")
    if kind == "binary":
        return (f"`{path}` 是二进制文件（{kb} KB，{Path(path).suffix.lstrip('.').upper()}）：\n\n"
                f"```cbpfile binary {path}\n```\n")
    note = "（较大，已截断）" if view.get("truncated") else ""
    return (f"这是 `{path}` 的内容（{view['lines']} 行{note and '，' + note}）：\n\n"
            f"```cbpfile text {path}\n{view['content']}\n```\n")
