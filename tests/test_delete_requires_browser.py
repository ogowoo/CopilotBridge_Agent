# -*- coding: utf-8 -*-
"""远端会话删除：浏览器未就绪时要给出清晰错误，而不是 NoneType.goto 崩溃。

真事：浏览器按需启动（/health browser=idle），删除流程直接用 `page.goto`，
弹窗里显示 `'NoneType' object has no attribute 'goto'`（用户："重试远程删除失败"）。
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry import bridge as b
from gantry import copilot_delete as cd


class EnsurePageTests(unittest.TestCase):
    def setUp(self):
        self.saved_page = b.page
        self.saved_ensure = getattr(b, "ENSURE_BROWSER", None)

    def tearDown(self):
        b.page = self.saved_page
        if self.saved_ensure is None:
            if hasattr(b, "ENSURE_BROWSER"):
                delattr(b, "ENSURE_BROWSER")
        else:
            b.ENSURE_BROWSER = self.saved_ensure

    def test_raises_clear_error_when_no_browser(self):
        b.page = None
        b.ENSURE_BROWSER = lambda *a, **k: _false()

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(cd._ensure_page_for_delete())
        msg = str(ctx.exception)
        self.assertIn("浏览器未就绪", msg)
        self.assertNotIn("goto", msg)

    def test_delete_reports_error_instead_of_crashing(self):
        b.page = None
        b.ENSURE_BROWSER = lambda *a, **k: _false()

        out = asyncio.run(cd.delete_copilot_conversation(
            "https://m365.cloud.microsoft/chat/conversation/abc",
            "[CBP-AGENT][task-x][C001]"))
        self.assertFalse(out["deleted"])
        self.assertIn("浏览器未就绪", out["error"] or "")

    def test_finalize_without_results_does_not_touch_browser(self):
        b.page = None

        def boom(*a, **k):
            raise AssertionError("不该碰浏览器")
        b.ENSURE_BROWSER = boom
        out = asyncio.run(cd.finalize_session_remote_deletion("task-x", []))
        self.assertEqual(out, [])

    def test_ensure_ok_when_page_exists(self):
        b.page = object()
        saved_ready = cd.wait_copilot_ready
        cd.wait_copilot_ready = lambda *a, **k: _true()
        try:
            async def run():
                return await cd._ensure_page_for_delete()
            asyncio.run(run())      # 不抛即通过
        finally:
            cd.wait_copilot_ready = saved_ready

    def test_needs_login_reports_clear_error(self):
        """浏览器起来了但没登录（实测 browser=needs_login）→ 要提示去登录。"""
        b.page = object()
        saved_ready = cd.wait_copilot_ready
        cd.wait_copilot_ready = lambda *a, **k: _false()
        try:
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(cd._ensure_page_for_delete())
            self.assertIn("需要登录", str(ctx.exception))
        finally:
            cd.wait_copilot_ready = saved_ready


async def _false():
    return False


async def _true():
    return True


if __name__ == "__main__":
    unittest.main()
