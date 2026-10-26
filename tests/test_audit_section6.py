# -*- coding: utf-8 -*-
"""审计第 6 节落地：回合预算提示 / 目录增量列出 / grep 定位规则 / 子代理复用提示。"""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gantry.agent as A  # noqa: E402
import gantry.prompt as P  # noqa: E402
from gantry.tools import ToolRegistry  # noqa: E402
from gantry.tools_extra import similar_task_hint  # noqa: E402


class TurnBudgetTests(unittest.TestCase):
    def test_nudge_wired_and_configurable(self):
        src = (ROOT / "gantry" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("【收尾判断】", src, "连续空转回合要催收尾判断")
        self.assertIn('turn_budget_nudge', src)
        self.assertIn("_dry_turns", src)

    def test_dry_turn_counter_grows_without_progress(self):
        """三个回合都只读同一个文件 → _dry_turns 递增。"""
        ws = Path(tempfile.mkdtemp(prefix="cbp_dry_"))
        (ws / "a.py").write_text("print(1)\n", encoding="utf-8")

        async def chat(prompt, model=None, force_new=False, **kw):
            return ('{"type":"tool","tool":"read_text","arguments":{"path":"a.py"},'
                    '"reason":"再看一次"}')

        saved = []
        state = {"state": "running", "total_steps": 0}
        runner = A.AgentRunner(
            tools=ToolRegistry(ws, {}), chat=chat,
            config={"max_total_steps": 6, "turn_budget_nudge": 2, "repeat_guard_limit": 99},
            state_getter=lambda: dict(state),
            state_saver=lambda **kw: state.update(kw),
            step_saver=lambda st: saved.append(dict(st)),
            file_event_saver=lambda ev: None,
            approval_check=lambda *a, **k: True,
        )
        asyncio.run(runner.run("读文件", "copilot-auto"))
        self.assertGreater(getattr(runner, "_dry_turns", 0), 0,
                           "只读同一文件不产生新信息，应累计空转回合")


class DirIncrementalTests(unittest.TestCase):
    def test_changed_since_last_list(self):
        ws = Path(tempfile.mkdtemp(prefix="cbp_dir_"))
        (ws / "a.py").write_text("x", encoding="utf-8")
        reg = ToolRegistry(ws, {})
        first = asyncio.run(reg.list_directory("."))
        self.assertNotIn("changed_since_last_list", first)
        time.sleep(0.02)
        (ws / "b.py").write_text("y", encoding="utf-8")
        second = asyncio.run(reg.list_directory("."))
        self.assertEqual(second.get("changed_since_last_list"), ["b.py"])
        self.assertIn("不必再逐层重列", str(second.get("note") or ""))
        third = asyncio.run(reg.list_directory("."))
        self.assertNotIn("changed_since_last_list", third)

    def test_since_mtime_param(self):
        ws = Path(tempfile.mkdtemp(prefix="cbp_dir2_"))
        old = ws / "old.py"
        old.write_text("x", encoding="utf-8")
        stamp = time.time() - 100
        (ws / "new.py").write_text("y", encoding="utf-8")
        out = asyncio.run(ToolRegistry(ws, {}).list_directory(".", since_mtime=stamp))
        self.assertIn("new.py", out.get("changed_since_last_list") or [])
        self.assertIn("mtime", out["items"][0])


class GrepFirstPromptTests(unittest.TestCase):
    def test_rule_present(self):
        self.assertIn("定位优先用 grep", P.AGENT_PROTOCOL)
        self.assertIn("line_start", P.AGENT_PROTOCOL)


class SubagentReuseTests(unittest.TestCase):
    def test_similar_task_gets_hint(self):
        sibs = [{"id": "sub-1", "state": "completed",
                 "description": "全面分析工作区 pyshot 项目的架构、入口、启动链、依赖"}]
        hint = similar_task_hint("分析 pyshot 项目的架构与启动链依赖", sibs)
        self.assertIn("subagent_send", hint)
        self.assertIn("sub-1", hint)

    def test_different_task_no_hint(self):
        sibs = [{"id": "sub-1", "state": "completed",
                 "description": "全面分析工作区 pyshot 项目的架构、入口、启动链、依赖"}]
        self.assertEqual(similar_task_hint("修复 4K 屏下截图热键的 DPI 问题", sibs), "")

    def test_wired_into_subagent_many(self):
        src = (ROOT / "gantry" / "tools_extra.py").read_text(encoding="utf-8")
        self.assertIn("similar_task_hint", src)
        self.assertIn("_sib_hint", src)
        self.assertIn('_out["hint"] = _sib_hint', src)


if __name__ == "__main__":
    unittest.main()
