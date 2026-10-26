# -*- coding: utf-8 -*-
"""品牌与兼容层：Gantry（原 CopilotBridge Agent）。

集中处理"新名字优先、旧名字仍认"的地方，避免 585 处引用各自为政。
"""

from __future__ import annotations

import os
import re

APP_NAME = "Gantry"
APP_TITLE = "Gantry — Copilot 网页桥 + 本地 Agent 运行时"
# 兼容：旧仓库/旧包/旧标记
LEGACY_NAME = "CopilotBridge Agent"

# ── Copilot 会话标记（写在首条消息里，Copilot 用它做侧栏标题）──
MARKER_AGENT = "[GANTRY-AGENT]"
MARKER_AGENT_LEGACY = "[CBP-AGENT]"
MARKER_TURN = "[GANTRY-TURN:"
MARKER_TURN_LEGACY = "[CBP-TURN:"

# 既认新的也认旧的（正则片段，直接拼进其它正则里用）
MARKER_AGENT_ALT = r"\[(?:GANTRY|CBP)-AGENT\]"
MARKER_TURN_RE = re.compile(r"\[(?:GANTRY|CBP)-TURN:[0-9a-f]{6,32}\]")


def env(name: str, default: str | None = None) -> str | None:
    """读环境变量：`GANTRY_X` 优先，回落旧的 `CBP_X`。"""
    for prefix in ("GANTRY_", "CBP_"):
        v = os.environ.get(prefix + name)
        if v not in (None, ""):
            return v
    return default


def header_names(suffix: str) -> tuple[str, ...]:
    """请求头名：新的在前后备旧的（服务端两者都收）。"""
    return (f"X-Gantry-{suffix}", f"X-CBP-{suffix}")


def marker_prefixes(sid: str) -> list[str]:
    """同一会话的**所有**合法标记前缀（新名在前）。

    改名后新建的会话写 `[GANTRY-AGENT][sid]`，改名前的旧会话只有
    `[CBP-AGENT][sid]`；删除时的归属校验必须两套都认，否则旧会话会被判成
    "不属于当前 Session" 而永远删不掉（审核实测为必现）。
    """
    safe = str(sid or "")[:36]
    return [f"{MARKER_AGENT}[{safe}]", f"{MARKER_AGENT_LEGACY}[{safe}]"]


def marker_prefix_regex(sid: str) -> "re.Pattern[str]":
    """匹配任意一套前缀的正则（用于侧栏文本搜索）。"""
    alts = "|".join(re.escape(x) for x in marker_prefixes(sid))
    return re.compile(f"(?:{alts})")
