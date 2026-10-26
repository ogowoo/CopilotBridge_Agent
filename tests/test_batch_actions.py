# -*- coding: utf-8 -*-
"""批量动作协议测试：一次回答执行多个工具动作（省回合）。

背景：写入大文件要靠 b64 分块（每块 ≤3000 字符），原来一块一个 Copilot
回合非常浪费会话。批量协议允许一次回答里带多个工具动作，按顺序执行，
每个动作仍单独记一步；批量里不允许 final（完成必须单独返回，保证验证
闭环不被绕过）。
"""
import asyncio
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.agent import AgentRunner
from gantry.tools import ToolRegistry


def act(tool, **args):
    return {"type": "tool", "tool": tool, "arguments": args, "reason": "测试"}


class BatchParseTests(unittest.TestCase):
    def test_explicit_batch_form(self):
        raw = json.dumps({"type": "actions", "actions": [
            act("read_text", path="a.txt"), act("read_text", path="b.txt")]}, ensure_ascii=False)
        parsed = AgentRunner._parse(raw)
        self.assertEqual(parsed["type"], "actions")
        self.assertEqual(parsed["batch_size"], 2)
        self.assertEqual([a["arguments"]["path"] for a in parsed["actions"]], ["a.txt", "b.txt"])

    def test_concatenated_objects_become_batch(self):
        raw = '{"type":"tool","tool":"read_text","arguments":{"path":"a"}}\n' \
              '{"type":"tool","tool":"read_text","arguments":{"path":"b"}}'
        parsed = AgentRunner._parse(raw)
        self.assertEqual(parsed["type"], "actions")
        self.assertEqual(parsed["batch_size"], 2)

    def test_top_level_array_becomes_batch(self):
        raw = json.dumps([act("list_directory"), act("find_files", pattern="*.py")],
                         ensure_ascii=False)
        parsed = AgentRunner._parse(raw)
        self.assertEqual(parsed["type"], "actions")
        self.assertEqual(parsed["batch_size"], 2)

    def test_identical_actions_deduped(self):
        raw = json.dumps({"type": "actions", "actions": [
            act("read_text", path="a"), act("read_text", path="a")]}, ensure_ascii=False)
        parsed = AgentRunner._parse(raw)
        self.assertEqual(parsed["batch_size"], 1)

    def test_final_inside_batch_deferred_to_gate(self):
        """批内 final 不再静默丢弃：保留在动作末尾，由 run() 走验证门判定。

        丢弃它会让"批内写代码 + 宣布完成"这一轮根本不产生验证门事件。
        """
        raw = json.dumps({"type": "actions", "actions": [
            act("read_text", path="a"), {"type": "final", "content": "完成"}]}, ensure_ascii=False)
        parsed = AgentRunner._parse(raw)
        self.assertTrue(parsed.get("_batch_has_final"))
        self.assertEqual(parsed["actions"][-1]["type"], "final")
        self.assertEqual(len([a for a in parsed["actions"] if a.get("type") == "tool"]), 1)
        self.assertEqual(parsed["batch_size"], 2)

    def test_batch_with_only_final_raises(self):
        raw = json.dumps({"type": "actions", "actions": [{"type": "final", "content": "x"}]},
                         ensure_ascii=False)
        with self.assertRaises(ValueError):
            AgentRunner._parse(raw)

    def test_batch_capped(self):
        items = [act("read_text", path=f"f{i}.txt") for i in range(AgentRunner.MAX_BATCH_ACTIONS + 3)]
        parsed = AgentRunner._parse(json.dumps({"type": "actions", "actions": items}, ensure_ascii=False))
        self.assertEqual(parsed["batch_size"], AgentRunner.MAX_BATCH_ACTIONS)
        self.assertTrue(parsed.get("_batch_truncated"))

    def test_single_action_still_single(self):
        parsed = AgentRunner._parse(json.dumps(act("read_text", path="a"), ensure_ascii=False))
        self.assertEqual(parsed["type"], "tool")
        self.assertNotIn("actions", parsed)


class TruncatedBatchRecoveryTests(unittest.TestCase):
    def test_partial_batch_recovered(self):
        """批量被输出上限截断：完整的前几个动作仍要执行，不能整批作废。"""
        raw = ('{"type":"actions","actions":['
               '{"type":"tool","tool":"write_text_b64","arguments":{"path":"a.txt",'
               '"content_b64":"' + base64.b64encode(b"AAA").decode() + '"},'
               '"reason":"第一块"},'
               '{"type":"tool","tool":"append_text_b64","arguments":{"path":"a.txt",'
               '"content_b64":"' + base64.b64encode(b"BBB").decode() + '"},'
               '"reason":"第二块"},'
               '{"type":"tool","tool":"append_text_b64","arguments":{"path":"a.txt",'
               '"content_b64":"' + base64.b64encode(b"CCC").decode())   # 这里被截断
        parsed = AgentRunner._parse(raw)
        # 截断的批量：完整的前两个动作应被识别为批量并执行（_objects 的碎片
        # 扫描已能拿到完整对象；HTML 转义等极端情况由 _recover_partial_batch 兜底）
        self.assertEqual(parsed["type"], "actions")
        self.assertGreaterEqual(parsed["batch_size"], 2)
        paths = [a["arguments"]["path"] for a in parsed["actions"]]
        self.assertEqual(set(paths), {"a.txt"})


