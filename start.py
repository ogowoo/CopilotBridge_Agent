"""服务入口：启动 uvicorn，并支持 WebUI 的「终止服务 / 重启服务」。

重启语义：先走 uvicorn 优雅停机（关连接、lifespan 收尾、落库），随后
`cbp.lifecycle.exec_restart()` 用同一解释器重新执行本脚本。放在 `server.run()`
返回**之后**做，是为了让监听端口先释放——重启不是"再起一个进程去抢端口"。

Windows 注意：`os.execv` 是"新建进程再退旧的"，新进程有**新 PID**（POSIX 才是
原地替换）。因此慢机器上仍可能撞端口，这里加了绑定重试兜底。
"""
import json
import sys
import time
from pathlib import Path

import uvicorn

from cbp import lifecycle

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
SERVER = CFG.get("server", {})
BIND_RETRIES = 6          # 绑定失败重试次数（合计约 6 × 0.8s ≈ 5s 窗口）
BIND_RETRY_DELAY = 0.8


def build_server() -> "uvicorn.Server":
    """构造 uvicorn Server 并把"置 should_exit"注入生命周期层。

    不直接用 `uvicorn.run`：server 对象藏在内部，路由层就拿不到，WebUI 的
    「终止服务」只能靠信号硬杀（Windows 上是 TerminateProcess，收尾做不完）。
    """
    config = uvicorn.Config(
        "app:app",
        host=SERVER.get("host", "127.0.0.1"),
        port=int(SERVER.get("port", 8765)),
        reload=False,
    )
    server = uvicorn.Server(config)
    lifecycle.set_shutdown_hook(lambda: setattr(server, "should_exit", True))
    return server


def serve() -> None:
    """启动服务；端口被占（重启瞬间新旧进程交叠）时短暂重试。"""
    for attempt in range(1, BIND_RETRIES + 1):
        server = build_server()
        try:
            server.run()
            return
        except SystemExit as exc:              # uvicorn 绑定失败会 sys.exit(1)
            code = getattr(exc, "code", 1) or 1
            if attempt >= BIND_RETRIES:
                print(f"端口绑定失败（退出码 {code}），已重试 {BIND_RETRIES} 次，放弃。",
                      file=sys.stderr)
                raise
            print(f"端口暂不可用（退出码 {code}），{BIND_RETRY_DELAY}s 后重试 "
                  f"{attempt}/{BIND_RETRIES}（重启时的正常现象）", file=sys.stderr)
            time.sleep(BIND_RETRY_DELAY)
        except OSError as exc:                 # 少数环境下直接抛 OSError
            if attempt >= BIND_RETRIES:
                raise
            print(f"启动失败（{exc}），{BIND_RETRY_DELAY}s 后重试 "
                  f"{attempt}/{BIND_RETRIES}", file=sys.stderr)
            time.sleep(BIND_RETRY_DELAY)


if __name__ == "__main__":
    serve()
    # 正常退出后：若这次是「重启服务」触发的，就地重启。
    if lifecycle.restart_requested():
        lifecycle.exec_restart()
