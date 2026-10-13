# -*- coding: utf-8 -*-
"""工作区快照与回滚的回归测试。

运行: python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.snapshots import SnapshotStore
from cbp.tools import ToolRegistry


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_snap_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.store = SnapshotStore(self.tmp / "snaps")

    def test_modified_file_restored(self):
        f = self.ws / "a.py"
        f.write_text("原始内容\n", encoding="utf-8")
        self.store.capture("s1", "a.py", f, "write_text")
        f.write_text("被改坏了\n", encoding="utf-8")
        r = self.store.restore("s1", self.ws)
        self.assertEqual((r["restored"], r["deleted"], r["failed"]), (1, 0, 0))
        self.assertEqual(f.read_text(encoding="utf-8"), "原始内容\n")

    def test_new_file_deleted_on_restore(self):
        f = self.ws / "new.py"
        self.store.capture("s1", "new.py", f, "write_text")   # 当时不存在
        f.write_text("新建\n", encoding="utf-8")
        r = self.store.restore("s1", self.ws)
        self.assertEqual((r["restored"], r["deleted"]), (0, 1))
        self.assertFalse(f.exists())

    def test_first_capture_wins(self):
        """同一路径多次改动只保留最早原像 —— 回滚恢复到任务开始前。"""
        f = self.ws / "a.txt"
        f.write_text("v1", encoding="utf-8")
        self.store.capture("s1", "a.txt", f, "write_text")
        f.write_text("v2", encoding="utf-8")
        self.store.capture("s1", "a.txt", f, "write_text")   # 不应覆盖 v1
        f.write_text("v3", encoding="utf-8")
        self.store.restore("s1", self.ws)
        self.assertEqual(f.read_text(encoding="utf-8"), "v1")

    def test_deleted_directory_restored(self):
        d = self.ws / "pkg"
        d.mkdir()
        (d / "m.py").write_text("模块\n", encoding="utf-8")
        self.store.capture("s1", "pkg", d, "delete_path")
        import shutil
        shutil.rmtree(d)
        r = self.store.restore("s1", self.ws)
        self.assertEqual(r["restored"], 1)
        self.assertEqual((d / "m.py").read_text(encoding="utf-8"), "模块\n")

    def test_binary_file_restored(self):
        f = self.ws / "book.xlsx"
        f.write_bytes(bytes(range(256)) * 8)
        self.store.capture("s1", "book.xlsx", f, "write_excel")
        f.write_bytes(b"corrupted")
        self.store.restore("s1", self.ws)
        self.assertEqual(f.read_bytes(), bytes(range(256)) * 8)

    def test_oversize_skipped_not_crashing(self):
        f = self.ws / "big.bin"
        f.write_bytes(b"x" * 16)
        store = SnapshotStore(self.tmp / "snaps2")
        store.MAX_FILE_BYTES = 8
        store.capture("s1", "big.bin", f, "write_text")
        s = store.summary("s1")
        self.assertEqual(len(s["files"]), 0)
        self.assertTrue(s["skipped"])

    def test_restore_refuses_path_outside_root(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("外部", encoding="utf-8")
        self.store.capture("s1", "../outside.txt", outside, "write_text")
        outside.write_text("被改", encoding="utf-8")
        r = self.store.restore("s1", self.ws)
        self.assertEqual(r["failed"], 1)
        self.assertEqual(outside.read_text(encoding="utf-8"), "被改")

    def test_empty_manifest_message(self):
        r = self.store.restore("nope", self.ws)
        self.assertEqual(r["restored"], 0)
        self.assertIn("没有该 Session 的快照", r["message"])


class ToolSnapshotHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_snap_hook_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.store = SnapshotStore(self.tmp / "snaps")
        self.tools = ToolRegistry(self.ws, {})

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_write_append_replace_captured_once(self):
        self.tools.set_snapshot_hook(
            lambda tool, rel, target: self.store.capture("s1", rel, target, tool))
        self._run(self.tools.write_text("x.py", "第一版\n"))
        self._run(self.tools.append_text("x.py", "第二版\n"))
        self._run(self.tools.replace_text("x.py", "第一版", "改过"))
        s = self.store.summary("s1")
        self.assertEqual(len(s["files"]), 1)          # 只记一条
        self.assertEqual(s["files"][0]["kind"], "absent")  # 首次写入时确实不存在
        self.store.restore("s1", self.ws)
        self.assertFalse((self.ws / "x.py").exists())

    def test_move_captures_both_ends(self):
        src = self.ws / "old.txt"
        src.write_text("内容", encoding="utf-8")
        self.tools.set_snapshot_hook(
            lambda tool, rel, target: self.store.capture("s1", rel, target, tool))
        self._run(self.tools.move_path("old.txt", "new.txt"))
        s = self.store.summary("s1")
        kinds = {f["path"]: f["kind"] for f in s["files"]}
        self.assertEqual(kinds.get("old.txt"), "file")     # 源：还原回来
        self.assertEqual(kinds.get("new.txt"), "absent")   # 目标：回滚时删除
        self.store.restore("s1", self.ws)
        self.assertTrue(src.exists())
        self.assertFalse((self.ws / "new.txt").exists())

    def test_hook_failure_does_not_break_write(self):
        def boom(tool, rel, target):
            raise RuntimeError("快照炸了")
        self.tools.set_snapshot_hook(boom)
        r = self._run(self.tools.write_text("ok.txt", "内容"))
        self.assertEqual(r["written_chars"], 2)
        self.assertTrue((self.ws / "ok.txt").is_file())

    def test_no_hook_no_capture(self):
        self.tools.set_snapshot_hook(None)
        self._run(self.tools.write_text("y.txt", "内容"))
        self.assertFalse(self.store.exists("s1"))


if __name__ == "__main__":
    unittest.main()
