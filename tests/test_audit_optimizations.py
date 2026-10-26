# -*- coding: utf-8 -*-
"""审计优化回归：① 只读重复→回放缓存 ② 写操作重复仍被拦 ③ transient 标记 ④ 解析失败提示可操作。

用**真实 ToolRegistry**（临时工作区）+ 假 chat，驱动真实 AgentRunner。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gantry.agent as A  # noqa: E402
from gantry.tools import ToolRegistry  # noqa: E402


def make_ws():
    ws = Path(tempfile.mkdtemp(prefix="cbp_audit_ws_"))
    (ws / "a.py").write_text("print('x')\n", encoding="utf-8")
    (ws / "b.py").write_text("value = 1\n", encoding="utf-8")
    (ws / "sub").mkdir()
    return ws


class CountingTools(ToolRegistry):
    """真实注册表 + 执行计数（只数真正执行的次数）。"""

    def __init__(self, root, config=None):
        super().__init__(root, config or {})
        self.executed = 0

    async def execute(self, name, args):
        self.executed += 1
        return await super().execute(name, args)


def run_loop(script, tools, config=None):
    box = {"i": 0}

    async def chat(prompt, model=None, force_new=False, **kw):
        if box["i"] >= len(script):
            return '{"type":"final","markdown":"done"}'
        act = script[box["i"]]
        box["i"] += 1
        return json.dumps(act)

    saved = []
    cfg = {"repeat_guard_limit": 2, "keep_recent_steps": 10, "max_steps": 20}
    cfg.update(config or {})
    runner = A.AgentRunner(
        tools=tools, chat=chat, config=cfg,
        state_getter=lambda: {"state": "running"},
        state_saver=lambda **kw: None,
        step_saver=lambda st: saved.append(dict(st)),
        file_event_saver=lambda ev: None,
        approval_check=lambda *a, **k: True,
    )
    res = asyncio.run(runner.run("审计测试任务", "copilot-auto"))
    return res, saved, tools


class ReadonlyRepeatTests(unittest.TestCase):
    def test_repeat_readonly_returns_cache_not_block(self):
        same = {"type": "tool", "tool": "list_directory", "arguments": {"path": "."},
                "reason": "看看目录"}
        tools = CountingTools(make_ws())
        res, saved, tools = run_loop([same] * 6, tools)
        cached = [s for s in saved if s.get("cached")]
        guards = [s for s in saved if s.get("status") == "repeat_guard"]
        self.assertTrue(cached, "重复的只读调用应回放缓存")
        self.assertEqual(cached[0]["status"], "ok")
        self.assertIn("回放上次结果", str(cached[0].get("reason") or ""))
        self.assertFalse([g for g in guards if g.get("tool") == "list_directory"],
                         "只读工具不应再被 repeat_guard 拦下")

    def test_repeat_write_still_blocked(self):
        same = {"type": "tool", "tool": "replace_text",
                "arguments": {"path": "a.py", "find": "x", "replace": "y"},
                "reason": "改文件"}
        tools = CountingTools(make_ws())
        res, saved, tools = run_loop([same] * 6, tools)
        guards = [s for s in saved if s.get("status") == "repeat_guard"]
        self.assertTrue(guards, "写操作重复必须被拦下（保护副作用）")


class SourceGuards(unittest.TestCase):
    def test_service_unavailable_marked_transient(self):
        src = (ROOT / "gantry" / "agent.py").read_text(encoding="utf-8")
        i = src.find('"status": "service_unavailable"')
        self.assertGreater(i, 0)
        self.assertIn('"transient": True', src[i - 200:i + 300],
                      "限流/超时记录要标 transient，前端才能折叠")

    def test_parse_failure_hint_is_actionable(self):
        src = (ROOT / "gantry" / "agent.py").read_text(encoding="utf-8")
        i = src.find("【如何纠正】")
        self.assertGreater(i, 0, "解析失败要给可操作提示")
        seg = src[i:i + 800]
        for needle in ("一个 JSON", "read_text", '"type":"final"', "常用工具名"):
            self.assertIn(needle, seg, f"提示里缺少：{needle}")


class WorkLedgerTests(unittest.TestCase):
    """换会话时必须带「已看过 / 已改过」清单（pyshot 换了 4 次会话，92 次重复列目录）。"""

    def test_ledger_is_built_and_injected(self):
        import tempfile
        from gantry.tools import ToolRegistry

        ws = Path(tempfile.mkdtemp(prefix="cbp_ledger_"))
        tools = ToolRegistry(ws, {})
        runner = A.AgentRunner(
            tools=tools, chat=lambda *a, **k: "{}", config={},
            state_getter=lambda: {}, state_saver=lambda **k: None,
            step_saver=lambda s: None, file_event_saver=lambda e: None,
        )
        recent = [
            {"status": "ok", "tool": "list_directory", "arguments": {"path": "pyshot/"}},
            {"status": "ok", "tool": "read_text", "arguments": {"path": "pyshot/main.py"}},
            {"status": "ok", "tool": "grep", "arguments": {"path": "pyshot/tests"}},
            {"status": "ok", "tool": "read_text", "arguments": {"path": "pyshot/main.py"}},
        ]
        state = {"modified_files": ["pyshot/tests/test_a.py"],
                 "todos": [{"content": "A", "status": "completed"},
                           {"content": "B", "status": "pending"}],
                 "last_error": "上一处匹配失败", "total_steps": 12, "conversation_index": 2}
        ledger = runner._work_ledger(state, recent)
        self.assertIn("已读过的文件", ledger)
        self.assertIn("pyshot/main.py", ledger["已读过的文件"])
        self.assertEqual(len([p for p in ledger["已读过的文件"] if p == "pyshot/main.py"]), 1,
                         "同一文件只记一次")
        self.assertIn("pyshot/", ledger["已列过的目录"])
        self.assertIn("已改过的文件", ledger)
        self.assertIn("1", str(ledger["待办进度"]))
        prompt = runner._continuation_prompt("原任务", {"goal": "x"}, recent, state)
        self.assertIn("already_looked_at", prompt)
        self.assertIn("不要重新列", prompt)


class RateLimitWindowTests(unittest.TestCase):
    """限流处理的两条约束。

    曾经尝试过"一次睡到窗口结束"，实测**不可行**：单测护栏把重试时间参数钳在
    ≥60 秒，那个分支会让测试挂住；生产上长睡还会让暂停/取消变迟钝，而 Copilot
    往往提前恢复。最终保留下面两条真正有价值的改动。
    """

    def test_failure_budget_resets_after_window(self):
        src = (ROOT / "gantry" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("_window_passed", src,
                      "窗口过后失败计数要归零，否则一次长限流就把 30 次预算吃满、任务被硬停")
        self.assertIn("retry_note", src, "只有网页固定窗口才归零计数")

    def test_event_noise_reduced(self):
        src = (ROOT / "gantry" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("service_failures % 5 == 0", src, "限流事件要降噪（105 条 → 十几条）")
        self.assertIn('"transient": True', src)

    def test_no_giant_sleep_branch(self):
        src = (ROOT / "gantry" / "agent.py").read_text(encoding="utf-8")
        self.assertNotIn("睡到窗口结束", src,
                         "不要整段睡到窗口结束（拖慢测试、暂停/取消变迟钝）")


class UnknownToolHintTests(unittest.TestCase):
    """模型换会话后常编出工具名（run_tests/check_syntax/read_file…）。

    审计依据：`gantry/agent.py` 的注释里记过"模型瞎编 run_tests / check_syntax，
    怎么都过不了验证门，几小时的成果被判 failed"。
    """

    def test_alias_and_near_miss_suggestions(self):
        import tempfile
        from gantry.tools import ToolRegistry

        reg = ToolRegistry(Path(tempfile.mkdtemp(prefix="cbp_hint_")), {})
        cases = {
            "run_tests": "run_unittest",
            "check_syntax": "validate_python",
            "read_file": "read_text",
            "apply_diff": "apply_patch",
            "search": "grep",
        }
        for bad, good in cases.items():
            hint = reg._unknown_tool_hint(bad)
            self.assertIn(bad, hint)
            if good in reg._tools:
                self.assertIn(good, hint, f"{bad} 应提示改用 {good}")
            self.assertIn("当前可用工具", hint)

    def test_unknown_garbage_still_lists_tools(self):
        import tempfile
        from gantry.tools import ToolRegistry

        reg = ToolRegistry(Path(tempfile.mkdtemp(prefix="cbp_hint2_")), {})
        hint = reg._unknown_tool_hint("zzz_not_a_tool")
        self.assertIn("没有名为", hint)
        self.assertIn("当前可用工具", hint)

    def test_execute_raises_hint_not_plain_error(self):
        import asyncio
        import tempfile
        from gantry.tools import ToolRegistry, ToolError

        reg = ToolRegistry(Path(tempfile.mkdtemp(prefix="cbp_hint3_")), {})
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(reg.execute("run_tests", {}))
        msg = str(ctx.exception)
        self.assertIn("当前可用工具", msg, "未知工具的报错必须可操作")


class ToolErrorMessageTests(unittest.TestCase):
    def test_tool_errors_get_next_step(self):
        src = (ROOT / "gantry" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("下一步：核对", src,
                      "工具报错要补一句下一步（审计：116 条报错里 102 条没有引导）")


if __name__ == "__main__":
    unittest.main()
