# -*- coding: utf-8 -*-
"""精确编辑守卫：禁止静默整文件覆盖，编辑前必须先读。"""
import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from cbp.tools import ToolError, ToolRegistry  # noqa: E402


class PreciseEditGuardTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_edit_guard_"))
        self.reg = ToolRegistry(self.ws, {})
        self.reg.current_session_id = "s1"

    def test_new_file_write_allowed(self):
        out = asyncio.run(self.reg.write_text("a.txt", "hello\n"))
        self.assertTrue((self.ws / "a.txt").is_file())
        self.assertEqual(out["changed_lines"], 1)
        self.assertIn("+hello", out["diff"])

    def test_existing_file_write_rejected_without_overwrite(self):
        (self.ws / "a.txt").write_text("old\n", encoding="utf-8")
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(self.reg.write_text("a.txt", "new\n"))
        self.assertIn("文件已存在", str(ctx.exception))
        self.assertIn("overwrite=true", str(ctx.exception))

    def test_existing_file_overwrite_requires_read(self):
        (self.ws / "a.txt").write_text("old\n", encoding="utf-8")
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(self.reg.write_text("a.txt", "new\n", overwrite=True))
        self.assertIn("编辑前必须先读取文件", str(ctx.exception))

    def test_read_then_overwrite_returns_diff(self):
        (self.ws / "a.txt").write_text("old\n", encoding="utf-8")
        asyncio.run(self.reg.read_text("a.txt"))
        out = asyncio.run(self.reg.write_text("a.txt", "new\n", overwrite=True))
        self.assertTrue(out["changed"])
        self.assertIn("-old", out["diff"])
        self.assertIn("+new", out["diff"])

    def test_replace_text_requires_read(self):
        (self.ws / "a.txt").write_text("old\n", encoding="utf-8")
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(self.reg.replace_text("a.txt", "old", "new"))
        self.assertIn("编辑前必须先读取文件", str(ctx.exception))
        asyncio.run(self.reg.read_text("a.txt"))
        out = asyncio.run(self.reg.replace_text("a.txt", "old", "new"))
        self.assertEqual(out["replaced"], 1)
        self.assertIn("+new", out["diff"])

    def test_apply_patch_requires_read_and_returns_diff(self):
        (self.ws / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        with self.assertRaises(ToolError):
            asyncio.run(self.reg.apply_patch("a.txt", edits=[{"old": "two", "new": "TWO"}]))
        asyncio.run(self.reg.read_text("a.txt"))
        out = asyncio.run(self.reg.apply_patch(
            "a.txt", edits=[{"old": "two", "new": "TWO"}]))
        self.assertTrue(out["changed"])
        self.assertIn("+TWO", out["diff"])

    def test_write_b64_new_file_and_overwrite_guard(self):
        raw = base64.b64encode(b"hello\n").decode()
        asyncio.run(self.reg.write_text_b64("b.txt", raw))
        self.assertTrue((self.ws / "b.txt").is_file())
        raw2 = base64.b64encode(b"bye\n").decode()
        with self.assertRaises(ToolError):
            asyncio.run(self.reg.write_text_b64("b.txt", raw2))
        asyncio.run(self.reg.read_text("b.txt"))
        out = asyncio.run(self.reg.write_text_b64("b.txt", raw2, overwrite=True))
        self.assertTrue(out["changed"])
        self.assertIn("+bye", out["diff"])

    def test_python_symbols_and_ast_replace(self):
        (self.ws / "m.py").write_text(
            "@deco\ndef alpha():\n    return 1\n\n\nclass C:\n    def beta(self):\n        return 2\n",
            encoding="utf-8")
        syms = asyncio.run(self.reg.python_symbols("m.py"))
        names = {s["name"] for s in syms["symbols"]}
        self.assertIn("alpha", names)
        self.assertIn("C.beta", names)
        alpha = next(s for s in syms["symbols"] if s["name"] == "alpha")
        self.assertEqual(alpha["start"], 1, "装饰器行也要算进符号范围")
        # python_symbols 也是“读过”，因此可以直接按 AST 替换
        out = asyncio.run(self.reg.replace_python_symbol(
            "m.py", "alpha", "def alpha():\n    return 42\n"))
        self.assertTrue(out["changed"])
        self.assertIn("+    return 42", out["diff"])
        self.assertIn("return 42", (self.ws / "m.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
