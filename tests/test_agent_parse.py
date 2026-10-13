"""AgentRunner 动作解析的回归测试。

运行: python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.agent import AgentRunner


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
        from cbp.agent import AgentRunner  # noqa
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
        from cbp.agent import AgentRunner as AR
        step = {"step": 3, "status": "invalid_action",
                "error": "x", "raw_tail": '{"tool":"write_text","content_b64":"abc'}
        kept = AR.compact_value(step, 6000)
        self.assertIn("raw_tail", kept)


class WorkspaceContextTests(unittest.TestCase):
    """AGENT.md 注入与 skills 目录清单(渐进式披露)。"""

    def _runner(self, ws):
        import tempfile
        from pathlib import Path
        from cbp.tools import ToolRegistry
        root = Path(ws or tempfile.mkdtemp(prefix="cbp_ctx_"))
        tools = ToolRegistry(root, {})
        return AgentRunner(tools, None, {},
                           state_getter=dict,
                           state_saver=lambda **k: None,
                           step_saver=lambda x: None,
                           file_event_saver=lambda x: None), root

    def test_agent_md_loaded_and_capped(self):
        import tempfile
        from pathlib import Path
        ws = Path(tempfile.mkdtemp(prefix="cbp_ctx_"))
        (ws / "AGENT.md").write_text("项目约定X" * 3, encoding="utf-8")
        runner, _ = self._runner(ws)
        runner._agent_md = runner._load_agent_md()
        self.assertIn("项目约定X", runner._agent_md)
        self.assertIn("项目约定X", runner._system_prompt())
        # 超长发截断
        (ws / "AGENT.md").write_text("长" * 9000, encoding="utf-8")
        runner2, _ = self._runner(ws)
        text = runner2._load_agent_md()
        self.assertLessEqual(len(text), 8000 + 40)
        self.assertIn("已截断", text)

    def test_no_agent_md_no_section(self):
        runner, _ = self._runner(None)
        runner._agent_md = runner._load_agent_md()
        self.assertEqual(runner._agent_md, "")
        # 基础提示里会提到 AGENT.md（自更新规则），但注入段表头不应出现
        self.assertNotIn("项目约定（来自工作区 AGENT.md", runner._system_prompt())

    def test_skill_catalog_frontmatter(self):
        import tempfile
        from pathlib import Path
        ws = Path(tempfile.mkdtemp(prefix="cbp_ctx_"))
        d = ws / "skills" / "my-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: 我的技能\ndescription: 做某件事\n---\n# 正文\n", encoding="utf-8")
        runner, _ = self._runner(ws)
        cat = runner._load_skill_catalog()
        self.assertEqual(len(cat), 1)
        self.assertEqual(cat[0]["name"], "我的技能")
        self.assertEqual(cat[0]["description"], "做某件事")
        self.assertEqual(cat[0]["path"], "skills/my-skill/SKILL.md")

    def test_skill_catalog_fallback_parsing(self):
        import tempfile
        from pathlib import Path
        ws = Path(tempfile.mkdtemp(prefix="cbp_ctx_"))
        d = ws / "skills" / "plain-one"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# 标题技能\n\n这是简介段落。\n\n## 小节\n正文\n", encoding="utf-8")
        runner, _ = self._runner(ws)
        cat = runner._load_skill_catalog()
        self.assertEqual(cat[0]["name"], "标题技能")
        self.assertEqual(cat[0]["description"], "这是简介段落。")

    def test_skills_in_system_prompt(self):
        import tempfile
        from pathlib import Path
        ws = Path(tempfile.mkdtemp(prefix="cbp_ctx_"))
        d = ws / "skills" / "x"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\ndescription: 测试技能\n---\n", encoding="utf-8")
        runner, _ = self._runner(ws)
        runner._skills = runner._load_skill_catalog()
        prompt = runner._system_prompt()
        self.assertIn("可用技能", prompt)
        self.assertIn("测试技能", prompt)
        self.assertIn("SKILL.md", prompt)


class InstructionPathTests(unittest.TestCase):
    """自我指令文件识别：AGENT.md 与 skills/*.md 改动需在时间线里可见。"""

    def test_recognizes_instruction_files(self):
        from cbp.agent import AgentRunner as AR
        self.assertTrue(AR._is_instruction_path("AGENT.md"))
        self.assertTrue(AR._is_instruction_path("agent.md"))
        self.assertTrue(AR._is_instruction_path(r"AGENT.md"))
        self.assertTrue(AR._is_instruction_path("skills/webui-change/SKILL.md"))
        self.assertTrue(AR._is_instruction_path(r"skills\debug\SKILL.md"))

    def test_ignores_other_files(self):
        from cbp.agent import AgentRunner as AR
        self.assertFalse(AR._is_instruction_path("cbp/agent.py"))
        self.assertFalse(AR._is_instruction_path("README.md"))
        self.assertFalse(AR._is_instruction_path("docs/AGENT.md"))
        self.assertFalse(AR._is_instruction_path(""))


if __name__ == "__main__":
    unittest.main()
