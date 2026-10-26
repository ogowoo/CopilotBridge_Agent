"""同步工具经 execute() 调用的回归测试（skill_list 曾因无条件 await 永远失败）。"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.tools import ToolRegistry


class SyncToolExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gantry-synctool-"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.install = self.tmp / "install"
        (self.install / "skills" / "global-demo").mkdir(parents=True)
        (self.install / "skills" / "global-demo" / "SKILL.md").write_text(
            "---\nname: global-demo\ndescription: 全局示例技能\n---\n\n正文", encoding="utf-8")
        self.tools = ToolRegistry(self.ws, {})
        self.tools.set_global_root(self.install)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_skill_list_via_execute(self):
        r = asyncio.run(self.tools.execute("skill_list", {}))
        self.assertIsInstance(r, list)
        self.assertIn("global-demo", [s["name"] for s in r])

    def test_unknown_tool_and_bad_args(self):
        with self.assertRaises(Exception):
            asyncio.run(self.tools.execute("no_such_tool", {}))
        with self.assertRaises(Exception):
            asyncio.run(self.tools.execute("read_text", {"bogus": 1}))


if __name__ == "__main__":
    unittest.main()
