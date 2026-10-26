"""系统提示词必须逐字稳定（提供商 prompt cache 的前提），且不含每步易变成分。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.agent import AgentRunner
from gantry.tools import ToolRegistry


class PromptStabilityTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_prompt_"))

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _runner(self, task="写一个 python 脚本", config=None):
        cfg = {"max_total_steps": 10}
        cfg.update(config or {})
        r = AgentRunner(ToolRegistry(self.ws, cfg), lambda *a, **k: None, cfg,
                        state_getter=lambda: {"state": "ready"},
                        state_saver=lambda **k: None, step_saver=lambda s: None,
                        file_event_saver=lambda e: None)
        r._task_text = task
        return r

    def test_system_prompt_is_byte_stable(self):
        r = self._runner()
        first = r._system_prompt()
        second = r._system_prompt()
        self.assertEqual(first, second, "同一任务内系统提示词必须逐字一致，否则每次调用都 miss 缓存")

    def test_no_per_step_counter_in_system_prompt(self):
        r = self._runner()
        sys_prompt = r._system_prompt()
        for volatile in ("当前总步骤", "当前步骤"):
            self.assertNotIn(volatile, sys_prompt,
                             "步数计数器属于每步易变内容，应留在用户消息里")

    def test_tool_list_present_exactly_once(self):
        r = self._runner()
        sys_prompt = r._system_prompt()
        self.assertEqual(sys_prompt.count("可用工具："), 1, "工具清单只能出现一次（单一数据源）")

    def test_prompt_tracks_task_tool_trimming(self):
        """任务不同 → 工具面不同（P0-1 的预期行为，不违反稳定性：同一任务内才要求稳定）。"""
        a = self._runner("整理这个 excel 表格")._system_prompt()
        b = self._runner("写一个 python 脚本")._system_prompt()
        self.assertIn("read_excel", a)
        self.assertNotIn("read_excel", b)


if __name__ == "__main__":
    unittest.main()
