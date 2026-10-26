# -*- coding: utf-8 -*-
"""侧栏列表与会话页的数字必须来自同一口径。

用户："sessions 列表的 steps, session, token 跟会话页的数据不相符。"

## 查到的三处不一致
1. **步骤**：详情页 KPI 是「总步骤（含子代理）」= `total_steps + subagent_steps`（递归到孙），
   侧栏行只显示 `total_steps`（主会话自己）；
2. **Token**：同上（`estimated_tokens` vs `estimated_tokens + subagent_tokens`）；
3. **会话**：侧栏显示 `conversation_index + 1`（第几个对话），KPI 卡片却显示
   `conversation_index`（切换次数、0 起）——同一个任务两个"会话"数。

## 修法
- 后端：`SessionStore.subagent_totals_all()` 一次查询 + 后序汇总，算出**每个会话**
  的子 + 孙合计；`GET /v1/agent/sessions` 用它补 `subagent_steps/tokens/count/running`；
- 前端：侧栏行显示 `主 + 子` 的步骤/Token（虚线下划线 + `title` 给拆分），
  KPI 卡片「会话」改成 `conversation_index + 1`，与侧栏/活动面板一致。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

APP = (BASE / "webui" / "app.js").read_text(encoding="utf-8")
HTML = (BASE / "webui" / "index.html").read_text(encoding="utf-8")
API = ((BASE / "gantry" / "api.py").read_text(encoding="utf-8") + "".join((BASE / "gantry" / n).read_text(encoding="utf-8") for n in ("routes_core.py", "routes_config.py", "routes_agents.py", "routes_sessions.py")))
STORE = (BASE / "gantry" / "session_store.py").read_text(encoding="utf-8")


def _build_store(tmp: Path):
    from gantry.session_store import SessionStore

    store = SessionStore(tmp / "agent.db")
    ws = str(tmp)
    for sid, title, steps, tok in (
        ("parent1", "主任务", 100, 1000),
        ("sub1", "子1", 40, 400),
        ("sub2", "子2", 60, 600),
        ("grand1", "孙1", 10, 100),
        ("other", "别的任务", 999, 9999),
    ):
        store.create(sid, title, ws, "copilot-auto")
        store.patch(sid, total_steps=steps, estimated_tokens=tok)
    con = sqlite3.connect(tmp / "agent.db")
    con.execute("UPDATE sessions SET parent_id='parent1' WHERE id IN ('sub1','sub2')")
    con.execute("UPDATE sessions SET parent_id='sub1' WHERE id='grand1'")
    con.commit()
    con.close()
    return store


class TotalsAllTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = _build_store(self.tmp)

    def test_includes_children_and_grandchildren(self):
        totals = self.store.subagent_totals_all()
        p = totals["parent1"]
        self.assertEqual(p["count"], 3, "子 + 孙都要算")
        self.assertEqual(p["steps"], 110)
        self.assertEqual(p["tokens"], 1100)

    def test_leaf_and_unrelated_are_zero(self):
        totals = self.store.subagent_totals_all()
        self.assertEqual(totals["grand1"], {"count": 0, "steps": 0, "tokens": 0, "running": 0})
        self.assertEqual(totals["other"]["steps"], 0, "无关会话不能串进来")

    def test_running_counts_descendants(self):
        self.store.patch("sub1", state="running")
        self.store.patch("grand1", state="waiting")
        p = self.store.subagent_totals_all()["parent1"]
        self.assertEqual(p["running"], 2)

    def test_same_numbers_as_detail_side(self):
        """列表用的 totals_all 与详情页用的 subagent_stats 必须完全一致。"""
        a = self.store.subagent_totals_all()["parent1"]
        b = self.store.subagent_stats("parent1")
        self.assertEqual((a["steps"], a["tokens"], a["count"]),
                         (b["steps"], b["tokens"], b["count"]))


class ListEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = _build_store(self.tmp)
        from gantry import bridge as b
        self.b = b
        self.saved = b.AGENT_SESSIONS
        b.AGENT_SESSIONS = self.store
        from fastapi.testclient import TestClient
        from gantry import api as api_mod
        self.client = TestClient(api_mod.app)

    def tearDown(self):
        self.b.AGENT_SESSIONS = self.saved

    def test_list_row_carries_recursive_totals(self):
        rows = self.client.get("/v1/agent/sessions").json()["sessions"]
        row = next(x for x in rows if x["id"] == "parent1")
        self.assertEqual(row["total_steps"], 100, "主会话自己的步数不变")
        self.assertEqual(row["subagent_steps"], 110, "子 + 孙合计")
        self.assertEqual(row["subagent_tokens"], 1100)
        self.assertEqual(row["subagent_count"], 3)

    def test_list_row_matches_detail(self):
        rows = self.client.get("/v1/agent/sessions").json()["sessions"]
        row = next(x for x in rows if x["id"] == "parent1")
        detail = self.client.get("/v1/agent/sessions/parent1").json()["session"]
        for key in ("subagent_steps", "subagent_tokens", "subagent_count", "subagent_running"):
            self.assertEqual(row[key], detail[key], f"{key} 列表与详情不一致")


class FrontendConsistencyTests(unittest.TestCase):
    def test_sidebar_row_sums_subagents(self):
        i = APP.index("const renderCard=(s,opts)=>{")
        seg = APP[i:i + 2600]
        self.assertIn("s.subagent_steps", seg)
        self.assertIn("s.subagent_tokens", seg)
        self.assertIn("_ownSteps+_subSteps", seg.replace(" ", ""), "步骤要主 + 子相加")
        self.assertIn("_ownTk+_subTk", seg.replace(" ", ""), "Token 要主 + 子相加")

    def test_kpi_session_index_is_one_based(self):
        i = APP.index("function setKpis(")
        seg = APP[i:i + 2600]
        self.assertIn("kpiRoll').textContent=(o.conversation_index??0)+1", seg.replace(" ", ""),
                      "KPI 的会话序号要与侧栏一致（+1）")

    def test_kpi_label_is_session_not_switches(self):
        self.assertIn('data-kpi="roll"', HTML)
        self.assertIn(">会话</span>", HTML)
        self.assertNotIn("会话切换 / TOKEN", HTML)

    def test_store_exposes_single_source_of_truth(self):
        self.assertIn("def subagent_totals_all", STORE)
        self.assertIn("subagent_totals_all", API, "列表端点要用同一口径")


if __name__ == "__main__":
    unittest.main()
