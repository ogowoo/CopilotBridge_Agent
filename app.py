"""应用组装入口。

分层结构：
- bridge.py  配置、运行状态、Playwright 浏览器自动化与 Agent 会话编排
- api.py     FastAPI 路由（HTTP 请求/响应适配）
- 本文件     对外暴露 app，供 `uvicorn app:app`（start.py）加载
"""

from api import app

__all__ = ["app"]
