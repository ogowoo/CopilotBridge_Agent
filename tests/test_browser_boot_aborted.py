# -*- coding: utf-8 -*-
"""回归测试：首次启动的 `goto` 必须容忍 ERR_ABORTED。

事实：企业租户 SSO 会在 domcontentloaded 之前就重定向走，`page.goto` 抛
`net::ERR_ABORTED` —— 这不是失败。复用窗口/新窗口两处 goto 都有容错，
只有首次启动（browser.py:102 附近）漏了 → 整个 boot 被判 failed，
页面停在 about:blank（用户实录）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class ErrAbortedTests(unittest.TestCase):
    def test_first_goto_tolerates_err_aborted(self):
        src = (ROOT / "gantry" / "browser.py").read_text(encoding="utf-8")
        # 定位首次启动的 goto（"启动浏览器"主路径，set_shared_page 之后那一句）
        i = src.index('b.set_shared_page(b.ctx.pages[0] if b.ctx.pages else await b.ctx.new_page())')
        body = src[i:i + 900]
        self.assertIn("try:", body, "首次 goto 必须有 try/except")
        self.assertIn("ERR_ABORTED", body, "ERR_ABORTED 必须被容错（重定向中止是常态）")
        self.assertIn("wait_until=\"domcontentloaded\"", body)

    def test_all_goto_sites_tolerate_aborted(self):
        """三处 goto（复用窗口/首次启动/新窗口）都应容错，不能再漏一处。"""
        src = (ROOT / "gantry" / "browser.py").read_text(encoding="utf-8")
        sites = [m.start() for m in
                 __import__("re").finditer(r'\.goto\(b\.URL, wait_until="domcontentloaded"', src)]
        self.assertEqual(len(sites), 3, f"goto 站点数变了：{len(sites)}")
        for pos in sites:
            window = src[max(0, pos - 500):pos + 500]
            ok = "try:" in window and ("except Exception" in window or "ERR_ABORTED" in window)
            self.assertTrue(ok, f"goto@{pos} 缺少容错")


if __name__ == "__main__":
    unittest.main()
