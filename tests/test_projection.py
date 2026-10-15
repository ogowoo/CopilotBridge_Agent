"""事件投影（P2-10 第二期）：重放能重建状态，并与数据库列一致。"""
import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.agent import AgentRunner
from cbp.projection import compare_with_columns, project
from cbp.session_store import SessionStore
from cbp.tools import ToolRegistry


class ProjectionUnitTests(unittest.TestCase):
    def _events(self):
        return [
            {"id": 1, "kind": "state", "payload": {"from": "created", "to": "running"}},
            {"id": 2, "kind": "turn_begin", "payload": {"prompt_hash": "h1", "steps_before": 0,
                                                        "conversation_index": 0}},
            {"id": 3, "kind": "turn_done", "payload": {"prompt_hash": "h1", "steps_after": 1,
                                                       "answer_chars": 42}},
            {"id": 4, "kind": "step_begin", "payload": {"step_no": 1, "tool": "read_text",
                                                        "args_hash": "a1"}},
            {"id": 5, "kind": "step_done", "payload": {"step_no": 1, "status": "done"}},
            {"id": 6, "kind": "step_recorded", "payload": {"step_no": 1, "tool": "read_text",
                                                           "status": "ok"}},
            {"id": 7, "kind": "approval_granted", "payload": {"tool": "delete_path"}},
            {"id": 8, "kind": "approval_taken", "payload": {"tool": "delete_path"}},
            {"id": 9, "kind": "state", "payload": {"from": "running", "to": "completed"}},
            {"id": 10, "kind": "自定义未来事件", "payload": {}},
        ]

    def test_projection_rebuilds_state(self):
        p = project(self._events())
        self.assertEqual(p["event_count"], 10)
        self.assertEqual(p["last_event_id"], 10)
        self.assertEqual(p["turns"][0]["steps_after"], 1)
        self.assertTrue(p["turns"][0]["done"])
        self.assertEqual(p["steps"][0]["status"], "done")
        self.assertEqual(p["approvals"], {"granted": 1, "taken": 1, "denied": 0})
        self.assertEqual(p["recorded_steps"], [1])
        self.assertEqual(p["current_state"], "completed")
        self.assertEqual(p["unknown_kinds"], {"自定义未来事件": 1},
                         "未知事件只计数，不得破坏投影（便于将来加事件）")

    def test_projection_is_deterministic(self):
        self.assertEqual(project(self._events()), project(self._events()))

    def test_compare_detects_missing_turn(self):
        p = project(self._events())
        problems = compare_with_columns(p, [{"prompt_hash": "hX", "status": "done",
                                             "steps_after": 1}], [])
        self.assertTrue(problems and "hX" in problems[0])

    def test_compare_detects_status_mismatch(self):
        p = project(self._events())
        problems = compare_with_columns(p, [], [{"step_no": 1, "tool": "read_text",
                                                 "status": "done"}])
        self.assertEqual(problems, [], "事件里已收尾，列也是 done → 一致")

    def test_compare_flags_still_inflight(self):
        events = [e for e in self._events() if e["kind"] != "step_done"]
        p = project(events)
        problems = compare_with_columns(p, [], [{"step_no": 1, "tool": "read_text",
                                                 "status": "done"}])
        self.assertTrue(any("inflight" in x for x in problems))


class ProjectionAgainstRealRunTests(unittest.TestCase):
    """跑一次真实 runner 流程（假聊天函数），再检查"事件能重建列里的状态"。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_proj_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        (self.ws / "a.txt").write_text("hello", encoding="utf-8")
        self.store = SessionStore(self.tmp / "a.db")
        self.store.create("s1", "任务", str(self.ws), "m")
        self.state = {"state": "ready"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_events_reconstruct_checkpoint_tables(self):
        calls = []

        async def chat(prompt, model, make_new):
            # 像 bridge 那样在发请求前后登记回合检查点（turn_begin/turn_done 由桥接层发）
            calls.append(prompt)
            ph = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:20]
            self.store.begin_turn("s1", 0, ph, int(self.state.get("total_steps") or 0))
            if len(calls) == 1:
                ans = json.dumps({"type": "tool", "tool": "read_text",
                                  "arguments": {"path": "a.txt"}, "reason": "读"})
            else:
                ans = json.dumps({"type": "final", "content": "完"})
            self.store.save_turn("s1", 0, ph, ans, int(self.state.get("total_steps") or 0))
            return ans

        def state_saver(**kw):
            self.state.update(kw)
            self.store.patch("s1", **kw)

        runner = AgentRunner(ToolRegistry(self.ws, {}), chat, {"max_total_steps": 5},
                             state_getter=lambda: dict(self.state),
                             state_saver=state_saver,
                             step_saver=lambda s: self.store.add_step("s1", int(s.get("step") or 1), 0, s),
                             file_event_saver=lambda e: None,
                             step_begin=lambda no, tool, h: self.store.begin_step("s1", 0, no, tool, h),
                             step_finish=lambda no, st="done": self.store.finish_step("s1", 0, no, st))
        asyncio.run(runner.run("读 a.txt", "m"))

        events = self.store.events("s1")
        kinds = [e["kind"] for e in events]
        self.assertIn("turn_begin", kinds)
        self.assertIn("step_begin", kinds)
        self.assertIn("step_done", kinds)

        p = project(events)
        turns_rows = [{"prompt_hash": r["prompt_hash"], "status": r["status"],
                       "steps_after": r["steps_after"]}
                      for r in self.store._connect().execute(
                          "SELECT prompt_hash,status,steps_after FROM turn_results WHERE session_id='s1'")]
        steps_rows = [dict(r) for r in self.store._connect().execute(
            "SELECT step_no,tool,status FROM step_dispatch WHERE session_id='s1'")]
        problems = compare_with_columns(p, turns_rows, steps_rows)
        self.assertEqual(problems, [], f"投影与列必须一致：{problems}")
        self.assertGreaterEqual(p["event_count"], 4)


if __name__ == "__main__":
    unittest.main()
