"""沙箱拒绝必须结构化且可行动（DSH 的 FS_SANDBOX_DENIED 思路）。"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.tools import ToolError, ToolRegistry


class SandboxErrorTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_sbx_"))
        self.tools = ToolRegistry(self.ws, {})

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _denied(self, tool, args):
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(self.tools.execute(tool, args))
        return str(ctx.exception)

    def test_escape_denied_is_structured(self):
        msg = self._denied("read_text", {"path": "../../etc/passwd"})
        self.assertIn("FS_SANDBOX_ESCAPE", msg)
        self.assertIn("当前模式", msg)
        self.assertIn("workspace-write", msg)
        self.assertIn("原因:", msg)
        self.assertIn("可以这样做", msg)
        self.assertIn("allow_self_upgrade", msg, "要告诉模型合法的升级途径")

    def test_write_escape_also_structured(self):
        msg = self._denied("write_text", {"path": "../outside.txt", "content": "x"})
        self.assertIn("FS_SANDBOX_ESCAPE", msg)

    def test_inside_workspace_still_allowed(self):
        (self.ws / "ok.txt").write_text("hi", encoding="utf-8")
        out = asyncio.run(self.tools.execute("read_text", {"path": "./ok.txt"}))
        self.assertIn("hi", str(out))

    def test_no_denylist_leak_of_absolute_agent_path(self):
        """拒绝信息里不要泄露服务安装目录的绝对路径细节（只给模式与建议）。"""
        msg = self._denied("read_text", {"path": "../x"})
        self.assertNotIn("EdgeProfile", msg)


if __name__ == "__main__":
    unittest.main()
