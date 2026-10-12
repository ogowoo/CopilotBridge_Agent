"""AgentRunner 动作解析的回归测试。

运行: python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import AgentRunner


class ParseTests(unittest.TestCase):
    def test_plain_tool_action(self):
        a = AgentRunner._parse('{"type":"tool","tool":"read_text","arguments":{"path":"a.py"}}')
        self.assertEqual(a["tool"], "read_text")
        self.assertEqual(a["arguments"]["path"], "a.py")

    def test_final_action(self):
        a = AgentRunner._parse('{"type":"final","content":"完成了"}')
        self.assertEqual(a["type"], "final")
        self.assertEqual(a["content"], "完成了")

    def test_html_entities_in_valid_json_not_corrupted(self):
        a = AgentRunner._parse(
            '{"type":"final","content":"包含 &amp; 和 &quot;引号&quot; 的合法内容"}')
        self.assertEqual(a["content"], "包含 &amp; 和 &quot;引号&quot; 的合法内容")

    def test_html_escaped_whole_response_recovered(self):
        a = AgentRunner._parse(
            '{&quot;type&quot;:&quot;final&quot;,&quot;content&quot;:&quot;转义整体恢复&quot;}')
        self.assertEqual(a["content"], "转义整体恢复")

    def test_markdown_fence(self):
        a = AgentRunner._parse('```json\n{"type":"final","content":"带围栏"}\n```')
        self.assertEqual(a["content"], "带围栏")

    def test_duplicate_identical_actions_dedup(self):
        raw = '{"type":"final","content":"x"}\n{"type":"final","content":"x"}'
        a = AgentRunner._parse(raw)
        self.assertEqual(a["content"], "x")

    def test_distinct_actions_rejected(self):
        raw = '{"type":"final","content":"a"}\n{"type":"tool","tool":"read_text","arguments":{}}'
        with self.assertRaises(ValueError):
            AgentRunner._parse(raw)

    def test_nested_json_in_content_not_phantom(self):
        # content 里嵌一段 JSON 文本，不应被当成第二个动作
        import json as j
        payload = j.dumps({"type": "final",
                           "content": '示例: {"type":"tool","tool":"read_text"} 是格式说明'},
                          ensure_ascii=False)
        a = AgentRunner._parse(payload)
        self.assertEqual(a["type"], "final")
        self.assertIn("格式说明", a["content"])


class EscapeDecodeTests(unittest.TestCase):
    def test_decode_fragment_literal_backslash_n(self):
        # 源文本含字面量 \\n（反斜杠+n），不应被解成换行
        from agent import AgentRunner  # noqa
        # 通过 write 恢复路径触发 decode：构造未转义 write_text
        raw = '{"type":"tool","tool":"write_text","arguments":{"path":"a.txt","content":"第一行\\\\n第二行"},"reason":"x"}'
        a = AgentRunner._parse(raw)
        self.assertEqual(a["arguments"]["content"], "第一行\\n第二行")


class TruncatedWriteRecoveryTests(unittest.TestCase):
    """Copilot 输出上限截断写入动作时的部分恢复回归测试。"""

    def test_truncated_b64_write_partial_recovered(self):
        import base64 as b64
        full = 'print("hello")\nprint("world")\n'
        blob = b64.b64encode(full.encode("utf-8")).decode()
        # 模拟输出在 b64 中间被截断（约保留前 2/3）
        cut = blob[: len(blob) // 3 * 2]
        raw = '{"type":"tool","tool":"write_text_b64","arguments":{"path":"t.py","content_b64":"' + cut
        a = AgentRunner._parse(raw)
        self.assertEqual(a["tool"], "write_text")
        self.assertTrue(a["_truncated_partial"])
        self.assertIn('print("hello")', a["arguments"]["content"])

    def test_truncated_b64_multibyte_tail(self):
        import base64 as b64
        full = "第一行\n第二行\n第三行\n"
        blob = b64.b64encode(full.encode("utf-8")).decode()
        # 截在任意位置（可能切断多字节 UTF-8 字符）
        raw = ('{"type":"tool","tool":"append_text_b64","arguments":{"path":"t.txt",'
               '"content_b64":"' + blob[: len(blob) - 1])
        a = AgentRunner._parse(raw)
        self.assertEqual(a["tool"], "append_text")
        self.assertTrue(a["_truncated_partial"])
        # 恢复的内容必须是原前缀（损坏的尾部字节被丢弃）
        self.assertTrue(full.startswith(a["arguments"]["content"]))

    def test_truncated_inline_write_partial_recovered(self):
        raw = ('{"type":"tool","tool":"write_text","arguments":{"path":"t.txt",'
               '"content":"line1\\nline2\\nline3')
        a = AgentRunner._parse(raw)
        self.assertEqual(a["tool"], "write_text")
        self.assertTrue(a["_truncated_partial"])
        self.assertIn("line1\nline2", a["arguments"]["content"])

    def test_complete_b64_action_not_marked_truncated(self):
        import base64 as b64
        blob = b64.b64encode(b"ok\n").decode()
        raw = ('{"type":"tool","tool":"write_text_b64","arguments":{"path":"t.txt",'
               '"content_b64":"' + blob + '"},"reason":"写"}')
        a = AgentRunner._parse(raw)
        self.assertNotIn("_truncated_partial", a)
        self.assertEqual(a["tool"], "write_text_b64")

    def test_non_write_action_still_rejected(self):
        with self.assertRaises(ValueError):
            AgentRunner._parse('{"type":"tool","tool":"read_text","arguments":{"path":"a')

    def test_invalid_action_keeps_raw_tail(self):
        from agent import AgentRunner as AR
        step = {"step": 3, "status": "invalid_action",
                "error": "x", "raw_tail": '{"tool":"write_text","content_b64":"abc'}
        kept = AR.compact_value(step, 6000)
        self.assertIn("raw_tail", kept)


if __name__ == "__main__":
    unittest.main()
