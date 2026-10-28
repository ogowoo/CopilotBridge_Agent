# -*- coding: utf-8 -*-
"""前端 id 引用完整性（ui_refs）：JS 里引用的元素必须在 index.html 或动态生成里存在。

为什么需要：自升级代理曾加了读 `setTeamScopeHeartbeat` 的逻辑，却**忘了把复选框加进
设置页**就宣布完成（task-mvs4un2x）。py_compile + unittest 都覆盖不了"元素存不存在"，
这条静态检查就是专门拦这个的。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gantry.ui_refs import ALLOWED_MISSING, check_ui_id_refs  # noqa: E402


class CheckFunctionTests(unittest.TestCase):
    def _mk(self, html: str, js: str) -> Path:
        root = Path(tempfile.mkdtemp(prefix="gantry_ui_"))
        (root / "webui").mkdir()
        (root / "webui" / "index.html").write_text(html, encoding="utf-8")
        (root / "webui" / "app.js").write_text(js, encoding="utf-8")
        return root

    def test_missing_id_detected(self):
        root = self._mk('<div id="a"></div>',
                        "document.getElementById('a');document.getElementById('ghost');")
        r = check_ui_id_refs(root)
        ids = [m["id"] for m in r["missing"]]
        self.assertEqual(ids, ["ghost"])

    def test_dynamically_created_id_counts(self):
        root = self._mk('<div></div>',
                        "el.innerHTML=`<input id='dyn'>`;el.id='mine';"
                        "document.getElementById('dyn');document.getElementById('mine');")
        r = check_ui_id_refs(root)
        self.assertEqual(r["missing"], [])

    def test_missing_files_skips(self):
        root = Path(tempfile.mkdtemp(prefix="gantry_ui_empty_"))
        r = check_ui_id_refs(root)
        self.assertTrue(r["skipped"])

    def test_line_numbers_reported(self):
        root = self._mk('<div></div>', "\n\ndocument.getElementById('ghost');")
        r = check_ui_id_refs(root)
        self.assertEqual(r["missing"][0]["lines"], [3])


class RealCodeTests(unittest.TestCase):
    def test_current_webui_has_no_missing_refs(self):
        """现网 webui 必须零缺漏（`sid` 是有意防御的兜底，已白名单）。"""
        r = check_ui_id_refs(ROOT)
        self.assertFalse(r["skipped"])
        self.assertGreater(r["defined"], 100)
        self.assertEqual(r["missing"], [],
                         f"新缺漏：{r['missing']}（要么补上元素，要么说明理由再加白名单）")

    def test_allowlist_is_tiny_and_documented(self):
        """白名单不许膨胀（每个都得有理由，见 ui_refs.py 的注释）。"""
        self.assertLessEqual(len(ALLOWED_MISSING), 3)

    def test_self_validate_includes_ui_refs(self):
        src = (ROOT / "gantry" / "tools_self.py").read_text(encoding="utf-8")
        self.assertIn("check_ui_id_refs", src)
        self.assertIn('"ui_refs"', src)
        self.assertIn("引用了不存在的元素 id", src)


if __name__ == "__main__":
    unittest.main()
