# -*- coding: utf-8 -*-
"""发送失败退避期间的暂停：CopilotSendFailed 的 backoff 循环必须检查 paused。

真实场景（issue #5）：任务在"消息未能发送"的退避里，用户点暂停，runner 不理会，
退避结束后又自动重发——表现为"暂停按钮点了没反应"。
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


class SendFailurePauseTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="gantry_sendpause_"))
        (self.ws / "a.txt").write_text("x\n", encoding="utf-8")

    def test_pause_during_send_backoff_stops_runner(self):
        """退避中点暂停：runner 必须停放，不再发新回合。"""
        state = {"state": "running", "total_steps": 0}
        calls = {"n": 0}

        async def chat(prompt, model, force_new):
            calls["n"] += 1
            # 每回合都发不出去
            raise b.CopilotSendFailed("模拟发送失败")

        runner = AgentRunner(
            tools=ToolRegistry(self.ws, {}), chat=chat, config={
                "send_retry_base_seconds": 60,   # 长退避，留出暂停窗口
                "max_service_unavailable": 10,
            },
            state_getter=lambda: dict(state),
            state_saver=lambda **kw: state.update(kw),
            step_saver=lambda s: None, file_event_saver=lambda e: None,
        )

        async def run_and_pause():
            task = asyncio.create_task(runner.run("发一条消息", "copilot-auto"))
            await asyncio.sleep(2)              # 让它进入退避
            state["state"] = "paused"           # 模拟用户点暂停
            await asyncio.sleep(2)              # 退避循环应检测到并停放
            # runner 此时应停在暂停循环里，不再发新回合
            n_after_pause = calls["n"]
            state["state"] = "cancelled"        # 结束
            await asyncio.sleep(1)
            return n_after_pause, task

        try:
            n_paused, task = asyncio.run(run_and_pause())
            self.assertEqual(n_paused, 1, "暂停后不应再发新回合")
        finally:
            try:
                task.cancel()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
