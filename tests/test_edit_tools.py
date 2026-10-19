# -*- coding: utf-8 -*-
"""空白容错编辑：多级匹配、缩进校正、补丁原子应用、失败诊断。

真实痛点：`replace_text` 要求逐字相同，模型抄片段时缩进/行尾空白稍有出入就失败，
于是"读一遍—猜一遍"循环，把回合与 token 烧光（真实任务里几十上百步）。
"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import editmatch
from cbp.tools import ToolError, ToolRegistry


class MatchTests(unittest.TestCase):
    def test_exact(self):
        m = editmatch.find_span("a\nb\nc\n", "b")
        self.assertEqual((m.strategy, m.unique), (editmatch.STRATEGY_EXACT, True))

    def test_trailing_whitespace_tolerated(self):
        text = "def f():\n    return 1   \n"
        m = editmatch.find_span(text, "    return 1")
        self.assertIsNotNone(m)
        self.assertIn(m.strategy, (editmatch.STRATEGY_TRAILING_WS,
                                   editmatch.STRATEGY_INDENT, "exact"))

    def test_indentation_drift_tolerated(self):
        text = "class A:\n\tdef f(self):\n\t\treturn 1\n"
        # 模型用 4 空格写片段，文件是 tab
        m = editmatch.find_span(text, "    def f(self):\n        return 1")
        self.assertIsNotNone(m, "缩进差异应能容错匹配")
        self.assertEqual(m.strategy, editmatch.STRATEGY_INDENT)

    def test_whitespace_collapse(self):
        text = "x = f(1,   2)\n"
        m = editmatch.find_span(text, "x = f(1, 2)")
        self.assertIsNotNone(m)
        self.assertEqual(m.strategy, editmatch.STRATEGY_WS)

    def test_ambiguous_reports_count(self):
        text = "a\nb\na\nb\n"
        m = editmatch.find_span(text, "a\nb")
        self.assertFalse(m.unique)
        self.assertEqual(m.count, 2)

    def test_missing_returns_none(self):
        self.assertIsNone(editmatch.find_span("abc\n", "zzz"))


class ReindentTests(unittest.TestCase):
    def test_tab_file_keeps_tabs(self):
        out = editmatch.reindent("    return 2", "\t\treturn 1", "\n")
        self.assertEqual(out, "\t\treturn 2")

    def test_relative_nesting_preserved(self):
        frag = "if x:\n    y()"
        out = editmatch.reindent(frag, "  if a:\n      b()", "\n")
        self.assertTrue(out.startswith("  if x:"), out)
        self.assertIn("\n      y()", out)

    def test_eol_applied(self):
        out = editmatch.reindent("a\nb", "a", "\r\n")
        self.assertEqual(out, "a\r\nb")


class PatchParseTests(unittest.TestCase):
    def test_codex_style(self):
        patch = """*** Begin Patch
*** Update File: a.py
@@ def f():
 x = 1
-y = 2
+y = 3
*** End Patch"""
        hunks = editmatch.parse_patch(patch)
        self.assertEqual(len(hunks), 1)
        self.assertEqual(hunks[0].old_lines, ["x = 1", "y = 2"])
        self.assertEqual(hunks[0].new_lines, ["x = 1", "y = 3"])

    def test_two_hunks(self):
        patch = "@@\n a\n-b\n+c\n@@\n x\n-y\n+z"
        hunks = editmatch.parse_patch(patch)
        self.assertEqual(len(hunks), 2)

    def test_edits_normalization(self):
        got = editmatch.parse_edits([{"old": "a", "new": "b"}, {"old": ""}])
        self.assertEqual(got, [("a", "b", 1)])


class RegistryEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_edit_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.reg = ToolRegistry(self.ws, {"max_write": 200000})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, content: str) -> Path:
        p = self.ws / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_replace_text_fuzzy_indent(self):
        p = self._write("a.py", "def f():\n\treturn 1\n")
        out = asyncio.run(self.reg.execute("replace_text",
                                           {"path": "a.py", "old": "    return 1",
                                            "new": "    return 2"}))
        self.assertEqual(out["match"], editmatch.STRATEGY_INDENT)
        self.assertEqual(p.read_text(encoding="utf-8"), "def f():\n\treturn 2\n",
                         "新内容要按文件缩进（tab）校正")

    def test_replace_text_failure_has_diff(self):
        self._write("b.md", "# 标题\n\n正文\n")
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(self.reg.execute("replace_text",
                                         {"path": "b.md", "old": "# 标提", "new": "# 新"}))
        msg = str(ctx.exception)
        self.assertIn("首个差异", msg, "失败要给差异位置，模型才能一轮修好")

    def test_apply_patch_multi_block_atomic(self):
        p = self._write("c.py", "a = 1\nb = 2\nc = 3\n")
        patch = "@@\n-a = 1\n+a = 10\n@@\n-c = 3\n+c = 30"
        out = asyncio.run(self.reg.execute("apply_patch",
                                           {"path": "c.py", "patch": patch}))
        self.assertEqual(out["blocks"], 2)
        self.assertEqual(p.read_text(encoding="utf-8"), "a = 10\nb = 2\nc = 30\n")

    def test_apply_patch_rolls_back_on_failure(self):
        p = self._write("d.py", "x = 1\n")
        patch = "@@\n-x = 1\n+x = 2\n@@\n-nope = 9\n+nope = 8"
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(self.reg.execute("apply_patch", {"path": "d.py", "patch": patch}))
        self.assertIn("未写入任何改动", str(ctx.exception))
        self.assertEqual(p.read_text(encoding="utf-8"), "x = 1\n", "原子性：失败必须不落盘")

    def test_apply_patch_edits_list(self):
        p = self._write("e.txt", "one\ntwo\n")
        out = asyncio.run(self.reg.execute("apply_patch", {
            "path": "e.txt",
            "edits": [{"old": "one", "new": "1"}, {"old": "two", "new": "2"}]}))
        self.assertEqual(out["blocks"], 2)
        self.assertEqual(p.read_text(encoding="utf-8"), "1\n2\n")

    def test_apply_patch_ambiguous_rejected(self):
        self._write("f.txt", "same\nsame\n")
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(self.reg.execute("apply_patch",
                                         {"path": "f.txt", "edits": [{"old": "same", "new": "x"}]}))
        self.assertIn("无法唯一定位", str(ctx.exception))

    def test_apply_patch_requires_existing_file(self):
        with self.assertRaises(ToolError):
            asyncio.run(self.reg.execute("apply_patch",
                                         {"path": "nope.py", "edits": [{"old": "a", "new": "b"}]}))


if __name__ == "__main__":
    unittest.main()
