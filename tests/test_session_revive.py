# -*- coding: utf-8 -*-
"""体验细节：cancelled 会话发消息应自动恢复；凭据失效的错误要能看懂。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

API = ((ROOT / "gantry" / "api.py").read_text(encoding="utf-8") + "".join((ROOT / "gantry" / n).read_text(encoding="utf-8") for n in ("routes_core.py", "routes_config.py", "routes_agents.py", "routes_sessions.py")))
PROV = (ROOT / "gantry" / "providers.py").read_text(encoding="utf-8")


class CancelledReviveTests(unittest.TestCase):
    """用户截图：任务取消后输入 hi，只得到「Session 状态为 cancelled，不能继续」。"""

    def test_messages_endpoint_revives_cancelled(self):
        i = API.find("async def continue_session_message(")
        self.assertGreater(i, 0)
        seg = API[i:i + 1400]
        self.assertIn('state.get("state") == "cancelled"', seg, "取消态要能被消息唤醒")
        self.assertIn('state="ready"', seg)
        self.assertIn("cancel_requested=False", seg)
        self.assertNotIn('in {"cancelled", "archived"}', seg,
                         "不要再把 cancelled 和 archived 一起 409")

    def test_archived_still_refused(self):
        i = API.find("async def continue_session_message(")
        seg = API[i:i + 1400]
        self.assertIn('state.get("state") == "archived"', seg)
        self.assertIn("409", seg, "归档是明确意图，仍应拒绝")


class CredentialHintTests(unittest.TestCase):
    def test_invalid_token_gets_actionable_hint(self):
        from gantry.providers import _credential_hint
        hint = _credential_hint('{"code":40003,"msg":"INVALID_TOKEN","data":null}')
        self.assertIn("凭据", hint)
        self.assertIn("重新", hint)

    def test_other_errors_untouched(self):
        from gantry.providers import _credential_hint
        self.assertEqual(_credential_hint('{"detail":"bad request"}'), "")

    def test_hint_wired_into_http_errors(self):
        self.assertIn("_credential_hint(body)", PROV,
                      "HTTP 错误里要带上凭据提示")


if __name__ == "__main__":
    unittest.main()
