# -*- coding: utf-8 -*-
"""KPI 的步数/token 必须把子代理算进来。

用户："我发现步数/token 等信息不会统计 subagents 的数据。"

## 原因
子代理是**独立会话**，`total_steps` / `estimated_tokens` 各自记账；主会话的 KPI 只读
自己的字段 → 跑了几个子代理、几百步，界面上完全看不出来（只有主会话的数字）。

## 修法
- `SessionStore.subagent_stats(sid)`:沿 `parent_id` 向下遍历（默认 4 层、防环），
  汇总子 + 孙的 `total_steps` / `estimated_tokens` / 数量；
- `/v1/agent/sessions/{sid}` 与 `_runtime_snapshot()` 都带上
  `subagent_steps` / `subagent_tokens` / `subagent_count`;
- 前端 `setKpis()` 显示**合计**,并在 `title` 里给出拆分
  「本会话 100 + 子代理 110（3 个）」,「总步骤」卡片上加小标「含 3 个子代理」。

## 实测（父 100 步/1000 token + 子1 40/400 + 子2 60/600 + 孙1 10/100，另有一个无关会话 999/9999）
- 接口：`subagent_steps=110, subagent_tokens=1100, subagent_count=3`（无关会话未计入）
- 界面：总步骤 **210**、Token **2,100**,title 给出拆分，小标「含 3 个子代理」。
"""
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

APP = (BASE / "webui" / "app.js").read_text(encoding="utf-8")
API = ((BASE / "cbp" / "api.py").read_text(encoding="utf-8") + "".join((BASE / "cbp" / n).read_text(encoding="utf-8") for n in ("routes_core.py", "routes_config.py", "routes_agents.py", "routes_sessions.py")))
STORE = (BASE / "cbp" / "session_store.py").read_text(encoding="utf-8")


def _mk_store(tmp: Path):
    from cbp.session_store import SessionStore
    return SessionStore(tmp / "agent.db")


class SubagentStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = _mk_store(self.tmp)
        import sqlite3
        self.db = self.tmp / "agent.db"
        ws = str(self.tmp)
        for sid, title, steps, tok in (
            ("parent1", "主任务", 100, 1000),
            ("sub1", "子1", 40, 400),
            ("sub2", "子2", 60, 600),
            ("sub3", "孙1", 10, 100),
            ("other", "别的任务", 999, 9999),
        ):
            self.store.create(sid, title, ws, "copilot-auto")
            self.store.patch(sid, total_steps=steps, estimated_tokens=tok)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE sessions SET parent_id='parent1' WHERE id IN ('sub1','sub2')")
        con.execute("UPDATE sessions SET parent_id='sub1' WHERE id='sub3'")
        con.commit()
        con.close()

    def test_aggregates_children_and_grandchildren(self):
        st = self.store.subagent_stats("parent1")
        self.assertEqual(st["count"], 3, "子 + 孙都要算")
        self.assertEqual(st["steps"], 110)
        self.assertEqual(st["tokens"], 1100)

    def test_excludes_unrelated_sessions(self):
        st = self.store.subagent_stats("parent1")
        self.assertNotIn("other", st["ids"])
        self.assertLess(st["steps"], 999, "无关会话不能计入")

    def test_leaf_has_no_subagents(self):
        st = self.store.subagent_stats("sub3")
        self.assertEqual((st["count"], st["steps"], st["tokens"]), (0, 0, 0))

    def test_unknown_session_is_inert(self):
        st = self.store.subagent_stats("nope")
        self.assertEqual(st["count"], 0)


class ApiExposureTests(unittest.TestCase):
    def test_runtime_snapshot_has_subagent_fields(self):
        for key in ("subagent_steps", "subagent_tokens", "subagent_count"):
            self.assertIn(f'"{key}"', API, f"runtime 快照缺 {key}")

    def test_session_detail_merges_totals(self):
        i = API.index('@router.get("/v1/agent/sessions/{sid}")')  # 端点已拆到 routes_sessions.py
        seg = API[i:i + 1400]
        self.assertIn("subagent_stats", seg)
        self.assertIn('x["subagent_steps"]', seg)


class FrontendKpiTests(unittest.TestCase):
    def test_kpi_sums_subagents(self):
        i = APP.index("function setKpis(")
        seg = APP[i:i + 2200]
        self.assertIn("subagent_steps", seg)
        self.assertIn("subagent_tokens", seg)
        self.assertIn("_ownSteps+_ss", seg.replace(" ", ""), "总步骤要主 + 子相加")

    def test_kpi_tooltip_breaks_down(self):
        i = APP.index("function setKpis(")
        seg = APP[i:i + 2200]
        self.assertIn("本会话 {a} + 子代理 {b}", seg, "title 要给拆分说明")

    def test_kpi_note_element_exists(self):
        html = (BASE / "webui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="kpiSubNote"', html)

    def test_css_marks_subagent_numbers(self):
        css = (BASE / "webui" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".kpi .kpisub", css)
        self.assertIn('b[data-sub="1"]', css)


class StoreSourceTests(unittest.TestCase):
    def test_depth_guard(self):
        i = STORE.index("def subagent_stats")
        seg = STORE[i:i + 2000]
        self.assertIn("max_depth", seg)
        self.assertIn("seen", seg, "要防环（父指向自己时不能死循环）")


if __name__ == "__main__":
    unittest.main()
