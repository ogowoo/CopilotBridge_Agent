# -*- coding: utf-8 -*-
"""待办清单（todo_write）：标签、计数口径、可展开。

用户两次追问：① "计划 0/5 是什么？我没用 /plan"；② "todo 的话也没看到 todo list"。
查下来三件事：

1. 那个条目的标签写的是「计划」,但数据源是 `todo_write` 写的**待办清单**,
   与 `/plan` 计划模式无关 → 标签惹的祸，改成「待办」;
2. **计数口径错了**：待办结构是 `{content, status}`（pending/in_progress/completed），
   前端却按 `x.done` 过滤 → 永远算 0,即使全做完也显示 0/N（用户看到的"0/5"不涨）;
3. 界面只显示一个数字，看不到条目 → 做成可展开清单（点一下展开，状态符号 + 状态文字，
   展开状态记在 localStorage）。
"""
import re
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
APP = (BASE / "webui" / "app.js").read_text(encoding="utf-8")
CSS = (BASE / "webui" / "style.css").read_text(encoding="utf-8")
I18N = (BASE / "webui" / "i18n.js").read_text(encoding="utf-8")


class TodoBarTests(unittest.TestCase):
    def test_counts_by_status_not_done_flag(self):
        """必须按 status==='completed' 计数（结构里没有 done 字段）。"""
        seg = APP[APP.index("function renderGoalBar"):APP.index("async function applyRuntime")]
        self.assertIn("status==='completed'", seg, "计数必须看 status")
        self.assertNotIn("todos.filter(x=>x&&x.done)", seg,
                         "旧写法按 x.done 计数，永远得 0（用户实测 bug）")

    def test_label_is_todo_not_plan(self):
        seg = APP[APP.index("function renderGoalBar"):APP.index("async function applyRuntime")]
        self.assertIn("tr('待办')", seg)
        self.assertNotIn("tr('计划')", seg, "标签不能叫「计划」——会让人以为是 /plan 模式")

    def test_list_is_expandable(self):
        self.assertIn("toggleTodos", APP)
        self.assertIn('data-act="toggleTodos"', APP, "展开按钮要走 data-act 委托（CSP 无内联）")
        self.assertIn("gantry-todos-open", APP, "展开状态要持久化")
        self.assertIn("toggleTodos:()=>toggleTodos()", APP, "ACTIONS 未注册")

    def test_status_icons_and_texts(self):
        seg = APP[APP.index("const TODO_ICON"):APP.index("function renderGoalBar")]
        for st in ("completed", "in_progress", "pending"):
            self.assertIn(st, seg, f"缺少状态 {st} 的图标/文案")
        self.assertIn("已完成", seg)
        self.assertIn("进行中", seg)

    def test_css_for_list(self):
        for sel in (".todolist", ".todoitem", "todo-completed", "todo-in_progress"):
            self.assertIn(sel, CSS, f"缺少样式 {sel}")

    def test_english_entries(self):
        for key in ('"待办"', '"已完成"', '"进行中"'):
            self.assertIn(key, I18N, f"缺少英文词条 {key}")
        # 不许出现含糊的 done/active 覆盖（会盖掉 Completed/In progress）
        self.assertNotIn('"已完成": "done"', I18N)
        self.assertNotIn('"进行中": "active"', I18N)


class TodoWriteShapeTests(unittest.TestCase):
    """后端写入的结构要与前端读取的口径一致（防止以后又漂移）。"""

    def test_backend_writes_content_and_status(self):
        src = (BASE / "gantry" / "tools_extra.py").read_text(encoding="utf-8")
        seg = src[src.index("async def todo_write"):src.index("async def todo_write") + 1200]
        self.assertIn('"content"', seg)
        self.assertIn('"status"', seg)
        self.assertIn("completed", seg)
        # 以及同步进会话状态（界面靠 state.todos 显示）
        self.assertIn("todos=clean", seg)


if __name__ == "__main__":
    unittest.main()
