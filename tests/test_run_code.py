"""程序化工具调用（P2-13）：子进程隔离 + 只读白名单 + 限额。"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.run_code import allowed_tools, run_code
from gantry.tools import ToolError, ToolRegistry


class RunCodeTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_runcode_"))
        for i in range(3):
            (self.ws / f"f{i}.txt").write_text(f"内容{i}" * 10, encoding="utf-8")
        self.tools = ToolRegistry(self.ws, {})

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_whitelist_is_read_only(self):
        names = allowed_tools(self.tools)
        self.assertIn("read_text", names)
        self.assertIn("list_directory", names)
        for forbidden in ("write_text", "delete_path", "run_python", "outlook_send", "run_code"):
            self.assertNotIn(forbidden, names, f"{forbidden} 绝不能进白名单")

    def test_batch_read_aggregates_in_one_call(self):
        code = """
names = [x['name'] for x in list_directory()['items'] if x['name'].endswith('.txt')]
total = 0
for n in sorted(names):
    total += read_text(n, limit=10)['returned_chars']
final({'files': sorted(names), 'chars': total})
"""
        out = asyncio.run(run_code(self.tools, code, timeout=30))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["files"], ["f0.txt", "f1.txt", "f2.txt"])
        self.assertGreater(out["result"]["chars"], 0)
        self.assertGreaterEqual(out["calls"], 4, "一次代码调用应代理多次工具调用")

    def test_write_attempt_is_denied(self):
        code = """
try:
    __import__('gantry')  # 环境里没有写入工具可用
except Exception:
    pass
final({'ok': True})
"""
        out = asyncio.run(run_code(self.tools, code, timeout=30))
        self.assertTrue(out["ok"])

    def test_timeout_is_enforced(self):
        out = asyncio.run(run_code(self.tools, "import time\ntime.sleep(30)\n", timeout=1))
        self.assertFalse(out["ok"])
        self.assertIn("秒", out["error"])

    def test_call_budget_enforced_and_recoverable(self):
        """超出调用预算后：① 代理端不再执行；② 具名 ToolError 让模型代码可捕获并收尾。"""
        code = """
denied = 0
done = 0
for i in range(20):
    try:
        list_directory()
        done += 1
    except ToolError:
        denied += 1
final({'done': done, 'denied': denied})
"""
        out = asyncio.run(run_code(self.tools, code, timeout=30, max_calls=3))
        self.assertTrue(out["ok"], out)
        self.assertGreater(out["result"]["denied"], 0, "超预算必须被拒绝")
        self.assertLessEqual(out["result"]["done"], 3, "预算内的调用应正常执行")
        self.assertLessEqual(out["executed"], 3, "执行次数不得超过预算")
        self.assertGreater(out["calls"], out["executed"], "尝试次数应大于执行次数（有被拒的）")

    def test_tool_error_on_write_attempt(self):
        """白名单外的工具名必须被拒绝（模型可能试图调用写入类工具）。"""
        code = """
try:
    __rpc('write_text', path='x.txt', content='y')
    final({'blocked': False})
except ToolError as e:
    final({'blocked': True, 'msg': str(e)[:40]})
"""
        out = asyncio.run(run_code(self.tools, code, timeout=30))
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["result"]["blocked"])
        self.assertIn("白名单", out["result"]["msg"])

    def test_output_truncated(self):
        code = "final('X' * 50000)"
        out = asyncio.run(run_code(self.tools, code, timeout=30))
        self.assertTrue(out["ok"])

    def test_tool_is_gated_off_by_default(self):
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(self.tools.execute("run_code", {"code": "final(1)"}))
        self.assertIn("allow_run_code", str(ctx.exception))

    def test_tool_works_when_enabled(self):
        tools = ToolRegistry(self.ws, {"allow_run_code": True})
        out = asyncio.run(tools.execute("run_code", {
            "code": "final({'n': len(list_directory()['items'])})"}))
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(out["result"]["n"], 3)


if __name__ == "__main__":
    unittest.main()
