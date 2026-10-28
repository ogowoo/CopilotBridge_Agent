# -*- coding: utf-8 -*-
"""自升级快照作用域命名 / 可列出的回归测试。

事故：`_dir()` 把"非字母数字开头"的名字加 `s` 前缀来防 `.`/`..`，连带把
`_self_<sid>` 改成 `s_self_<sid>`；而 CLI 与 `rollback_self_upgrade(use_last)`
按 `_self_` 前缀找 → 自升级快照**永远列不出来**（只有 --sid 能恢复）。
"""
import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gantry.snapshots import SnapshotStore  # noqa: E402


class SelfScopeNamingTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="gantry_snap_"))
        self.store = SnapshotStore(self.base)
        self.root = Path(tempfile.mkdtemp(prefix="gantry_root_"))
        self.sid = "task-abc"
        self.scope = f"_self_{self.sid}"

    def test_self_scope_dir_not_prefixed(self):
        f = self.root / "gantry" / "x.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("v1", encoding="utf-8")
        self.store.capture(self.scope, "gantry/x.py", f, "self_replace_text", root=self.root)
        d = self.store._dir(self.scope)
        self.assertEqual(d.name, self.scope, "`_self_` 不该被加 s 前缀")
        self.assertTrue(d.is_dir())

    def test_scopes_prefix_finds_self_scope(self):
        f = self.root / "a.py"
        f.write_text("v1", encoding="utf-8")
        self.store.capture(self.scope, "a.py", f, "self_replace_text", root=self.root)
        self.assertIn(self.scope, self.store.scopes("_self_"))

    def test_dot_names_still_guarded(self):
        """危险名（. / .. ）仍要加前缀，不能指向 snapshots 根或其父。"""
        for bad in (".", ".."):
            d = self.store._dir(bad).resolve()
            self.assertEqual(d.parent, self.base.resolve(), f"{bad} 必须落在 base 之下")
            self.assertNotEqual(d, self.base.resolve())

    def test_legacy_prefixed_scope_still_listed_by_bridge(self):
        """旧消毒产物 `s_self_<sid>` 也要能被 use_last 认出来。"""
        src = (ROOT / "gantry" / "bridge.py").read_text(encoding="utf-8")
        self.assertIn('SNAPSHOTS.scopes("_self_")', src)
        self.assertIn('SNAPSHOTS.scopes("s_self_")', src)

    def test_cli_lists_self_scope(self):
        f = self.root / "b.py"
        f.write_text("v1", encoding="utf-8")
        self.store.capture(self.scope, "b.py", f, "self_replace_text", root=self.root)
        # CLI 用的是 data/snapshots，这里把临时 base 指过去不方便，
        # 改为直接验证 CLI 源码里的前缀判断（两套都认）
        src = (ROOT / "scripts" / "rollback.py").read_text(encoding="utf-8")
        self.assertIn("s\" + SELF_PREFIX", src.replace("'", '"'),
                      "CLI 要同时认 `_self_` 与旧的 `s_self_`")


if __name__ == "__main__":
    unittest.main()
