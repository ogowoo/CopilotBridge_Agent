# -*- coding: utf-8 -*-
"""新话题首轮：Copilot 通道提示"可直接让 Copilot 生成文件"。

背景：copilot_file 让 Copilot 生成文件并提供下载入口，文件内容**不经过聊天消息**,
因此不受 Copilot 单条输出上限约束——大文件一个回合就能拿到，不必用
write_text_b64 分块写（多回合 + 可能截断）。用户要求：全新话题且用 Copilot 时，
首轮就提示可以走这条路。

约束：
- 只在 Copilot 网页通道提示。provider 会话没有 copilot_file，提示了会让模型去
  调用不存在的工具（实测连续失败 10 次纯烧回合）。
- 只在 copilot_file 真在本次工具面里时提示（allow_copilot_file=false 或工具裁剪
  把它去掉了就不提）。
- **不写进系统提示词**:系统提示词要保持逐字稳定（prompt cache），提示只加在
  首轮的用户提示里（total==0 分支）。
- 顺带钉住一个真实能力缺口：copilot_file 原本归在 copilot 组，而那组关键字是
  "图片/截图/附件"，导致**任何"生成文件"类任务都会把它裁掉**（模型看不到这个
  工具）。它是写工具，必须常驻 core。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.agent import AgentRunner
from cbp.tools import ToolRegistry


def _register_copilot_file(tools):
    async def _fake(path, instruction):
        return {"path": path}
    tools.register("copilot_file", _fake, "让 Copilot 生成文件并下载到工作区",
                   {"path": "相对路径", "instruction": "内容要求"})
    return tools


class FirstTurnCopilotHintTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_firstturn_"))

    def _tools(self, web_tools=True, with_copilot_file=True):
        tools = ToolRegistry(self.ws, {"max_total_steps": 5})
        tools.set_web_copilot_tools(web_tools)
        if with_copilot_file:
            _register_copilot_file(tools)
        return tools

    def _runner(self, **kw):
        tools = self._tools(**kw)
        r = AgentRunner(tools, lambda *a, **k: None, {"max_total_steps": 5},
                        state_getter=lambda: {"state": "ready"},
                        state_saver=lambda **k: None, step_saver=lambda s: None,
                        file_event_saver=lambda e: None)
        r._task_text = "生成一个报告文件"
        return r

    def test_hint_present_on_copilot_channel(self):
        self.assertTrue(self._runner(web_tools=True)._first_turn_copilot_hint())

    def test_no_hint_on_provider_channel(self):
        """provider 会话没有 copilot_file：提示了只会白烧回合。"""
        self.assertFalse(self._runner(web_tools=False)._first_turn_copilot_hint())

    def test_no_hint_when_tool_absent(self):
        """allow_copilot_file=false 时工具不存在，不应提示。"""
        self.assertFalse(self._runner(web_tools=True,
                                      with_copilot_file=False)._first_turn_copilot_hint())

    def test_copilot_file_survives_keyword_pruning(self):
        """回归：copilot_file 是写工具，不能被关键字裁剪掉。

        原缺陷：它归在 copilot 组（关键字=图片/截图/附件），任何"生成文件"类任务
        都会把它裁掉 -> 模型看不到该工具，"让 Copilot 直接生成文件"这条路堵死。
        """
        for task in ("生成一个报告文件", "写一个中文 README", "看看目录"):
            tools = self._tools(web_tools=True)
            r = AgentRunner(tools, lambda *a, **k: None, {"max_total_steps": 5},
                            state_getter=lambda: {"state": "ready"},
                            state_saver=lambda **k: None, step_saver=lambda s: None,
                            file_event_saver=lambda e: None)
            r._task_text = task
            names = r._active_tool_names() or set()
            self.assertIn("copilot_file", names, f"任务 {task!r} 下 copilot_file 被裁掉了")

    def test_system_prompt_stays_byte_stable(self):
        """提示不能污染系统提示词（prompt cache 依赖逐字稳定）。"""
        r = self._runner()
        self.assertNotIn("首轮提示", r._system_prompt())
        self.assertEqual(r._system_prompt(), r._system_prompt())

    def test_hint_reaches_first_turn_prompt_only(self):
        """首轮提示词里出现建议；后续回合不再重复啰嗦。

        注意桩必须**持久化 state**:否则 total_steps 永远为 0,循环每回合都走
        total==0 分支，提示会重复（这是桩的假象，不是产品行为）。
        """
        prompts = []
        state = {"state": "ready"}

        async def chat(prompt, model, make_new):
            prompts.append(prompt)
            if len(prompts) == 1:
                return json.dumps({"type": "tool", "tool": "list_directory",
                                   "arguments": {"path": "."}}, ensure_ascii=False)
            return json.dumps({"type": "final", "content": "done"}, ensure_ascii=False)

        tools = _register_copilot_file(ToolRegistry(self.ws, {"max_total_steps": 5}))
        tools.set_web_copilot_tools(True)
        r = AgentRunner(tools, chat, {"max_total_steps": 5},
                        state_getter=lambda: state,
                        state_saver=lambda **k: state.update(k),
                        step_saver=lambda s: None,
                        file_event_saver=lambda e: None)
        asyncio.run(r.run("生成一个报告文件", "m"))
        self.assertGreaterEqual(len(prompts), 2)
        self.assertIn("首轮提示", prompts[0])
        self.assertIn("copilot_file", prompts[0])
        self.assertGreater(int(state.get("total_steps", 0)), 0, "工具动作应推进步数")
        self.assertNotIn("首轮提示", prompts[1], "只有首轮才提示，避免每回合烧 token")


if __name__ == "__main__":
    unittest.main()
