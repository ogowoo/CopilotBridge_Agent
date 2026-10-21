# -*- coding: utf-8 -*-
"""窗口池单测（纯逻辑，不碰浏览器）。"""
import asyncio
import unittest


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakePage:
    def __init__(self, idx):
        self.idx = idx
        self.closed = False

    async def close(self):
        self.closed = True


class PoolTests(unittest.TestCase):
    def pool(self, max_windows=2):
        from cbp.window_pool import WindowPool
        made = []

        async def factory(index):
            made.append(index)
            return FakePage(index)

        p = WindowPool(factory, max_windows=max_windows)
        return p, made

    def test_single_window_serializes(self):
        async def main():
            p, made = self.pool(1)
            w = await p.acquire("s1")
            self.assertEqual(len(p._windows), 1)
            # 第二个请求必须排队：不释放就超时
            with self.assertRaises(Exception):
                await p.acquire("s2", wait_timeout=0.05)
            p.release(w)
            w2 = await p.acquire("s2", wait_timeout=1.0)
            self.assertIs(w, w2)
            p.release(w2)
            return made
        self.assertEqual(run(main()), [1])

    def test_two_windows_run_in_parallel(self):
        async def main():
            p, made = self.pool(2)
            w1 = await p.acquire("a")
            w2 = await p.acquire("b")
            self.assertNotEqual(w1.index, w2.index)
            self.assertEqual(sorted(p.running()), ["a", "b"])
            self.assertTrue(p.is_running("a"))
            p.release(w1)
            p.release(w2)
            self.assertEqual(p.stats()["busy"], 0)
            return made
        self.assertEqual(run(main()), [1, 2])

    def test_set_max_shrinks_idle_windows(self):
        async def main():
            p, _ = self.pool(3)
            held = [await p.acquire(sid) for sid in ("a", "b", "c")]   # 同时持有才会开 3 个
            self.assertEqual(p.stats()["windows"], 3)
            for w in held:
                p.release(w)
            out = await p.set_max(1)
            self.assertEqual(out["max"], 1)
            return p.stats()["windows"]
        self.assertLessEqual(run(main()), 1)

    def test_set_max_keeps_busy_windows(self):
        async def main():
            p, _ = self.pool(3)
            w1 = await p.acquire("a")
            await p.set_max(1)
            self.assertIn("a", p.running())     # 正在跑的不打断
            p.release(w1)
            return True
        self.assertTrue(run(main()))

    def test_queue_limit(self):
        from cbp.window_pool import WindowBusy
        async def main():
            p, _ = self.pool(1)
            w = await p.acquire("a")
            # 把等待队列塞满（max(4, 1*3) = 4）
            tasks = [asyncio.create_task(p.acquire(f"q{i}", wait_timeout=5)) for i in range(4)]
            await asyncio.sleep(0.05)
            with self.assertRaises(WindowBusy):
                await p.acquire("overflow", wait_timeout=5)
            p.release(w)
            await asyncio.sleep(0.05)
            for t in tasks:
                t.cancel()
            return True
        self.assertTrue(run(main()))


if __name__ == "__main__":
    unittest.main()
