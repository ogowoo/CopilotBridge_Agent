# -*- coding: utf-8 -*-
"""服务生命周期：让 HTTP 层能"优雅停机"（WebUI 的「终止服务/退出」按钮）。

为什么需要单独一层：
- `start.py` 用 `uvicorn.run("app:app")`，server 对象藏在 uvicorn 内部，路由层拿不到；
- 直接 `os.kill(SIGINT)` 在 Windows 上会走 TerminateProcess（硬杀），浏览器进程与
  Playwright 连接都来不及收尾。

做法：入口（start.py）显式构造 `uvicorn.Server` 并把 `should_exit` 的置位函数注入进来
（`set_shutdown_hook`）。拿不到注入时退回信号，并始终有"硬退出兜底"——
按钮必须真的能停掉服务，哪怕优雅停机卡住。
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

log = logging.getLogger("cbp.lifecycle")

_hook = None                 # 由入口注入：设置 uvicorn Server.should_exit
_force_seconds = 8.0         # 优雅停机的宽限时间，超时硬退出
_shutting_down = False


def set_shutdown_hook(fn) -> None:
    """注入优雅停机实现（入口调用；测试里也可注入桩）。"""
    global _hook
    _hook = fn


def shutdown_pending() -> bool:
    return _shutting_down


def _force_exit_later(seconds: float) -> None:
    """宽限期内没退干净就硬退出：按钮不能出现"点了没反应"。"""
    def killer():
        time.sleep(max(0.5, float(seconds)))
        log.warning("优雅停机超过 %.1fs 仍未退出，执行硬退出", seconds)
        os._exit(0)
    threading.Thread(target=killer, name="cbp-force-exit", daemon=True).start()


def request_shutdown(force_after: float | None = None) -> str:
    """请求停机，返回采用的方式：uvicorn / signal / already。

    幂等：重复调用只生效一次（按钮可能被连点）。
    """
    global _shutting_down
    if _shutting_down:
        return "already"
    _shutting_down = True
    _force_exit_later(_force_seconds if force_after is None else force_after)
    if _hook is not None:
        try:
            _hook()
            log.info("已请求优雅停机（uvicorn should_exit）")
            return "uvicorn"
        except Exception as exc:                      # 钩子坏了也要能停
            log.warning("优雅停机钩子失败，退回信号：%s", exc)
    try:
        os.kill(os.getpid(), signal.SIGINT)
        log.info("已发送 SIGINT 请求停机")
        return "signal"
    except Exception as exc:
        log.error("发送 SIGINT 失败：%s", exc)
        return "none"
