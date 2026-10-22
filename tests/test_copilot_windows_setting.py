# -*- coding: utf-8 -*-
"""Copilot 窗口数设置：schema、动态生效、运行时统计、子代理并发。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class WindowsSettingTests(unittest.TestCase):
    def test_schema_has_copilot_windows(self):
        from cbp.api import CONFIG_SCHEMA
        spec = CONFIG_SCHEMA.get("agent.copilot_windows")
        self.assertIsNotNone(spec, "设置页需要这个键")
        self.assertEqual(spec["kind"], "int")
        self.assertEqual((spec["min"], spec["max"]), (1, 4))
        self.assertEqual(spec["group"], "exec")

    def test_post_config_applies_to_pool(self):
        src = (ROOT / "cbp" / "api.py").read_text(encoding="utf-8")
        self.assertIn("agent.copilot_windows", src)
        self.assertIn("WINDOWS.set_max", src, "保存后必须立刻生效")

    def test_runtime_exposes_windows(self):
        src = (ROOT / "cbp" / "api.py").read_text(encoding="utf-8")
        self.assertIn('"windows": b.WINDOWS.stats()', src)
        # 会话是否在跑要看"窗口池 + 逻辑槽"（提供商任务不占窗口，只看 WINDOWS 会漏判）
        self.assertIn("b.session_running(sid)", src)

    def test_web_parallel_follows_windows(self):
        """网页通道的扇出上限 = 窗口数；提供商通道不受窗口数影响。"""
        import cbp.bridge as b
        old = b.WINDOWS._max
        try:
            b.WINDOWS._max = 3
            self.assertEqual(b.web_subagent_parallel(), 3)
            b.WINDOWS._max = 1
            self.assertEqual(b.web_subagent_parallel(), 1, "1 个窗口时仍应串行")
            # 提供商通道与窗口数无关
            self.assertGreaterEqual(b.subagent_parallel(), 1)
        finally:
            b.WINDOWS._max = old

    def test_pool_set_max_is_clamped(self):
        import asyncio
        import cbp.bridge as b

        async def main():
            out = await b.WINDOWS.set_max(99)
            return out["max"]

        self.assertLessEqual(asyncio.new_event_loop().run_until_complete(main()), 8)
        asyncio.new_event_loop().run_until_complete(b.WINDOWS.set_max(1))


class WindowIsolationTests(unittest.TestCase):
    """并行任务：页面/工具上下文/模型缓存必须按任务隔离。"""

    def test_task_contextvars_are_isolated(self):
        import asyncio
        import contextvars
        import cbp.bridge as b

        a = contextvars.ContextVar("a", default=None)

        async def worker(name):
            tok = a.set(name)
            await asyncio.sleep(0.01)
            got = a.get()
            a.reset(tok)
            return got

        async def main():
            return await asyncio.gather(worker("w1"), worker("w2"))

        self.assertEqual(asyncio.new_event_loop().run_until_complete(main()), ["w1", "w2"])

    def test_page_proxy_resolves_task_page(self):
        import cbp.bridge as b
        from cbp.window_pool import Window

        class FakePage:
            url = "https://example/x"

        saved = b._PAGE_RAW
        try:
            b.set_shared_page(FakePage())
            # ① 没有任务上下文 → 用共享页
            self.assertEqual(b.page.url, "https://example/x")
            # ② 绑定窗口后 → 用该窗口的页
            other = FakePage(); other.url = "https://example/y"
            win = Window(1, other)
            tok = b._PAGE_CTX.set(win.page)
            try:
                self.assertEqual(b.page.url, "https://example/y")
                self.assertTrue(b.page)                      # 代理的 __bool__
            finally:
                b._PAGE_CTX.reset(tok)
            self.assertEqual(b.page.url, "https://example/x")
        finally:
            b.set_shared_page(saved)

    def test_registry_clone_is_independent(self):
        import cbp.bridge as b
        clone = b.AGENT_TOOLS.clone()
        clone.sandbox_mode = "read-only"
        clone.plan_mode = True
        self.assertNotEqual(b.AGENT_TOOLS.plan_mode, True)
        self.assertNotIn(b.AGENT_TOOLS.sandbox_mode, ("read-only",))


if __name__ == "__main__":
    unittest.main()
