# -*- coding: utf-8 -*-
"""provider（API）会话禁用"需要 Copilot 网页会话"的工具。

真实时间线（task-mv7zkarh）：模型反复调用 copilot_file，失败 10 次
（"Copilot 回答里没有找到下载入口"）——该工具靠驱动 Copilot 网页对话生成文件，
而提供商会话根本没有网页对话，注定失败、纯烧回合。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import bridge as b
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore
from cbp.tools import ToolError, ToolRegistry

WEB_TOOLS = ("copilot_attach", "copilot_file")


class WebToolGateTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_webtool_"))
        self.tools = ToolRegistry(self.ws, {"allow_office": False, "allow_outlook": False})
        # 测试环境手动注册（桥接层在导入时注册）
        for name in WEB_TOOLS:
            async def _fn(*a, **k):
                return {"ok": True}
            self.tools.register(name, _fn, "需要 Copilot 网页会话", {"path": "相对路径"})

    def _names(self):
        return {d["name"] for d in self.tools.descriptions()}

    def test_enabled_by_default(self):
        self.assertTrue(self.tools.allow_web_copilot_tools)
        self.assertTrue(set(WEB_TOOLS) <= self._names())

    def test_disabled_hides_from_prompt_and_tool_list(self):
        self.tools.set_web_copilot_tools(False)
        self.assertFalse(set(WEB_TOOLS) & self._names(), "禁用后不该再注入工具清单")
        groups = self.tools.tool_list()["groups"]
        flat = {n for names in groups.values() for n in names}
        self.assertFalse(set(WEB_TOOLS) & flat, "tool_list 也不该列出")

    def test_disabled_execution_gives_actionable_error(self):
        self.tools.set_web_copilot_tools(False)
        with self.assertRaises(ToolError) as cm:
            asyncio.run(self.tools.execute("copilot_file", {"path": "x.py", "instruction": "生成"}))
        msg = str(cm.exception)
        self.assertIn("需要 Copilot 网页会话", msg)
        self.assertIn("write_text_b64", msg)      # 给出替代做法

    def test_reenable_restores(self):
        self.tools.set_web_copilot_tools(False)
        self.tools.set_web_copilot_tools(True)
        self.assertTrue(set(WEB_TOOLS) <= self._names())


class ProviderSessionToolSurfaceTests(unittest.TestCase):
    """走一遍真实 run_agent：provider 会话的工具清单里没有 copilot_*，且跑完复位。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_provsurf_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, dict(b.PROVIDERS))
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, _r, provs) = self.saved
        b.PROVIDERS.clear(); b.PROVIDERS.update(provs)
        b.AGENT_TOOLS.set_web_copilot_tools(True)

    def test_provider_prompt_excludes_copilot_tools(self):
        prompts = []

        class P:
            async def complete(self, model, messages, response_schema=None):
                prompts.append("\n".join(m.get("content") or "" for m in messages))
                return '{"type":"final","content":"完成"}'

        b.PROVIDERS["fakeprov"] = P()
        req = b.AgentRequest(model="fakeprov/m1",
                             messages=[b.Message(role="user", content="把结果生成一个文件")],
                             stream=False, session_id="s1", workspace=str(self.ws))
        asyncio.run(b.run_agent(req))
        joined = prompts[0]
        # 1) 工具清单 JSON 里不该出现 copilot_* —— 模型看不到就不会调
        import json as _json
        import re as _re
        # 工具清单是系统提示词的最后一段：从标记处截到末尾再解析。
        # （不能用非贪婪 .*? 到第一个 ]——工具描述里可能有 phases:[{...}] 之类嵌套）
        marker = "可用工具：\n"
        self.assertIn(marker, joined, "提示词里未找到工具清单")
        tail = joined.split(marker, 1)[1].strip()
        # 系统提示词之后还会拼 "用户任务：..."，解析前截掉
        # 工具清单之后还有"项目约定/AGENT.md"等段落：用 raw_decode 只取第一个 JSON 值
        tools_list, _end = _json.JSONDecoder().raw_decode(tail)
        names = {x["name"] for x in tools_list}
        self.assertFalse([n for n in names if n.startswith("copilot")])
        # 2) 也不能再教模型"大文件优先用 copilot_attach 丢给 Copilot 分析"
        self.assertNotIn("丢给 Copilot 分析", joined)
        self.assertIn("没有 Copilot 网页可用", joined)   # 明确告知不可用
        # 跑完复位，不影响后续会话
        self.assertTrue(b.AGENT_TOOLS.allow_web_copilot_tools)

    def test_provider_calling_copilot_file_is_refused(self):
        """模型硬调用（例如凭记忆）时也必须被拒，并给出替代做法。"""
        released = iter([
            '{"type":"tool","tool":"copilot_file","arguments":{"path":"x.py","instruction":"生成"}}',
            '{"type":"final","content":"改为分块写入"}',
        ])

        class P:
            async def complete(self, model, messages, response_schema=None):
                return next(released, '{"type":"final","content":"结束"}')

        b.PROVIDERS["fakeprov"] = P()
        req = b.AgentRequest(model="fakeprov/m1",
                             messages=[b.Message(role="user", content="生成文件")],
                             stream=False, session_id="s2", workspace=str(self.ws))
        asyncio.run(b.run_agent(req))
        steps = b.AGENT_SESSIONS.steps("s2")
        bad = [s for s in steps if s.get("tool") == "copilot_file"]
        self.assertTrue(bad)
        self.assertEqual(bad[0].get("status"), "error")
        self.assertIn("需要 Copilot 网页会话", str(bad[0].get("error")))


if __name__ == "__main__":
    unittest.main()
