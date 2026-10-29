# -*- coding: utf-8 -*-
"""版本号**单一来源**（single source of truth）。

规则见 `docs/VERSIONING.md`：`MAJOR.MINOR.PATCH[-channel.N]`（SemVer 2.0 子集）。

- `MAJOR`：不兼容变更（API/协议/数据库结构/工具契约），必须在 CHANGELOG 写迁移说明
- `MINOR`：向后兼容的功能新增
- `PATCH`：向后兼容的修复（含纯 UI/文档/测试）
- `-channel.N`：预发布通道，`alpha < beta < preview`；无后缀即 stable

**任何代码/脚本都不要硬编码版本号**，一律 `from .version import VERSION`
（`pyproject.toml`、README 由 `scripts/bump_version.py` 同步）。
"""

from __future__ import annotations

import re

VERSION = "11.0.0-preview.34"

_SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?$"
)
CHANNELS = ("alpha", "beta", "preview")


def parse(version: str | None = None) -> dict:
    """拆出版本组成：major/minor/patch/pre/channel/serial。"""
    v = VERSION if version is None else str(version)
    m = _SEMVER.match(v.strip())
    if not m:
        raise ValueError(f"版本号不符合 MAJOR.MINOR.PATCH[-channel.N] 规则：{v!r}")
    pre = m.group("pre") or ""
    channel, serial = "", 0
    if pre:
        parts = pre.split(".")
        channel = parts[0]
        if len(parts) > 1 and parts[1].isdigit():
            serial = int(parts[1])
    return {
        "version": m.group(0),
        "major": int(m.group("major")),
        "minor": int(m.group("minor")),
        "patch": int(m.group("patch")),
        "pre": pre,
        "channel": channel,
        "serial": serial,
        "stable": not pre,
    }


def is_stable(version: str | None = None) -> bool:
    return parse(version)["stable"]


def tag_name(version: str | None = None) -> str:
    """发布标签名（`v` + 版本号）。"""
    return "v" + (VERSION if version is None else str(version))


def base_version(version: str | None = None) -> str:
    """去掉预发布后缀的主版本：10.3.0-preview.3 → 10.3.0。"""
    p = parse(version)
    return f"{p['major']}.{p['minor']}.{p['patch']}"


def next_version(kind: str = "patch", version: str | None = None,
                 channel: str = "preview") -> str:
    """计算下一个版本号。

    kind:
      - `patch` / `minor` / `major`：升主版本号，并挂到 `-channel.1`（预发布起步）
      - `preview` / `beta` / `alpha`：保持主版本号，预发布序号 +1（换通道从 1 起）
      - `stable`：去掉预发布后缀（正式发布）
    """
    p = parse(version)
    kind = (kind or "patch").lower()
    if kind in ("patch", "minor", "major"):
        major, minor, patch = p["major"], p["minor"], p["patch"]
        if kind == "major":
            major, minor, patch = major + 1, 0, 0
        elif kind == "minor":
            minor, patch = minor + 1, 0
        else:
            patch += 1
        return f"{major}.{minor}.{patch}-{channel}.1"
    if kind == "stable":
        return f"{p['major']}.{p['minor']}.{p['patch']}"
    if kind in CHANNELS:
        if p["channel"] == kind:
            return f"{p['major']}.{p['minor']}.{p['patch']}-{kind}.{p['serial'] + 1}"
        return f"{p['major']}.{p['minor']}.{p['patch']}-{kind}.1"
    raise ValueError(f"未知的升级类型：{kind!r}（可选 patch/minor/major/preview/beta/alpha/stable）")


if __name__ == "__main__":  # pragma: no cover - 手工查看用
    import json
    import sys

    print(json.dumps(parse(sys.argv[1] if len(sys.argv) > 1 else None), ensure_ascii=False, indent=2))
