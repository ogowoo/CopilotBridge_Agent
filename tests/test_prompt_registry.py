"""提示词分段注册表：顺序、作用域遮蔽、变量 fail-fast、兼容既有拼接（逐字不变）。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.agent import AgentRunner
from cbp.prompt import AGENT_PROTOCOL, PromptError, PromptRegistry
from cbp.tools import ToolRegistry


class RegistryTests(unittest.TestCase):
    def test_order_and_default_separator(self):
        reg = PromptRegistry().add("late", "B", order=10).add("early", "A", order=-5)
        self.assertEqual(reg.render(), "AB", "默认分隔符为空串（便于替换既有拼接）")
        self.assertEqual(reg.names(), ["early", "late"])
        self.assertEqual(reg.render(separator="|"), "A|B")

    def test_same_scope_overrides(self):
        reg = PromptRegistry().add("x", "旧").add("x", "新")
        self.assertEqual(reg.render(), "新")

    def test_near_scope_shadows_far(self):
        reg = PromptRegistry().add("rules", "全局规则", order=0, scope="global")
        reg.add("rules", "项目规则", order=0, scope="project")
        self.assertEqual(reg.render(), "项目规则", "近层必须遮蔽同名远层")
        self.assertEqual(reg.names(), ["rules"])

    def test_unknown_variable_fails_loudly(self):
        reg = PromptRegistry().add("x", "值={{MISSING}}")
        with self.assertRaises(PromptError) as ctx:
            reg.render()
        self.assertIn("MISSING", str(ctx.exception))

    def test_variable_substitution(self):
        reg = PromptRegistry().add("x", "分流：{{ROUTE}}")
        self.assertEqual(reg.render(variables={"ROUTE": "本地"}), "分流：本地")

    def test_empty_name_rejected(self):
        with self.assertRaises(PromptError):
            PromptRegistry().add("", "x")


class SystemPromptUsesRegistryTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_preg_"))

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _prompt(self, task="写一个 python 脚本"):
        r = AgentRunner(ToolRegistry(self.ws, {}), lambda *a, **k: None, {"max_total_steps": 5},
                        state_getter=lambda: {"state": "ready"},
                        state_saver=lambda **k: None, step_saver=lambda s: None,
                        file_event_saver=lambda e: None)
        r._task_text = task
        return r._system_prompt()

    def test_protocol_constant_used_verbatim(self):
        p = self._prompt()
        # 协议文本必须原样进入（首句与末条规则）
        self.assertIn("你是本地任务 Agent", p)
        # 末条规则（协议常量尾部）必须原样进入；这句只在 Agent 协议里
        self.assertIn("琐碎或一次性的过程不要沉淀。", p)
        self.assertNotIn("{{CBP_FILE_ROUTING}}", p, "占位符必须被替换")
        self.assertNotIn("__CBP_FILE_ROUTING__", p)

    def test_sections_appear_in_order(self):
        p = self._prompt()
        i_proto = p.index("你是本地任务 Agent")
        i_tools = p.index("可用工具：")
        self.assertLess(i_proto, i_tools, "协议必须在工具清单之前")
        self.assertEqual(p.count("可用工具："), 1)

    def test_protocol_constant_matches_placeholder_contract(self):
        self.assertIn("{{CBP_FILE_ROUTING}}", AGENT_PROTOCOL)


if __name__ == "__main__":
    unittest.main()
