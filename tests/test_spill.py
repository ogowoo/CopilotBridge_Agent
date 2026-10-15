"""结果 spill 落盘：超大结果写工作区 .spill/，上下文只留摘要+路径提示。"""
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


class SpillTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_spill_"))
        self.state = {"state": "ready"}
        self.steps = []

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _run(self, config=None, size=60000):
        (self.ws / "big.txt").write_text("A" * size, encoding="utf-8")
        calls = []

        async def chat(prompt, model, make_new):
            calls.append(prompt)
            if len(calls) == 1:
                return json.dumps({"type": "tool", "tool": "read_text",
                                   "arguments": {"path": "big.txt", "limit": size},
                                   "reason": "读大文件"})
            return json.dumps({"type": "final", "content": "完"})

        cfg = {"max_total_steps": 4}
        cfg.update(config or {})
        runner = AgentRunner(ToolRegistry(self.ws, {}), chat, cfg,
                             state_getter=lambda: dict(self.state),
                             state_saver=lambda **kw: self.state.update(kw),
                             step_saver=lambda s: self.steps.append(s),
                             file_event_saver=lambda e: None)
        runner._session_id = "spill-sid"
        asyncio.run(runner.run("读大文件", "m"))
        return calls

    def test_large_result_is_spilled_with_hint(self):
        self._run()
        step = next(s for s in self.steps if s.get("tool") == "read_text")
        self.assertIn("spill", step, "超阈值结果必须带落盘信息")
        rel = step["spill"]["spill_path"]
        self.assertTrue(rel.startswith(".spill/"), rel)
        spilled = self.ws / rel
        self.assertTrue(spilled.is_file(), "落盘文件必须真的存在")
        payload = json.loads(spilled.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(json.dumps(payload, ensure_ascii=False)), 60000,
                                "落盘的是完整内容，不是截断后的")
        self.assertIn("hint", step["spill"])
        self.assertIn("read_text", step["spill"]["hint"])

    def test_small_result_not_spilled(self):
        self._run(size=200)
        step = next(s for s in self.steps if s.get("tool") == "read_text")
        self.assertNotIn("spill", step)
        self.assertFalse((self.ws / ".spill").exists(), "小结果不该产生 .spill 目录")

    def test_spill_can_be_disabled(self):
        self._run(config={"spill_threshold_chars": 0})
        step = next(s for s in self.steps if s.get("tool") == "read_text")
        self.assertNotIn("spill", step)

    def test_spill_path_readable_by_agent_tools(self):
        """落盘路径必须能用普通工具读回（否则提示等于骗模型）。"""
        self._run()
        step = next(s for s in self.steps if s.get("tool") == "read_text")
        rel = step["spill"]["spill_path"]
        back = asyncio.run(ToolRegistry(self.ws, {}).execute(
            "read_text", {"path": rel, "limit": 20}))
        self.assertIn("content", str(back) + json.dumps(back, ensure_ascii=False)[:0] or True)
        self.assertTrue(isinstance(back, dict) and back.get("content") is not None)


if __name__ == "__main__":
    unittest.main()
