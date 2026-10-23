# -*- coding: utf-8 -*-
"""对抗式审计（adversarial audit）：审计员并行 + 裁判核验 + 收敛判定。

用户："安排两个或以上的 subagent 对抗性审查完成的项目，再安排一个裁判，
直至仅剩较小问题才算完成。""然后："只能跑一轮？继续完成它。"

覆盖：解析容错、单轮收敛、major→修复→再审、major 上限与误报剔除。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from cbp import bridge as b
from cbp.session_store import SessionStore


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class ParseTests(unittest.TestCase):
    def test_plain_array(self):
        out = b._audit_parse('[{"title":"t","severity":"p1"}]')
        self.assertIsInstance(out, list)
        self.assertEqual(out[0]["title"], "t")

    def test_markdown_fence(self):
        out = b._audit_parse('```json\n[{"title":"t"}]\n```')
        self.assertIsInstance(out, list)

    def test_prose_around_object(self):
        out = b._audit_parse('分析完成：\n{"issues":[],"verdict":"clean"}\n以上。')
        self.assertIsInstance(out, dict)
        self.assertEqual(out["verdict"], "clean")

    def test_invalid_returns_none(self):
        self.assertIsNone(b._audit_parse("这不是 JSON"))
        self.assertIsNone(b._audit_parse(""))


class AuditFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_audit_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.AGENT_TOOLS.root, b.run_subagent)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "a.db")
        b.AGENT_TOOLS.set_root(self.ws, False)

    def tearDown(self):
        b.AGENT_SESSIONS, root, b.run_subagent = self.saved
        b.AGENT_TOOLS.set_root(root, False)

    def test_converges_when_clean(self):
        calls = []

        async def fake(task, description="", model="", max_steps=0):
            calls.append(description)
            if "审计员" in description:
                return {"agent_id": "sub-a", "content": "[]"}
            if "裁判" in description:
                return {"agent_id": "sub-j",
                        "content": '{"issues":[],"false_positives":[],"verdict":"clean"}'}
            return {"agent_id": "sub-f", "content": '{"fixed":[],"failed":[]}'}

        b.run_subagent = fake
        out = run(b.adversarial_audit(objective="验证", auditors=2,
                                      max_rounds=3, auto_fix=True))
        self.assertTrue(out["complete"])
        self.assertEqual(out["verdict"], "clean")
        self.assertEqual(len(out["rounds"]), 1, "clean 一轮就收敛")
        self.assertFalse(any("修复" in c for c in calls), "clean 不该触发修复")

    def test_major_then_minor_triggers_fix_and_second_round(self):
        seq = []

        async def fake(task, description="", model="", max_steps=0):
            if "审计员" in description:
                return {"agent_id": "sub-a",
                        "content": '[{"title":"X 未校验输入","severity":"p1",'
                                   '"evidence":{"file":"a.py","lines":"10"},"suggestion":"校验"}]'}
            if "裁判" in description:
                n = len([c for c in seq if c == "judge"])
                seq.append("judge")
                if n == 0:
                    return {"agent_id": "sub-j",
                            "content": '{"issues":[{"title":"X 未校验输入","severity":"p1",'
                                       '"evidence":{"file":"a.py","lines":"10"},'
                                       '"suggestion":"校验","sources":["correctness"]}],'
                                       '"false_positives":[],"verdict":"major"}'}
                return {"agent_id": "sub-j",
                        "content": '{"issues":[],"false_positives":[],"verdict":"clean"}'}
            if "修复" in description:
                seq.append("fix")
                return {"agent_id": "sub-f",
                        "content": '{"fixed":["X 未校验输入"],"failed":[]}'}
            return {"agent_id": "sub-x", "content": "[]"}

        b.run_subagent = fake
        out = run(b.adversarial_audit(objective="验证", auditors=2,
                                      max_rounds=3, auto_fix=True))
        self.assertTrue(out["complete"])
        self.assertEqual(len(out["rounds"]), 2, "major → 修复 → 再审一轮")
        self.assertEqual(out["fixes"], 1)
        self.assertEqual(out["rounds"][0]["verdict"], "major")
        self.assertEqual(out["rounds"][1]["verdict"], "clean")

    def test_major_stops_when_no_fix(self):
        async def fake(task, description="", model="", max_steps=0):
            if "审计员" in description:
                return {"agent_id": "a",
                        "content": '[{"title":"t","severity":"p1",'
                                   '"evidence":{"file":"a.py","lines":"1"},"suggestion":"s"}]'}
            if "裁判" in description:
                return {"agent_id": "j",
                        "content": '{"issues":[{"title":"t","severity":"p1",'
                                   '"evidence":{"file":"a.py","lines":"1"},"suggestion":"s"}],'
                                   '"verdict":"major"}'}
            return {"agent_id": "f", "content": "{}"}

        b.run_subagent = fake
        out = run(b.adversarial_audit(objective="x", auditors=2,
                                      max_rounds=3, auto_fix=False))
        self.assertFalse(out["complete"])
        self.assertEqual(len(out["rounds"]), 1, "不自动修复时 major 一轮就停，避免空转")

    def test_judge_unparseable_defaults_to_major(self):
        async def fake(task, description="", model="", max_steps=0):
            if "审计员" in description:
                return {"agent_id": "a",
                        "content": '[{"title":"t","severity":"p1",'
                                   '"evidence":{"file":"a.py","lines":"1"},"suggestion":"s"}]'}
            if "裁判" in description:
                return {"agent_id": "j", "content": "这是自然语言，不是 JSON"}
            return {"agent_id": "f", "content": "{}"}

        b.run_subagent = fake
        out = run(b.adversarial_audit(objective="x", auditors=2,
                                      max_rounds=3, auto_fix=False))
        self.assertEqual(out["rounds"][0]["verdict"], "major",
                         "裁判输出无法解析时按 major（保守）处理")

    def test_messages_and_session_state(self):
        async def fake(task, description="", model="", max_steps=0):
            if "审计员" in description:
                return {"agent_id": "a", "content": "[]"}
            if "裁判" in description:
                return {"agent_id": "j", "content": '{"issues":[],"verdict":"clean"}'}
            return {"agent_id": "f", "content": "{}"}

        b.run_subagent = fake
        out = run(b.adversarial_audit(objective="验证", auditors=2,
                                      max_rounds=3, auto_fix=True))
        msgs = b.AGENT_SESSIONS.messages(out["session_id"])
        self.assertTrue(any("对抗式审计" in m["content"] for m in msgs))
        self.assertTrue(any("审计完成" in m["content"] for m in msgs))
        self.assertEqual(b.AGENT_SESSIONS.get(out["session_id"])["state"], "completed")


class AuditApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_audit_api_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.AGENT_TOOLS.root, b.run_subagent)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "a.db")
        b.AGENT_TOOLS.set_root(self.ws, False)
        from fastapi.testclient import TestClient
        from cbp import api as api_mod
        self.client = TestClient(api_mod.app)

    def tearDown(self):
        b.AGENT_SESSIONS, root, b.run_subagent = self.saved
        b.AGENT_TOOLS.set_root(root, False)

    def test_empty_objective_400(self):
        r = self.client.post("/v1/agent/audit", json={})
        self.assertEqual(r.status_code, 400)

    def test_starts_background_session(self):
        async def fake(task, description="", model="", max_steps=0):
            if "审计员" in description:
                return {"agent_id": "a", "content": "[]"}
            if "裁判" in description:
                return {"agent_id": "j", "content": '{"issues":[],"verdict":"clean"}'}
            return {"agent_id": "f", "content": "{}"}

        b.run_subagent = fake
        r = self.client.post("/v1/agent/audit", json={"objective": "验证"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["started"])
        self.assertTrue(str(d["session_id"]).startswith("audit-"))
        self.assertTrue(b.AGENT_SESSIONS.get(d["session_id"]))


if __name__ == "__main__":
    unittest.main()
