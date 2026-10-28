# -*- coding: utf-8 -*-
"""从子代理会话必须能回到父会话，且子代理面板不能消失。

用户："我在 subagent 里点开 subagent 的任务看详细信息后无法返回到正常的 live stream
（只能看到 todo/还有状态条，看不到 subagent 那个了）。"

## 根因（两处叠加）
1. `openSubagent` 只是 `load(子代理 id)`，而 `refreshSubagents()` 按
   `parent=<current>` 查询 —— 子代理自己**没有下级**，列表变空 →
   `renderSubagents([])` 直接把整块面板 `display:none`,并且**没有任何返回入口**；
2. 即使列表非空，`syncTeamCard()` 只在"空列表"分支调用：`#teamCard` 被
   `hidden` 之后**再也没被显示回来**（实测 `#subagentBar` 高度 0、`teamCard.hidden=true`）,
   于是"看不到子代理那块"。

## 修法
- `load()` 记住 `parentOfCurrent = session.parent_id`;
- `refreshSubagents()` 按 `parentOfCurrent || current` 查询（看子代理时查它的父会话，
  兄弟列表不会凭空消失）；
- `renderSubagents()`:
  - 有 `parentOfCurrent` 时即使列表为空也保留面板；
  - 列表首行插入「← 返回父会话 · <父会话 id>」(`.subagentback`),点它回父会话；
  - **非空分支也调用** `syncTeamCard()`,把 `#teamCard` 的 hidden 收回来。

## 实测（真服务 + Edge，父 parent1 + 子 sub1/sub2）
| 状态 | current | teamCard.hidden | 面板高 | 返回行 |
|---|---|---|---|---|
| 父会话 | parent1 | false | 109 | 无 |
| 进入子代理 | sub2 | **false** | **148** | 有（在视口内） |
| 点返回 | parent1 | false | 109 | 无 |
"""
from gantry.ui_src import ui_js
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
APP = ui_js()
_RAW = (BASE / "webui" / "style.css").read_text(encoding="utf-8")
CSS = re.sub(r"/\*[\s\S]*?\*/", " ", _RAW)


class SubagentBackNavTests(unittest.TestCase):
    def test_remembers_parent_on_load(self):
        self.assertIn("parentOfCurrent=s.parent_id||null;", APP,
                      "load() 必须记住当前会话的父会话")

    def test_query_uses_effective_parent(self):
        self.assertIn("const _scope=parentOfCurrent||current;", APP,
                      "看子代理时要按父会话查询，否则列表变空、面板消失")

    def test_keeps_panel_when_viewing_subagent(self):
        self.assertIn("if(!list.length && !parentOfCurrent)", APP,
                      "看子代理时即使它没有下级也要保留面板（否则用户被困住）")

    def test_back_row_exists_and_loads_parent(self):
        self.assertIn("subagentback", APP)
        i = APP.index("subagentback")
        seg = APP[i:i + 700]
        self.assertIn("openSubagent", seg, "返回行要点得动（复用 openSubagent → load）")
        self.assertIn("parentOfCurrent", seg)

    def test_team_card_resynced_on_nonempty(self):
        """非空分支也必须同步 #teamCard，否则面板被 hidden 后永不恢复。"""
        i = APP.index("box.appendChild(row)});")
        seg = APP[i:i + 400]
        self.assertIn("syncTeamCard", seg,
                      "非空渲染后要调用 syncTeamCard，把 teamCard 的 hidden 收回来")

    def test_back_row_styled(self):
        self.assertIn(".subagentrow.subagentback", CSS)


if __name__ == "__main__":
    unittest.main()
