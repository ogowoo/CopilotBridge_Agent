# -*- coding: utf-8 -*-
"""审批存储：allowed-once（逐参数）与本会话级授权（approval_scope=session）。

背景：用户实测 pwsh 被拦后模型改了命令措辞，args_hash 变了 → 又弹授权。
本会话级授权让"一次授权、本会话内该工具都放行"，比 never 安全（只影响单会话单工具），
比逐参数省事。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.session_store import SessionStore


class ApprovalScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_appr_"))
        self.st = SessionStore(self.tmp / "a.db")
        self.st.create("s1", "t", str(self.tmp), "m")

    def test_fail_closed_without_grant(self):
        self.assertFalse(self.st.take_approval("s1", "pwsh", "h1"))

    def test_allowed_once_consumes(self):
        self.st.grant_approval("s1", "pwsh", "h1")
        self.assertTrue(self.st.take_approval("s1", "pwsh", "h1"))
        self.assertFalse(self.st.take_approval("s1", "pwsh", "h1"),
                         "allowed-once 必须用后即焚")

    def test_session_grant_allows_any_args(self):
        self.st.grant_session_approval("s1", "pwsh")
        self.assertTrue(self.st.take_approval("s1", "pwsh", "h1"))
        self.assertTrue(self.st.take_approval("s1", "pwsh", "h2"),
                        "会话级授权应覆盖不同参数")
        self.assertTrue(self.st.take_approval("s1", "pwsh", "h3"),
                        "会话级授权不消费，持续有效")

    def test_session_grant_is_tool_scoped(self):
        self.st.grant_session_approval("s1", "pwsh")
        self.assertFalse(self.st.take_approval("s1", "delete_path", "h1"),
                         "会话级授权只能放行被授权的那个工具")

    def test_session_grant_is_session_scoped(self):
        self.st.create("s2", "t", str(self.tmp), "m")
        self.st.grant_session_approval("s1", "pwsh")
        self.assertFalse(self.st.take_approval("s2", "pwsh", "h1"),
                         "会话级授权不能跨会话")


if __name__ == "__main__":
    unittest.main()
