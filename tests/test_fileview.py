# -*- coding: utf-8 -*-
"""测试：文件直读（文本/图片/二进制 + 路径解析 + 不误判）。"""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cbp.fileview import maybe_file_view, render_message  # noqa: E402


class FileViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "todo_roadmap.md").write_text("# 标题\n\n第一段\n第二段\n", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (self.root / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        (self.root / "report.pdf").write_bytes(b"%PDF-1.4" + b"0" * 128)

    def tearDown(self):
        self.tmp.cleanup()

    def test_text_file_with_relative_path(self):
        v = maybe_file_view("把 docs/todo_roadmap.md 加载进来我看看", self.root)
        self.assertIsNotNone(v)
        self.assertEqual(v["path"], "docs/todo_roadmap.md")
        self.assertEqual(v["kind"], "text")
        self.assertIn("# 标题", v["content"])
        self.assertGreaterEqual(v["lines"], 3)

    def test_bare_filename_resolves(self):
        v = maybe_file_view("给我看下 todo_roadmap.md", self.root)
        self.assertIsNotNone(v, "只给文件名也要能在工作区里找到")
        self.assertEqual(v["kind"], "text")

    def test_中文紧跟文件名也能抽到路径(self):
        v = maybe_file_view("直接加载docs/todo_roadmap.md给我看", self.root)
        self.assertIsNotNone(v, "中文紧跟在 .md 后面时也要能匹配")

    def test_code_file(self):
        v = maybe_file_view("看下 src/main.py 的内容", self.root)
        self.assertIsNotNone(v)
        self.assertEqual(v["kind"], "text")
        self.assertIn("print", v["content"])

    def test_image(self):
        v = maybe_file_view("打开 shot.png 给我看", self.root)
        self.assertIsNotNone(v)
        self.assertEqual(v["kind"], "image")
        msg = render_message(v, "sid")
        self.assertIn("cbpfile image", msg)

    def test_binary(self):
        v = maybe_file_view("看看 report.pdf", self.root)
        self.assertIsNotNone(v)
        self.assertEqual(v["kind"], "binary")
        self.assertIn("cbpfile binary", render_message(v, "sid"))

    def test_plain_question_not_matched(self):
        self.assertIsNone(maybe_file_view("这个项目结构怎么样", self.root))
        self.assertIsNone(maybe_file_view("继续优化性能", self.root))

    def test_missing_file_not_matched(self):
        self.assertIsNone(maybe_file_view("看下 nope.md 的内容", self.root))

    def test_render_has_fence_with_kind_and_path(self):
        v = maybe_file_view("看下 docs/todo_roadmap.md", self.root)
        msg = render_message(v, "sid")
        self.assertIn("```cbpfile text docs/todo_roadmap.md", msg)
        self.assertTrue(msg.rstrip().endswith("```"))


if __name__ == "__main__":
    unittest.main()
