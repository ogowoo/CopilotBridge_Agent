# -*- coding: utf-8 -*-
"""快照版本化：每次写操作前捕获带 rev 的原像，回滚可选定版本。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.snapshots import SnapshotStore


class SnapshotVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_snapver_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.store = SnapshotStore(self.tmp / "snaps")

    def test_versions_listed_in_rev_order(self):
        f = self.ws / "a.py"
        f.write_text("v1", encoding="utf-8")
        self.store.capture("s1", "a.py", f, "write_text")
        f.write_text("v2", encoding="utf-8")
        self.store.capture("s1", "a.py", f, "write_text")
        vs = self.store.versions("s1")
        self.assertEqual([v["rev"] for v in vs], [1, 2])
        self.assertEqual([v["path"] for v in vs], ["a.py", "a.py"])

    def test_restore_to_first_version_is_baseline(self):
        f = self.ws / "a.txt"
        f.write_text("v1", encoding="utf-8")
        self.store.capture("s1", "a.txt", f, "write_text")     # rev 1
        f.write_text("v2", encoding="utf-8")
        self.store.capture("s1", "a.txt", f, "write_text")     # rev 2
        f.write_text("v3", encoding="utf-8")
        r = self.store.restore_rev("s1", self.ws, 1)
        self.assertEqual(r["restored"], 1)
        self.assertEqual(f.read_text(encoding="utf-8"), "v1")

    def test_restore_to_later_version_undoes_only_later_writes(self):
        f = self.ws / "a.txt"
        f.write_text("v1", encoding="utf-8")
        self.store.capture("s1", "a.txt", f, "write_text")     # rev 1
        f.write_text("v2", encoding="utf-8")
        self.store.capture("s1", "a.txt", f, "write_text")     # rev 2
        f.write_text("v3", encoding="utf-8")
        r = self.store.restore_rev("s1", self.ws, 2)
        self.assertEqual(r["restored"], 1)
        self.assertEqual(f.read_text(encoding="utf-8"), "v2")  # 撤销第 2 次写之后的变化

    def test_file_created_after_rev_is_deleted(self):
        new = self.ws / "new.py"
        self.store.capture("s1", "new.py", new, "write_text")  # rev 1（当时不存在）
        new.write_text("新建\n", encoding="utf-8")
        # 其它文件在 rev 1 之前就存在并改动，作为对照
        f = self.ws / "old.py"
        f.write_text("v1", encoding="utf-8")
        self.store.capture("s1", "old.py", f, "write_text")    # rev 2
        f.write_text("v2", encoding="utf-8")
        # 回滚到 rev 1：new.py 尚未创建 → 删除；old.py 尚未被本任务改动 → 还原 v1
        r = self.store.restore_rev("s1", self.ws, 1)
        self.assertFalse(new.exists())
        self.assertEqual(f.read_text(encoding="utf-8"), "v1")

    def test_rev_cap_keeps_first_and_recent(self):
        store = SnapshotStore(self.tmp / "snaps2")
        store.MAX_REVS_PER_PATH = 3
        f = self.ws / "a.txt"
        for i in range(6):
            f.write_text(f"v{i}", encoding="utf-8")
            store.capture("s1", "a.txt", f, "write_text")
        hist = store.load("s1")["history"]["a.txt"]
        revs = [e["rev"] for e in hist]
        self.assertEqual(len(revs), 3)
        self.assertEqual(revs[0], 1)          # 最早保留
        self.assertEqual(revs[-1], 6)         # 最近保留

    def test_full_restore_still_baseline(self):
        """不指定版本的回滚仍是整任务回到开始前（向后兼容）。"""
        f = self.ws / "a.txt"
        f.write_text("v1", encoding="utf-8")
        self.store.capture("s1", "a.txt", f, "write_text")
        f.write_text("v2", encoding="utf-8")
        self.store.capture("s1", "a.txt", f, "write_text")
        f.write_text("v3", encoding="utf-8")
        r = self.store.restore("s1", self.ws)
        self.assertEqual(f.read_text(encoding="utf-8"), "v1")


if __name__ == "__main__":
    unittest.main()
