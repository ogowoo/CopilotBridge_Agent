# -*- coding: utf-8 -*-
"""测试：文件直读（文本/图片/二进制 + 路径解析 + 不误判）。"""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gantry.fileview import maybe_file_view, render_message  # noqa: E402


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




class ReferentialViewTests(unittest.TestCase):
    """"原文 / 这个文件 / 刚才那个" 用上次展示过的文件；纯分析句不命中。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "roadmap.md").write_text("# t\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_refer_words_use_last_path(self):
        for text in ("加载一下原文文件内容", "把这个文件打开给我看", "show me that file"):
            with self.subTest(text=text):
                v = maybe_file_view(text, self.root, last_path="docs/roadmap.md")
                self.assertIsNotNone(v, text)
                self.assertEqual(v["path"], "docs/roadmap.md")

    def test_refer_without_last_path_not_matched(self):
        self.assertIsNone(maybe_file_view("加载一下原文文件内容", self.root))

    def test_analysis_request_not_matched(self):
        self.assertIsNone(maybe_file_view("现在帮我治理一下发布流水线问题", self.root))


class NoopGuardScopeTests(unittest.TestCase):
    """noop_guard 的"需要动手"要按**最新指令**判断，不能只看 original_task。"""

    def test_needs_action_uses_latest_instruction(self):
        src = (ROOT / "gantry" / "agent.py").read_text(encoding="utf-8")
        i = src.index("_needs_action =")
        seg = src[max(0, i - 900):i + 200]
        self.assertIn("_latest_ins", seg, "要按最新指令（pending/追加任务）判断")
        self.assertIn("用户追加任务：", seg, "追加任务要从 original_task 里切出来")


if __name__ == "__main__":
    unittest.main()

class MultiLangIntentTests(unittest.TestCase):
    """动词/名词支持英文与繁体；英文单独出现也不能误判（要能落到真实文件）。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "roadmap.md").write_text("# t\nbody\n", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_traditional_chinese(self):
        self.assertEqual((maybe_file_view("打開 docs/roadmap.md 給我看", self.root) or {}).get("path"),
                         "docs/roadmap.md")
        self.assertEqual((maybe_file_view("載入 docs/roadmap.md，顯示內容", self.root) or {}).get("path"),
                         "docs/roadmap.md")
        self.assertEqual((maybe_file_view("讀取 src/main.py 的原始碼", self.root) or {}).get("path"),
                         "src/main.py")

    def test_english_verbs(self):
        for text in ("show me docs/roadmap.md",
                     "Show me the contents of docs/roadmap.md",
                     "open docs/roadmap.md please",
                     "display src/main.py",
                     "read docs/roadmap.md and summarize it",
                     "cat src/main.py",
                     "print the file docs/roadmap.md"):
            with self.subTest(text=text):
                v = maybe_file_view(text, self.root)
                self.assertIsNotNone(v, text)
                self.assertIn(v["path"], ("docs/roadmap.md", "src/main.py"))

    def test_english_without_real_file_not_matched(self):
        for text in ("read the code and fix the bug",
                     "open the settings panel",
                     "show me the diff of the last commit"):
            with self.subTest(text=text):
                self.assertIsNone(maybe_file_view(text, self.root))


if __name__ == "__main__":
    unittest.main()
