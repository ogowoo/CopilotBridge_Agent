# -*- coding: utf-8 -*-
"""API 提供商 Agent 通道：必须带会话历史 + 必须写 runtime（供 WebUI 面板显示）。

实测故障（DeepSeek 会话）：
  1) AgentRunner 只在首回合把系统提示词（含可用工具清单）放进 prompt，后续
     回合只发"继续同一任务。上一动作实际结果：…"。浏览器版 Copilot 自己保留
     上下文所以没问题；提供商是无状态 HTTP 调用，若不发历史，第二回合起模型
     看不到工具清单与任务目标，直接回"本轮消息未附带可用工具清单"而卡死。
  2) provider_turn 完全不写 runtime → WebUI"最近发送内容/当前回答/执行状态"
     三个面板全程空白。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry import bridge as b
from gantry.session_store import SessionStore
from gantry.snapshots import SnapshotStore


class _FakeProvider:
    """记录每次收到的 messages，便于断言历史是否带上工具清单。"""
    name = "fakeprov"
    def __init__(self):
        self.calls = []
        self.reply = '{"type":"final","content":"完成"}'
        self.fail = None

    async def complete(self, model, messages, response_schema=None):
        self.calls.append([dict(m) for m in messages])
        if self.fail:
            raise self.fail
        return self.reply


class ProviderAgentTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_prov_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root,
                      dict(b.PROVIDERS), dict(b.RUNTIME), dict(b._PROVIDER_AGENT_HISTORY),
                      b._PROVIDER_HISTORY_MAX_CHARS)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.root = self.ws
        b.RUNTIME.clear()
        b._PROVIDER_AGENT_HISTORY.clear()
        self.prov = _FakeProvider()
        b.PROVIDERS["fakeprov"] = self.prov
        b.AGENT_SESSIONS.create("s1", "任务", str(self.ws), "fakeprov/m1")

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root,
         provs, rt, hist, b._PROVIDER_HISTORY_MAX_CHARS) = self.saved
        b.PROVIDERS.clear(); b.PROVIDERS.update(provs)
        b.RUNTIME.clear(); b.RUNTIME.update(rt)
        b._PROVIDER_AGENT_HISTORY.clear(); b._PROVIDER_AGENT_HISTORY.update(hist)

    def _turn(self, prompt, force_new=False):
        return asyncio.run(b.provider_turn("s1", "fakeprov/m1", prompt, force_new=force_new))

    def test_first_turn_carries_tool_list(self):
        first = "你是本地任务 Agent…\n可用工具：\n" + '{"list_directory":{}, "read_text":{}}'
        self._turn(first)
        sent = self.prov.calls[0]
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn("list_directory", sent[1]["content"])

    def test_later_turn_still_sees_tool_list_from_history(self):
        """核心回归：第二回合的提示很短，但历史里仍带着首回合的工具清单。"""
        first = "系统提示词…可用工具：\n" + '{"read_text":{}, "write_text_b64":{}}'
        self._turn(first)
        self._turn("继续同一任务。上一动作实际结果：\n{\"status\":\"ok\"}")
        sent = self.prov.calls[1]
        joined = "\n".join(m.get("content") or "" for m in sent)
        self.assertIn("read_text", joined)            # 工具清单仍在
        self.assertIn("write_text_b64", joined)
        self.assertIn("继续同一任务", sent[-1]["content"])
        # 历史应包含首回合的 assistant 回复
        self.assertEqual([m["role"] for m in sent],
                         ["system", "user", "assistant", "user"])

    def test_force_new_resets_history(self):
        self._turn("首轮…可用工具：read_text")
        self._turn("换会话后的摘要提示…可用工具：list_directory", force_new=True)
        sent = self.prov.calls[1]
        joined = "\n".join(m.get("content") or "" for m in sent)
        self.assertIn("list_directory", joined)
        self.assertNotIn("首轮", joined)              # 旧历史已丢弃

    def test_runtime_previews_written(self):
        """面板要有内容：prompt_preview / answer_preview / phase 必须被写入。"""
        self._turn("用户任务：\n写个文件")
        rt = b.RUNTIME.get("s1") or {}
        self.assertIn("写个文件", rt.get("prompt_preview") or "")
        self.assertIn("完成", rt.get("answer_preview") or "")
        self.assertIn(rt.get("phase"), {"api_sending", "api_received"})

    def test_failed_turn_does_not_duplicate_prompt(self):
        """失败回合要回滚刚追加的 user 消息，重试时历史不重复。"""
        self.prov.fail = b.providers_mod.ProviderUnavailable("HTTP 429")
        with self.assertRaises(b.CopilotRateLimited):
            self._turn("提示A")
        self.prov.fail = None
        self._turn("提示A")                            # 重试同一提示
        sent = self.prov.calls[-1]
        self.assertEqual([m["content"] for m in sent].count("提示A"), 1)

    def test_history_trim_keeps_system_and_tool_list(self):
        """历史超上限时回收老回合，但 system 与**首条（带工具清单）**必须保住。

        实测 493 步任务：历史裁剪把首条（含可用工具清单）丢掉后，模型忘记
        工具名、开始瞎编 run_tests/check_syntax，最后卡在验证门判 failed。
        """
        b._PROVIDER_HISTORY_MAX_CHARS = 300   # system 本身约 600 字符，故只断言结构
        self._turn("首轮提示：可用工具清单 list_directory read_text write_text_b64 " + "A" * 200)
        for i in range(10):
            self._turn(f"继续同一任务。第 {i} 轮结果：" + "x" * 80)
        hist = b._PROVIDER_AGENT_HISTORY["s1"]
        joined = "\n".join(m.get("content") or "" for m in hist)
        self.assertEqual(hist[0]["role"], "system")        # system 始终保留
        self.assertIn("可用工具清单", hist[1]["content"])   # 首条（工具清单）保住
        self.assertLessEqual(len(hist), 5)                 # 老回合被回收
        self.assertNotIn("第 3 轮结果", joined)             # 中间回合已丢
        self.assertIn("第 9 轮结果", joined)                # 最近一轮仍在

    def test_discard_clears_history(self):
        self._turn("提示")
        self.assertIn("s1", b._PROVIDER_AGENT_HISTORY)
        b.discard_provider_agent_history("s1")
        self.assertNotIn("s1", b._PROVIDER_AGENT_HISTORY)


if __name__ == "__main__":
    unittest.main()
