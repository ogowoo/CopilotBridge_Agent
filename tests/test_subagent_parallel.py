# -*- coding: utf-8 -*-
"""子代理并行扇出（仅 API 提供商通道）。

用户明确要求"必须做"的第 1 项：**API 提供商模型要能真并行跑多个子代理**，
Copilot 网页通道保持顺序（只有一个 page，并发会互相踩页）。

本文件钉住四件事：
1. 提供商模型 → 真并行（墙钟时间接近"最慢的一个"，而不是求和）；
2. 并发上限生效（`agent.subagent_parallel`），且在跑的子任务数不超过它；
3. 单项失败不影响其余（失败隔离），结果顺序 == 任务顺序（gather 保序）；
4. 网页 Copilot 通道 → 仍然严格顺序（不允许并发）。
另外验证并行的前提：**任务级上下文隔离**（会话 id / 网页工具开关 / 递归深度），
否则两个并发子代理会互相覆盖全局变量。
"""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import bridge as b
from cbp import fanout as fanout_mod
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore


def _setup(tmp: Path):
    b.AGENT_SESSIONS = SessionStore(tmp / "agent.db")
    b.SNAPSHOTS = SnapshotStore(tmp / "snaps")
    ws = tmp / "ws"
    ws.mkdir(exist_ok=True)
    b.AGENT_TOOLS.set_root(ws, False)
    return ws


class ParallelFanoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_par_"))
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, b.AGENT_CFG)
        _setup(self.tmp)
        self.seen = []          # 并发观察：(开始, 结束)
        self.peak = 0
        self._live = 0

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, b.AGENT_CFG) = self.saved

    def _fake_subagent(self, delay=0.25, fail_on=()):
        async def run(task, description="", model="", max_steps=40):
            self._live += 1
            self.peak = max(self.peak, self._live)
            t0 = time.time()
            try:
                await asyncio.sleep(delay)
                if task in fail_on:
                    raise RuntimeError("故意失败：" + task)
                return {"agent_id": "sub-" + task, "steps": 1, "content": "结果:" + task}
            finally:
                self._live -= 1
                self.seen.append((task, t0, time.time()))
        return run

    def test_provider_runs_in_parallel(self):
        b.AGENT_CFG["subagent_parallel"] = 4
        b.run_subagent = self._fake_subagent(0.3)
        out = asyncio.run(fanout_mod.fanout(["a", "b", "c"], model="deepseek/deepseek-chat"))
        self.assertTrue(out["parallel"], "提供商模型必须走并行")
        self.assertEqual(out["succeeded"], 3)
        # 3 个各 0.3s：并行应显著小于串行的 0.9s
        self.assertLess(out["seconds"], 0.7, f"没并行起来（{out['seconds']}s）")
        self.assertGreaterEqual(self.peak, 2, "观察到的峰值并发应 ≥2")
        self.assertEqual([x["index"] for x in out["results"]], [1, 2, 3], "结果要保序")

    def test_concurrency_cap_respected(self):
        b.AGENT_CFG["subagent_parallel"] = 2
        b.run_subagent = self._fake_subagent(0.2)
        out = asyncio.run(fanout_mod.fanout(["a", "b", "c", "d"], model="deepseek/x"))
        self.assertEqual(out["concurrency"], 2)
        self.assertLessEqual(self.peak, 2, f"并发超过上限：{self.peak}")

    def test_failure_isolation(self):
        b.AGENT_CFG["subagent_parallel"] = 3
        b.run_subagent = self._fake_subagent(0.1, fail_on=("bad",))
        out = asyncio.run(fanout_mod.fanout(["ok1", "bad", "ok2"], model="kimi/x"))
        self.assertEqual(out["succeeded"], 2)
        self.assertEqual(out["failed"], 1)
        self.assertTrue([r for r in out["results"] if r["index"] == 2 and not r["ok"]])

    def test_copilot_stays_sequential(self):
        b.AGENT_CFG["subagent_parallel"] = 8
        b.run_subagent = self._fake_subagent(0.15)
        out = asyncio.run(fanout_mod.fanout(["a", "b", "c"], model="copilot-auto"))
        self.assertFalse(out["parallel"], "网页通道必须顺序")
        self.assertEqual(self.peak, 1, f"Copilot 通道不允许并发（峰值 {self.peak}）")
        self.assertGreaterEqual(out["seconds"], 0.4, "顺序执行应接近求和")

    def test_single_task_not_parallel(self):
        b.AGENT_CFG["subagent_parallel"] = 4
        b.run_subagent = self._fake_subagent(0.05)
        out = asyncio.run(fanout_mod.fanout(["only"], model="deepseek/x"))
        self.assertFalse(out["parallel"], "只有一个任务时不必并行")


