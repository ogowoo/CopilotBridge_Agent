# -*- coding: utf-8 -*-
"""解析失败文案要按**模型来源**区分（此前硬编码 Copilot，Kimi 会话里误导）。

用户实测：用 Kimi（kimi-coding/k3）跑深研，时间线写成
"Copilot 未返回完整、可解析的 Agent JSON 动作" —— 明明不是 Copilot 的问题。
"""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.agent import AgentRunner
from cbp.tools import ToolRegistry


class ParseErrorSourceTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_src_"))

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _runner(self, state, answer):
        cfg = {"max_total_steps": 5}

        async def chat(prompt, model, make_new):
            return answer

        tools = ToolRegistry(self.ws, cfg)
        return AgentRunner(tools, chat, cfg,
                           state_getter=lambda: state,
                           state_saver=lambda **k: state.update(k),
                           step_saver=lambda s: None,
                           file_event_saver=lambda e: None)

    def test_provider_model_says_provider(self):
        state = {"state": "ready"}
        r = self._runner(state, "这不是 JSON")
        asyncio.run(r.run("随便问一句", "kimi-coding/k3"))
        steps = state.get("recent_steps") or []
        errs = [s.get("error") or "" for s in steps if s.get("status") == "invalid_action"]
        self.assertTrue(errs, "应记录 invalid_action 步骤")
        self.assertIn("提供商模型", errs[0])
        self.assertNotIn("Copilot", errs[0])

    def test_copilot_model_says_copilot(self):
        state = {"state": "ready"}
        r = self._runner(state, "这不是 JSON")
        asyncio.run(r.run("随便问一句", "copilot-auto"))
        steps = state.get("recent_steps") or []
        errs = [s.get("error") or "" for s in steps if s.get("status") == "invalid_action"]
        self.assertTrue(errs)
        self.assertIn("Copilot", errs[0])


if __name__ == "__main__":
    unittest.main()
