# -*- coding: utf-8 -*-
"""端到端：提供商（API）通道的 Agent 写文件路径必须真的落盘。

用户反馈"能读，没见有写文件"。工具清单里写类工具确实都在（core 组），
所以这里用假提供商按真实协议返回一个 write_text_b64 动作，验证
run_agent → _parse → 工具执行 → 落盘 全链路可用；若本用例通过，说明
"不写文件"不是代码路径问题，而是模型没发出写动作（可据此查提示词/模型）。
"""
import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import bridge as b
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore


class _ScriptedProvider:
    """按脚本依次返回回答，并记录每回合收到的 messages。"""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, model, messages, response_schema=None):
        self.calls.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else '{"type":"final","content":"结束"}'


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode()


class ProviderWritePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_provwrite_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root,
                      b.AGENT_TOOLS.config if hasattr(b.AGENT_TOOLS, "config") else None,
                      dict(b.PROVIDERS), dict(b._PROVIDER_AGENT_HISTORY))
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        b._PROVIDER_AGENT_HISTORY.clear()

    def tearDown(self):
        (b.AGENT_SESSIONS, b.SNAPSHOTS, _root, _cfg,
         provs, hist) = self.saved
        b.PROVIDERS.clear(); b.PROVIDERS.update(provs)
        b._PROVIDER_AGENT_HISTORY.clear(); b._PROVIDER_AGENT_HISTORY.update(hist)

    def _run(self, replies, task="写一个 out.py 文件"):
        prov = _ScriptedProvider(replies)
        b.PROVIDERS["fakeprov"] = prov
        req = b.AgentRequest(model="fakeprov/m1",
                             messages=[b.Message(role="user", content=task)],
                             stream=False, session_id="s1", workspace=str(self.ws))
        result, sid = asyncio.run(b.run_agent(req))
        return prov, result, sid

    def test_write_text_b64_lands_on_disk(self):
        content = "print('hello')\n"
        prov, result, sid = self._run([
            '{"type":"tool","tool":"write_text_b64","arguments":{"path":"out.py","content_b64":"%s"},"reason":"写入"}' % b64(content),
            '{"type":"final","content":"已写入 out.py"}',
        ])
        target = self.ws / "out.py"
        self.assertTrue(target.is_file(), "提供商通道未能把文件写盘")
        self.assertEqual(target.read_text(encoding="utf-8"), content)
        # 第二步的提示应带上"上一动作实际结果"，且历史里能看到首回合工具清单
        second = prov.calls[1]
        joined = "\n".join(m.get("content") or "" for m in second)
        self.assertIn("write_text_b64", joined)
        self.assertIn("可用工具", joined)

    def test_append_and_replace_also_work(self):
        self._run([
            '{"type":"tool","tool":"write_text_b64","arguments":{"path":"a.txt","content_b64":"%s"}}' % b64("v1\n"),
            '{"type":"tool","tool":"append_text_b64","arguments":{"path":"a.txt","content_b64":"%s"}}' % b64("v2\n"),
            '{"type":"tool","tool":"replace_text","arguments":{"path":"a.txt","old":"v1","new":"V1"}}',
            '{"type":"final","content":"完成"}',
        ])
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "V1\nv2\n")

    def test_write_action_reaches_step_log(self):
        """写动作必须在时间线里留下可核对的一步（用户据此判断有没有写）。"""
        _, _, sid = self._run([
            '{"type":"tool","tool":"write_text_b64","arguments":{"path":"b.txt","content_b64":"%s"}}' % b64("x"),
            '{"type":"final","content":"完成"}',
        ])
        steps = b.AGENT_SESSIONS.steps(sid)
        wrote = [s for s in steps if s.get("tool") == "write_text_b64"]
        self.assertTrue(wrote, "时间线里没有写步骤")
        self.assertEqual(wrote[0].get("status"), "ok")


if __name__ == "__main__":
    unittest.main()
