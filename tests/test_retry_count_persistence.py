# -*- coding: utf-8 -*-
"""限流重试计数必须跨"挂起→恢复"延续，不能每次重置为 1。

真实场景（issue #5）：任务被限流挂起后恢复，界面显示"自动重试 1 次"——
因为新 runner 的 service_failures 从 0 开始。用户分不清到底是第几次重试。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry import bridge as b
from gantry.agent import AgentRunner
from gantry.tools import ToolRegistry


class RetryCountPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="gantry_retrycount_"))
        (self.ws / "a.txt").write_text("x\n", encoding="utf-8")

    def test_retry_attempt_persists_across_resume(self):
        """runner 重启后，service_failures 从持久化的 retry_attempt 继续。"""
        state = {"state": "running", "total_steps": 0,
                 "retry_attempt": 7, "retry_note": "window"}

        async def chat(prompt, model, force_new):
            # 第一轮就被限流
            raise b.CopilotRateLimited("模拟限流", window_seconds=None)

        runner = AgentRunner(
            tools=ToolRegistry(self.ws, {}), chat=chat, config={
                "max_service_unavailable": 10,
                "service_retry_base_seconds": 1,
            },
            state_getter=lambda: dict(state),
            state_saver=lambda **kw: state.update(kw),
            step_saver=lambda s: None, file_event_saver=lambda e: None,
        )

        try:
            result = asyncio.run(runner.run("测试", "copilot-auto"))
        except Exception:
            pass
        # 退避结束后会回循环顶部重发，而那次重发也会限流——
        # 关键是：第二次限流时 service_failures 必须是 8（7+1），不是 1。
        # 挂起时会写 retry_attempt；检查持久化的计数。
        self.assertEqual(int(state.get("retry_attempt") or 0), 8,
                         f"恢复后计数应延续：期望 retry_attempt=8，实际是 {state.get('retry_attempt')!r}")


if __name__ == "__main__":
    unittest.main()
