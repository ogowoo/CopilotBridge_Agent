# -*- coding: utf-8 -*-
"""测试：自升级规则里必须带真实安装目录，且明确"别按名字猜路径"。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class SelfUpgradePathTests(unittest.TestCase):
    def test_rules_include_actual_root(self):
        """模型不该再按文件夹名猜（实测它去猜 c:\\gantry-main）。"""
        src = (ROOT / "gantry" / "agent_prompt.py").read_text(encoding="utf-8")
        self.assertIn("global_root", src, "规则要读 global_root")
        self.assertIn("安装目录的真实路径", src)
        self.assertIn("不要按名字猜路径", src.replace("绝对不要按名字猜路径", "不要按名字猜路径"))

    def test_global_root_from_package_location(self):
        """安装目录永远由 gantry/bridge.py 的位置反推，与文件夹叫什么无关。"""
        import gantry.bridge as b
        expected = Path(b.__file__).resolve().parent.parent
        self.assertEqual(b.BASE, expected)
        self.assertEqual(b.AGENT_TOOLS.global_root, expected)
        self.assertTrue((expected / "gantry").is_dir(), "BASE 下应有 gantry/ 包")

    def test_template_warns_about_guessing(self):
        js = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
        assert "self_search_text" in js, "模板要引导先定位"
        assert "不要猜" in js, "模板要明确不要猜路径"


if __name__ == "__main__":
    unittest.main()
