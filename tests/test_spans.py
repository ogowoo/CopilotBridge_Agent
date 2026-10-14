"""本地 span：字段对齐 OTel GenAI 语义约定、并发安全、失败不影响主流程。"""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.spans import SpanRecorder, Span


class SpanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_spans_"))
        self.rec = SpanRecorder(self.tmp / "spans.jsonl", enabled=True, max_lines=100)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_otel_field_names(self):
        with Span(self.rec, "tool.read_text", "execute_tool",
                  **{"gen_ai.tool.name": "read_text", "gen_ai.tool.call.id": "s1-3",
                     "gen_ai.request.model": "copilot-auto", "cbp.session_id": "s1",
                     "cbp.step": 3}) as sp:
            sp.set(**{"cbp.result_chars": 42})
        rec = self.rec.tail(1)[0]
        attrs = rec["attributes"]
        self.assertEqual(attrs["gen_ai.operation.name"], "execute_tool")
        self.assertEqual(attrs["gen_ai.tool.name"], "read_text")
        self.assertEqual(attrs["cbp.result_chars"], 42)
        self.assertEqual(rec["status"], "ok")
        for key in ("trace_id", "span_id", "start_time", "end_time", "duration_ms"):
            self.assertIn(key, rec)
        # 会话 id 不能拿 trace_id 冒充
        self.assertNotEqual(attrs["cbp.session_id"], rec["trace_id"])

    def test_error_captured_not_raised(self):
        try:
            with Span(self.rec, "tool.boom", "execute_tool") as sp:
                raise ValueError("炸了")
        except ValueError:
            pass
        rec = self.rec.tail(1)[0]
        self.assertEqual(rec["status"], "error")
        self.assertIn("炸了", rec["error"])

    def test_async_usage_and_duration(self):
        async def run():
            async with Span(self.rec, "agent.turn", "invoke_agent",
                            **{"gen_ai.request.model": "deepseek/deepseek-chat"}) as sp:
                await asyncio.sleep(0.01)
                sp.set(**{"cbp.answer_chars": 10})
        asyncio.run(run())
        rec = self.rec.tail(1)[0]
        self.assertEqual(rec["attributes"]["gen_ai.operation.name"], "invoke_agent")
        self.assertGreaterEqual(rec["duration_ms"], 5)

    def test_rotation_keeps_recent(self):
        for i in range(150):
            with Span(self.rec, f"op{i}", "execute_tool") as sp:
                sp.set(**{"cbp.step": i})
        recs = self.rec.tail(500)
        self.assertLessEqual(len(recs), 120, "超过上限应轮转")
        self.assertEqual(recs[-1]["attributes"]["cbp.step"], 149, "应保留最近的")

    def test_disabled_recorder_writes_nothing(self):
        rec = SpanRecorder(self.tmp / "off.jsonl", enabled=False)
        with Span(rec, "x", "execute_tool"):
            pass
        self.assertFalse((self.tmp / "off.jsonl").exists())

    def test_tail_tolerates_broken_lines(self):
        p = self.tmp / "bad.jsonl"
        p.write_text('{"ok":1}\nNotJSON\n{"ok":2}\n', encoding="utf-8")
        rec = SpanRecorder(p, enabled=True)
        self.assertEqual(len(rec.tail(10)), 2)


if __name__ == "__main__":
    unittest.main()
