# -*- coding: utf-8 -*-
"""日志落盘：托盘/后台模式下没有终端，日志必须写文件。

为什么单独一个模块：`python start.py` 默认会**派生后台进程**（相当于 pythonw），
用户随后就能关掉终端。这时 stdout/stderr 都没了，出问题只能靠文件日志——
所以入口必须在任何别的工作之前把日志配好，并且控制台模式下**同时**打到终端。

文件位置：`data/logs/gantry.log`（5 MB × 7 个滚动）。也可用环境变量
`GANTRY_LOG_DIR` / `GANTRY_LOG_LEVEL` 覆盖。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def log_dir(root: Path) -> Path:
    """日志目录（默认 <root>/data/logs；GANTRY_LOG_DIR 可覆盖）。"""
    env = (os.environ.get("GANTRY_LOG_DIR") or "").strip()
    d = Path(env) if env else Path(root) / "data" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_file(root: Path) -> Path:
    return log_dir(root) / "gantry.log"


def has_console() -> bool:
    """当前解释器有没有控制台窗口（pythonw.exe / 分离进程里没有）。"""
    if os.name != "nt":
        return sys.stderr is not None
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # noqa: BLE001
        return sys.stderr is not None


def setup_logging(root: Path, *, console: bool | None = None,
                  level: str | None = None) -> Path:
    """配置根日志：文件（滚动）+ 可选控制台。返回日志文件路径。

    幂等：重复调用不会叠加 handler（重启/多次 import 都安全）。
    """
    root = Path(root)
    console = has_console() if console is None else console
    lvl = (level or os.environ.get("GANTRY_LOG_LEVEL") or "INFO").upper()
    try:
        level_no = getattr(logging, lvl)
    except AttributeError:
        level_no = logging.INFO

    logger = logging.getLogger()
    logger.setLevel(level_no)
    # 去掉自己之前加过的 handler（保留别人加的，比如测试里的 caplog）
    for h in list(logger.handlers):
        if getattr(h, "_gantry_file", False) or getattr(h, "_gantry_console", False):
            logger.removeHandler(h)

    path = log_file(root)
    fh = logging.handlers.RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=7, encoding="utf-8")
    fh.setFormatter(logging.Formatter(FMT))
    fh._gantry_file = True                      # type: ignore[attr-defined]
    logger.addHandler(fh)

    if console and sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter(FMT))
        sh._gantry_console = True               # type: ignore[attr-defined]
        logger.addHandler(sh)

    # uvicorn 自己的 logger 不要另起 handler，否则每条日志打两遍
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    logging.captureWarnings(True)
    logger.info("日志已就绪：%s（控制台=%s）", path, console)
    return path
