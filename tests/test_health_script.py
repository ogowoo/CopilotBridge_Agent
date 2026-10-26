"""巡检脚本：区分"遗留会话（0 事件）"与"真不一致"，退出码可接 CI。"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from cbp.session_store import SessionStore


class HealthScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_health_"))
        self.db = self.tmp / "a.db"
        self.store = SessionStore(self.db)

    def _run(self, *args):
        return subprocess.run([sys.executable, str(BASE / "scripts" / "health.py"),
                               "--db", str(self.db), *args],
                              cwd=BASE, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    def test_consistent_session_exit_zero(self):
        self.store.create("s-new", "任务", str(self.tmp), "m")
        self.store.begin_turn("s-new", 0, "ph", 0)
        self.store.save_turn("s-new", 0, "ph", "答案", 1)
        r = self._run("--session", "s-new")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("不一致 0", r.stdout)

    def test_legacy_session_without_events_is_skipped(self):
        self.store.create("s-old", "任务", str(self.tmp), "m")
        # 直接写检查点列但**不发事件**，模拟事件功能上线前的历史会话
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO turn_results(session_id,conversation_index,prompt_hash,answer,"
                    "steps_after,status,created) VALUES('s-old',0,'ph','a',1,'done',0)")
        con.commit()
        con.close()
        r = self._run("--session", "s-old")
        self.assertEqual(r.returncode, 0, "遗留会话不该让巡检失败")
        self.assertIn("早于事件功能", r.stdout)

    def test_real_mismatch_fails(self):
        """有事件但列里多出一个往返：应判为不一致并以非 0 退出。"""
        self.store.create("s-bad", "任务", str(self.tmp), "m")
        self.store.begin_turn("s-bad", 0, "ph1", 0)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO turn_results(session_id,conversation_index,prompt_hash,answer,"
                    "steps_after,status,created) VALUES('s-bad',0,'phX','a',1,'done',0)")
        con.commit()
        con.close()
        r = self._run("--session", "s-bad")
        self.assertEqual(r.returncode, 1)
        self.assertIn("不一致 1", r.stdout)

    def test_coverage_gap_is_warning_not_failure(self):
        """表里有记录却没有对应事件＝覆盖告警；默认不致命，--strict 才致命。"""
        self.store.create("s-gap", "任务", str(self.tmp), "m")
        self.store.begin_turn("s-gap", 0, "ph", 0)          # 有事件
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO file_events(session_id,created,tool,path,result) "
                    "VALUES('s-gap',0,'write_text','a.txt','ok')")   # 但没有 file_event 事件
        con.commit()
        con.close()
        r = self._run("--session", "s-gap")
        self.assertEqual(r.returncode, 0, "覆盖告警默认不该让巡检失败")
        self.assertIn("事件覆盖告警", r.stdout)
        r2 = self._run("--session", "s-gap", "--strict")
        self.assertEqual(r2.returncode, 1, "--strict 下覆盖缺口应致命")

    def test_legacy_session_has_no_coverage_warning(self):
        self.store.create("s-legacy", "任务", str(self.tmp), "m")
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO file_events(session_id,created,tool,path,result) "
                    "VALUES('s-legacy',0,'write_text','a.txt','ok')")
        con.commit()
        con.close()
        r = self._run("--session", "s-legacy")
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("事件覆盖告警", r.stdout, "0 事件的历史会话不该报覆盖缺口")

    def test_json_output_shape(self):
        self.store.create("s1", "任务", str(self.tmp), "m")
        self.store.begin_turn("s1", 0, "ph", 0)
        r = self._run("--session", "s1", "--json")
        data = json.loads(r.stdout)
        self.assertIn("checked", data)
        self.assertIn("legacy_without_events", data)
        self.assertEqual(data["checked"], 1)


if __name__ == "__main__":
    unittest.main()