class BatchExecutionTests(unittest.TestCase):
    def _runner(self, ws, chat, state, steps, config=None):
        return AgentRunner(ToolRegistry(ws, {}), chat, config or {"max_total_steps": 20},
                           state_getter=lambda: dict(state),
                           state_saver=lambda **kw: state.update(kw),
                           step_saver=lambda s: steps.append(s),
                           file_event_saver=lambda e: None)

    def test_batch_writes_two_files_in_one_turn(self):
        ws = Path(tempfile.mkdtemp(prefix="cbp_batch_"))
        turns, steps = [], []
        state = {"state": "ready"}

        async def chat(prompt, model, make_new):
            turns.append(prompt)
            if len(turns) == 1:
                return json.dumps({"type": "actions", "actions": [
                    act("write_text", path="one.txt", content="1"),
                    act("write_text", path="two.txt", content="2"),
                ]}, ensure_ascii=False)
            return json.dumps({"type": "final", "content": "完成"}, ensure_ascii=False)

        runner = self._runner(ws, chat, state, steps)
        result = asyncio.run(runner.run("写两个文件", "m"))
        self.assertEqual(state.get("state"), "completed")
        self.assertEqual((ws / "one.txt").read_text(encoding="utf-8"), "1")
        self.assertEqual((ws / "two.txt").read_text(encoding="utf-8"), "2")
        # 一个回合两个动作：回合数=2（批量 + final），步骤数>=3
        self.assertEqual(len(turns), 2)
        tool_steps = [s for s in steps if s.get("tool")]
        self.assertEqual(len(tool_steps), 2)
        self.assertEqual(tool_steps[0].get("batch_of"), 2)
        self.assertEqual(steps[-1].get("batch_result"), {"size": 2, "ok": 2, "error": 0})

    def test_batch_costs_counted_once_per_turn(self):
        """同一回合的多个动作不能把 token 计数放大 N 倍。"""
        ws = Path(tempfile.mkdtemp(prefix="cbp_batch_"))
        turns, steps = [], []
        state = {"state": "ready"}

        async def chat(prompt, model, make_new):
            turns.append(prompt)
            if len(turns) == 1:
                return json.dumps({"type": "actions", "actions": [
                    act("write_text", path="a.txt", content="x" * 100),
                    act("write_text", path="b.txt", content="y" * 100),
                    act("write_text", path="c.txt", content="z" * 100),
                ]}, ensure_ascii=False)
            return json.dumps({"type": "final", "content": "完成"}, ensure_ascii=False)

        runner = self._runner(ws, chat, state, steps)
        asyncio.run(runner.run("写三个文件", "m"))
        est = int(state.get("estimated_tokens", 0))
        # 若按动作重复计数，估算值会约等于 3 倍回合成本；这里只应记一次
        per_action = [s for s in steps if s.get("tool")]
        self.assertEqual(len(per_action), 3)
        self.assertLess(est, 3 * sum(len(json.dumps(s, ensure_ascii=False)) for s in per_action))

    def test_batch_error_does_not_stop_later_actions(self):
        ws = Path(tempfile.mkdtemp(prefix="cbp_batch_"))
        turns, steps = [], []
        state = {"state": "ready"}

        async def chat(prompt, model, make_new):
            turns.append(prompt)
            if len(turns) == 1:
                return json.dumps({"type": "actions", "actions": [
                    act("read_text", path="不存在.txt"),
                    act("write_text", path="ok.txt", content="ok"),
                ]}, ensure_ascii=False)
            return json.dumps({"type": "final", "content": "完成"}, ensure_ascii=False)

        runner = self._runner(ws, chat, state, steps)
        asyncio.run(runner.run("容错", "m"))
        self.assertTrue((ws / "ok.txt").is_file())
        statuses = [s.get("status") for s in steps if s.get("tool")]
        self.assertEqual(statuses, ["error", "ok"])

    def test_batch_validation_gate_still_applies(self):
        """批量写代码文件后，未验证的 final 不能直接完成。

        验证门仍然生效（改了代码必须有真验证），只是第一次被拒时由**系统补跑**
        validate_python + run_unittest（实测长任务里模型换过多次会话后会忘记
        工具名、瞎编 run_tests/check_syntax，自己永远过不了门），补跑通过即
        正常收尾——因此这里看到的是补跑步骤而不是 validation_gate 步骤。
        """
        ws = Path(tempfile.mkdtemp(prefix="cbp_batch_"))
        turns, steps = [], []
        state = {"state": "ready"}

        async def chat(prompt, model, make_new):
            turns.append(prompt)
            if len(turns) == 1:
                return json.dumps({"type": "actions", "actions": [
                    act("write_text", path="m.py", content="print(1)\n"),
                    act("write_text", path="n.py", content="print(2)\n"),
                ]}, ensure_ascii=False)
            if len(turns) == 2:
                return json.dumps({"type": "final", "content": "完成"}, ensure_ascii=False)
            if len(turns) == 3:
                return json.dumps(act("validate_python", path="."), ensure_ascii=False)
            if len(turns) == 4:
                return json.dumps(act("run_unittest", path="tests"), ensure_ascii=False)
            return json.dumps({"type": "final", "content": "完成"}, ensure_ascii=False)

        (ws / "tests").mkdir()
        (ws / "tests" / "test_x.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): self.assertTrue(True)\n",
            encoding="utf-8")
        runner = self._runner(ws, chat, state, steps)
        asyncio.run(runner.run("写代码", "m"))
        tools = [(s.get("tool"), s.get("status")) for s in steps]
        self.assertIn(("validate_python", "ok"), tools)          # 系统补跑语法检查
        self.assertIn(("run_unittest", "ok"), tools)             # 系统补跑单元测试
        self.assertTrue(any(s.get("reason") == "验证门补跑（模型未自行验证）" for s in steps))
        self.assertEqual(state.get("state"), "completed")
        self.assertEqual(len(turns), 2)                          # 补跑通过，无需再喂模型回合


if __name__ == "__main__":
    unittest.main()
