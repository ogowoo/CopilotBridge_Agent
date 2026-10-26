"""应用组装入口。

包结构：gantry/api.py 路由层，gantry/bridge.py 能力层；
本文件仅对外暴露 app，供 `uvicorn app:app`（start.py）加载。

导入失败时的 failback：Agent 的自我升级把代码改坏（语法错误、导入错误）
会导致服务直接起不来。这里捕获导入异常并给出不依赖服务的回滚命令。
"""

import sys
import traceback

try:
    from gantry.api import app
except Exception:  # noqa: BLE001 — 任何导入期异常都要给出可操作的回滚指引
    traceback.print_exc()
    print("\n" + "=" * 64, file=sys.stderr)
    print("[启动失败] 代码导入异常，服务无法启动。", file=sys.stderr)
    print("很可能是最近一次 Agent 自我升级改坏了代码。可以回滚：", file=sys.stderr)
    print("    python scripts/rollback.py --list          # 查看可回滚的任务", file=sys.stderr)
    print("    python scripts/rollback.py --last --yes    # 回滚最近一次任务改动", file=sys.stderr)
    print("（该命令不依赖服务与 WebUI，直接按快照原像还原工作区文件）", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    raise

__all__ = ["app"]
