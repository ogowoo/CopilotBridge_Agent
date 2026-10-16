import json
from pathlib import Path
import uvicorn

from cbp import lifecycle

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
SERVER = CFG.get("server", {})

if __name__ == "__main__":
    # 显式构造 Server（而不是 uvicorn.run）：把"置 should_exit"的能力注入生命周期层，
    # 这样 WebUI 的「终止服务/退出」按钮能走 uvicorn 的**优雅停机**（关连接、收尾），
    # 而不是被硬杀。拿不到注入时 cbp/lifecycle.py 会退回信号。
    config = uvicorn.Config(
        "app:app",
        host=SERVER.get("host", "127.0.0.1"),
        port=int(SERVER.get("port", 8765)),
        reload=False,
    )
    server = uvicorn.Server(config)
    lifecycle.set_shutdown_hook(lambda: setattr(server, "should_exit", True))
    server.run()
