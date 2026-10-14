"""span 报表：百分位与聚合口径。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import spans as S  # noqa: E402  （scripts/spans.py）


def rec(op, ms, tool=None, model=None, sid="s1", status="ok", chars=0):
    a = {"gen_ai.operation.name": op, "cbp.session_id": sid}
    if tool:
        a["gen_ai.tool.name"] = tool
    if model:
        a["gen_ai.request.model"] = model
    a["cbp.result_chars"] = chars
    return {"status": status, "duration_ms": ms, "attributes": a}


class SpanReportTests(unittest.TestCase):
    def test_percentile(self):
        self.assertEqual(S.pct([], 0.5), 0.0)
        self.assertEqual(S.pct([10], 0.95), 10)
        self.assertAlmostEqual(S.pct([0, 10], 0.5), 5.0)
        self.assertAlmostEqual(S.pct(list(range(1, 101)), 0.95), 95.05, places=1)

    def test_aggregation_split_and_errors(self):
        recs = [rec("invoke_agent", 1000, model="copilot-auto"),
                rec("invoke_agent", 3000, model="copilot-auto", status="error"),
                rec("execute_tool", 2, tool="read_text", chars=120),
                rec("execute_tool", 4, tool="read_text", chars=80),
                rec("execute_tool", 6, tool="bash", status="error")]
        rep = S.analyse(recs)
        self.assertEqual(rep["spans"], 5)
        self.assertEqual(rep["errors"], 2)
        self.assertAlmostEqual(rep["error_rate"], 0.4, places=3)
        self.assertEqual(rep["time_split"]["remote_s"], 4.0)
        self.assertAlmostEqual(rep["time_split"]["local_s"], 0.012, places=2)  # 报表按 ms 舍入
        self.assertGreater(rep["time_split"]["remote_share"], 0.99)
        self.assertEqual(rep["by_model"]["copilot-auto"]["count"], 2)
        self.assertEqual(rep["by_model"]["copilot-auto"]["errors"], 1)
        self.assertEqual(rep["by_tool"]["read_text"]["count"], 2)
        self.assertEqual(rep["by_tool"]["read_text"]["avg_result_chars"], 100.0)
        self.assertEqual(rep["by_tool"]["bash"]["errors"], 1)
        self.assertEqual(rep["by_session"]["s1"]["turns"], 2)
        self.assertEqual(rep["by_session"]["s1"]["tools"], 3)


if __name__ == "__main__":
    unittest.main()
