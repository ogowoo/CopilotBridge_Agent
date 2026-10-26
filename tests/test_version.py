# -*- coding: utf-8 -*-
"""版本相关的回归测试：保证"单一来源"不被破坏。

跨机器协作最容易出的问题：
- 有人从别的机器合过来，`pyproject.toml` 和 `cbp/version.py` 不一致；
- 有人又在别处硬编码了版本号；
- CHANGELOG 里没有当前版本的段落（发了版却没记录）。
"""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cbp.version import VERSION, base_version, next_version, parse, tag_name  # noqa: E402


class VersionFormatTests(unittest.TestCase):
    def test_version_parses_as_semver(self):
        p = parse()
        self.assertEqual(p["version"], VERSION)
        self.assertGreaterEqual(p["major"], 1)

    def test_tag_name_prefix(self):
        self.assertEqual(tag_name("1.2.3"), "v1.2.3")
        self.assertTrue(tag_name().startswith("v"))

    def test_next_versions(self):
        self.assertEqual(next_version("patch", "1.2.3"), "1.2.4-preview.1")
        self.assertEqual(next_version("minor", "1.2.3"), "1.3.0-preview.1")
        self.assertEqual(next_version("major", "1.2.3"), "2.0.0-preview.1")
        self.assertEqual(next_version("stable", "1.2.3-preview.4"), "1.2.3")
        self.assertEqual(next_version("preview", "1.2.3-preview.4"), "1.2.3-preview.5")
        self.assertEqual(next_version("beta", "1.2.3-preview.4"), "1.2.3-beta.1")

    def test_bad_version_rejected(self):
        for bad in ("1.2", "v1.2.3", "1.2.3.4", "x.y.z"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse(bad)


class SingleSourceTests(unittest.TestCase):
    def test_pyproject_matches_version_module(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        i = text.index('version = "')
        pyver = text[i + len('version = "'):].split('"', 1)[0]
        self.assertEqual(pyver, VERSION, "pyproject.toml 与 cbp/version.py 不一致")

    def test_no_hardcoded_version_elsewhere(self):
        """除 version.py / CHANGELOG / docs 外，不允许出现当前版本字面量。"""
        allow = {"version.py", "CHANGELOG.md", "VERSIONING.md", "test_version.py",
                 "bump_version.py", "README.md", "pyproject.toml"}
        offenders = []
        for pat in ("*.py", "*.js", "*.html", "*.json"):
            for f in list(ROOT.glob(pat)) + list((ROOT / "cbp").glob(pat)) + list((ROOT / "webui").glob(pat)):
                if f.name in allow:
                    continue
                try:
                    if VERSION in f.read_text(encoding="utf-8"):
                        offenders.append(str(f.relative_to(ROOT)))
                except (OSError, UnicodeDecodeError):
                    continue
        self.assertEqual(offenders, [], f"这些文件硬编码了版本号：{offenders}")

    def test_api_reports_version(self):
        src = (ROOT / "cbp" / "api.py").read_text(encoding="utf-8")
        self.assertIn("from .version import VERSION", src)
        core = (ROOT / "cbp" / "routes_core.py").read_text(encoding="utf-8")
        self.assertIn('"version": _APP_VERSION', core, "GET / 应该返回版本号")

    def test_changelog_has_current_version(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertTrue(text.lstrip().startswith("# 更新日志"))
        self.assertIn(f"## [{VERSION}]", text,
                      f"CHANGELOG 缺少当前版本 {VERSION} 的小节（发布前用 bump_version.py 生成）")
        self.assertIn("## [Unreleased]", text)

    def test_base_version(self):
        self.assertEqual(base_version("10.3.0-preview.3"), "10.3.0")


if __name__ == "__main__":
    unittest.main()
