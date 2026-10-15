"""P2-10 第三期开关：同一条事实两条读取路径（列/投影），不一致时回退到列。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import bridge as B
from cbp.session_store import SessionStore


class EventSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_evsrc_"))
        self.store = SessionStore(self.tmp / "a.db")
        self.store.create("s1", "任务", str(self.tmp), "m")
        self._orig = B.AGENT_SESSIONS
        B.AGENT_SESSIONS = self.store
        self._cfg = dict(B.AGENT_CFG)

    def tearDown(self):
        B.AGENT_SESSIONS = self._orig
        B.AGENT_CFG.clear()
        B.AGENT_CFG.update(self._cfg)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_source_is_columns(self):
        B.AGENT_CFG.pop("event_source", None)
        self.assertEqual(B.event_source(), "columns", "默认必须是列读取（可随时回退）")

    def test_config_and_env_resolution(self):
        B.AGENT_CFG["event_source"] = "projection"
        self.assertEqual(B.event_source(), "projection")
        B.AGENT_CFG["event_source"] = "乱写"
        self.assertEqual(B.event_source(), "columns", "非法值必须回落到 columns")
        B.AGENT_CFG.pop("event_source", None)
        import os
        os.environ["CBP_EVENT_SOURCE"] = "projection"
        try:
            self.assertEqual(B.event_source(), "projection")
        finally:
            os.environ.pop("CBP_EVENT_SOURCE", None)

    def test_projection_and_columns_agree(self):
        self.store.begin_step("s1", 0, 3, "outlook_send", "h")
        via_cols = B.pending_steps_via("s1", "columns")
        via_proj = B.pending_steps_via("s1", "projection")
        self.assertEqual([(x["conversation_index"], x["step_no"], x["tool"]) for x in via_cols],
                         [(x["conversation_index"], x["step_no"], x["tool"]) for x in via_proj])
        self.assertEqual(len(via_proj), 1)

    def test_finished_step_not_pending_in_both(self):
        self.store.begin_step("s1", 0, 3, "outlook_send", "h")
        self.store.finish_step("s1", 0, 3)
        self.assertEqual(B.pending_steps_via("s1", "columns"), [])
        self.assertEqual(B.pending_steps_via("s1", "projection"), [])

    def test_disagreement_falls_back_to_columns(self):
        """投影与列不一致时必须回退到列（安全优先），而不是相信投影。"""
        # 只写列（不发事件）→ 投影认为没有未收尾步骤，列里有
        con = self.store._connect()
        con.execute("INSERT INTO step_dispatch(session_id,conversation_index,step_no,tool,"
                    "args_hash,status,created) VALUES('s1',0,9,'delete_path','h','inflight',0)")
        con.commit()
        got = B.pending_steps_via("s1", "projection")
        self.assertEqual([(x["step_no"], x["tool"]) for x in got], [(9, "delete_path")],
                         "不一致时应采用列的结果")

    def test_multi_conversation_generation(self):
        """换会话后 step_no 复用，投影必须按对话序号分代（否则会把旧代当成未收尾）。"""
        self.store.begin_step("s1", 0, 5, "write_text", "h1")
        self.store.finish_step("s1", 0, 5)
        self.store.begin_step("s1", 1, 5, "write_text", "h2")
        got = B.pending_steps_via("s1", "projection")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["conversation_index"], 1)


if __name__ == "__main__":
    unittest.main()