class SubagentManyToolTests(unittest.TestCase):
    """模型实际走的是 subagent_many 工具（不是 fanout 接口），必须同样并行。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_many_"))
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, b.AGENT_CFG)
        _setup(self.tmp)
        b.AGENT_SESSIONS.create("p1", "父任务", str(self.tmp), "deepseek/x")
        self.live = 0
        self.peak = 0

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, b.AGENT_CFG) = self.saved

    def _registry(self, tool_extra_cls):
        t = b.AGENT_TOOLS
        t.subagent_fn = self._fn
        return t

    def _fn(self, task, description="", model="", max_steps=40):
        async def run():
            self.live += 1
            self.peak = max(self.peak, self.live)
            try:
                await asyncio.sleep(0.25)
                return {"agent_id": "sub-" + task, "steps": 1, "content": "r:" + task}
            finally:
                self.live -= 1
        return run()

    def _call(self, model):
        from cbp.tools_extra import register_extra_tools
        b.AGENT_CFG["subagent_parallel"] = 4
        b._SESSION_CTX.set("p1")          # 父会话（模型取不到 model 时用它兜底）
        # 走真实注册路径：subagent_fn 由 register_extra_tools 注入
        impl = register_extra_tools(b.AGENT_TOOLS, {}, subagent_fn=self._fn)
        return asyncio.run(impl.subagent_many(tasks=["a", "b", "c"], model=model))

    def test_provider_parallel_via_tool(self):
        out = self._call("deepseek/deepseek-chat")
        self.assertTrue(out["parallel"])
        self.assertEqual(out["succeeded"], 3)
        self.assertLess(out["seconds"], 0.6, f"工具路径没并行（{out['seconds']}s）")
        self.assertGreaterEqual(self.peak, 2)

    def test_copilot_sequential_via_tool(self):
        out = self._call("copilot-auto")
        self.assertFalse(out["parallel"])
        self.assertEqual(self.peak, 1)
        self.assertGreaterEqual(out["seconds"], 0.6)

    def test_stop_on_error_still_works_sequential(self):
        def boom(task, description="", model="", max_steps=40):
            async def run():
                raise RuntimeError("失败：" + task)
            return run()

        from cbp.tools_extra import register_extra_tools
        b.AGENT_CFG["subagent_parallel"] = 4
        b._SESSION_CTX.set("p1")
        impl = register_extra_tools(b.AGENT_TOOLS, {}, subagent_fn=boom)
        out = asyncio.run(impl.subagent_many(
            tasks=["x", "y"], model="copilot-auto", stop_on_error=True))
        self.assertEqual(out["succeeded"], 0)
        self.assertEqual(len(out["results"]), 1, "stop_on_error 应中断后续")

    def test_copilot_parallel_follows_windows(self):
        """网页通道并行数 = 窗口数 - 1（父任务自己占一个窗口）。"""
        saved_max = b.WINDOWS._max
        b.WINDOWS._max = 3        # 父任务占 1，剩 2 个给子代理
        try:
            out = self._call("copilot-auto")
        finally:
            b.WINDOWS._max = saved_max
        self.assertFalse(out["parallel"], "网页通道不是「提供商并行」语义")
        self.assertEqual(out["concurrency"], 2)
        self.assertGreaterEqual(self.peak, 2, f"网页子代理没并行起来（峰值 {self.peak}）")
        self.assertLess(out["seconds"], 0.7, f"应并行完成（{out['seconds']}s）")

    def test_copilot_children_do_not_inherit_parent_page(self):
        """子代理必须清掉继承来的页面上下文，否则会驱动父任务那个标签页。"""
        seen = []

        def fn(task, description="", model="", max_steps=40):
            async def run():
                seen.append(b._PAGE_CTX.get())
                return {"agent_id": "sub-" + task, "steps": 1, "content": "ok"}
            return run()

        from cbp.tools_extra import register_extra_tools
        b.AGENT_CFG["subagent_parallel"] = 4
        b._SESSION_CTX.set("p1")
        parent_page = object()
        tok = b._PAGE_CTX.set(parent_page)     # 模拟"父任务正占着一个窗口页面"
        saved_max = b.WINDOWS._max
        b.WINDOWS._max = 3
        try:
            impl = register_extra_tools(b.AGENT_TOOLS, {}, subagent_fn=fn)
            asyncio.run(impl.subagent_many(tasks=["a", "b"], model="copilot-auto"))
        finally:
            b.WINDOWS._max = saved_max
            b._PAGE_CTX.reset(tok)
        self.assertTrue(seen, "子代理应被调用")
        for page in seen:
            self.assertIsNone(page, "子代理继承了父任务的页面上下文（会抢同一标签页）")


class DriveChildConcurrentTests(unittest.TestCase):
    """真代码路径：并发跑两个 `_drive_child`（LLM 换成假的），验证上下文隔离。

    没有 provider key 时这是能拿到的最强证据：走真实 runner（工具循环、step 落库、
    上下文设置/复位），只把模型回复换成测试给定的内容。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_drive_"))
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, b.AGENT_CFG)
        ws = _setup(self.tmp)
        for sid in ("c1", "c2"):
            b.AGENT_SESSIONS.create(sid, "子任务" + sid, str(ws), "deepseek/x")
        self.observed = []
        self._orig = b.provider_turn

    def tearDown(self):
        b.provider_turn = self._orig
        (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, b.AGENT_CFG) = self.saved

    def test_two_children_keep_their_own_context(self):
        async def fake_provider_turn(sid, model_id, prompt, force_new=False):
            self.observed.append((sid, b.current_session(), b.web_copilot_allowed(),
                                  b.subagent_chain_depth()))
            await asyncio.sleep(0.05)
            return '{"type":"final","content":"完成:%s"}' % sid

        b.provider_turn = fake_provider_turn

        async def main():
            return await asyncio.gather(
                b._drive_child("c1", "任务一", "deepseek/x", 3, parent_sid="p",
                               parent_root=str(self.tmp), is_provider=True),
                b._drive_child("c2", "任务二", "deepseek/x", 3, parent_sid="p",
                               parent_root=str(self.tmp), is_provider=True),
            )

        r1, r2 = asyncio.run(main())
        self.assertIsNone(r1.get("error"), r1.get("error"))
        self.assertIsNone(r2.get("error"), r2.get("error"))
        self.assertIn("完成:c1", r1["content"])
        self.assertIn("完成:c2", r2["content"])
        # 每条模型调用必须看到**自己**的会话 id，且提供商通道不允许网页工具
        for sid, seen_sid, web_ok, depth in self.observed:
            self.assertEqual(seen_sid, sid, f"{sid} 看到了别人的上下文 {seen_sid}")
            self.assertFalse(web_ok, "提供商通道不该允许网页 Copilot 工具")
            self.assertEqual(depth, 1, "子代理内部的任务链深度应为 1")


