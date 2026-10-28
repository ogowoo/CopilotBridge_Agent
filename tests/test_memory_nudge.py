# -*- coding: utf-8 -*-
"""记忆提醒（memory_nudge）的回归测试。

实测根因：memory_write 在 agent.db 全历史里只被调过 1 次——软提示对模型等于不存在。
现在 final 前拦一次（做了实质工作却没记过 memory_write），提醒它收尾记一条。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class MemoryNudgeTests(unittest.TestCase):
    def test_nudge_block_exists(self):
        src = (ROOT / "gantry" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("memory_nudge", src, "要有 memory_nudge 拦截块")
        self.assertIn("_did_memory_write", src)
        self.assertIn("_productive_n", src)
        self.assertIn("_memory_nudged", src, "要有 fail-open 标记（只拦一次）")

    def test_nudge_is_fail_open(self):
        """只拦一次：_memory_nudged=True 后不再拦（不无限拦）。"""
        src = (ROOT / "gantry" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("_memory_nudged = True", src, "拦完要把标记置上，下次放行")

    def test_configurable(self):
        src = (ROOT / "gantry" / "routes_config.py").read_text(encoding="utf-8")
        self.assertIn("agent.memory_nudge", src, "设置页要有开关")

    def test_productive_tools_list(self):
        src = (ROOT / "gantry" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("_PRODUCTIVE_TOOLS", src)
        self.assertIn("write_text", src.split("_PRODUCTIVE_TOOLS")[1][:400])


if __name__ == "__main__":
    unittest.main()
