"""限流与发送失败的分类：只有真正的服务侧限流才启动固定窗口与前端倒计时。

背景（实测 UI 误报）：发送失败/浏览器未就绪曾被归为 CopilotServiceUnavailable，
于是界面弹出"Copilot 限流，预计恢复 57:59"的倒计时浮窗，但实际并未被限流。
"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.agent import (AgentRunner, CopilotRateLimited, CopilotSendFailed,
                       CopilotServiceUnavailable)
from gantry.tools import ToolRegistry


class ErrorTaxonomyTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_tax_"))
        self.state = {"state": "ready"}
        self.steps = []

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _runner(self, chat, config=None):
        cfg = {"max_total_steps": 8, "send_retry_base_seconds": 0.01}
        cfg.update(config or {})
        return AgentRunner(ToolRegistry(self.ws, {}), chat, cfg,
                           state_getter=lambda: dict(self.state),
                           state_saver=lambda **kw: self.state.update(kw),
                           step_saver=lambda s: self.steps.append(s),
                           file_event_saver=lambda e: None)

    def test_send_failure_is_not_rate_limit(self):
        async def chat(prompt, model, make_new):
            raise CopilotSendFailed("问题停留在输入框")
        result = asyncio.run(self._runner(chat).run("写个文件", "m"))
        self.assertIsNone(self.state.get("retry_resume_at"),
                          "发送失败不得设置限流恢复时间（否则 UI 误报限流）")
        self.assertIsNone(self.state.get("rate_limited_since"))
        statuses = [s.get("status") for s in self.steps]
        self.assertIn("send_failed", statuses)
        self.assertNotIn("service_unavailable", statuses)
        self.assertIn("未能把消息送进", result.content)

    def test_rate_limit_does_set_window(self):
        """真限流会设置恢复时间；max_service_unavailable=1 让它跑一次就收尾（不在测试里长退避）。"""
        async def chat(prompt, model, make_new):
            raise CopilotRateLimited("We're temporarily unable to respond")
        snaps = []
        runner = self._runner(chat, {"max_total_steps": 8, "max_service_unavailable": 1,
                                     "service_retry_base_seconds": 1, "service_retry_max_seconds": 1})
        inner = runner.state_saver

        def saver(**kw):
            snaps.append(dict(kw))
            inner(**kw)

        runner.state_saver = saver
        asyncio.run(runner.run("写个文件", "m"))
        # 限流窗口在第一次超限时设置；达到重试上限后会被清掉（避免浮窗一直显示），
        # 所以断言"过程中曾经设置过"，而不是最终仍存在。
        self.assertTrue(any(s.get("retry_resume_at") for s in snaps), "真限流应设置过恢复时间")
        self.assertIn("service_unavailable", [s.get("status") for s in self.steps])

    def test_send_failed_is_not_subclass_of_service_unavailable(self):
        self.assertFalse(issubclass(CopilotSendFailed, CopilotServiceUnavailable))
        self.assertTrue(issubclass(CopilotRateLimited, CopilotServiceUnavailable))


if __name__ == "__main__":
    unittest.main()
