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


if __name__ == "__main__":
    unittest.main()
