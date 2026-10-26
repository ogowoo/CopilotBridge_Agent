# -*- coding: utf-8 -*-
"""回归：删除弹窗的「重试远程并强制清理本地记录」不能没反应。

事故：`removeSession()` 本地删除成功后把 `current` 清空，重试按钮 `forceDeleteLocal()`
第一行 `if(!current)return;` 直接静默返回；即使带上了 sid，后端也会因为
「Session 不存在」返回 404（任务已经删了）。用户看到的就是"按钮不超作用"。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
ROUTES = (ROOT / "gantry" / "routes_sessions.py").read_text(encoding="utf-8")


class ForceDeleteRetryTests(unittest.TestCase):
    def test_button_does_not_depend_on_current(self):
        i = APP.index("async function forceDeleteLocal(")
        seg = APP[i:i + 900]
        self.assertIn("lastDeleteResult", seg, "要用弹窗记住的 sid")
        self.assertNotIn("if(!current)return;", seg, "不能因为 current 为空就静默返回")

    def test_dialog_remembers_sid_and_urls(self):
        i = APP.index("function showDeleteResult(")
        seg = APP[i:i + 600]
        self.assertIn("_sid", seg)
        self.assertIn("_urls", seg)
        self.assertIn("showDeleteResult(d,sid)", APP, "调用处要传 sid")

    def test_backend_accepts_urls_without_local_state(self):
        i = ROUTES.index("async def delete_session(")
        seg = ROUTES[i:i + 2600]
        self.assertIn("urls: list[str] | None = Query", seg, "要能接收 urls")
        self.assertIn("if delete_remote and urls:", seg, "本地不存在时仍可重试远端")
        self.assertIn("本地记录已不存在", seg)


if __name__ == "__main__":
    unittest.main()
