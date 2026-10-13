"""skill_create 工具测试：两级作用域、权限门、覆盖保护与 frontmatter 格式。"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.tools import ToolError, ToolRegistry, parse_skill_md


class SkillCreateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp-skill-"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.install = self.tmp / "install"
        self.install.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _registry(self, **cfg):
        tools = ToolRegistry(self.ws, cfg)
        tools.set_global_root(self.install)
        return tools

    def test_project_skill_created_with_frontmatter(self):
        tools = self._registry()
        r = asyncio.run(tools.execute("skill_create", {
            "name": "WebUI Theme Change",
            "description": "改主题时怎么加一套配色",
            "content": "# 加主题\n\n1. 在 :root 加变量组\n2. 补 THEMES 数组",
        }))
        self.assertEqual(r["slug"], "webui-theme-change")        # 名称规范化为短横线
        self.assertEqual(r["scope"], "project")
        target = self.ws / "skills" / "webui-theme-change" / "SKILL.md"
        self.assertTrue(target.is_file())
        text = target.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        name, desc = parse_skill_md("webui-theme-change", text)
        self.assertEqual(name, "webui-theme-change")
        self.assertEqual(desc, "改主题时怎么加一套配色")
        # 建完立即可被列出（无需重启）
        self.assertIn("webui-theme-change", [s["name"] for s in tools.skill_list()])

    def test_description_falls_back_to_first_paragraph(self):
        tools = self._registry()
        asyncio.run(tools.execute("skill_create", {
            "name": "no-desc-skill", "content": "# 标题\n\n这是首个段落描述。"}))
        _name, desc = parse_skill_md("no-desc-skill",
                                     (self.ws / "skills" / "no-desc-skill" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(desc, "这是首个段落描述。")

    def test_description_skips_short_headings(self):
        """回退描述不能把「什么时候用」这类小标题当成简介。"""
        tools = self._registry()
        asyncio.run(tools.execute("skill_create", {
            "name": "skip-heading",
            "content": "什么时候用\n- 接手新项目时快速了解目标与架构。\n\n步骤\n1. 先读 README"}))
        _name, desc = parse_skill_md("skip-heading",
                                     (self.ws / "skills" / "skip-heading" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("接手新项目", desc)
        self.assertNotEqual(desc, "什么时候用")

    def test_duplicate_requires_overwrite(self):
        tools = self._registry()
        args = {"name": "dup-skill", "content": "第一版"}
        asyncio.run(tools.execute("skill_create", args))
        with self.assertRaises(ToolError):
            asyncio.run(tools.execute("skill_create", args))
        r = asyncio.run(tools.execute("skill_create",
                                      {"name": "dup-skill", "content": "第二版", "overwrite": True}))
        self.assertTrue(r["created"])
        self.assertIn("第二版", (self.ws / "skills" / "dup-skill" / "SKILL.md").read_text(encoding="utf-8"))

    def test_global_scope_requires_self_upgrade(self):
        tools = self._registry()  # allow_self_upgrade 默认 False
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(tools.execute("skill_create", {
                "name": "g-skill", "content": "x", "scope": "global"}))
        self.assertIn("allow_self_upgrade", str(ctx.exception))

        tools2 = self._registry(allow_self_upgrade=True)
        r = asyncio.run(tools2.execute("skill_create", {
            "name": "g-skill", "content": "全局技能", "scope": "global"}))
        self.assertEqual(r["scope"], "global")
        self.assertTrue((self.install / "skills" / "g-skill" / "SKILL.md").is_file())
        # 全局技能对所有工作区可见
        self.assertIn("g-skill", [s["name"] for s in tools2.skill_list()])

    def test_gates_and_validation(self):
        off = self._registry(allow_skill_write=False)
        with self.assertRaises(ToolError):
            asyncio.run(off.execute("skill_create", {"name": "x", "content": "y"}))

        tools = self._registry()
        for bad in ({"name": "!!!", "content": "y"},          # 名称无法规范化
                    {"name": "ok-name", "content": "   "},    # 空内容
                    {"name": "ok-name", "content": "y", "scope": "world"}):
            with self.assertRaises(ToolError):
                asyncio.run(tools.execute("skill_create", bad))

    def test_snapshot_captured_before_write(self):
        captured = []
        tools = self._registry()
        tools.set_snapshot_hook(lambda tool, rel, target: captured.append((tool, rel, str(target))))
        asyncio.run(tools.execute("skill_create", {"name": "snap-skill", "content": "内容"}))
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "skill_create")
        self.assertEqual(captured[0][1], "skills/snap-skill/SKILL.md")


if __name__ == "__main__":
    unittest.main()
