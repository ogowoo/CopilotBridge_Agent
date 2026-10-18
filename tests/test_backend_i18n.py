# -*- coding: utf-8 -*-
"""后端错误文案本地化：翻译表、模式匹配、未知原文回落、HTTP 头生效。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import i18n


class TranslateTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(i18n.translate("Session 不存在", "en"), "Session not found")

    def test_pattern_with_argument(self):
        out = i18n.translate("没有名为 mcp2 的插件", "en")
        self.assertEqual(out, "No plugin named mcp2")

    def test_generic_not_found_pattern(self):
        self.assertEqual(i18n.translate("工作区目录不存在", "en"), "工作区目录 not found")

    def test_unknown_message_stays(self):
        msg = "某个还没翻译的消息"
        self.assertEqual(i18n.translate(msg, "en"), msg)

    def test_non_english_langs_untouched(self):
        self.assertEqual(i18n.translate("Session 不存在", "zh"), "Session 不存在")
        self.assertEqual(i18n.translate("Session 不存在", "tw"), "Session 不存在")

    def test_empty(self):
        self.assertEqual(i18n.translate("", "en"), "")

    def test_untranslated_audit_helper(self):
        bad = i18n.untranslated(["Session 不存在", "某个还没翻译的消息"], "en")
        self.assertEqual(bad, ["某个还没翻译的消息"])


class HandlerTests(unittest.TestCase):
    def test_http_error_translated_by_header(self):
        from fastapi.testclient import TestClient

        from cbp import api as api_mod

        client = TestClient(api_mod.app)
        zh = client.get("/v1/agent/sessions/__nope__")
        self.assertEqual(zh.status_code, 404)
        self.assertIn("不存在", zh.json()["detail"])

        en = client.get("/v1/agent/sessions/__nope__", headers={"X-CBP-Lang": "en"})
        self.assertEqual(en.status_code, 404)
        self.assertEqual(en.json()["detail"], "Session not found")


if __name__ == "__main__":
    unittest.main()
