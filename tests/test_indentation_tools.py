# -*- coding: utf-8 -*-
"""缩进/空白编辑不再靠"字符串携带空格"——修掉空转死循环。

用户实测（`for debug/…-timeline-2026-10-19-10-35-24.json`,377 步、7 个 Copilot
会话）："一直在重复替换空格/制表的动作"。日志给出的证据：

- 58 次 `replace_text` 里 **15 次 `old == new`**（纯空操作，文件哈希不变）；
- 真缩进修改里，模型想要的 8 个空格到我们手里只剩 **1 个**
  （step 337/354: `old='\\t\\tu32.ShowWindow(hwnd, 0)'` → `new=' u32.ShowWindow(hwnd, 0)'`）；
- 根因：`current_answer()` 用 `inner_text`（**渲染后**文本），HTML 会把连续空格
  折叠成一个、行首缩进吃掉 → 参数里的缩进失真 → 改不动 → 再改 → 死循环，
  期间还因 JSON 非法（`\\'`）废掉 58 个回合、rollover 6 次。

修法（本文件守）：
1. `convert_indentation`：制表符↔空格，**只动行首**,一次搞定全文件或指定区间；
2. `indent_lines`：按行区间增减 N 个空格（用**数字**表达缩进，不依赖字符串空白）；
3. `replace_text(count=0)` = 全部替换；有残留时回 `remaining_matches` + 提示；
4. `replace_text` 空操作（before==after）时返回 `warning`,不再静默；
5. `read_text(reveal_whitespace=true)` 让模型"看见"空格与制表符；
6. `current_answer()` 在渲染文本折叠了空白时，改用 `body_text_raw()`（textContent）;
7. 重复动作护栏：同一工具+同样参数超过阈值就不再执行，直接给出换路建议。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.tools import ToolRegistry

BASE = Path(__file__).resolve().parent.parent


class IndentationToolsTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_ind_"))
        self.r = ToolRegistry(self.ws, {})
        self.f = self.ws / "a.py"

    def test_convert_tabs_to_spaces_only_leading(self):
        self.f.write_bytes('def f():\n\tif x:\n\t\treturn "a\tb"\n'.encode("utf-8"))
        out = asyncio.run(self.r.convert_indentation("a.py", width=4))
        self.assertTrue(out["changed"])
        text = self.f.read_text(encoding="utf-8")
        self.assertIn("\n    if x:\n", text)
        self.assertIn("\n        return", text)
        self.assertIn('"a\tb"', text, "字符串里的制表符不能动")

    def test_convert_spaces_to_tabs(self):
        self.f.write_bytes(b"def f():\n    return 1\n")
        asyncio.run(self.r.convert_indentation("a.py", mode="spaces2tabs", width=4))
        self.assertIn(b"\treturn 1", self.f.read_bytes())

    def test_convert_noop_reports_zero(self):
        self.f.write_bytes(b"def f():\n    return 1\n")
        out = asyncio.run(self.r.convert_indentation("a.py", width=4))
        self.assertEqual(out["changed_lines"], 0)
        self.assertFalse(out["changed"])

    def test_convert_preserves_crlf(self):
        g = self.ws / "a.txt"
        g.write_bytes(b"a\r\n\tb\r\n")
        asyncio.run(self.r.convert_indentation("a.txt", width=4))
        self.assertEqual(g.read_bytes(), b"a\r\n    b\r\n")

    def test_indent_lines_add_and_remove(self):
        self.f.write_bytes(b"a\nb\nc\n")
        out = asyncio.run(self.r.indent_lines("a.py", line_start=2, line_count=1, delta=4))
        self.assertEqual(out["changed_lines"], 1)
        self.assertEqual(self.f.read_bytes(), b"a\n    b\nc\n")
        asyncio.run(self.r.indent_lines("a.py", line_start=2, line_count=1, delta=-4))
        self.assertEqual(self.f.read_bytes(), b"a\nb\nc\n")

    def test_indent_lines_skips_blank_lines(self):
        self.f.write_bytes(b"a\n\nb\n")
        out = asyncio.run(self.r.indent_lines("a.py", line_start=1, line_count=3, delta=2))
        self.assertEqual(out["changed_lines"], 2, "空行不该被加缩进")

    def test_indent_lines_range_only(self):
        self.f.write_bytes(b"a\nb\nc\n")
        asyncio.run(self.r.indent_lines("a.py", line_start=2, line_count=1, delta=2))
        self.assertEqual(self.f.read_bytes(), b"a\n  b\nc\n", "区间外不能动")


class ReplaceAllTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_all_"))
        self.r = ToolRegistry(self.ws, {})
        self.f = self.ws / "a.txt"
        self.f.write_text("a=1 b=1 c=1\n", encoding="utf-8")

    def test_count_zero_replaces_all(self):
        out = asyncio.run(self.r.replace_text("a.txt", old="=1", new="=2", count=0))
        self.assertEqual(out["replaced"], 3)
        self.assertEqual(out["remaining_matches"], 0)
        self.assertEqual(self.f.read_text(encoding="utf-8"), "a=2 b=2 c=2\n")

    def test_default_count_one_still_one(self):
        out = asyncio.run(self.r.replace_text("a.txt", old="=1", new="=2"))
        self.assertEqual(out["replaced"], 1)
        self.assertGreater(out["remaining_matches"], 0)
        self.assertIn("count=0", out.get("hint", ""), "有残留要提示 0=全部")

    def test_noop_returns_warning(self):
        out = asyncio.run(self.r.replace_text("a.txt", old="a=1", new="a=1"))
        self.assertIn("warning", out, "空操作必须显式告警，否则模型会反复重试")
        self.assertIn("reveal_whitespace", out["warning"])


class RevealWhitespaceTests(unittest.TestCase):
    def test_line_mode_makes_whitespace_visible(self):
        ws = Path(tempfile.mkdtemp(prefix="cbp_vis_"))
        (ws / "a.py").write_bytes(b"\t  x = 1\n")
        r = ToolRegistry(ws, {})
        d = asyncio.run(r.read_text("a.py", line_start=1, line_count=1, reveal_whitespace=True))
        self.assertIn("→", d["lines"], "制表符应显示成 →")
        self.assertIn("·", d["lines"], "空格应显示成 ·")
        self.assertIn("whitespace", d)


class AnswerUnfoldTests(unittest.TestCase):
    """渲染文本折叠空白时，改用 textContent 版本（根因修复）。"""

    def test_prefers_unfolded_when_collapsed(self):
        from gantry import bridge as b
        collapsed = '{"type":"tool","tool":"replace_text","arguments":{"old":" a","new":" a"}}'
        unfolded = '{"type":"tool","tool":"replace_text","arguments":{"old":" a","new":"        a"}}'
        async def fake_inner():
            return "Copilot said\n" + collapsed
        async def fake_raw():
            return "Copilot said" + unfolded
        orig_b, orig_r = b.body_text, b.body_text_raw
        orig_extract = b.extract_latest_answer
        b.body_text, b.body_text_raw = fake_inner, fake_raw
        b.extract_latest_answer = lambda t: t.replace("Copilot said", "").strip()
        try:
            got = asyncio.run(b._prefer_unfolded(collapsed))
        finally:
            b.body_text, b.body_text_raw = orig_b, orig_r
            b.extract_latest_answer = orig_extract
        self.assertIn("        a", got, "应改用保留空白的版本")

    def test_keeps_text_when_no_json(self):
        from gantry import bridge as b
        self.assertEqual(asyncio.run(b._prefer_unfolded("普通回答")), "普通回答")


class RepeatGuardWiringTests(unittest.TestCase):
    def test_guard_exists_and_can_be_tuned(self):
        src = (BASE / "gantry" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("repeat_guard", src)
        self.assertIn("repeat_guard_limit", src, "阈值应可配")
        self.assertIn("convert_indentation", src, "拦截提示里要指向专用工具")
        self.assertIn("count 传 0", src)


if __name__ == "__main__":
    unittest.main()
