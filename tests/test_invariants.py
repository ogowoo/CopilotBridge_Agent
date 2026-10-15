"""不变量检查必须全通过；并验证它真的能抓到破坏（避免"永远绿"的假保险）。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import invariants as inv


class InvariantTests(unittest.TestCase):
    def test_all_invariants_pass(self):
        failures = inv.check_all()
        self.assertEqual(failures, {}, f"不变量被破坏: {failures}")

    def test_registry_is_not_empty_and_named(self):
        names = inv.registered()
        self.assertGreaterEqual(len(names), 6)
        for n in names:
            self.assertTrue(n.strip(), "不变量必须有可读名字")

    def test_violation_is_detected(self):
        """临时注册一条必失败的检查，确认 check_all 会报出来（防止假保险）。"""

        @inv.invariant("测试用：必然失败")
        def _boom():
            raise AssertionError("故意失败")

        try:
            failures = inv.check_all()
            self.assertIn("测试用：必然失败", failures)
            self.assertIn("故意失败", failures["测试用：必然失败"])
        finally:
            inv._REGISTRY.pop("测试用：必然失败", None)

    def test_duplicate_name_rejected(self):
        with self.assertRaises(RuntimeError):
            @inv.invariant("工具分组表与注册表一致")
            def _dup():
                pass


if __name__ == "__main__":
    unittest.main()
