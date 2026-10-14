# -*- coding: utf-8 -*-
"""跟进消息队列：入队即返回、按序消费、单条失败不阻断。

此前 continue_session_message 同步阻塞到整轮跑完；改为每会话 FIFO 队列，
HTTP 立即返回 queued/position，后台 worker 逐条处理。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from cbp import bridge as b
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore


def make_stub(record: list, gate: "asyncio.Event | None" = None):
    """可控消费桩：记录顺序；gate 未置位时卡住，便于断言"仍排队"。"""
    async def fake(sid, item):
        record.append(item["content"])
        if gate is not None:
            await gate.wait()
        return None
    return fake


async def drain_until_empty(timeout=2.0):
    for _ in range(int(timeout / 0.02)):
        if b.queue_depth("s1") == 0 and not b._MSG_WORKERS.get("s1"):
            return
        await asyncio.sleep(0.02)


class MessageQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_mq_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, b._process_queued_message)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.root = self.ws
        b._MSG_QUEUES.clear()
        b._MSG_WORKERS.clear()
        b.AGENT_SESSIONS.create("s1", "任务", str(self.ws), "copilot-auto")

    def tearDown(self):
        for t in list(b._MSG_WORKERS.values()):
            t.cancel()
        b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, b._process_queued_message = self.saved
        b._MSG_QUEUES.clear()
        b._MSG_WORKERS.clear()

    def test_enqueue_returns_immediately_with_position(self):
        gate = asyncio.Event()
        b._process_queued_message = make_stub([], gate)
        async def main():
            r1 = await b.enqueue_session_message("s1", "a", "copilot-auto", False, None)
            await asyncio.sleep(0.05)   # 让 worker 抓走第一条（进入处理中）
            r2 = await b.enqueue_session_message("s1", "b", "copilot-auto", False, None)
            r3 = await b.enqueue_session_message("s1", "c", "copilot-auto", False, None)
            return r1, r2, r3
        r1, r2, r3 = asyncio.run(main())
        self.assertTrue(r1["queued"])
        self.assertEqual(r1["position"], 1)   # 空队列：第一个
        self.assertEqual(r2["position"], 1)   # 第一条已在处理，你是下一个
        self.assertEqual(r3["position"], 2)   # 前面还有 b 在等
        self.assertIn("s1", b._MSG_WORKERS)
        gate.set()
        asyncio.run(drain_until_empty())

    def test_fifo_order_and_failure_isolation(self):
        order = []
        async def fake(sid, item):
            order.append(item["content"])
            if item["content"] == "boom":
                raise RuntimeError("故意失败")
        b._process_queued_message = fake
        async def main():
            await b.enqueue_session_message("s1", "first", "copilot-auto", False, None)
            await b.enqueue_session_message("s1", "boom", "copilot-auto", False, None)
            await b.enqueue_session_message("s1", "third", "copilot-auto", False, None)
            await drain_until_empty()
        asyncio.run(main())
        self.assertEqual(order, ["first", "boom", "third"])
        self.assertEqual(b.queue_depth("s1"), 0)

    def test_queue_depth_in_runtime(self):
        # 第一条被 worker 抓去处理（卡在 gate），第二条还在队列里等待
        gate = asyncio.Event()
        b._process_queued_message = make_stub([], gate)
        async def main():
            await b.enqueue_session_message("s1", "a", "copilot-auto", False, None)
            await b.enqueue_session_message("s1", "b", "copilot-auto", False, None)
            await asyncio.sleep(0.05)   # 让 worker 抓走第一条
        asyncio.run(main())
        from cbp.api import app
        c = TestClient(app)
        r = c.get("/v1/agent/sessions/s1/runtime").json()
        self.assertEqual(r.get("queue_pending"), 1)   # 一条在处理、一条在等
        gate.set()
        asyncio.run(drain_until_empty())

    def test_continue_message_endpoint_queues(self):
        gate = asyncio.Event()
        b._process_queued_message = make_stub([], gate)
        b.AGENT_SESSIONS.patch("s1", state="paused")
        from cbp.api import app
        c = TestClient(app)
        r = c.post("/v1/agent/sessions/s1/messages",
                   json={"content": "帮我继续", "agent_mode": False})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d.get("queued"))
        self.assertIn("position", d)
        self.assertIn("id", d)
        # 用户消息已先落库，历史里立即可见
        msgs = b.AGENT_SESSIONS.messages("s1")
        self.assertTrue(any(m["role"] == "user" and m["content"] == "帮我继续" for m in msgs))
        gate.set()
        asyncio.run(drain_until_empty())


class QueueEditDeleteTests(unittest.TestCase):
    """排队消息的查看/修改/删除 + 消息表同步。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_mq_edit_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root,
                      b._process_queued_message, b._message_worker)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.root = self.ws
        b._MSG_QUEUES.clear(); b._MSG_WAKE.clear(); b._MSG_WORKERS.clear(); b._MSG_INFLIGHT.clear()
        b.AGENT_SESSIONS.create("s1", "任务", str(self.ws), "copilot-auto")
        # 不消费的 worker：条目停在"等待"态，便于测编辑/删除
        async def noop_worker(sid):
            await asyncio.Event().wait()
        b._message_worker = noop_worker
        b._process_queued_message = make_stub([])

    def tearDown(self):
        for t in list(b._MSG_WORKERS.values()):
            t.cancel()
        b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, b._process_queued_message, b._message_worker = self.saved
        b._MSG_QUEUES.clear(); b._MSG_WAKE.clear(); b._MSG_WORKERS.clear(); b._MSG_INFLIGHT.clear()

    def _enqueue(self, content):
        msg_id = b.AGENT_SESSIONS.add_message("s1", "user", content, 0)
        return asyncio.run(b.enqueue_session_message("s1", content, "copilot-auto", False, None, msg_id))

    def test_list_queue(self):
        self._enqueue("第一条")
        self._enqueue("第二条")
        from cbp.api import app
        c = TestClient(app)
        d = c.get("/v1/agent/sessions/s1/queue").json()
        self.assertEqual(d["depth"], 2)
        self.assertEqual([x["content"] for x in d["pending"]], ["第一条", "第二条"])
        self.assertEqual([x["position"] for x in d["pending"]], [1, 2])
        self.assertFalse(any(x.get("processing") for x in d["pending"]))

    def test_delete_queue_item_syncs_message(self):
        r1 = self._enqueue("要删的")
        self._enqueue("保留的")
        from cbp.api import app
        c = TestClient(app)
        res = c.delete(f"/v1/agent/sessions/s1/queue/{r1['id']}")
        self.assertEqual(res.status_code, 200)
        d = c.get("/v1/agent/sessions/s1/queue").json()
        self.assertEqual([x["content"] for x in d["pending"]], ["保留的"])
        # 落库的 user 消息也被删
        msgs = b.AGENT_SESSIONS.messages("s1")
        self.assertTrue(all(m["content"] != "要删的" for m in msgs))

    def test_edit_queue_item_syncs_message(self):
        r1 = self._enqueue("原文")
        from cbp.api import app
        c = TestClient(app)
        res = c.post(f"/v1/agent/sessions/s1/queue/{r1['id']}", json={"content": "改后"})
        self.assertEqual(res.status_code, 200)
        d = c.get("/v1/agent/sessions/s1/queue").json()
        self.assertEqual(d["pending"][0]["content"], "改后")
        # 落库的 user 消息同步更新
        msgs = b.AGENT_SESSIONS.messages("s1")
        self.assertTrue(any(m["content"] == "改后" for m in msgs))

    def test_delete_missing_item_404(self):
        from cbp.api import app
        c = TestClient(app)
        self.assertEqual(c.delete("/v1/agent/sessions/s1/queue/nonexistent").status_code, 404)
        self.assertEqual(c.post("/v1/agent/sessions/s1/queue/nonexistent",
                                json={"content": "x"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
