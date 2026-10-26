# -*- coding: utf-8 -*-
"""SSE 实时推送：runtime 快照/队列/新消息直接推给浏览器，前端不必轮询。

要点：
- 每会话一组订阅队列，队列**有界**（慢客户端只丢事件，不拖慢 Agent）；
- 断线必须在 finally 里退订，否则订阅者集合会一直涨（内存泄漏）；
- 连接建立先送一份当前快照，客户端连上即有内容；
- 无订阅者时 runtime_update 不付出序列化成本。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry import bridge as b
from gantry.session_store import SessionStore
from gantry.snapshots import SnapshotStore


class EventBusTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(b._EVENT_SUBS), dict(b.RUNTIME)
        b._EVENT_SUBS.clear(); b.RUNTIME.clear()

    def tearDown(self):
        subs, rt = self.saved
        b._EVENT_SUBS.clear(); b._EVENT_SUBS.update(subs)
        b.RUNTIME.clear(); b.RUNTIME.update(rt)

    def test_subscribe_publish_unsubscribe(self):
        async def main():
            q = b.subscribe_events("s1")
            b.publish_event("s1", "queue")
            got = q.get_nowait()
            b.unsubscribe_events("s1", q)
            return got, "s1" in b._EVENT_SUBS
        got, still = asyncio.run(main())
        self.assertEqual(got, {"type": "queue"})
        self.assertFalse(still, "退订后不应残留会话键（否则内存泄漏）")

    def test_publish_without_subscribers_is_noop(self):
        b.publish_event("nope", "queue")          # 不应抛异常
        self.assertNotIn("nope", b._EVENT_SUBS)

    def test_runtime_update_pushes_snapshot(self):
        async def main():
            q = b.subscribe_events("s1")
            b.runtime_update("s1", phase="api_streaming", answer_preview="逐字",
                             detail="正在流式返回")
            ev = q.get_nowait()
            b.unsubscribe_events("s1", q)
            return ev
        ev = asyncio.run(main())
        self.assertEqual(ev["type"], "runtime")
        self.assertEqual(ev["data"]["phase"], "api_streaming")
        self.assertEqual(ev["data"]["answer_preview"], "逐字")

    def test_runtime_update_without_subscriber_still_updates_state(self):
        b.runtime_update("s1", phase="waiting")
        self.assertEqual(b.RUNTIME["s1"]["phase"], "waiting")

    def test_slow_subscriber_drops_oldest_not_blocks(self):
        """慢客户端只丢最旧事件：Agent 绝不能被前端拖住。"""
        async def main():
            q = b.subscribe_events("s1")
            for i in range(b._EVENT_QUEUE_MAX + 10):
                b.runtime_update("s1", phase="api_streaming", answer_preview=str(i))
            size = q.qsize()
            last = None
            while not q.empty():
                last = q.get_nowait()
            b.unsubscribe_events("s1", q)
            return size, last
        size, last = asyncio.run(main())
        self.assertLessEqual(size, b._EVENT_QUEUE_MAX)
        self.assertEqual(last["data"]["answer_preview"],
                         str(b._EVENT_QUEUE_MAX + 9), "最新状态必须送达")

    def test_multiple_subscribers_all_receive(self):
        async def main():
            q1 = b.subscribe_events("s1"); q2 = b.subscribe_events("s1")
            b.publish_event("s1", "messages")
            out = (q1.get_nowait(), q2.get_nowait())
            b.unsubscribe_events("s1", q1); b.unsubscribe_events("s1", q2)
            return out
        a, c = asyncio.run(main())
        self.assertEqual(a, c)


class EventsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_sse_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, dict(b._EVENT_SUBS))
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        b._EVENT_SUBS.clear()
        b.AGENT_SESSIONS.create("s1", "任务", str(self.ws), "fakeprov/m1")
        b.RUNTIME["s1"] = {"phase": "api_streaming", "answer_preview": "首帧内容",
                           "detail": "正在流式返回", "updated": 1.0}
        from gantry.api import app
        self.app = app

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, subs) = self.saved
        b._EVENT_SUBS.clear(); b._EVENT_SUBS.update(subs)
        b.RUNTIME.pop("s1", None)

    def test_stream_sends_snapshot_then_events(self):
        """真实 uvicorn 端到端：先送快照，推送事件即时到达，断线后退订。

        用假 ASGI scope 测过一版，结果被 Starlette 的 spec_version 分支
        （spec_version<2.4 时走 listen_for_disconnect 折叠任务组）误导——
        脚手架不合规会让流提前结束。这里起真服务，行为才等于用户实际看到的。
        """
        import httpx
        import uvicorn

        async def main():
            config = uvicorn.Config(self.app, host="127.0.0.1", port=8731,
                                    log_level="error", lifespan="off")
            server = uvicorn.Server(config)
            server_task = asyncio.create_task(server.serve())
            try:
                for _ in range(100):
                    if server.started:
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(server.started, "测试服务器未启动")
                url = "http://127.0.0.1:8731/v1/agent/sessions/s1/events"
                frames = []
                async with httpx.AsyncClient(timeout=10.0) as client:
                    async with client.stream("GET", url) as r:
                        self.assertEqual(r.status_code, 200)
                        self.assertIn("text/event-stream", r.headers.get("content-type", ""))
                        lines = r.aiter_lines()

                        async def next_frame(budget=8):
                            """读到下一个 data: 帧（SSE 每帧后有一个空行分隔符，别数错）"""
                            for _ in range(budget):
                                line = await asyncio.wait_for(lines.__anext__(), timeout=5)
                                if line.startswith("data: "):
                                    return json.loads(line[6:])
                            raise AssertionError("未读到 data 帧")

                        # 第 1 帧：连接即送的 runtime 快照
                        frames.append(await next_frame())
                        # 推送两类事件：队列与 runtime（流式逐字的来源）
                        b.publish_event("s1", "queue")
                        b.runtime_update("s1", phase="api_streaming", answer_preview="逐字增长")
                        frames.append(await next_frame())
                        frames.append(await next_frame())
                types = [f["type"] for f in frames]
                self.assertEqual(types[0], "runtime")
                self.assertIn("queue", types)
                self.assertIn("runtime", types[1:])
                streamed = [f for f in frames if f["type"] == "runtime"][-1]
                self.assertEqual(streamed["data"]["answer_preview"], "逐字增长")
                # 客户端断开后订阅者必须清空（否则内存泄漏）
                for _ in range(60):
                    if not b._EVENT_SUBS.get("s1"):
                        break
                    await asyncio.sleep(0.05)
                self.assertFalse(b._EVENT_SUBS.get("s1"), "断线后应退订")
            finally:
                server.should_exit = True
                try:
                    await asyncio.wait_for(server_task, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    server_task.cancel()

        asyncio.run(main())

    def test_unknown_session_404(self):
        from fastapi.testclient import TestClient
        c = TestClient(self.app)
        self.assertEqual(c.get("/v1/agent/sessions/nope/events").status_code, 404)


if __name__ == "__main__":
    unittest.main()
