# -*- coding: utf-8 -*-
"""父任务显示 completed、子代理却还在跑 —— 状态必须如实反映。

用户："task 的状态在 subagent 还在跑的时候会显示是 completed，看看是什么问题。"

## 查到的两个成因
1. 父任务收尾时 `cancel_running_children()` **只取消直接子会话**
   （`WHERE parent_id=?`）,孙代理（workflow → subagent 这种两层）不会被取消,
   于是"父已 completed、孙还在跑";
2. 就算子孙被取消/或仍在收尾，界面也**只按父会话的 state 渲染**:
   侧栏绿色「已完成」、KPI 绿色状态卡 —— 用户看到绿色就以为整个任务结束了。

## 修法
- `cancel_running_children()` 改成**递归**取消所有后代（上限 6 层、`seen` 防环）;
- `subagent_stats()` 增加 `running` 计数；API 暴露 `subagent_running`
  （详情 + runtime 快照；列表端点用一条 `subagent_totals_all()` 批量补全（含孙），
  避免 N+1 且与详情页同一口径）;
- 前端 `stateLabel()` / `stateClassOf()`:父会话 state 已结束但 `subagent_running>0`
  时显示「已完成 · 子代理 N 仍在跑」并把颜色降为琥珀（KPI 卡 + 侧栏行都生效）,
  等后代真正结束再回到绿色「已完成」。

## 实测
| 场景 | KPI | 卡片 | 侧栏 |
|---|---|---|---|
| 子代理 running | 已完成 · 子代理 1 仍在跑 | waiting（琥珀）| 同文案，`sstat warn` |
| 子代理 completed | 已完成 | completed | `sstat done` |
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

STORE = (BASE / "gantry" / "session_store.py").read_text(encoding="utf-8")
API = ((BASE / "gantry" / "api.py").read_text(encoding="utf-8") + "".join((BASE / "gantry" / n).read_text(encoding="utf-8") for n in ("routes_core.py", "routes_config.py", "routes_agents.py", "routes_sessions.py")))
APP = (BASE / "webui" / "app.js").read_text(encoding="utf-8")

from gantry.session_store import SessionStore  # noqa: E402


def _setup(tmp: Path, states=("running", "completed")):
    store = SessionStore(tmp / "agent.db")
    ws = str(tmp)
    store.create("parent1", "主任务", ws, "copilot-auto")
    store.patch("parent1", state="completed", total_steps=100, estimated_tokens=1000)
    store.create("sub1", "子代理", ws, "copilot-auto")
    store.patch("sub1", total_steps=40, estimated_tokens=400)
    store.create("grand1", "孙代理", ws, "copilot-auto")
    store.patch("grand1", total_steps=10, estimated_tokens=100)
    con = sqlite3.connect(tmp / "agent.db")
    con.execute("UPDATE sessions SET parent_id='parent1' WHERE id='sub1'")
    con.execute("UPDATE sessions SET parent_id='sub1' WHERE id='grand1'")
    con.commit()
    con.close()
    return store


class RecursiveCancelTests(unittest.TestCase):
    def test_cancels_grandchildren_too(self):
        tmp = Path(tempfile.mkdtemp())
        store = _setup(tmp)
        store.patch("sub1", state="running")
        store.patch("grand1", state="running")
        n = store.cancel_running_children("parent1", "父任务已完成")
        self.assertEqual(n, 2, "子 + 孙都必须被取消（原来只取消直接子会话）")
        self.assertEqual(store.get("sub1")["state"], "cancelled")
        self.assertEqual(store.get("grand1")["state"], "cancelled")

    def test_does_not_touch_unrelated(self):
        tmp = Path(tempfile.mkdtemp())
        store = _setup(tmp)
        store.create("other", "别的任务", str(tmp), "copilot-auto")
        store.patch("other", state="running")
        store.patch("sub1", state="running")
        store.cancel_running_children("parent1", "done")
        self.assertEqual(store.get("other")["state"], "running")


class RunningCountTests(unittest.TestCase):
    def test_stats_counts_running(self):
        tmp = Path(tempfile.mkdtemp())
        store = _setup(tmp)
        store.patch("sub1", state="running")
        store.patch("grand1", state="running")
        st = store.subagent_stats("parent1")
        self.assertEqual(st["running"], 2)
        self.assertEqual(st["count"], 2)

    def test_summary_by_parent_is_one_row_per_parent(self):
        tmp = Path(tempfile.mkdtemp())
        store = _setup(tmp)
        store.patch("sub1", state="running")
        agg = store.subagent_summary_by_parent()
        self.assertIn("parent1", agg)
        self.assertEqual(agg["parent1"]["running"], 1)
        self.assertEqual(agg["sub1"]["count"], 1, "孙代理挂在 sub1 下")


class ApiAndFrontendTests(unittest.TestCase):
    def test_api_exposes_subagent_running(self):
        self.assertIn('"subagent_running"', API)
        i = API.index('@router.get("/v1/agent/sessions")')  # 端点已拆到 routes_sessions.py
        seg = API[i:i + 1800]
        self.assertTrue("subagent_totals_all" in seg or "subagent_summary_by_parent" in seg,
                        "列表端点要批量补子代理状态，否则侧栏徽标永远是绿色")

    def test_frontend_state_label_mentions_running_subagents(self):
        self.assertIn("function stateLabel", APP)
        self.assertIn("function stateClassOf", APP)
        # 直接在整个文件里找（函数体里可能跨多行，切片容易切偏）
        self.assertIn("子代理 {n} 仍在跑", APP, "文案要说明还有子代理在跑")
        i = APP.index("function subRunningOf")
        self.assertIn("subagent_running", APP[i:i + 300])

    def test_frontend_downgrades_color(self):
        i = APP.index("function stateClassOf")
        seg = APP[i:i + 400]
        self.assertIn("'warn'", seg, "有子代理在跑时颜色要降级，不能是绿色 done")

    def test_sidebar_row_uses_state_label(self):
        self.assertIn('title="${esc(stateLabel(s))}"', APP,
                      "侧栏行必须用带子代理判断的文案")
        self.assertIn("stateClassOf(s)", APP)


if __name__ == "__main__":
    unittest.main()
