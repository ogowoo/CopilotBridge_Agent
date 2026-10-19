# -*- coding: utf-8 -*-
"""`cbp/todo_sync.py`：把工作区 TODO 文件同步成会话待办。"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import todo_sync as ts  # noqa: E402
from cbp.session_store import SessionStore  # noqa: E402

DOC = """# pyshot 治理清单

## P0 测试基线
- [x] 补齐 tests/test_watermark.py 的边界用例
- [ ] 修掉 test_smoke 在同一进程重复创建 QApplication

## P1 可靠性
- [ ] 托盘退出时保存会话
- [ ] 快捷键冲突时提示用户

### 备注
- 这段没有复选框，不应被当成"复选框清单"之外的垃圾
"""


class ParseTests(unittest.TestCase):
    def test_checkboxes_parsed_with_status(self):
        todos = ts.parse_todos(DOC)
        self.assertTrue(todos)
        by = {t["content"]: t["status"] for t in todos}
        self.assertEqual(by.get("补齐 tests/test_watermark.py 的边界用例"), "completed")
        self.assertEqual(by.get("修掉 test_smoke 在同一进程重复创建 QApplication"), "pending")

    def test_limit(self):
        doc = "\n".join(f"- [ ] 任务 {i}" for i in range(80))
        self.assertEqual(len(ts.parse_todos(doc)), ts.MAX_ITEMS)

    def test_heading_checkboxes(self):
        """真实格式：复选框写在标题里（pyshot_todo.md 就是这样）。"""
        doc = ("# PyShot 治理 TODO\n\n## P0\n\n### [ ] P0-1 建立分层可重复的测试基线\n"
               "- **任务目标**：形成唯一测试入口\n- **问题依据**：已复现\n"
               "### [x] P0-2 修复 QApplication 冲突\n- **任务目标**：不再崩\n")
        todos = ts.parse_todos(doc)
        self.assertEqual(len(todos), 2, "说明性 bullet 不能被当成待办")
        self.assertEqual(todos[0]["content"], "P0-1 建立分层可重复的测试基线")
        self.assertEqual(todos[0]["status"], "pending")
        self.assertEqual(todos[1]["status"], "completed")

    def test_heading_file_does_not_fall_back_to_bullets(self):
        """有标题的文档里，`- 说明` 不是待办。"""
        doc = "## 说明\n- 本清单基于分析报告\n- 已复现问题表示有证据\n"
        self.assertEqual(ts.parse_todos(doc), [])

    def test_bare_list_still_supported(self):
        """没有标题的裸清单仍然支持。"""
        todos = ts.parse_todos("- 读 main.py\n- 改 hotkeyui.py\n")
        self.assertEqual(len(todos), 2)
        self.assertEqual(todos[0]["status"], "pending")

    def test_short_item_gets_heading_context(self):
        todos = ts.parse_todos("## P1 可靠性\n- [ ] 保存会话\n")
        self.assertIn("P1 可靠性", todos[0]["content"])

    def test_empty(self):
        self.assertEqual(ts.parse_todos(""), [])


class MergeTests(unittest.TestCase):
    def test_completed_not_regressed_by_file(self):
        old = [{"content": "A", "status": "completed"}]
        new = [{"content": "A", "status": "pending"}]
        self.assertEqual(ts.merge_todos(old, new)[0]["status"], "completed")

    def test_in_progress_kept(self):
        old = [{"content": "A", "status": "in_progress"}]
        new = [{"content": "A", "status": "pending"}]
        self.assertEqual(ts.merge_todos(old, new)[0]["status"], "in_progress")

    def test_completed_kept_when_missing_from_file(self):
        old = [{"content": "旧完成项", "status": "completed"}]
        new = [{"content": "新计划", "status": "pending"}]
        out = ts.merge_todos(old, new)
        self.assertIn("旧完成项", [t["content"] for t in out])

    def test_new_item_appended(self):
        old = [{"content": "A", "status": "pending"}]
        new = [{"content": "A", "status": "pending"}, {"content": "B", "status": "pending"}]
        self.assertEqual(len(ts.merge_todos(old, new)), 2)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_todosync_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.store = SessionStore(self.tmp / "a.db")
        self.store.create("t1", "任务", str(self.ws), "copilot-auto")
        ts.reset_cache()

    def test_sync_writes_todos(self):
        (self.ws / "pyshot_todo.md").write_text(DOC, encoding="utf-8")
        self.assertTrue(ts.sync_todos_from_files(self.store, "t1"))
        todos = self.store.get("t1")["todos"]
        self.assertEqual(len(todos), 4)
        self.assertTrue(any(t["status"] == "completed" for t in todos))

    def test_second_call_is_noop_when_unchanged(self):
        (self.ws / "pyshot_todo.md").write_text(DOC, encoding="utf-8")
        ts.sync_todos_from_files(self.store, "t1")
        self.assertFalse(ts.sync_todos_from_files(self.store, "t1"), "没变化不该重复写")

    def test_changed_file_resyncs(self):
        f = self.ws / "todo.md"
        f.write_text("- [ ] A\n", encoding="utf-8")
        ts.sync_todos_from_files(self.store, "t1")
        time.sleep(0.02)
        f.write_text("- [x] A\n- [ ] B\n", encoding="utf-8")
        self.assertTrue(ts.sync_todos_from_files(self.store, "t1"))
        contents = [t["content"] for t in self.store.get("t1")["todos"]]
        self.assertIn("B", contents)

    def test_missing_file_does_not_clear(self):
        (self.ws / "todo.md").write_text("- [ ] A\n", encoding="utf-8")
        ts.sync_todos_from_files(self.store, "t1")
        before = self.store.get("t1")["todos"]
        (self.ws / "todo.md").unlink()
        ts.reset_cache()
        ts.sync_todos_from_files(self.store, "t1")
        self.assertEqual(self.store.get("t1")["todos"], before, "文件没了不能清空清单")

    def test_find_prefers_newest(self):
        a = self.ws / "todo_a.md"
        b = self.ws / "todo_b.md"
        a.write_text("- [ ] A\n", encoding="utf-8")
        time.sleep(0.02)
        b.write_text("- [ ] B\n", encoding="utf-8")
        self.assertEqual(ts.find_todo_file(self.ws), b)


if __name__ == "__main__":
    unittest.main()
