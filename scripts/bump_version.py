# -*- coding: utf-8 -*-
"""版本管理脚本：改号 / 更新 CHANGELOG / 打标签 / 推送（跨机器协作的统一入口）。

用法：
    python scripts/bump_version.py show
    python scripts/bump_version.py patch|minor|major|preview|beta|alpha|stable
    python scripts/bump_version.py --to 10.4.0-preview.1
    python scripts/bump_version.py preview --commit --tag --push

做四件事（幂等）：
1. 写 `gantry/version.py` 的 VERSION；
2. 同步 `pyproject.toml` 的 version；
3. 在 `CHANGELOG.md` 顶部生成新版本小节（把 `## [Unreleased]` 下的内容搬过去）；
4. 可选：git add/commit + tag v<version> + push（含 tags）。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from gantry.version import VERSION, next_version, parse, tag_name  # noqa: E402

VERSION_FILE = BASE / "gantry" / "version.py"
PYPROJECT = BASE / "pyproject.toml"
CHANGELOG = BASE / "CHANGELOG.md"
README = BASE / "README.md"


def _write(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8", newline="\n")


def set_version(new: str) -> None:
    parse(new)  # 校验格式
    s = VERSION_FILE.read_text(encoding="utf-8")
    s2 = re.sub(r'^VERSION = "[^"]+"', f'VERSION = "{new}"', s, count=1, flags=re.M)
    assert s2 != s or f'VERSION = "{new}"' in s, "version.py 里找不到 VERSION 定义"
    _write(VERSION_FILE, s2)

    t = PYPROJECT.read_text(encoding="utf-8")
    t2 = re.sub(r'^version = "[^"]+"', f'version = "{new}"', t, count=1, flags=re.M)
    _write(PYPROJECT, t2)

    if README.exists():
        r = README.read_text(encoding="utf-8")
        r2 = re.sub(r"（\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?）", f"（{new}）", r, count=1)
        # README 里也有加粗形态 **x.y.z**，以前只处理全角括号，导致版本行长期不同步
        r2 = re.sub(r"\*\*\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?\*\*", f"**{new}**", r2, count=1)
        if r2 != r:
            _write(README, r2)


def roll_changelog(new: str) -> None:
    """把 Unreleased 下的条目搬到新版本小节；没有 Unreleased 就建一个空的。"""
    today = date.today().isoformat()
    if CHANGELOG.exists():
        s = CHANGELOG.read_text(encoding="utf-8")
    else:
        s = "# 更新日志\n\n格式参考 Keep a Changelog；版本规则见 docs/VERSIONING.md。\n"
    if re.search(rf"^## \[{re.escape(new)}\]", s, re.M):
        print(f"CHANGELOG 已有 [{new}] 小节，跳过插入")
        _write(CHANGELOG, s)
        return
    m = re.search(r"^## \[Unreleased\]\s*$(.*?)(?=^## \[|\Z)", s, re.S | re.M)
    body = (m.group(1).strip() if m else "")
    head = ("## [Unreleased]\n\n### Added\n- \n\n"
            "### Changed\n- \n\n### Fixed\n- \n\n")
    section = f"## [{new}] - {today}\n\n" + (body or "*（本次发布无条目）*") + "\n\n"
    if m:
        s = s[:m.start()] + head + section + s[m.end():]
    else:
        # 插到第一个版本小节之前
        idx = re.search(r"^## \[", s, re.M)
        s = (s[:idx.start()] if idx else s.rstrip() + "\n\n") + head + section + (s[idx.start():] if idx else "")
    _write(CHANGELOG, s)


def run(cmd: list[str]) -> int:
    print("+", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(BASE))


def main() -> int:
    ap = argparse.ArgumentParser(description="CopilotBridge_Agent 版本管理")
    ap.add_argument("kind", nargs="?", default="show",
                    choices=["show", "patch", "minor", "major", "preview", "beta", "alpha", "stable"])
    ap.add_argument("--to", help="直接指定版本号（优先于 kind）")
    ap.add_argument("--set", action="store_true", help="只改文件，不动 CHANGELOG")
    ap.add_argument("--commit", action="store_true", help="git add+commit")
    ap.add_argument("--tag", action="store_true", help="git tag v<version>")
    ap.add_argument("--push", action="store_true", help="git push（含 tags）")
    ap.add_argument("--message", default="", help="commit message（默认 chore(release): vX）")
    args = ap.parse_args()

    if args.kind == "show" and not args.to:
        p = parse()
        print(f"VERSION = {VERSION}")
        print(f"  主版本 {p['major']}.{p['minor']}.{p['patch']}"
              f" | 通道 {p['channel'] or 'stable'}"
              f"{'#' + str(p['serial']) if p['serial'] else ''}")
        print("  下一个 patch  :", next_version("patch"))
        print("  下一个 minor  :", next_version("minor"))
        print("  下一个 preview:", next_version("preview"))
        print("  转正式版     :", next_version("stable"))
        return 0

    new = args.to or next_version(args.kind)
    set_version(new)
    if not args.set:
        roll_changelog(new)
    print(f"版本已更新：{VERSION} → {new}")
    print(f"  {VERSION_FILE.relative_to(BASE)}")
    print(f"  {PYPROJECT.relative_to(BASE)}")
    if not args.set:
        print(f"  {CHANGELOG.relative_to(BASE)}（新小节 [{new}]）")

    if args.commit:
        run(["git", "add", "-A"])
        run(["git", "commit", "-m", args.message or f"chore(release): {tag_name(new)}"])
    if args.tag:
        run(["git", "tag", "-a", tag_name(new), "-m", f"release {new}"])
    if args.push:
        run(["git", "push"])
        run(["git", "push", "--tags"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
