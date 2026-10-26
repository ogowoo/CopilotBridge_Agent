# -*- coding: utf-8 -*-
"""`gantry/auto_team.py`：按 todo 自动派子代理的**决策**部分（默认关闭）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry import auto_team as at  # noqa: E402


def T(content, status="pending", **kw):
    return {"content": content, "status": status, **kw}


class PlanTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(at.is_enabled({}))
        self.assertFalse(at.is_enabled(None))
        self.assertTrue(at.is_enabled({"auto_team_enabled": True}))

    def test_needs_two_items(self):
        self.assertEqual(at.plan_dispatch([T("改 A.py")]), [])

    def test_independent_items_picked(self):
        todos = [T("改造 tools/run_tests.py 的子进程隔离"),
                 T("修正 ui/editor.py 的快捷键刷新"),
                 T("补 tests/test_watermark.py 边界用例")]
        picked = at.plan_dispatch(todos)
        self.assertEqual(len(picked), 3)

    def test_same_scope_is_dependency(self):
        """两项都指向同一个文件 → 视为有依赖，不够 2 项独立任务，不派发。"""
        todos = [T("改 pyshot/main.py 的菜单文本"),
                 T("改 pyshot/main.py 的托盘标题")]
        self.assertEqual(at.plan_dispatch(todos), [])

    def test_mixed_scope_picks_only_independent(self):
        todos = [T("改 pyshot/main.py 的菜单文本"),
                 T("改 pyshot/main.py 的托盘标题"),
                 T("补 tests/test_watermark.py 用例")]
        picked = at.plan_dispatch(todos)
        self.assertEqual(len(picked), 2, "同一文件的第二项应被跳过，另一文件的应被选中")

    def test_skip_completed_and_blocked(self):
        todos = [T("A.py 已完成", "completed"), T("B.py 已阻塞", "blocked"),
                 T("C.py 待办"), T("D.py 待办")]
        picked = at.plan_dispatch(todos)
        self.assertEqual(sorted(t["content"][:3] for t in picked), ["C.p", "D.p"])

    def test_no_dispatch_when_children_running(self):
        todos = [T("A.py 待办"), T("B.py 待办")]
        self.assertEqual(at.plan_dispatch(todos, running_children=1), [])

    def test_max_agents_cap(self):
        todos = [T(f"feature{i}.py 待办") for i in range(10)]
        self.assertLessEqual(len(at.plan_dispatch(todos, max_agents=3)), 3)


class TaskTextTests(unittest.TestCase):
    def test_self_contained(self):
        tasks = at.build_tasks([T("改 A.py")], goal="让 pyshot 通过测试")
        self.assertEqual(len(tasks), 1)
        self.assertIn("改 A.py", tasks[0])
        self.assertIn("让 pyshot 通过测试", tasks[0])
        self.assertIn("验证", tasks[0])


if __name__ == "__main__":
    unittest.main()
