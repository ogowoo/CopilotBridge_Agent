# -*- coding: utf-8 -*-
"""后端错误文案的多语言层（只做**展示**，协议与日志仍是中文）。

为什么需要：界面文案已经三语，但 `HTTPException` 的 detail 是中文，英文界面下通过
toast 原样显示（实测 4/6 个常见错误仍是中文）。改法不是逐个改 raise（48 处且会继续长），
而是在**全局异常处理器**里按语言翻译一次。

约定：
- 前端每个请求带 `X-CBP-Lang: zh|tw|en`（`api()` 统一注入），缺省按 zh 处理
- 这里维护"中文原文 → 英文"表；支持 `{name}` 占位符做模式匹配（如"没有名为 X 的插件"）
- 表里没有的保持中文原样（宁可见中文，也不要丢信息），并计入 `untranslated()` 供审计
"""
from __future__ import annotations

import re
from typing import Iterable

# 精确匹配：一条中文消息一个英文
EXACT: dict[str, str] = {
    "Session 不存在": "Session not found",
    "会话不存在": "Session not found",
    "Session 正在运行，不能删除": "Session is running; stop it before deleting",
    "该 Session 暂无 Copilot 会话 URL": "This session has no Copilot conversation URL yet",
    "该会话没有等待回答的问题": "This session has no pending question",
    "记忆不存在或不属于当前可见作用域": "Memory entry not found in the visible scope",
    "消息不存在": "Message not found",
    "队列条目不存在（可能已被处理）": "Queue entry not found (it may have been processed already)",
    "附件不存在": "Attachment not found",
    "浏览器未就绪": "Browser is not ready",
    "有请求/任务正在使用浏览器，请稍后再切换":
        "The browser is busy with a request or task; try switching later",
    "有请求/任务正在使用浏览器，请稍后再切换页面":
        "The browser is busy with a request or task; try switching pages later",
    "有任务正在运行，不能切换工作区": "A task is running; cannot switch workspace",
    "query 不能为空": "query must not be empty",
    "message 不能为空": "message must not be empty",
    "搜索词不能为空": "Search terms must not be empty",
    "消息不能为空": "Message must not be empty",
    "指令不能为空": "Instruction must not be empty",
    "内容不能为空": "Content must not be empty",
    "写入失败": "Write failed",
    "删除失败": "Delete failed",
    "非法文件名": "Invalid file name",
    "headless 必须是布尔值": "headless must be a boolean",
    "需要 name": "name is required",
    "未知操作": "Unknown operation",
    "至少需要一条 user 消息": "At least one user message is required",
    "content 或 model 至少提供一个": "Provide at least one of content or model",
    "需要 tool 与 args_hash（或先用 approval_pending 记录）":
        "tool and args_hash are required (or record approval_pending first)",
}

# 模式匹配：中文正则 → 英文模板（用 {1} {2} 引用捕获组）
PATTERNS: list[tuple[str, str]] = [
    (r"mode 必须是 (.+) 之一", "mode must be one of {1}"),
    (r"没有名为 (.+?) 的插件", "No plugin named {1}"),
    (r"没有名为 (.+?) 的记忆", "No memory entry named {1}"),
    (r"未知操作[:：]?\s*(.+)", "Unknown operation: {1}"),
    (r"^(.{1,40}?)不存在$", "{1} not found"),
]

_TW_EXTRA = {
    "Session 不存在": "Session 不存在",
}


def translate(message: str, lang: str = "zh") -> str:
    """把中文错误文案按语言转成展示文本；未知语言/未知文案原样返回。"""
    text = str(message or "")
    if not text or lang not in ("en", "tw"):
        return text
    if lang == "tw":
        # 繁体只做字形转换（与前端 toTraditional 一致）；这里不重复实现，交给前端
        return text
    if text in EXACT:
        return EXACT[text]
    for pattern, template in PATTERNS:
        m = re.match(pattern, text)
        if m:
            out = template
            for i, g in enumerate(m.groups(), 1):
                out = out.replace("{%d}" % i, g)
            return out
    return text


def untranslated(messages: Iterable[str], lang: str = "en") -> list[str]:
    """审计用：哪些消息在英文下仍会显示中文。"""
    bad = []
    for msg in messages:
        if not msg:
            continue
        if translate(msg, lang) == msg and re.search(r"[\u4e00-\u9fff]", msg):
            bad.append(msg)
    return bad
