# -*- coding: utf-8 -*-
"""README 三语版的回归测试（issue #3）。

钉住：
1. 三份文件都在（README.md 默认英文、README.zh-CN.md、README.zh-TW.md）；
2. 每份都有语言切换行，且互指另外两份；
3. 主标题统一是 `# Gantry`；
4. 代码围栏成对（``` 数量为偶数，拼接时没断块）。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ReadmeLangTests(unittest.TestCase):
    FILES = {
        "README.md": "English",
        "README.zh-CN.md": "简体中文",
        "README.zh-TW.md": "繁體中文",
    }

    def test_all_three_exist_and_nonempty(self):
        for f in self.FILES:
            p = ROOT / f
            self.assertTrue(p.is_file(), f"缺少 {f}")
            self.assertGreater(len(p.read_text(encoding="utf-8")), 10000, f"{f} 内容过短")

    def test_switcher_links_each_other(self):
        for f in self.FILES:
            t = (ROOT / f).read_text(encoding="utf-8")
            switch = t.splitlines()[2]
            for other in self.FILES:
                if other == f:
                    continue
                self.assertIn(f"({other})", switch, f"{f} 的切换行缺指向 {other} 的链接")

    def test_title_unified(self):
        for f in self.FILES:
            first = (ROOT / f).read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(first, "# Gantry", f"{f} 首行应为 '# Gantry'，实为 {first!r}")

    def test_code_fences_balanced(self):
        for f in self.FILES:
            t = (ROOT / f).read_text(encoding="utf-8")
            self.assertEqual(t.count("```") % 2, 0, f"{f} 的 ``` 围栏不成对")

    def test_english_is_default(self):
        """默认 README.md 应是英文版：前 30 行里中文行不应超过少数示例。"""
        head = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[:30]
        cjk = [ln for ln in head if re.search(r"[\u4e00-\u9fff]", ln)]
        # 切换行里的「简体中文/繁體中文」、快速手册图卡的 [简体]/[繁體] 链接不算
        cjk = [ln for ln in cjk if not ln.startswith("> **Language**")
               and not ln.startswith("> or just save a card image")]
        self.assertEqual(cjk, [], f"默认版前 30 行应全英文，仍有中文：{cjk[:3]}")


if __name__ == "__main__":
    unittest.main()
