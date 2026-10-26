"""tools_memory.py — 长期记忆工具（ToolRegistry 的 mixin）：写入/检索/遗忘跨会话事实与偏好。

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

from .tools import ToolError  # noqa: F401


class MemoryMixin:
    # ── 长期记忆 ────────────────────────────────────────────────────
    def _mem_scopes(self, scope: str = "all") -> list[str]:
        key = str(scope or "all").strip().lower()
        if key.startswith("glob"):
            return ["global"]
        if key.startswith("proj") or key.startswith("work"):
            return [self.memory_scope]
        return ["global", self.memory_scope]

    def _mem(self):
        if self.memory is None:
            raise ToolError("长期记忆未启用（服务未注入 MemoryStore）")
        return self.memory

    async def memory_write(self, text: str, kind: str = "fact", tags: list | None = None,
                           pinned: bool = False, scope: str = "global") -> dict[str, Any]:
        """记录一条长期记忆：跨会话累积的事实、偏好、结论与踩坑。"""
        if not self.allow_memory_write:
            raise ToolError("记忆写入已在 config.json 中禁用（allow_memory_write=false）")
        if isinstance(tags, str):
            tags = [t.strip() for t in re.split(r"[,，]", tags) if t.strip()]
        target = "global" if str(scope or "global").lower().startswith("glob") else self.memory_scope
        try:
            r = self._mem().remember(target, text, kind=kind, tags=list(tags or []),
                                     source_session=self.session_id(),
                                     pinned=bool(pinned))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        r["note"] = "已写入长期记忆：后续相关任务会自动检索到（无需重启）"
        return r

    async def memory_search(self, query: str = "", limit: int = 8, scope: str = "all") -> dict[str, Any]:
        """检索长期记忆。query 为空时返回最近更新的若干条。"""
        limit = max(1, min(self._int_arg(limit, 8, "limit"), 30))
        scopes = self._mem_scopes(scope)
        store = self._mem()
        items = (store.search(query, scopes, limit=limit) if str(query or "").strip()
                 else store.list(scopes, limit=limit))
        return {"query": query, "checked_scopes": scopes, "returned": len(items),
                "items": [{"id": m["id"], "kind": m["kind"], "text": m["text"],
                           "tags": m.get("tags") or [], "pinned": bool(m["pinned"]),
                           "use_count": m["use_count"], "scope": m["scope"]} for m in items]}

    async def memory_forget(self, id: int) -> dict[str, Any]:
        """删除一条记忆（只允许删当前任务可见作用域内的）。"""
        mid = self._int_arg(id, 0, "id")
        if mid <= 0:
            raise ToolError("id 必须是正整数")
        ok = self._mem().forget(mid, scopes=["global", self.memory_scope])
        if not ok:
            raise ToolError(f"未找到可删除的记忆 id={mid}（可能属于别的工作区）")
        return {"forgot": mid}
