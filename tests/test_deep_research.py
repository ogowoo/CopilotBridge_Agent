# -*- coding: utf-8 -*-
"""深研一键任务的错误路径（不打网络、不启浏览器）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import bridge


class DeepResearchErrorTests(unittest.TestCase):
    def test_empty_query_rejected(self):
        with self.assertRaises(bridge.ToolError):
            import asyncio
            asyncio.run(bridge.deep_research_task("   "))

    def test_missing_research_plugin(self):
        """research 插件未加载时给出明确提示，而不是跑到超时。"""
        import asyncio
        old = bridge.PLUGINS.records.pop("research", None)
        try:
            with self.assertRaises(bridge.ToolError) as ctx:
                asyncio.run(bridge.deep_research_task("随便问一句"))
            self.assertIn("research", str(ctx.exception))
        finally:
            if old is not None:
                bridge.PLUGINS.records["research"] = old


if __name__ == "__main__":
    unittest.main()
