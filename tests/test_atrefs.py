# -*- coding: utf-8 -*-
"""@ 文件引用的回归测试（DSH 的 @ 体验）。

钉住：
1. `@路径` 能展开成文件内容（带【引用文件】块）；
2. 越界（..、绝对路径、盘符）一律不展开；
3. 代码围栏里的 @ 不展开；
4. 大小保护：单文件截断、总数截断、最多 4 个；
5. 目录/二进制不展开；
6. 工作区检索能跳过 .git/__pycache__/data。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gantry.atrefs import expand_at_mentions, search_files  # noqa: E402


class AtRefTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="gantry_at_"))
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.md").write_text("# A\n内容甲", encoding="utf-8")
        (self.root / "b.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "big.txt").write_text("y" * 30000, encoding="utf-8")
        (self.root / "pic.png").write_bytes(b"\x89PNG")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "hidden.txt").write_text("x", encoding="utf-8")

    def test_expands_simple_ref(self):
        out, refs = expand_at_mentions("看下 @docs/a.md 这份", self.root)
        self.assertEqual(len(refs), 1)
        self.assertIn("【引用文件 docs/a.md", out)
        self.assertIn("内容甲", out)

    def test_outside_and_absolute_not_expanded(self):
        for bad in ("@../etc/passwd", "@/etc/passwd", "@C:/Windows/x.ini"):
            out, refs = expand_at_mentions(f"看下 {bad}", self.root)
            self.assertEqual(refs, [], bad)

    def test_fence_not_expanded(self):
        out, refs = expand_at_mentions("```\n@docs/a.md\n```", self.root)
        self.assertEqual(refs, [], "围栏里的 @ 不展开")

    def test_caps(self):
        out, refs = expand_at_mentions("看下 @big.txt", self.root)
        self.assertEqual(len(refs), 1)
        self.assertLessEqual(refs[0]["chars"], 20000, "单文件要截断")

    def test_max_files(self):
        for i in range(6):
            (self.root / f"f{i}.md").write_text(f"c{i}", encoding="utf-8")
        out, refs = expand_at_mentions(" ".join(f"@f{i}.md" for i in range(6)), self.root)
        self.assertLessEqual(len(refs), 4, "最多 4 个")

    def test_binary_and_dir_not_expanded(self):
        out, refs = expand_at_mentions("看下 @pic.png 和 @docs", self.root)
        self.assertEqual(refs, [])

    def test_search_skips_noise(self):
        files = search_files(self.root, "")
        self.assertIn("docs/a.md", files)
        self.assertNotIn(".git/hidden.txt", files)

    def test_search_matches_substring(self):
        files = search_files(self.root, "a.md")
        self.assertEqual(files, ["docs/a.md"])


if __name__ == "__main__":
    unittest.main()
