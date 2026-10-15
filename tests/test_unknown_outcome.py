"""结果未知封锁：超时后禁止原样重试不可幂等动作，直到只读动作核对过。"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import tools as T
from cbp.tools import ToolError, ToolRegistry


class UnknownOutcomeGateTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_uo_"))
        self.tools = ToolRegistry(self.ws, {"allow_delete": True, "allow_outlook": True,
                                            "approval_policy": "never"})

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_blocked_then_read_only_clears(self):
        self.tools._mark_unknown_outcome("outlook", "测试注入")
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(self.tools.execute("delete_path", {"path": "x"}))
        msg = str(ctx.exception)
        self.assertIn("TOOL_TIMEOUT_UNKNOWN", msg)
        self.assertIn("结果未知", msg)
        self.assertIn("只读", msg)
        # 只读动作成功后解除封锁
        asyncio.run(self.tools.execute("list_directory", {"path": "."}))
        self.assertIsNone(self.tools._com_unknown)
        # 解除后不再报"结果未知"，而是走正常路径（不存在的路径按各工具自身语义处理：
        # 可能返回 deleted=False，也可能报"文件不存在"——两种都不该再提结果未知）
        try:
            out = asyncio.run(self.tools.execute("delete_path", {"path": "x"}))
            self.assertNotIn("TOOL_TIMEOUT_UNKNOWN", str(out))
        except ToolError as exc:
            self.assertNotIn("TOOL_TIMEOUT_UNKNOWN", str(exc))

    def test_read_only_still_works_while_blocked(self):
        self.tools._mark_unknown_outcome("outlook", "测试注入")
        out = asyncio.run(self.tools.execute("list_directory", {"path": "."}))
        self.assertIn("items", out)

    def test_idempotent_tools_not_blocked(self):
        self.tools._mark_unknown_outcome("outlook", "测试注入")
        (self.ws / "a.txt").write_text("hi", encoding="utf-8")
        out = asyncio.run(self.tools.execute("read_text", {"path": "a.txt"}))
        self.assertIn("hi", str(out))

    def test_outlook_timeout_marks_unknown_and_does_not_retry(self):
        """提交的 future 永不完成 → 必须超时报未知，且**只提交一次**（不重试）。"""
        submitted = []

        class _StubWorker:
            def submit(self, fn):
                submitted.append(fn)
                return asyncio.get_running_loop().create_future()   # 永不完成

        orig = T._ComWorker.get
        T._ComWorker.get = staticmethod(lambda: _StubWorker())
        self.tools.outlook_timeout = 0.05
        try:
            with self.assertRaises(ToolError) as ctx:
                asyncio.run(self.tools._outlook_run(lambda ns: {"ok": True}))
        finally:
            T._ComWorker.get = orig
        msg = str(ctx.exception)
        self.assertIn("TOOL_TIMEOUT", msg)
        self.assertIn("禁止原样重试", msg)
        self.assertEqual(len(submitted), 1, "超时后绝不能再次提交同一调用（重试=重复副作用）")
        self.assertIsNotNone(self.tools._com_unknown, "必须留下结果未知标记")


if __name__ == "__main__":
    unittest.main()
