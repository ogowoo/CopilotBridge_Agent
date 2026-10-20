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

import cbp.agent as A  # noqa: E402
from cbp.tools import ToolRegistry  # noqa: E402


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
        src = (ROOT / "cbp" / "agent.py").read_text(encoding="utf-8")
        i = src.find('"status": "service_unavailable"')
        self.assertGreater(i, 0)
        self.assertIn('"transient": True', src[i - 200:i + 300],
                      "限流/超时记录要标 transient，前端才能折叠")

    def test_parse_failure_hint_is_actionable(self):
        src = (ROOT / "cbp" / "agent.py").read_text(encoding="utf-8")
        i = src.find("【如何纠正】")
        self.assertGreater(i, 0, "解析失败要给可操作提示")
        seg = src[i:i + 800]
        for needle in ("一个 JSON", "read_text", '"type":"final"', "常用工具名"):
            self.assertIn(needle, seg, f"提示里缺少：{needle}")


if __name__ == "__main__":
    unittest.main()
