"""服务入口：启动 uvicorn（+ 系统托盘），并支持 WebUI 的「终止服务 / 重启服务」。

## 两种跑法

```
python start.py                 # 默认：**派生后台进程**（相当于 pythonw）后立刻返回
                                #   → 终端可以关掉，托盘图标常驻，日志写 data/logs/gantry.log
python start.py --foreground    # 前台：日志同时打在终端（Ctrl+C 结束），托盘也起
```

后台模式不需要先跑 `scripts/login.py`：托盘菜单里的「启动自动化浏览器」会按需拉起
浏览器（命中服务的 `/v1/agent/browser`），登录态沿用 `EdgeProfile`。

其它开关：`--no-tray`（不起托盘，适合服务/CI）、`--detach` / `--no-detach`（强制是否派生）、
`--tray-selftest`（只建一下图标就退，验证 Win32 可用）、`--print-menu`（打印托盘菜单）。

## 重启语义

先走 uvicorn 优雅停机（关连接、lifespan 收尾、落库），随后 `gantry.lifecycle.exec_restart()`
用**同一解释器**重新执行本脚本。放在 `server.run()` 返回**之后**做，是为了让监听端口先释放
——重启不是"再起一个进程去抢端口"。

Windows 注意：`os.execv` 是"新建进程再退旧的"，新进程有**新 PID**（POSIX 才是原地替换）。
因此慢机器上仍可能撞端口，这里加了绑定重试兜底。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
# 从任意 cwd 启动都要能 `import app` / `gantry`（重启时 exec 保留 cwd，
# 用 `gantry serve` 或快捷方式启动时 cwd 未必是项目根）。
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
SERVER = CFG.get("server", {})
BIND_RETRIES = 6          # 绑定失败重试次数（合计约 6 × 0.8s ≈ 5s 窗口）
BIND_RETRY_DELAY = 0.8
DEFAULT_PORT = 8765


def _port() -> int:
    """端口优先级：环境变量（`gantry serve --port` / 容器）> config.json > 8765。"""
    try:
        from gantry.brand import env as _env

        raw = (_env("PORT") or "").strip()
    except Exception:                       # noqa: BLE001
        raw = (os.environ.get("GANTRY_PORT") or "").strip()
    try:
        return int(raw) if raw else int(SERVER.get("port", DEFAULT_PORT))
    except ValueError:
        return int(SERVER.get("port", DEFAULT_PORT))


def build_server() -> "uvicorn.Server":
    """构造 uvicorn Server 并把"置 should_exit"注入生命周期层。

    不直接用 `uvicorn.run`：server 对象藏在内部，路由层就拿不到，WebUI 的
    「终止服务」只能靠信号硬杀（Windows 上是 TerminateProcess，收尾做不完）。
    """
    import uvicorn

    from gantry import lifecycle

    config = uvicorn.Config(
        "app:app",
        host=SERVER.get("host", "127.0.0.1"),
        port=_port(),
        reload=False,
        log_config=None,                    # 日志统一由 gantry.logcfg 管（否则重复打）
    )
    server = uvicorn.Server(config)
    lifecycle.set_shutdown_hook(lambda: setattr(server, "should_exit", True))
    return server


def serve() -> None:
    """启动服务；端口被占（重启瞬间新旧进程交叠）时短暂重试。"""
    import logging

    log = logging.getLogger("gantry.start")
    for attempt in range(1, BIND_RETRIES + 1):
        server = build_server()
        try:
            server.run()
            return
        except SystemExit as exc:              # uvicorn 绑定失败会 sys.exit(1)
            code = getattr(exc, "code", 1) or 1
            if attempt >= BIND_RETRIES:
                log.error("端口绑定失败（退出码 %s），已重试 %s 次，放弃。", code, BIND_RETRIES)
                raise
            log.warning("端口暂不可用（退出码 %s），%.1fs 后重试 %s/%s（重启时的正常现象）",
                        code, BIND_RETRY_DELAY, attempt, BIND_RETRIES)
            time.sleep(BIND_RETRY_DELAY)
        except OSError as exc:                 # 少数环境下直接抛 OSError
            if attempt >= BIND_RETRIES:
                raise
            log.warning("启动失败（%s），%.1fs 后重试 %s/%s", exc, BIND_RETRY_DELAY,
                        attempt, BIND_RETRIES)
            time.sleep(BIND_RETRY_DELAY)


# ── 托盘 ───────────────────────────────────────────────────────────────────
def _tray_disabled(args: list[str]) -> bool:
    return "--no-tray" in args or (os.environ.get("GANTRY_NO_TRAY") or "") == "1"


def start_tray(port: int, lang: str | None = None):
    """起托盘（无 GUI 的机器上失败也不该拖垮服务，只记一条日志）。"""
    import logging

    from gantry.tray import Tray, default_menu

    log = logging.getLogger("gantry.start")
    try:
        tray = Tray(port=port, root=BASE, menu_spec=default_menu,
                    icon_path=BASE / "assets" / "gantry.ico", lang=lang)
        tray.start()
        if tray.wait_ready(5.0):
            log.info("托盘已就绪（右键图标有菜单）")
        else:
            log.warning("托盘未能在 5s 内就绪（服务继续运行）")
        return tray
    except Exception as exc:                   # noqa: BLE001
        log.warning("托盘启动失败（服务继续运行）：%s", exc)
        return None


# ── 后台派生（"相当于 pythonw"）──────────────────────────────────────────────
def _pythonw() -> str | None:
    """同目录下的 pythonw.exe（无控制台窗口）。找不到就退回当前解释器。"""
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand) if cand.is_file() else None


def build_detach_command(args: list[str]) -> list[str]:
    """派生后台子进程的命令行（抽出来便于测试）。"""
    exe = _pythonw() or sys.executable
    return [exe, str(BASE / "start.py"), "--tray-child", *args]


def _wait_ready(port: int, seconds: float = 30.0) -> bool:
    import urllib.request

    url = f"http://127.0.0.1:{port}/health"
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:      # noqa: S310
                if r.status == 200:
                    return True
        except Exception:                        # noqa: BLE001
            time.sleep(0.4)
    return False


def health_ok(port: int) -> bool:
    return _wait_ready(port, 1.0)


def detach(args: list[str]) -> int:
    """把服务丢到后台（无控制台），等它就绪后返回。终端随后可以关掉。"""
    from gantry.logcfg import log_file

    port = _port()
    if health_ok(port):
        print(f"服务已在运行：http://127.0.0.1:{port}/ui/（托盘图标在系统托盘里）")
        return 0
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    env = dict(os.environ)
    env["GANTRY_TRAY_CHILD"] = "1"
    cmd = build_detach_command(args)
    try:
        subprocess.Popen(cmd, cwd=str(BASE), env=env, creationflags=flags,
                         close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:                    # noqa: BLE001
        print(f"派生后台进程失败：{exc}", file=sys.stderr)
        return 1
    print("已在后台启动（无窗口）。这个终端可以关了。")
    if _wait_ready(port, 30.0):
        print(f"服务就绪：http://127.0.0.1:{port}/ui/")
    else:
        print("服务 30 秒内未就绪——看日志：", file=sys.stderr)
    print(f"日志文件：{log_file(BASE)}")
    print("托盘图标在右下角系统托盘：右键可「打开控制台 / 启动自动化浏览器 / 重启 / 停止」。")
    return 0


def _arg_lang(argv: list[str]) -> str | None:
    """`--lang zh|tw|en`（也可用 GANTRY_LANG）。"""
    if "--lang" in argv:
        i = argv.index("--lang")
        if i + 1 < len(argv):
            return argv[i + 1]
    return (os.environ.get("GANTRY_LANG") or "").strip() or None


def print_menu(lang: str | None = None) -> int:
    """打印托盘菜单（不需要 GUI，便于测试/排障）。"""
    from gantry.tray import Tray, default_menu

    tray = Tray(port=_port(), root=BASE, menu_spec=default_menu, lang=lang)
    for item in default_menu(tray):
        if item[0] == "-":
            print("----")
        elif item[0] == "--submenu--":
            print(f"{item[1]}:")
            for act, lab in item[2]:
                print(f"    {act}: {lab}")
        else:
            print(f"{item[0]}: {item[1]}")
    return 0


def main(argv: list[str]) -> int:
    from gantry.logcfg import setup_logging

    tray_child = ("--tray-child" in argv) or (os.environ.get("GANTRY_TRAY_CHILD") == "1")
    foreground = ("--foreground" in argv) or ("-f" in argv) or not tray_child

    if "--print-menu" in argv:
        return print_menu(_arg_lang(argv))
    if "--tray-selftest" in argv:
        from gantry.tray import selftest

        setup_logging(BASE, console=True)
        return selftest(_port(), BASE)
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0

    # 后台子进程（或前台）：先把日志配好，再干别的
    setup_logging(BASE, console=foreground and not tray_child)

    tray = None
    if not _tray_disabled(argv):
        tray = start_tray(_port(), _arg_lang(argv))
    try:
        serve()
    finally:
        if tray is not None:
            try:
                tray.stop()
            except Exception:                    # noqa: BLE001
                pass
    # 正常退出后：若这次是「重启服务」触发的，就地重启。
    from gantry import lifecycle

    if lifecycle.restart_requested():
        lifecycle.exec_restart()
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    detached_child = ("--tray-child" in argv) or (os.environ.get("GANTRY_TRAY_CHILD") == "1")
    force_fg = ("--foreground" in argv) or ("-f" in argv)
    no_detach = "--no-detach" in argv
    if detached_child or force_fg or no_detach or "--print-menu" in argv \
            or "--tray-selftest" in argv or "--help" in argv or "-h" in argv:
        sys.exit(main(argv))
    # 默认：先试派生后台；失败则退回前台（至少能用）
    if os.name == "nt":
        code = detach(argv)
        if code == 0:
            sys.exit(0)
        print("后台启动失败，改为前台运行…", file=sys.stderr)
    sys.exit(main(argv + ["--foreground"]))