class TaskContextIsolationTests(unittest.TestCase):
    """并行的前提：任务级上下文互不干扰（否则会串会话/串工具集）。"""

    def test_contextvars_are_per_task(self):
        seen = {}

        async def worker(name):
            b._SESSION_CTX.set("sid-" + name)
            b._WEB_TOOLS_CTX.set(name == "a")        # a 允许网页工具，b 不允许
            b._SUBAGENT_DEPTH_CTX.set(1)
            await asyncio.sleep(0.05)                 # 让另一个任务插进来改全局
            seen[name] = (b.current_session(), b.web_copilot_allowed(),
                          b.subagent_chain_depth())

        async def main():
            await asyncio.gather(worker("a"), worker("b"))

        asyncio.run(main())
        self.assertEqual(seen["a"], ("sid-a", True, 1))
        self.assertEqual(seen["b"], ("sid-b", False, 1), "任务 b 不该被 a 覆盖")

    def test_depth_guard_uses_chain_not_active_count(self):
        """并行时"活跃子代理数"会 >1，不能拿它判断递归深度。"""
        async def main():
            # 模拟：两个子代理同时活跃（活跃计数=2），但任务链深度只有 1
            b._SUBAGENT_ACTIVE["sub-x"] = time.time()
            b._SUBAGENT_ACTIVE["sub-y"] = time.time()
            for sid in ("sub-x", "sub-y"):
                b.AGENT_SESSIONS.patch(sid, state="running") if b.AGENT_SESSIONS.get(sid) else None
            b._SUBAGENT_DEPTH_CTX.set(0)
            deep_ok = b.subagent_chain_depth() < b._SUBAGENT_MAX_DEPTH
            b._SUBAGENT_DEPTH_CTX.set(1)
            blocked = b.subagent_chain_depth() >= b._SUBAGENT_MAX_DEPTH
            return deep_ok, blocked

        tmp = Path(tempfile.mkdtemp(prefix="cbp_par2_"))
        saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root)
        try:
            _setup(tmp)
            for sid in ("sub-x", "sub-y"):
                b.AGENT_SESSIONS.create(sid, "x", str(tmp), "m")
            deep_ok, blocked = asyncio.run(main())
            self.assertTrue(deep_ok, "链深度 0 时应允许派生子代理（不受活跃数影响）")
            self.assertTrue(blocked, "链深度达到上限时应拦住嵌套")
        finally:
            (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root) = saved
            b._SUBAGENT_ACTIVE.clear()


if __name__ == "__main__":
    unittest.main()
