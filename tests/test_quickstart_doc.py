# -*- coding: utf-8 -*-
"""快速手册的防遗忘测试：文档里写的入口/URL 必须真实存在且一致。

之前踩过"文档写着 install.py，但脚本改名/删了"的坑——读到文档的人会直接卡住。
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class QuickstartDocTests(unittest.TestCase):
    def setUp(self):
        self.doc = (ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
        self.cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    def test_entry_scripts_exist(self):
        for rel in ("install.py", "start.py", "scripts/login.py", "config.example.json"):
            self.assertTrue((ROOT / rel).exists(), f"手册提到的 {rel} 不存在")
            self.assertIn(rel, self.doc, f"手册应包含 {rel}")

    def test_ui_url_matches_config_port(self):
        port = int((self.cfg.get("server") or {}).get("port", 8765))
        self.assertIn(f"127.0.0.1:{port}/ui", self.doc,
                      "手册里的 UI 地址要和 config.json 的端口一致")

    def test_mentions_core_concepts(self):
        for word in ("Agent 执行", "工作目录", "权限", "步骤时间线", "待办"):
            self.assertIn(word, self.doc, f"一页手册应提到「{word}」")

    def test_install_script_installs_deps_and_browser(self):
        src = (ROOT / "install.py").read_text(encoding="utf-8")
        self.assertIn("requirements.txt", src)
        self.assertIn("playwright", src, "install.py 要装 Playwright 浏览器")


if __name__ == "__main__":
    unittest.main()
