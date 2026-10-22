# -*- coding: utf-8 -*-
"""待办 / 子代理面板：新一轮会话没有时不能显示。

用户："todo, subagent 是新一轮会话没有时就不要显示吧。"

## 根因
- `resetView()`（新任务）与 `load()`（切会话）只清了标题/KPI/消息，没清待办条与
  子代理面板：上一轮的条会一直留到新会话的第一份 runtime 快照到达（浏览器冷启动
  时可能好几秒），看起来就是"明明没有却还显示"；
- `syncTeamCard()` 判断"有没有内容"用的是 `!gb.hidden` 与 `display!=='none'`：
  goalBar 隐藏了但 innerHTML 还留着旧内容、subagentBar 只是 `display:''`（回落到
  样式表的 none）都会被当成"有"，于是外层留一个空卡片；
- `renderGoalBar()` 在"没有目标/待办"分支里把条藏掉后没调用 `syncTeamCard()`，
  外层卡片要等下一次子代理刷新才收起。
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

APP = (BASE / "webui" / "app.js").read_text(encoding="utf-8")


class ClearOnNewSessionTests(unittest.TestCase):
    def test_helper_exists_and_clears_everything(self):
        i = APP.index("function clearGoalSubagents()")
        seg = APP[i:i + 900]
        self.assertIn("goalBar", seg)
        self.assertIn("subagentBar", seg)
        self.assertIn("subagentList", seg)
        self.assertIn("_goalSig=''", seg.replace(" ", ""), "签名要重置，否则下一次同内容不再重绘")
        self.assertIn("syncTeamCard()", seg)

    def test_new_task_clears_panels(self):
        i = APP.index("function resetView()")
        seg = APP[i:i + 700]
        self.assertIn("clearGoalSubagents()", seg,
                      "新任务（新一轮）必须清掉上一轮的待办/子代理")

    def test_session_switch_clears_panels_before_await(self):
        i = APP.index("async function load(id)")
        seg = APP[i:i + 3000]
        self.assertIn("clearGoalSubagents()", seg)
        # 清理必须发生在网络请求之前：否则新会话首帧仍显示旧面板
        self.assertLess(seg.index("clearGoalSubagents()"), seg.index("await api("),
                        "未命中视图缓存时，清面板要在请求详情之前")

    def test_parent_scope_refreshed_after_load(self):
        i = APP.index("async function load(id)")
        seg = APP[i:i + 2500]
        self.assertIn("refreshSubagents(true)", seg)


class SyncTeamCardTests(unittest.TestCase):
    def test_empty_goal_bar_does_not_count_as_content(self):
        i = APP.index("function syncTeamCard()")
        seg = APP[i:i + 700]
        self.assertIn("gb.innerHTML", seg.replace(" ", ""),
                      "goalBar 隐藏但留着旧内容时不能算有内容")
        self.assertIn("display==='block'", seg.replace(" ", ""),
                      "subagentBar 的 ''（回落样式表 none）不能算显示")

    def test_hiding_goal_bar_syncs_card(self):
        i = APP.index("function renderGoalBar")
        j = APP.index("_goalSig='none'", i)
        seg = APP[j:j + 260]
        self.assertIn("syncTeamCard()", seg,
                      "待办条藏掉后外层卡片要立刻判定（否则留空卡片）")

    def test_subagent_empty_hides_card(self):
        i = APP.index("function renderSubagents")
        seg = APP[i:i + 900]
        self.assertIn("bar.style.display='none'", seg.replace(" ", ""))
        self.assertIn("syncTeamCard()", seg)


if __name__ == "__main__":
    unittest.main()
