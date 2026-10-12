"""应用组装入口。

包结构：cbp/api.py 路由层，cbp/bridge.py 能力层；
本文件仅对外暴露 app，供 `uvicorn app:app`（start.py）加载。
"""

from cbp.api import app

__all__ = ["app"]
