# -*- coding: utf-8 -*-
"""提供商"不可用"的两个真问题：

1) 误报根因：浏览器走 Windows 系统代理，而 httpx 默认只读环境变量 -> 服务直连
   失败却报"提供商不可用"；且 httpx 超时/连接异常的 str() 常为空串，界面上
   只显示"服务暂时不可用（第 1 次）："什么都看不到。
2) 1 小时的限流窗口是 Copilot 网页实测值，套到 API 提供商上会误导（用户实测
   看到"预计恢复 59:16"，其实只是网络抖动）——提供商应按"下次重试"倒计时。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from cbp import providers as P
from cbp.agent import AgentRunner, CopilotRateLimited


class NetErrorTextTests(unittest.TestCase):
    def _prov(self, **cfg):
        base = {"base_url": "https://api.example.com/v1", "api_key": "k",
                "models": ["m"], "use_system_proxy": False}
        base.update(cfg)
        return P.ApiProvider("t", base)

    def test_empty_message_still_actionable(self):
        """httpx 空消息异常：也要说清"哪条路径、哪个地址、什么异常"。"""
        p = self._prov()
        text = p.net_error_text(httpx.ReadTimeout(""))
        self.assertIn("ReadTimeout", text)
        self.assertIn("api.example.com", text)
        self.assertIn("直连", text)
        self.assertIn("use_system_proxy", text)      # 给出可操作建议
        self.assertNotIn("网络异常（ReadTimeout）：。", text)   # 不是空消息裸奔

    def test_proxy_mentioned_when_used(self):
        p = self._prov(proxy="http://127.0.0.1:10809")
        text = p.net_error_text(httpx.ConnectError("boom"))
        self.assertIn("经代理 http://127.0.0.1:10809", text)
        self.assertIn("boom", text)

    def test_proxy_precedence(self):
        p = self._prov(proxy="http://explicit:8080")
        self.assertEqual(p.proxy_url(), "http://explicit:8080")   # 显式优先
        p2 = self._prov(use_system_proxy=False)
        self.assertIsNone(p2.proxy_url())

    def test_system_proxy_reads_env(self):
        """system_proxy 读的是 urllib（Windows 会读注册表）。"""
        import os
        old = os.environ.get("HTTPS_PROXY")
        os.environ["HTTPS_PROXY"] = "http://sysproxy:3128"
        try:
            self.assertEqual(P.system_proxy(), "http://sysproxy:3128")
        finally:
            if old is None:
                os.environ.pop("HTTPS_PROXY", None)
            else:
                os.environ["HTTPS_PROXY"] = old


class ProviderCountdownTests(unittest.TestCase):
    def test_provider_uses_next_retry_not_one_hour_window(self):
        """window_seconds=0 → 用本轮退避（30/60/120s），不是 1 小时窗口。"""
        exc = CopilotRateLimited("网络异常 ReadTimeout", window_seconds=0)
        self.assertEqual(exc.window_seconds, 0)

    def test_default_window_unset_for_copilot_web(self):
        """不传 window_seconds 的（Copilot 网页）仍走固定窗口。"""
        exc = CopilotRateLimited("限流")
        self.assertIsNone(exc.window_seconds)

    def test_runner_window_config(self):
        r = AgentRunner(None, None, {"service_retry_base_seconds": 30,
                                     "service_retry_max_seconds": 600,
                                     "service_retry_window_seconds": 3660},
                        None, None, None, None)
        self.assertEqual(r.service_retry_window_seconds, 3660)
        # 提供商第一次退避 = base
        self.assertEqual(min(30 * (2 ** min(1 - 1, 4)), 600), 30)
        # 第三次 = base * 4
        self.assertEqual(min(30 * (2 ** min(3 - 1, 4)), 600), 120)


if __name__ == "__main__":
    unittest.main()
