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
_restarting = False
_epoch = 0                   # 每次 request / disarm 递增，用于作废已排队的兜底定时器
_force_exit_enabled = True    # False 时不武装"宽限期到点硬退出"（测试 / 挂调试器）
_pending: dict = {}           # 已排队的延时触发 {kind: Timer}，可被 disarm 取消
_atexit: list = []           # 重启前要执行的收尾（关库、刷日志等）


def set_shutdown_hook(fn) -> None:
    """注入优雅停机实现（入口调用；测试里也可注入桩）。"""
    global _hook
    _hook = fn


def shutdown_pending() -> bool:
    return _shutting_down


def restart_requested() -> bool:
    return _restarting


def schedule(kind: str, delay: float = 0.4) -> bool:
    """延时触发停机/重启（HTTP 层用：**先回响应再动**，否则前端看到连接重置）。

    返回是否真的排上。用一个可取消的 Timer 登记在 `_pending` 里——不能直接在
    路由里 `threading.Timer(...).start()`：那样这个定时器无法取消，测试里它会
    在用例结束之后才触发，反过来给**测试进程**发 SIGINT / os._exit（实测踩过，
    表现为"跑全量时进程莫名其妙被杀、没有任何摘要"）。
    """
    if kind in _pending:
        return False
    fn = request_restart if kind == "restart" else request_shutdown
    t = threading.Timer(max(0.05, float(delay)), fn)
    t.daemon = True
    _pending[kind] = t
    t.start()
    return True


def cancel_pending() -> None:
    """取消已排队的延时触发（幂等）。"""
    for kind, t in list(_pending.items()):
        try:
            t.cancel()
        except Exception:
            pass
        _pending.pop(kind, None)


def set_force_exit(enabled: bool) -> None:
    """开关"宽限期到点硬退出/重启"的兜底。

    生产默认开启（按钮不能点了没反应）。测试里必须关掉：端点用的是
    `threading.Timer(0.4, request_*)`，它可能在 teardown 之后才触发，
    再武装一个真兜底定时器，几秒后就把**测试进程** os._exit 掉了（实测踩过）。
    挂调试器时也建议关掉，免得断点停住被强退。
    """
    global _force_exit_enabled
    _force_exit_enabled = bool(enabled)


def disarm() -> None:
    """取消已排队的停机/重启（测试与"点了又反悔"用）。

    只清状态**是不够的**：`_force_exit_later` 排的是 daemon 定时线程，时间到了仍会
    执行 os._exit/exec —— 测试里就因此把整个测试进程重启了（实测：跑全量时看到
    "正在重启服务 ... start.py discover -s tests"）。所以用 epoch 作废旧定时器。
    """
    global _epoch, _shutting_down, _restarting
    _epoch += 1
    _shutting_down = False
    _restarting = False
    cancel_pending()


def on_restart(fn) -> None:
    """注册"重启前收尾"回调（异常不影响重启流程）。"""
    _atexit.append(fn)


def _entry_argv() -> list[str]:
    """重启用的命令行：沿用当前进程的 argv（入口脚本 + 原有参数）。

    `sys.argv[0]` 常是相对路径（`python start.py`），解析成绝对路径；不存在时退回
    安装目录下的 start.py——重启不能因为 cwd 不同就起不来。
    """
    import sys
    from pathlib import Path
    argv = list(sys.argv)
    script = argv[0] if argv else ""
    if script and Path(script).is_file():
        argv[0] = str(Path(script).resolve())
    else:
        base = Path(__file__).resolve().parent.parent
        argv = [str(base / "start.py")] + argv[1:]
    return argv


