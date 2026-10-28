# -*- coding: utf-8 -*-
"""任务完成后的「改了哪些文件」卡片：快照原像 vs 当前文件的行级统计。

用户："像 DSH 那样，任务完成后要显示交付的结果/文件。"

DSH 的卡片是「Edited N files +232 −30」+ 逐文件 +A −D + 「All N files」展开。
本项目不依赖 git，但快照在每次写操作前保存了原像，正好等价于"任务开始前"的内容，
所以可以用 difflib 算真实的行级增删，而不是只列出路径。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from gantry.ui_src import ui_css, ui_js

APP = ui_js()
HTML = (BASE / "webui" / "index.html").read_text(encoding="utf-8")
CSS = ui_css()
I18N = (BASE / "webui" / "i18n.js").read_text(encoding="utf-8")
API = ((BASE / "gantry" / "api.py").read_text(encoding="utf-8") + "".join((BASE / "gantry" / n).read_text(encoding="utf-8") for n in ("routes_core.py", "routes_config.py", "routes_agents.py", "routes_sessions.py")))

from gantry.snapshots import SnapshotStore


class ChangesStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_chg_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.snaps = SnapshotStore(self.tmp / "snaps")
        self.sid = "task-1"

    def _write(self, rel, text, encoding="utf-8"):
        p = self.ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding=encoding)
        return p

    def _capture(self, rel, tool="write_text"):
        target = self.ws / rel
        self.snaps.capture(self.sid, rel, target, tool=tool, root=self.ws)

    def test_modified_added_deleted_and_binary(self):
        # 1) 修改：捕获原像后改写
        self._write("a.py", "line1\nline2\n")
        self._capture("a.py")
        self._write("a.py", "line1\nline2 changed\nline3\n")
        # 2) 新增：捕获时不存在
        self._capture("new.txt")
        self._write("new.txt", "hello\nworld\n")
        # 3) 删除：捕获后删掉
        self._write("gone.txt", "x\ny\n")
        self._capture("gone.txt")
        (self.ws / "gone.txt").unlink()
        # 4) 二进制：行数不可算，只列路径
        b = self.ws / "book.xlsx"
        b.write_bytes(b"PK\x03\x04\x00\x00")
        self._capture("book.xlsx")
        b.write_bytes(b"PK\x03\x04\x00\x00\x01")

        out = self.snaps.changes(self.sid, default_root=self.ws)
        by = {f["path"]: f for f in out["files"]}
        self.assertEqual(by["a.py"]["status"], "modified")
        self.assertEqual((by["a.py"]["additions"], by["a.py"]["deletions"]), (2, 1))
        self.assertEqual(by["new.txt"]["status"], "added")
        self.assertEqual((by["new.txt"]["additions"], by["new.txt"]["deletions"]), (2, 0))
        self.assertEqual(by["gone.txt"]["status"], "deleted")
        self.assertEqual((by["gone.txt"]["additions"], by["gone.txt"]["deletions"]), (0, 2))
        self.assertTrue(by["book.xlsx"]["binary"])
        self.assertEqual(by["book.xlsx"]["additions"], 0)
        self.assertEqual(out["count"], 4)
        self.assertEqual((out["additions"], out["deletions"]), (4, 3))

    def test_unchanged_file_is_omitted(self):
        self._write("same.txt", "a\nb\n")
        self._capture("same.txt")
        self._write("same.txt", "a\nb\n")          # 写了但内容没变
        out = self.snaps.changes(self.sid, default_root=self.ws)
        self.assertEqual(out["count"], 0)
        self.assertEqual(out["files"], [])

    def test_sorted_by_change_size(self):
        for name, lines in (("small.txt", 1), ("big.txt", 5)):
            self._capture(name)
            self._write(name, "\n".join(str(i) for i in range(lines)) + "\n")
        out = self.snaps.changes(self.sid, default_root=self.ws)
        self.assertEqual(out["files"][0]["path"], "big.txt", "改动大的排前面（对齐 DSH）")


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_diff_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.snaps = SnapshotStore(self.tmp / "snaps")
        self.sid = "task-2"
        p = self.ws / "a.txt"
        p.write_text("one\ntwo\n")
        self.snaps.capture(self.sid, "a.txt", p, tool="write_text", root=self.ws)
        p.write_text("one\nTWO\nthree\n")

    def test_unified_diff(self):
        out = self.snaps.diff(self.sid, "a.txt", default_root=self.ws)
        self.assertIsNotNone(out)
        self.assertEqual(out["status"], "modified")
        self.assertIn("-two", out["diff"])
        self.assertIn("+TWO", out["diff"])
        self.assertIn("+three", out["diff"])

    def test_unknown_path_is_refused(self):
        """接口不能变成任意文件读取：只允许快照里记录过的路径。"""
        self.assertIsNone(self.snaps.diff(self.sid, "../secret.txt", default_root=self.ws))
        self.assertIsNone(self.snaps.diff(self.sid, "nope.txt", default_root=self.ws))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_chg_api_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        from gantry import bridge as b
        from gantry.session_store import SessionStore
        from gantry.snapshots import SnapshotStore

        self.b = b
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        b.AGENT_SESSIONS.create("s1", "任务", str(self.ws), "copilot-auto")
        p = self.ws / "a.txt"
        p.write_text("one\n")
        b.SNAPSHOTS.capture("s1", "a.txt", p, tool="write_text", root=self.ws)
        p.write_text("one\ntwo\n")
        from fastapi.testclient import TestClient
        from gantry import api as api_mod
        self.client = TestClient(api_mod.app)

    def tearDown(self):
        (self.b.AGENT_SESSIONS, self.b.SNAPSHOTS, root) = self.saved
        self.b.AGENT_TOOLS.set_root(root, False)

    def test_changes_endpoint(self):
        d = self.client.get("/v1/agent/sessions/s1/changes").json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["additions"], 1)
        self.assertEqual(d["files"][0]["path"], "a.txt")
        self.assertIn("deliverables", d)

    def test_diff_endpoint(self):
        d = self.client.get("/v1/agent/sessions/s1/changes/diff", params={"path": "a.txt"}).json()
        self.assertIn("+two", d["diff"])
        r = self.client.get("/v1/agent/sessions/s1/changes/diff", params={"path": "x.txt"})
        self.assertEqual(r.status_code, 404)

    def test_missing_session_404(self):
        self.assertEqual(self.client.get("/v1/agent/sessions/nope/changes").status_code, 404)


class FrontendCardTests(unittest.TestCase):
    """前端卡片：DSH 式的标题/统计/逐文件/展开/看 diff。"""

    def test_card_renders_header_and_rows(self):
        self.assertIn("function renderChangesCard", APP)
        self.assertIn("chg-head", APP)
        self.assertIn("chg-row", APP)
        self.assertIn("data-diff", APP, "点文件要能看 diff")

    def test_card_shows_sums_and_expander(self):
        i = APP.index("function renderChangesCard")
        seg = APP[i:i + 3200]
        self.assertIn("d.additions", seg)
        self.assertIn("d.deletions", seg)
        self.assertIn("changesExpanded?files:files.slice(0,6)", seg.replace(" ", ""))
        self.assertIn("全部 {n} 个文件", seg)

    def test_card_opens_in_shared_viewer(self):
        self.assertIn("openChangeDiff", APP)
        i = APP.index("async function openChangeDiff")
        seg = APP[i:i + 1200]
        self.assertIn("openMask('liveMask')", seg)
        self.assertIn("/changes/diff?path=", seg)
        self.assertIn("d-add", seg, "新增行要着色")
        self.assertIn("d-del", seg, "删除行要着色")

    def test_card_triggers_on_completion_only(self):
        self.assertIn("maybeLoadChanges(r.session_state)", APP)
        i = APP.index("function maybeLoadChanges")
        seg = APP[i:i + 700]
        self.assertIn("CHANGE_DONE", seg, "运行中不拉改动，只在结束时拉")
        self.assertIn("!CHANGE_DONE.includes(st)", seg.replace(" ", ""))

    def test_card_is_cleared_on_session_switch(self):
        self.assertIn("clearChanges();", APP)

    def test_i18n_keys_present(self):
        for key in ("本次改动 {n} 个文件", "全部 {n} 个文件", "交付物",
                    "查看差异", "二进制文件，无文本差异", "没有差异"):
            self.assertIn('"' + key + '"', I18N, f"缺英文翻译: {key}")

    def test_css_present(self):
        for cls in (".chgcard", ".chg-row", ".chg-ico.added", ".dline.d-add", ".dline.d-del"):
            self.assertIn(cls, CSS)

    def test_api_routes_registered(self):
        self.assertIn('/v1/agent/sessions/{sid}/changes"', API)
        self.assertIn('/changes/diff', API)


if __name__ == "__main__":
    unittest.main()
