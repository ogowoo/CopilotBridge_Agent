# -*- coding: utf-8 -*-
"""窗口闪退的回归：release 不关页面、有任务时重建浏览器要跳过、失败不杀 Playwright。"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

POOL = (ROOT / "cbp" / "window_pool.py").read_text(encoding="utf-8")
# 浏览器/窗口池层已拆到 cbp/browser.py（bridge.py 第三批解耦），源码断言两处一起读
BRIDGE = ((ROOT / "cbp" / "bridge.py").read_text(encoding="utf-8")
          + (ROOT / "cbp" / "browser.py").read_text(encoding="utf-8"))
API = (ROOT / "cbp" / "api.py").read_text(encoding="utf-8")


class FakePage:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class ReleaseNeverClosesTests(unittest.TestCase):
    def test_release_keeps_page_open(self):
        """用户实测：开任务后自动化窗口闪退——release 里那条 close 必须不存在。"""
        i = POOL.find("    def release(self, win: Window) -> None:")
        j = POOL.find("    # ── 状态", i)
        body = POOL[i:j]
        self.assertNotIn("page.close()", body, "归还窗口时不能关页面")
        self.assertIn("_free.put_nowait(win)", body)

    def test_release_puts_window_back(self):
        from cbp.window_pool import Window, WindowPool

        async def main():
            made = []

            async def factory(i):
                made.append(i)
                return FakePage()

            pool = WindowPool(factory, max_windows=2)
            w = await pool.acquire("s1")
            pool.release(w)
            self.assertEqual(pool.stats()["busy"], 0)
            self.assertFalse(w.page.closed, "页面不能被关")
            w2 = await pool.acquire("s2")
            self.assertIs(w2, w, "应复用同一个窗口")
            pool.release(w2)
            return True

        self.assertTrue(asyncio.new_event_loop().run_until_complete(main()))


class NoRelaunchWhileBusyTests(unittest.TestCase):
    def test_start_browser_skips_when_busy(self):
        i = BRIDGE.find("async def _start_browser(")
        j = BRIDGE.find("\n\ndef ", i)
        body = BRIDGE[i:j]
        self.assertIn("WINDOWS.running()", body, "有任务在用浏览器时不能重建")
        self.assertIn("跳过重建", body)

    def test_boot_failure_keeps_browser_when_busy(self):
        self.assertIn("WINDOWS.running()", API)
        self.assertIn("保留当前窗口", API)

    def test_window_waits_for_browser(self):
        i = BRIDGE.find("async def _window(")
        j = BRIDGE.find("\n\ndef ", i)
        body = BRIDGE[i:j]
        self.assertIn("ensure_browser_ready()", body, "取窗口前要先确保浏览器起来")


if __name__ == "__main__":
    unittest.main()
