# -*- coding: utf-8 -*-
"""按行读 / 按行替换：把"模型被迫用 PowerShell 改文件"的推力去掉。

来自真实时间线（`for debug/cbp-task-mveq9t5g-timeline-*.json`,PyShot 克隆任务）:
37 次有效 pwsh 调用里 **31 次是文件操作**，其中——
- step 138 用 `(Get-Content main.py)[514]` 读第 514 行（因为 read_text 的 offset 是
  **字符**偏移，表达不了"第几行"）；
- step 144 用 `Get-Content -Raw` + `Set-Content -Encoding utf8` 改文件 → **整文件
  变乱码**（模型自己的 reason 写着"整文件 UTF-8 被按 Windows-1252 误解后重写"）；
- step 149 建 `main.py.mojibake.bak` 做 cp1252→utf8 修复；
- step 153–157 逐字符码调缩进（`[int][char]$_`），step 168 又因 PowerShell 语法
  （`+ :NewLine`）报错；
- 一个 20KB 的 Markdown 报告用 heredoc `@'...'@` 写（因为提示词要求 Base64）。

结论：工具是 Python 的，但**行级定位、整段/缩进替换、写大段文档**这三件事没有顺手
的 Python 路径，模型就去用 shell 了。这里补前两件（纯 Python，无需 shell）。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.tools import ToolError, ToolRegistry


class LineReadTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_line_"))
        self.r = ToolRegistry(self.ws, {})
        (self.ws / "a.py").write_bytes(
            "# 头\n\ndef f():\n        return 1\n\n\ndef g():\n    return 2\n".encode("utf-8"))

    def test_reads_requested_lines_with_numbers(self):
        d = asyncio.run(self.r.read_text("a.py", line_start=4, line_count=2))
        self.assertTrue(d["numbered"])
        self.assertIn("4:         return 1", d["lines"])
        self.assertIn("5:", d["lines"])
        self.assertEqual(d["line_count"], 2)
        self.assertEqual(d["total_lines"], 8)

    def test_line_start_beyond_eof(self):
        d = asyncio.run(self.r.read_text("a.py", line_start=999))
        self.assertEqual(d["line_count"], 0)
        self.assertEqual(d["lines"], "")

    def test_character_mode_still_works(self):
        # limit 是"字符"数，前 4 个字符是 "#"、" "、"头"、"\n"
        d = asyncio.run(self.r.read_text("a.py", offset=0, limit=4))
        self.assertEqual(d["content"], "# 头\n")
        self.assertIn("total_chars", d)


class ReplaceLinesTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_rl_"))
        self.r = ToolRegistry(self.ws, {})
        self.f = self.ws / "a.py"
        self.f.write_bytes(b"line1\nline2\nline3\nline4\n")

    def test_replaces_one_line(self):
        out = asyncio.run(self.r.replace_lines("a.py", start=2, count=1, content="LINE2"))
        self.assertEqual(out["replaced_lines"], 1)
        self.assertTrue(out["changed"])
        self.assertEqual(self.f.read_text(encoding="utf-8"), "line1\nLINE2\nline3\nline4\n")

    def test_fixes_indentation_without_fragment_matching(self):
        """真实场景：把第 4 行的缩进从 8 空格改成 4，不需要 shell 也不依赖片段。"""
        out = asyncio.run(self.r.replace_lines("a.py", start=4, count=1, content="    X"))
        self.assertTrue(out["changed"])
        self.assertTrue(self.f.read_text(encoding="utf-8").splitlines()[3].startswith("    X"))

    def test_empty_content_deletes_lines(self):
        asyncio.run(self.r.replace_lines("a.py", start=2, count=2, content=""))
        self.assertEqual(self.f.read_text(encoding="utf-8"), "line1\nline4\n")

    def test_replaces_multiple_lines_with_block(self):
        asyncio.run(self.r.replace_lines("a.py", start=2, count=2, content="b\nc\nd"))
        self.assertEqual(self.f.read_text(encoding="utf-8"), "line1\nb\nc\nd\nline4\n")

    def test_preserves_crlf(self):
        g = self.ws / "crlf.txt"
        g.write_bytes(b"a\r\nb\r\nc\r\n")
        asyncio.run(self.r.replace_lines("crlf.txt", start=2, count=1, content="B"))
        self.assertEqual(g.read_bytes(), b"a\r\nB\r\nc\r\n", "CRLF 文件不该被改成 LF")

    def test_rejects_bad_line_numbers(self):
        with self.assertRaises(ToolError):
            asyncio.run(self.r.replace_lines("a.py", start=0, count=1, content="x"))
        with self.assertRaises(ToolError):
            asyncio.run(self.r.replace_lines("a.py", start=99, count=1, content="x"))

    def test_rejects_non_utf8(self):
        bad = self.ws / "gbk.txt"
        bad.write_bytes("中文".encode("gbk"))
        with self.assertRaises(ToolError):
            asyncio.run(self.r.replace_lines("gbk.txt", start=1, count=1, content="x"))

    def test_snapshot_and_file_event_recorded(self):
        out = asyncio.run(self.r.replace_lines("a.py", start=1, count=1, content="L1"))
        ev = out["file_event"]
        self.assertEqual(ev["tool"], "replace_lines")
        self.assertNotEqual(ev["before_hash"], ev["after_hash"])


class NoShellForLinesRuleTests(unittest.TestCase):
    def test_rule_points_at_line_tools(self):
        from cbp.prompt import FILE_OPS_RULE
        self.assertIn("replace_lines", FILE_OPS_RULE)
        self.assertIn("line_start", FILE_OPS_RULE)
        self.assertIn("Get-Content", FILE_OPS_RULE)


if __name__ == "__main__":
    unittest.main()
