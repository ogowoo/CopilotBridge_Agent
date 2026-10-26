"""tools_skills.py — 技能库工具（ToolRegistry 的 mixin）：列出/读取/新建目录式技能（SKILL.md）。

从 tools.py 拆出（第四批解耦）：方法体原样搬入 mixin，运行时行为不变。
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import inspect
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import editmatch

from .tools import ToolError, parse_skill_md  # noqa: F401


class SkillsMixin:
    def _skill_dirs(self) -> list[tuple[str, Path]]:
        out = []
        if self.global_root:
            d = self.global_root / "skills"
            if d.is_dir():
                out.append(("global", d))
        d = self.root / "skills"
        if d.is_dir():
            out.append(("project", d))
        return out

    def skill_list(self) -> list[dict[str, Any]]:
        """两级技能清单：项目同名技能覆盖全局。"""
        merged: dict[str, dict[str, Any]] = {}
        for scope, d in self._skill_dirs():
            try:
                for sub in sorted(d.iterdir()):
                    if not sub.is_dir():
                        continue
                    md = sub / "SKILL.md"
                    if not md.is_file():
                        continue
                    text = md.read_text(encoding="utf-8", errors="replace")
                    name, desc = parse_skill_md(sub.name, text)
                    merged[name] = {"name": name, "description": desc, "scope": scope,
                                    "path": f"skills/{sub.name}/SKILL.md", "abs": str(md)}
            except Exception:
                continue
        return list(merged.values())[:30]

    async def skill_read(self, name: str) -> dict[str, Any]:
        """读取技能全文。全局技能位于工作区之外，read_text 受沙箱限制读不到，
        必须用本工具读取。"""
        if not name:
            raise ToolError("name 不能为空")
        key = str(name).strip().lower()
        for skill in self.skill_list():
            dirname = Path(skill["path"]).parent.name.lower()
            if skill["name"].lower() == key or dirname == key:
                p = Path(skill["abs"])
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:
                    raise ToolError(f"读取技能失败: {exc}") from exc
                cap = 12000
                return {"name": skill["name"], "scope": skill["scope"], "path": skill["path"],
                        "content": text if len(text) <= cap
                        else text[:cap] + f"\n…[共 {len(text)} 字符，已截断]"}
        raise ToolError(f"未找到技能: {name}（可用 skill_list 查看可用技能）")

    async def skill_create(self, name: str, content: str, description: str = "",
                           scope: str = "project", overwrite: bool = False) -> dict[str, Any]:
        """把任务中验证有效的做法沉淀为技能（SKILL.md）。

        scope=project（默认）写入工作区 skills/，只影响该工作区的任务；
        scope=global 写入服务安装目录 skills/，对所有工作区生效，因此要求
        allow_self_upgrade（与修改服务自身代码同级权限）。

        约定：目录名 = 技能名（小写短横线），SKILL.md 用 YAML frontmatter
        声明 name/description，正文写"什么时候用、怎么做、坑在哪"。
        """
        if not self.allow_skill_write:
            raise ToolError("技能写入已在 config.json 中禁用（allow_skill_write=false）")
        slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
        if not slug:
            raise ToolError("技能名不合法：请用小写字母/数字/短横线，如 webui-theme-change")
        if not content or not str(content).strip():
            raise ToolError("content 不能为空")
        if len(content) > self.max_write:
            raise ToolError("技能内容超过写入上限")
        scope_key = str(scope or "project").strip().lower()
        if scope_key in ("project", "workspace"):
            base = self.root / "skills"
        elif scope_key in ("global", "service"):
            if not self.allow_self_upgrade:
                raise ToolError("写入全局技能需要开启 allow_self_upgrade（影响所有工作区）")
            if not self.global_root:
                raise ToolError("未设置服务安装目录，无法写全局技能")
            base = self.global_root / "skills"
        else:
            raise ToolError("scope 只能是 project 或 global")

        target = (base / slug / "SKILL.md").resolve()
        if not overwrite and target.is_file():
            raise ToolError(f"技能已存在: {slug}（要覆盖请带 overwrite=true，或先用 skill_read 看现状）")

        desc_line = " ".join(str(description or "").split())[:120]
        if not desc_line:
            # 没给描述就从正文提炼：跳过标题/字段标签/过短行，优先取足够长的句子，
            # 避免把「什么时候用」这类小标题当成技能简介。
            candidates = []
            for line in str(content).splitlines():
                s = " ".join(line.split())
                if not s or s.startswith("#") or s.startswith("---") or s.startswith(">"):
                    continue
                if s.endswith(":") or s.endswith("："):
                    continue
                candidates.append(s)
            for cand in candidates:
                if len(cand) >= 8:
                    desc_line = cand[:120]
                    break
            if not desc_line and candidates:
                desc_line = " ".join(candidates[:2])[:120]
        body = str(content).strip()
        text = f"---\nname: {slug}\ndescription: {desc_line}\n---\n\n{body}\n"

        self._snap("skill_create", target.relative_to(self.root).as_posix()
                   if scope_key == "project" else target.relative_to(self.global_root).as_posix(),
                   target)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = self._hash(target)
        with target.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
        parsed_name, parsed_desc = parse_skill_md(slug, text)
        return {"created": True, "name": parsed_name, "slug": slug, "scope": scope_key,
                "path": str(target), "description": parsed_desc, "chars": len(text),
                "note": "技能即时可用（下次 skill_list/任务上下文即出现），无需重启服务",
                "file_event": {"tool": "skill_create", "path": str(target),
                               "before_hash": before, "after_hash": self._hash(target), "result": "ok"}}
