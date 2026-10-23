# -*- coding: utf-8 -*-
"""护栏：Copilot 未就绪时要明确失败，不能一直挂在 sending。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class SendGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 页面自动化层已拆到 cbp/page.py、浏览器层拆到 cbp/browser.py（第三批解耦）
        cls.src = ((ROOT / "cbp" / "bridge.py").read_text(encoding="utf-8")
                   + (ROOT / "cbp" / "page.py").read_text(encoding="utf-8")
                   + (ROOT / "cbp" / "browser.py").read_text(encoding="utf-8")
                   + (ROOT / "cbp" / "agentrun.py").read_text(encoding="utf-8"))

    def test_send_raises_when_not_ready(self):
        i = self.src.find("async def send(prompt, make_new, model_id):")
        j = self.src.find("\nasync def ", i + 20)
        body = self.src[i:j]
        self.assertIn("wait_copilot_ready(90)", body)
        self.assertIn("Copilot 未就绪", body, "等不到就绪必须报明确错误")
        self.assertIn("raise CopilotSendFailed", body.replace("raise b.", "raise "))

    def test_page_url_accessed_safely(self):
        """恢复/续跑时浏览器可能还没起，page.url 直接访问会 NoneType 崩。"""
        self.assertIn("def page_url_safe()", self.src)
        i = self.src.find("async def turn(prompt,model,force_new=False):")
        j = self.src.find("async def ", i + 20)
        body = self.src[i:j]
        self.assertIn("ensure_browser_ready()", body, "turn 开始就该确保浏览器可用")
        self.assertIn("page_url_safe()", body)


if __name__ == "__main__":
    unittest.main()
