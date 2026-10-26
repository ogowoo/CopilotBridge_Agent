"""工具参数错误必须可行动：列出合法参数 + 高频坑提示。"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.tools import ToolRegistry, ToolError


class ToolArgErrorTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_argerr_"))
        self.tools = ToolRegistry(self.ws, {})

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_unexpected_kwarg_lists_valid_arguments(self):
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(self.tools.execute("read_text", {"wrong_name": "x"}))
        msg = str(ctx.exception)
        self.assertIn("wrong_name", msg)
        self.assertIn("合法参数", msg)
        for arg in ("path", "offset", "limit"):
            self.assertIn(arg, msg)

    def test_workspace_prefix_hint(self):
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(self.tools.execute("read_text", {"workspace/README.md": "x"}))
        self.assertIn("不要再加 workspace/ 前缀", str(ctx.exception))

    def test_valid_call_still_works(self):
        (self.ws / "a.txt").write_text("hello", encoding="utf-8")
        out = asyncio.run(self.tools.execute("read_text", {"path": "a.txt", "limit": 10}))
        self.assertIn("hello", str(out))


if __name__ == "__main__":
    unittest.main()
