# -*- coding: utf-8 -*-
"""工作目录选择器：目录浏览（对应 DSH 的 browse 后端）。

native（系统对话框）依赖桌面会话，单测不弹窗；这里只测纯函数化的 browse。
"""
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry import bridge


class BrowseTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cbp_browse_"))
        for name in ("alpha", "beta", "node_modules", ".hidden"):
            (self.root / name).mkdir()
        (self.root / "file.txt").write_text("x", encoding="utf-8")
        self._old = bridge.AGENT_TOOLS.root
        bridge.AGENT_TOOLS.root = self.root.resolve()

    def tearDown(self):
        bridge.AGENT_TOOLS.root = self._old
        shutil.rmtree(self.root, ignore_errors=True)

    def test_lists_only_visible_dirs(self):
        d = bridge.browse_directories()
        names = [e["name"] for e in d["entries"]]
        self.assertIn("alpha", names)
        self.assertIn("beta", names)
        self.assertNotIn("file.txt", names, "只列目录")
        self.assertNotIn("node_modules", names, "依赖目录应跳过")
        self.assertNotIn(".hidden", names, "隐藏目录应跳过")
        self.assertEqual(d["path"], str(self.root.resolve()))
        self.assertTrue(d["is_default"])

    def test_invalid_path_falls_back_to_default(self):
        d = bridge.browse_directories(str(self.root / "nope"))
        self.assertEqual(d["path"], str(self.root.resolve()))

    def test_parent_is_returned(self):
        d = bridge.browse_directories(str(self.root / "alpha"))
        self.assertEqual(d["parent"], str(self.root.resolve()))
        self.assertFalse(d["is_default"])


class MkdirTests(unittest.TestCase):
    """新建目录（工作区选择器的「新建目录」）：单层、防穿越。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cbp_mkdir_"))
        self._old = bridge.AGENT_TOOLS.root
        bridge.AGENT_TOOLS.root = self.root.resolve()

    def tearDown(self):
        bridge.AGENT_TOOLS.root = self._old
        shutil.rmtree(self.root, ignore_errors=True)

    def test_creates_single_level_dir(self):
        d = bridge.create_directory(str(self.root), "new-ws")
        self.assertTrue((self.root / "new-ws").is_dir())
        self.assertEqual(Path(d["path"]).name, "new-ws")

    def test_rejects_separators_and_dotdot(self):
        for bad in ("a/b", "a\\b", "..", ".", "a:b"):
            with self.assertRaises(bridge.ToolError):
                bridge.create_directory(str(self.root), bad)

    def test_rejects_existing(self):
        (self.root / "dup").mkdir()
        with self.assertRaises(bridge.ToolError):
            bridge.create_directory(str(self.root), "dup")

    def test_rejects_missing_parent(self):
        with self.assertRaises(bridge.ToolError):
            bridge.create_directory(str(self.root / "nope"), "x")


if __name__ == "__main__":
    unittest.main()