def exec_restart() -> str:
    """就地重启：同一解释器重新执行入口脚本（PID 不变、无端口竞争）。

    用 exec 而不是"起新进程再退出"：exec 替换进程映像，监听套接字随之关闭，
    没有"新进程抢不到端口"的竞态窗口，用户点完几乎立刻能重连。
    exec 失败时退回"派生新进程 + 退出自己"。
    """
    import subprocess
    import sys
    from pathlib import Path
    argv = _entry_argv()
    for h in list(_atexit):
        try:
            h()
        except Exception:
            pass
    try:
        log.warning("正在重启服务：%s %s", sys.executable, " ".join(argv))
        os.execv(sys.executable, [sys.executable] + argv)
        return "exec"                       # 正常情况下 exec 不返回
    except Exception as exc:                # noqa: BLE001
        log.error("exec 重启失败，改为派生新进程：%s", exc)
        try:
            flags = 0
            if os.name == "nt":
                flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            subprocess.Popen([sys.executable] + argv,
                             cwd=str(Path(argv[0]).parent),
                             creationflags=flags, close_fds=True)
        except Exception as exc2:           # noqa: BLE001
            log.error("派生新进程也失败：%s", exc2)
        os._exit(0)
        return "spawn"


def _bump_epoch() -> None:
    global _epoch
    _epoch += 1


def _force_exit_later(seconds: float) -> None:
    """宽限期内没退干净就兜底：停机→硬退出，重启→执行重启。

    重启场景必须真的重启：否则用户点了「重启服务」却等来一个死进程。
    """
    if not _force_exit_enabled:
        return
    epoch = _epoch

    def killer():
        time.sleep(max(0.5, float(seconds)))
        if epoch != _epoch:                  # 期间被 disarm / 重新 request 过
            return
        if _restarting:
            log.warning("优雅停机超过 %.1fs，直接执行重启", seconds)
            exec_restart()
            return
        log.warning("优雅停机超过 %.1fs 仍未退出，执行硬退出", seconds)
        os._exit(0)
    threading.Thread(target=killer, name="cbp-force-exit", daemon=True).start()


def request_shutdown(force_after: float | None = None) -> str:
    """请求停机，返回采用的方式：uvicorn / signal / already。

    幂等：重复调用只生效一次（按钮可能被连点）。
    """
    global _shutting_down, _restarting
    if _shutting_down or _restarting:
        return "already"
    _shutting_down = True
    _restarting = False
    _bump_epoch()
    _force_exit_later(_force_seconds if force_after is None else force_after)
    if _hook is not None:
        try:
            _hook()
            log.info("已请求优雅停机（uvicorn should_exit）")
            return "uvicorn"
        except Exception as exc:                      # 钩子坏了也要能停
            log.warning("优雅停机钩子失败，退回信号：%s", exc)
    if not _force_exit_enabled:
        # 测试/调试模式：绝不真的给本进程发信号（否则整个测试进程被打断）
        log.debug("force_exit 已禁用，跳过 SIGINT 停机回退")
        return "none"
    try:
        os.kill(os.getpid(), signal.SIGINT)
        log.info("已发送 SIGINT 请求停机")
        return "signal"
    except Exception as exc:
        log.error("发送 SIGINT 失败：%s", exc)
        return "none"


def request_restart(force_after: float | None = None) -> str:
    """请求重启服务（WebUI「重启服务」菜单）。返回采用的方式：uvicorn / signal / already。

    语义与停机一致：先优雅退出（关连接、收尾、落库），随后**同一个进程**用
    `exec_restart()` 重新执行入口脚本。正在运行的任务同样会中断；若配置了
    `agent.auto_resume`，新进程启动后会自动续跑被中断的会话。
    """
    global _shutting_down, _restarting
    if _shutting_down or _restarting:
        return "already"
    _restarting = True
    _shutting_down = True
    _bump_epoch()
    _force_exit_later(_force_seconds if force_after is None else force_after)
    if _hook is not None:
        try:
            _hook()
            log.info("已请求重启（优雅停机后 exec 重启）")
            return "uvicorn"
        except Exception as exc:                      # 钩子坏了也要能重启
            log.warning("重启钩子失败，退回信号：%s", exc)
    if not _force_exit_enabled:
        log.debug("force_exit 已禁用，跳过 SIGINT 重启回退")
        return "none"
    try:
        os.kill(os.getpid(), signal.SIGINT)
        log.info("已发送 SIGINT 请求重启")
        return "signal"
    except Exception as exc:
        log.error("发送 SIGINT 失败：%s", exc)
        return "none"
