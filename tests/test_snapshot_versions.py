# -*- coding: utf-8 -*-
"""快照版本化（按 Copilot 回合计版本）：一次 complete = 一个版本。

同一回合内对同一路径的多次写只记一个版本（首写原像）；"回滚到版本 N"
= 撤销第 N 回合之后的文件改动。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.snapshots import SnapshotStore


class SnapshotVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_snapver_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.store = SnapshotStore(self.tmp / "snaps")

    def test_versions_are_turn_numbered(self):
        a = self.ws / "a.py"; a.write_text("v0", encoding="utf-8")
        b = self.ws / "b.py"; b.write_text("b0", encoding="utf-8")
        self.store.next_turn("s1")                     # 回合 1
        self.store.capture("s1", "a.py", a, "write_text"); a.write_text("v1", encoding="utf-8")
        self.store.capture("s1", "b.py", b, "write_text"); b.write_text("b1", encoding="utf-8")
        self.store.next_turn("s1")                     # 回合 2
        self.store.capture("s1", "a.py", a, "write_text"); a.write_text("v2", encoding="utf-8")
        hist = self.store.load("s1")["history"]
        self.assertEqual([e["rev"] for e in hist["a.py"]], [1, 2])
        self.assertEqual([e["rev"] for e in hist["b.py"]], [1])

    def test_same_turn_multiple_writes_single_version(self):
        f = self.ws / "a.txt"; f.write_text("v0", encoding="utf-8")
        self.store.next_turn("s1")
        self.store.capture("s1", "a.txt", f, "write_text"); f.write_text("v1", encoding="utf-8")
        self.store.capture("s1", "a.txt", f, "write_text"); f.write_text("v2", encoding="utf-8")
        hist = self.store.load("s1")["history"]["a.txt"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["rev"], 1)

    def test_rollback_to_zero_is_full_rollback(self):
        f = self.ws / "a.txt"; f.write_text("v1", encoding="utf-8")
        self.store.next_turn("s1"); self.store.capture("s1", "a.txt", f, "write_text"); f.write_text("v2", encoding="utf-8")
        self.store.next_turn("s1"); self.store.capture("s1", "a.txt", f, "write_text"); f.write_text("v3", encoding="utf-8")
        r = self.store.restore_rev("s1", self.ws, 0)
        self.assertEqual(r["restored"], 1)
        self.assertEqual(f.read_text(encoding="utf-8"), "v1")

    def test_rollback_to_turn_undoes_later_turns(self):
        f = self.ws / "a.txt"; f.write_text("v1", encoding="utf-8")
        self.store.next_turn("s1"); self.store.capture("s1", "a.txt", f, "write_text"); f.write_text("v2", encoding="utf-8")
        self.store.next_turn("s1"); self.store.capture("s1", "a.txt", f, "write_text"); f.write_text("v3", encoding="utf-8")
        r = self.store.restore_rev("s1", self.ws, 1)   # 撤销回合 2，保留回合 1
        self.assertEqual(r["restored"], 1)
        self.assertEqual(f.read_text(encoding="utf-8"), "v2")

    def test_file_created_in_turn_deleted_on_earlier_rollback(self):
        new = self.ws / "new.py"
        self.store.next_turn("s1")
        self.store.capture("s1", "new.py", new, "write_text")   # 回合 1 新建（absent）
        new.write_text("新建\n", encoding="utf-8")
        r = self.store.restore_rev("s1", self.ws, 0)
        self.assertEqual(r["deleted"], 1)
        self.assertFalse(new.exists())

    def test_file_kept_when_rollback_to_its_creating_turn(self):
        new = self.ws / "new.py"
        self.store.next_turn("s1")
        self.store.capture("s1", "new.py", new, "write_text")   # 回合 1 新建
        new.write_text("新建\n", encoding="utf-8")
        r = self.store.restore_rev("s1", self.ws, 1)            # 回合 1 完成后
        self.assertEqual(r["deleted"], 0)
        self.assertTrue(new.exists())

    def test_rev_cap_keeps_first_and_recent(self):
        store = SnapshotStore(self.tmp / "snaps2")
        store.MAX_REVS_PER_PATH = 3
        f = self.ws / "a.txt"; f.write_text("v0", encoding="utf-8")
        for turn in range(1, 7):   # 6 个回合，每回合写一次
            store.next_turn("s1")
            store.capture("s1", "a.txt", f, "write_text")
            f.write_text(f"v{turn}", encoding="utf-8")
        revs = [e["rev"] for e in store.load("s1")["history"]["a.txt"]]
        self.assertEqual(len(revs), 3)
        self.assertEqual(revs[0], 1)    # 最早保留
        self.assertEqual(revs[-1], 6)   # 最近保留

    def test_full_restore_still_baseline(self):
        f = self.ws / "a.txt"; f.write_text("v1", encoding="utf-8")
        self.store.next_turn("s1"); self.store.capture("s1", "a.txt", f, "write_text"); f.write_text("v2", encoding="utf-8")
        self.store.next_turn("s1"); self.store.capture("s1", "a.txt", f, "write_text"); f.write_text("v3", encoding="utf-8")
        r = self.store.restore("s1", self.ws)
        self.assertEqual(f.read_text(encoding="utf-8"), "v1")


if __name__ == "__main__":
    unittest.main()
