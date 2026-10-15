"""工作区边界护栏：把服务安装目录当工作区必须显式开启自升级；敏感路径始终拒绝。"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.tools import ToolError, ToolRegistry


class WorkspaceGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_wsguard_"))
        self.install = self.tmp / "install"
        (self.install / "cbp").mkdir(parents=True)
        (self.install / "config.json").write_text("{}", encoding="utf-8")
        (self.install / "data").mkdir()
        self.ws = self.tmp / "ws"
        self.ws.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tools(self, root, allow_self_upgrade=False):
        t = ToolRegistry(root, {"allow_self_upgrade": allow_self_upgrade})
        t.set_global_root(self.install)
        return t

    def test_install_dir_as_workspace_is_refused_by_default(self):
        t = self._tools(self.ws)
        with self.assertRaises(ToolError) as ctx:
            t.set_root(self.install)
        msg = str(ctx.exception)
        self.assertIn("安装目录", msg)
        self.assertIn("allow_self_upgrade", msg)
        self.assertEqual(t.root, self.ws.resolve(), "被拒绝后不得改变当前工作区")

    def test_install_dir_allowed_with_self_upgrade(self):
        t = self._tools(self.ws, allow_self_upgrade=True)
        t.set_root(self.install)
        self.assertEqual(t.root, self.install.resolve())

    def test_workspace_subdir_of_install_is_allowed(self):
        """安装目录**内部**的独立子目录（默认 workspace/）是允许的。"""
        sub = self.install / "workspace"
        sub.mkdir()
        t = self._tools(self.ws)
        t.set_root(sub)
        self.assertEqual(t.root, sub.resolve())

    def test_cannot_escape_workspace_with_dotdot(self):
        t = self._tools(self.ws)
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(t.execute("read_text", {"path": "../install/config.json"}))
        self.assertIn("FS_SANDBOX_ESCAPE", str(ctx.exception))

    def test_sensitive_paths_refused_when_workspace_is_install_dir(self):
        """即使开了自升级把安装目录当工作区，敏感路径仍按 denylist 拒绝。"""
        t = self._tools(self.install, allow_self_upgrade=True)
        for rel in ("config.json", "data/agent.db"):
            with self.assertRaises(ToolError) as ctx:
                asyncio.run(t.execute("write_text", {"path": rel, "content": "x"}))
            self.assertIn("FS_SANDBOX_DENYLIST", str(ctx.exception), rel)

    def test_workspace_roots_whitelist_enforced(self):
        t = ToolRegistry(self.ws, {"workspace_roots": [str(self.ws)]})
        with self.assertRaises(ToolError) as ctx:
            t.set_root(self.tmp)
        self.assertIn("FS_SANDBOX_NOT_ALLOWED", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
