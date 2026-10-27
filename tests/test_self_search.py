# -*- coding: utf-8 -*-
"""自升级搜索工具的测试：模糊定位现在能走通了。"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gantry.api  # noqa: F401,E402  (先导入 api 规避循环导入)
from gantry.bridge import AGENT_TOOLS  # noqa: E402


class SelfSearchTests(unittest.TestCase):
    """self_find_files / self_search_text 的行为与防护。"""

    def _tools(self, allow: bool):
        """起一个临时"服务目录"，只读自己的副本，不动真实代码。"""
        tmp = Path(tempfile.mkdtemp(prefix="gantry_self_"))
        (tmp / "gantry").mkdir()
        (tmp / "gantry" / "a.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
        (tmp / "gantry" / "b.js").write_text("const X = 'magic_word';\n", encoding="utf-8")
        (tmp / "config.json").write_text("{}", encoding="utf-8")
        # 借用真实注册表的结构，换根目录
        t = AGENT_TOOLS
        old_root, old_allow = t.global_root, t.allow_self_upgrade
        t.global_root = tmp
        t.allow_self_upgrade = allow
        return t, tmp, old_root, old_allow

    def _restore(self, t, old_root, old_allow):
        t.global_root = old_root
        t.allow_self_upgrade = old_allow

    def test_find_files(self):
        t, tmp, oroot, oa = self._tools(True)
        try:
            r = asyncio.run(t.self_find_files("gantry/*.py"))
            self.assertEqual(r["files"], ["gantry/a.py"])
            self.assertEqual(r["count"], 1)
        finally:
            self._restore(t, oroot, oa)

    def test_search_text(self):
        t, tmp, oroot, oa = self._tools(True)
        try:
            r = asyncio.run(t.self_search_text("magic_word"))
            self.assertEqual(r["count"], 1)
            self.assertEqual(r["hits"][0]["file"], "gantry/b.js")
            self.assertIn("magic_word", r["hits"][0]["text"])
        finally:
            self._restore(t, oroot, oa)

    def test_denylist_blocks_config(self):
        """黑名单（config.json）不能被搜到。"""
        t, tmp, oroot, oa = self._tools(True)
        try:
            (tmp / "config.json").write_text('{"secret":"x"}', encoding="utf-8")
            r = asyncio.run(t.self_search_text("secret"))
            # config.json 在黑名单里，不该出现在结果里
            for h in r["hits"]:
                self.assertNotEqual(h["file"], "config.json")
        finally:
            self._restore(t, oroot, oa)

    def test_requires_allow_self_upgrade(self):
        t, tmp, oroot, oa = self._tools(False)
        try:
            with self.assertRaises(Exception):
                asyncio.run(t.self_search_text("hi"))
        finally:
            self._restore(t, oroot, oa)

    def test_registered_in_tool_list(self):
        """新工具必须在工具列表里（否则 agent 根本不知道有）。"""
        from gantry.tools import ToolRegistry
        # 直接看注册表源码里有没有
        src = (ROOT / "gantry" / "tools.py").read_text(encoding="utf-8")
        self.assertIn('"self_find_files"', src)
        self.assertIn('"self_search_text"', src)


if __name__ == "__main__":
    unittest.main()

class ToolRegistrationTests(unittest.TestCase):
    """描述/分组/分发表必须三处一致（漏分发表 = 模型看得到却调不动，实测）。"""

    def test_dispatch_and_group_and_description_all_have_new_tools(self):
        src = (ROOT / "gantry" / "tools.py").read_text(encoding="utf-8")
        for name in ("self_find_files", "self_search_text"):
            self.assertIn(f'"{name}"', src, f"{name} 缺注册")
            # 分组
            group_part = src.split('"self": (')[1].split(")")[0]
            assert f'"{name}"' in group_part, f"{name} 不在 TOOL_GROUPS[self] 里"
            # 分发表
            self.assertIn(f'"{name}": self.{name}', src, f"{name} 不在 _tools 分发表里")

    def test_every_described_tool_is_dispatchable(self):
        """所有被描述的工具都必须能 dispatch（不只是这两个新的）。"""
        import gantry.bridge as b
        tools = b.AGENT_TOOLS
        described = [x["name"] for x in tools.descriptions()]
        missing = [n for n in described if n not in tools._tools]
        self.assertEqual(missing, [], f"这些工具有描述但不能调用：{missing}")
