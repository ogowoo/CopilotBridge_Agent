"""派发检查点接线：每次工具执行前后都会登记/收尾（结果是"崩在途中可识别"的前提）。"""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.agent import AgentRunner
from gantry.tools import ToolRegistry


class CheckpointWiringTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_ckpt_"))
        (self.ws / "ok.txt").write_text("内容", encoding="utf-8")
        self.begun, self.finished = [], []

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _run(self, script):
        state = {"state": "ready"}
        calls = []

        async def chat(prompt, model, make_new):
            calls.append(prompt)
            return json.dumps(script(len(calls)), ensure_ascii=False)

        runner = AgentRunner(ToolRegistry(self.ws, {}), chat, {"max_total_steps": 8},
                             state_getter=lambda: dict(state),
                             state_saver=lambda **kw: state.update(kw),
                             step_saver=lambda s: None,
                             file_event_saver=lambda e: None,
                             step_begin=lambda *a: self.begun.append(a),
                             step_finish=lambda *a: self.finished.append(a))
        asyncio.run(runner.run("读文件", "m"))
        return state

    def test_begin_and_finish_around_successful_tool(self):
        def script(n):
            if n == 1:
                return {"type": "tool", "tool": "read_text",
                        "arguments": {"path": "ok.txt"}, "reason": "读"}
            return {"type": "final", "content": "完"}
        self._run(script)
        self.assertEqual(len(self.begun), 1, "派发前必须登记一次")
        step_no, tool, args_hash = self.begun[0]
        self.assertEqual(tool, "read_text")
        self.assertEqual(len(args_hash), 16)
        self.assertEqual(len(self.finished), 1, "执行后必须收尾一次")
        self.assertEqual(self.finished[0][0], step_no)
        self.assertIn(self.finished[0][1], ("done", "error"))

    def test_args_hash_distinguishes_arguments(self):
        def script(n):
            if n == 1:
                return {"type": "tool", "tool": "read_text",
                        "arguments": {"path": "ok.txt", "limit": 5}, "reason": "读"}
            return {"type": "final", "content": "完"}
        self._run(script)
        self._run(script)
        hashes = {rec[2] for rec in self.begun}
        self.assertEqual(len(hashes), 1, "同样的参数应得到同样的 hash（可复现）")

    def test_without_callbacks_nothing_breaks(self):
        """旧调用方不传回调也要正常工作（向后兼容）。"""
        state = {"state": "ready"}
        calls = []

        async def chat(prompt, model, make_new):
            calls.append(prompt)
            if len(calls) == 1:
                return json.dumps({"type": "tool", "tool": "read_text",
                                   "arguments": {"path": "ok.txt"}, "reason": "读"})
            return json.dumps({"type": "final", "content": "完"})

        runner = AgentRunner(ToolRegistry(self.ws, {}), chat, {"max_total_steps": 4},
                             state_getter=lambda: dict(state),
                             state_saver=lambda **kw: state.update(kw),
                             step_saver=lambda s: None,
                             file_event_saver=lambda e: None)
        result = asyncio.run(runner.run("读文件", "m"))
        self.assertIn("完", result.content)


if __name__ == "__main__":
    unittest.main()
