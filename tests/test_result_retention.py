"""工具结果保留策略：保头保尾（尾部常含错误与结论），而不是只留头部。"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.agent import AgentRunner


class RetentionTests(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(AgentRunner.retain_text("hello", 100), "hello")

    def test_head_and_tail_kept(self):
        body = "HEAD" + "x" * 5000 + "TAIL-ERROR: AssertionError"
        out = AgentRunner.retain_text(body, 1000)
        self.assertLessEqual(len(out), 1000 + 80)          # 允许省略标记本身的长度
        self.assertTrue(out.startswith("HEAD"))
        self.assertTrue(out.endswith("TAIL-ERROR: AssertionError"), "尾部（错误结论）必须保留")
        self.assertIn("已省略", out)
        self.assertIn(str(len(body) - 1000), out)            # 省略字符数要显式写出

    def test_compact_value_uses_retention(self):
        payload = {"content": "A" * 4000 + "THE-END"}
        out = AgentRunner.compact_value(payload, 500)
        self.assertTrue(out["content"].endswith("THE-END"), "嵌套字符串也要保尾")
        self.assertNotIn("...[truncated]", json.dumps(out), "不应再用旧的只保头标记")

    def test_b64_keys_dropped_and_lists_walked(self):
        """content_b64 会被整体丢弃（避免把大段 base64 回灌进提示词），列表逐项裁剪。"""
        out = AgentRunner.compact_value(
            {"content_b64": "Z" * 9000, "items": ["B" * 3000 + "LAST"]}, 200)
        self.assertNotIn("content_b64", out)
        self.assertTrue(out["items"][0].endswith("LAST"))

    def test_unicode_not_broken(self):
        out = AgentRunner.retain_text("中" * 500 + "尾", 100)
        self.assertTrue(out.endswith("尾"))
        self.assertEqual(out.count("\ufffd"), 0)


if __name__ == "__main__":
    unittest.main()
