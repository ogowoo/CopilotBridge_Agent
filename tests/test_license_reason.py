# -*- coding: utf-8 -*-
"""Copilot 许可证提示识别：不能当限流重试，也不能当正常回答。"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from cbp.bridge import license_reason  # noqa: E402


class LicenseReasonTests(unittest.TestCase):
    def test_english(self):
        self.assertTrue(license_reason(
            "You don't have a license to use Copilot. Contact your admin."))
        self.assertTrue(license_reason(
            "Copilot isn't available because your license does not include it."))

    def test_chinese(self):
        self.assertTrue(license_reason("你没有使用 Copilot 的许可证，请联系管理员。"))
        self.assertTrue(license_reason("您的授權已過期，無法使用 Copilot。"))

    def test_japanese(self):
        self.assertTrue(license_reason("Copilot を使用するためのライセンスがありません。"))

    def test_negative(self):
        self.assertEqual(license_reason("这是一段正常回答。"), "")
        self.assertEqual(license_reason(""), "")


if __name__ == "__main__":
    unittest.main()
