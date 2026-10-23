"""tools_outlook.py — Outlook 工具（ToolRegistry 的 mixin）：未读/阅读/搜索/发送，COM（经典 Outlook）与 OWA 网页后端两条路径。

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

from .tools import ToolError, _ComWorker  # noqa: F401


class OutlookMixin:
    async def _outlook_run(self, fn):
        if not self.allow_outlook:
            raise ToolError("Outlook 工具已在 config.json 中禁用（allow_outlook=false）")

        def runner():
            try:
                import win32com.client
            except ImportError:
                raise ToolError("缺少依赖 pywin32，请运行 pip install pywin32")
            # COM 线程初始化由 _ComWorker 统一负责（CoInitializeEx + 消息泵），
            # 这里不再每调用 CoInitialize/CoUninitialize，避免破坏公寓状态。
            try:
                app = win32com.client.Dispatch("Outlook.Application")
                ns = app.GetNamespace("MAPI")
                return fn(ns)
            except ToolError:
                raise
            except Exception as exc:
                raise ToolError(self._explain_outlook_error(exc))

        # COM 弹安全确认框或 Outlook 脱机时会永久阻塞，加超时保护。
        # 注意：COM 调用**无法真正取消**——超时只表示"我们不再等"，线程里的调用仍在跑。
        # 因此这里用 shield 保护底层 future（不抛弃它）并对它的最终结果挂回调，
        # 超时绝不重试（重试 = 在第一次可能已生效的情况下再来一次）。
        timeout = max(self.outlook_timeout, 1.0)
        worker = _ComWorker.get()
        for attempt in (1, 2):
            fut = worker.submit(runner)
            try:
                return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            except ToolError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                self._mark_unknown_outcome("outlook", f"{timeout:.0f} 秒无响应")

                def _late(done, _t=timeout):
                    """迟到的结果：不改变任务走向，只把真相记下来（不静默抛弃）。"""
                    try:
                        if done.cancelled():
                            return
                        err = done.exception()
                        if err is not None:
                            print(f"[tools] 超时后的 Outlook 调用最终失败：{err}"[:300], flush=True)
                        else:
                            print("[tools] 超时后的 Outlook 调用最终成功——副作用很可能已发生，"
                                  "已禁止原样重试，请先只读核对", flush=True)
                    except Exception:
                        pass

                fut.add_done_callback(_late)
                raise ToolError(
                    f"[TOOL_TIMEOUT] 调用 Outlook 超过 {timeout:.0f} 秒无响应：Outlook 可能弹出了"
                    "安全确认框、处于脱机或卡死状态。**该调用可能仍在后台执行**，因此结果未知："
                    "已禁止原样重试同一动作；请先在本机确认 Outlook 状态，并用只读工具"
                    "（如 outlook_unread）核对是否已经发生，再决定下一步。")
            except Exception as exc:
                if attempt == 1 and self._busy_hresult(exc):
                    await asyncio.sleep(2.0)  # Outlook 繁忙瞬时拒绝：退后重试一次（此时调用确未生效）
                    continue
                raise ToolError(self._explain_outlook_error(exc))

    @staticmethod
    def _busy_hresult(exc) -> bool:
        # RPC_E_CALL_REJECTED / RPC_E_SERVERCALL_RETRYLATER：Outlook 正忙
        try:
            code = int(exc.args[0]) & 0xFFFFFFFF
        except Exception:
            return False
        return code in (0x80010001, 0x80010102)

    @staticmethod
    def _explain_outlook_error(exc) -> str:
        """把 COM 异常翻译成可操作的诊断信息——'已登录但不可用'几乎都是这几类。"""
        hresult = None
        try:
            hresult = int(exc.args[0]) & 0xFFFFFFFF
        except Exception:
            pass
        known = {
            0x80040154: "未注册 Outlook COM 类——本机未安装 Outlook/Office",
            0x80080005: "Outlook 拒绝本次自动化调用——最常见于 Outlook 与服务的运行权限不一致"
                        "（一个管理员运行、一个非管理员运行）或不同 Windows 用户；"
                        "Outlook 与服务需在相同用户、相同提权状态下运行",
            0x8000401A: "当前会话无法启动 Outlook（非交互式会话）——请在已登录用户的桌面会话中运行服务",
            0x80070005: "访问被拒绝——Outlook 安全策略或杀毒软件拦截了 COM 自动化",
            0x80010105: "与 Outlook 的 RPC 通信失败——Outlook 可能卡死，重启 Outlook 后重试",
            0x80004004: "操作被中止——Outlook 忙、有模态对话框或安全确认被取消/超时；稍后重试，"
                        "持续出现请重启 Outlook（outlook_search 已改为本地过滤，不再触发此类查询中止）",
        }
        if hresult:
            hint = known.get(hresult, "未识别的 COM 错误，请把本段发给维护者")
            return f"Outlook 不可用：{hint}（0x{hresult:08X}: {exc}）"
        return f"Outlook 不可用（未安装、未启动或未登录账号）: {exc}"

    @staticmethod
    def _mail_brief(item) -> dict[str, Any]:
        return {"entry_id": item.EntryID, "subject": str(item.Subject or ""),
                "sender": str(item.SenderName or ""), "received": str(item.ReceivedTime),
                "unread": bool(item.UnRead)}

    def _owa_or_none(self):
        """已注册 OWA 后端时返回它；同时校验开关。"""
        if self.outlook_backend is None:
            return None
        if not self.allow_outlook:
            raise ToolError("Outlook 工具已在 config.json 中禁用（allow_outlook=false）")
        return self.outlook_backend

    async def outlook_unread(self, max_count: int = 20) -> dict[str, Any]:
        limit = max(1, min(self._int_arg(max_count, 20, "max_count"), 100))
        backend = self._owa_or_none()
        if backend is not None:
            return await backend.unread(limit)

        def work(ns):
            items = ns.GetDefaultFolder(6).Items.Restrict("[Unread] = true")  # 6=olFolderInbox
            items.Sort("[ReceivedTime]", True)
            out = []
            for item in items:
                if len(out) >= limit:
                    break
                out.append(self._mail_brief(item))
            return out

        mails = await self._outlook_run(work)
        return {"unread_returned": len(mails), "items": mails}

    async def outlook_read(self, entry_id: str) -> dict[str, Any]:
        if not entry_id:
            raise ToolError("entry_id 不能为空")
        backend = self._owa_or_none()
        if backend is not None:
            # OWA 后端没有 entry_id 概念，按主题关键词定位
            return await backend.read(entry_id)

        def work(ns):
            item = ns.GetItemFromID(entry_id)
            body = str(item.Body or "")
            brief = self._mail_brief(item)
            brief["body"] = body[:20000]
            brief["truncated"] = len(body) > 20000
            return brief

        return await self._outlook_run(work)

    async def outlook_search(self, keyword: str = "", folder: str = "inbox",
                             since: str = "", max_count: int = 20) -> dict[str, Any]:
        """按关键词/日期搜索指定邮件文件夹（收件箱、已发送等）。

        不再用 MAPI DASL @SQL 查询：它在部分环境会抛 0x80004004
        (Operation aborted) 且无规律可循；改为本地迭代过滤，行为确定。
        OWA 后端注册时优先走网页版（folder/since 参数暂不支持，忽略）。
        """
        limit = max(1, min(self._int_arg(max_count, 20, "max_count"), 100))
        keyword = str(keyword or "").strip()
        backend = self._owa_or_none()
        if backend is not None:
            if not keyword:
                raise ToolError("OWA 模式下 outlook_search 需要 keyword")
            return await backend.search(keyword, limit)
        folder_key = str(folder or "inbox").strip().lower()
        folder_id = {"inbox": 6, "sent": 5, "outbox": 4, "deleted": 3, "drafts": 16}.get(folder_key)
        if folder_id is None:
            try:
                folder_id = int(folder)
            except (TypeError, ValueError):
                raise ToolError(f"未知文件夹: {folder}（可用 inbox/sent/outbox/deleted/drafts 或 olFolder 数字）")
        keyword = str(keyword or "").strip()
        since_date = None
        if str(since or "").strip():
            try:
                since_date = datetime.strptime(str(since).strip()[:10], "%Y-%m-%d").date()
            except ValueError:
                raise ToolError("since 格式应为 YYYY-MM-DD")

        def work(ns):
            items = ns.GetDefaultFolder(folder_id).Items
            items.Sort("[ReceivedTime]", True)  # 倒序：配合 since 可以提前终止
            out = []
            for item in items:
                if len(out) >= limit:
                    break
                try:
                    when = item.ReceivedTime
                    d = when.date() if when is not None else None
                except Exception:
                    d = None
                if since_date is not None:
                    if d is None or d < since_date:
                        break  # 倒序遍历：更早的条目不可能命中
                if keyword and keyword.lower() not in str(item.Subject or "").lower():
                    continue
                out.append(self._mail_brief(item))
            return out

        mails = await self._outlook_run(work)
        return {"folder": folder_key, "keyword": keyword, "since": since or None,
                "returned": len(mails), "items": mails}

    async def outlook_send(self, to: str, subject: str, body: str, cc: str = "") -> dict[str, Any]:
        if not self.allow_outlook_send:
            raise ToolError("发送邮件已在 config.json 中禁用（allow_outlook_send=false）")
        if not to or not subject:
            raise ToolError("to 和 subject 不能为空")
        # 走 _owa_or_none 而不是直接判 backend：它会一并校验 allow_outlook，
        # 与 COM 路径的权限语义保持一致（否则 allow_outlook=false +
        # allow_outlook_send=true 时仍能通过 OWA 真实发信）。
        backend = self._owa_or_none()
        if backend is not None:
            return await backend.send(to, subject, body, cc)

        def work(ns):
            app = ns.Application
            mail = app.CreateItem(0)  # 0=olMailItem
            mail.To = to
            if cc:
                mail.CC = cc
            mail.Subject = subject
            mail.Body = body
            mail.Send()
            return True

        await self._outlook_run(work)
        return {"sent": True, "to": to, "subject": subject}
