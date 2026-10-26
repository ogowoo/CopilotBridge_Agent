# -*- coding: utf-8 -*-
"""源码里不许出现**别人机器上的绝对路径**。

真事：`scripts/i18n_static_scan.py` 把 `ROOT` 写死成 `C:\\Tools\\Test4All\\...`,
本机跑全套测试时「静态文案都有英文键」这个不变量直接 FileNotFoundError 失败——
也就是说这个不变量**只在作者机器上有效**，别人同步下来必然挂。

这类泄漏很隐蔽（自己机器上永远跑得过），所以加静态护栏：
- 只扫 .py/.json，跳过 tests（test_*.py 里的临时路径由 tempfile 生成，不写死）；
- 允许的例外：盘符通配（`C:` 后接反斜杠的路径）一律报错；`Path(__file__)` 这类
  相对推导不算问题。
"""
import re
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
# 形如 C:\xxx  /  D:\xxx  / C:/xxx 的绝对路径（排除注释里举例用的 "C:\\Users\\..."）
ABS_WIN = re.compile(r"""[A-Za-z]:[\\/](?!/)[^\s"'<>|)]{2,}""")
# 自己机器/作者机器的常见根目录特征，命中即视为泄漏
SUSPECT = ("Tools", "Test4All", "CopilotBridge_Agent\\", "CopilotBridge_PythonOnly")
SCAN_DIRS = ("gantry", "scripts", "webui")
EXTS = (".py", ".json", ".js", ".html")


def _files():
    for d in SCAN_DIRS:
        root = BASE / d
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in EXTS:
                if "__pycache__" in p.parts:
                    continue
                yield p


class NoForeignAbsolutePathTests(unittest.TestCase):
    def test_no_hardcoded_machine_paths(self):
        hits = []
        for p in _files():
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in ABS_WIN.finditer(text):
                frag = m.group(0)
                if any(s in frag for s in SUSPECT):
                    line = text[:m.start()].count("\n") + 1
                    hits.append(f"{p.relative_to(BASE)}:{line}: {frag[:60]}")
        self.assertEqual(hits, [],
                         "源码里写死了别的位置/机器路径（换机器会挂）:\n" + "\n".join(hits[:8]))

    def test_scanner_derives_root_from_file(self):
        p = BASE / "scripts" / "i18n_static_scan.py"
        if not p.is_file():
            self.skipTest("没有该脚本")
        src = p.read_text(encoding="utf-8")
        self.assertIn("Path(__file__)", src,
                      "安装目录必须从 __file__ 推导，不能写死")


if __name__ == "__main__":
    unittest.main()
