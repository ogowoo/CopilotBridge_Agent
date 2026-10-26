# -*- coding: utf-8 -*-
"""全局/项目两级 AGENT.md 与 skills、以及自升级工具链的测试。

运行: python -m unittest discover -s tests -v
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.agent import AgentRunner
from gantry.snapshots import SnapshotStore
from gantry.tools import ToolError, ToolRegistry, parse_skill_md


def build_runner(tools, config=None):
    return AgentRunner(tools, None, config or {},
                       state_getter=dict,
                       state_saver=lambda **k: None,
                       step_saver=lambda x: None,
                       file_event_saver=lambda x: None)


def write_skill(root: Path, name: str, desc: str, body: str = "正文"):
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n{body}\n",
                                encoding="utf-8")


class TwoScopeTests(unittest.TestCase):
    """全局（服务安装目录）+ 项目（工作区）两级注入。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_scope_"))
        self.global_root = self.tmp / "install"
        self.ws = self.tmp / "ws"
        self.global_root.mkdir()
        self.ws.mkdir()
        (self.global_root / "AGENT.md").write_text("全局约定：所有项目通用\n", encoding="utf-8")
        (self.ws / "AGENT.md").write_text("项目约定：本工程专用\n", encoding="utf-8")
        write_skill(self.global_root, "shared", "全局技能")
        write_skill(self.global_root, "global-only", "只在全局")
        write_skill(self.ws, "shared", "项目覆盖版技能")
        write_skill(self.ws, "project-only", "只在项目")
        self.tools = ToolRegistry(self.ws, {})
        self.tools.set_global_root(self.global_root)

    def test_agent_md_merges_both_scopes(self):
        runner = build_runner(self.tools)
        text = runner._load_agent_md()
        self.assertIn("【全局 AGENT.md】", text)
        self.assertIn("全局约定：所有项目通用", text)
        self.assertIn("【项目 AGENT.md】", text)
        self.assertIn("项目约定：本工程专用", text)
        prompt = runner._system_prompt()
        self.assertIn("全局 + 项目两级 AGENT.md", prompt)

    def test_agent_md_dedupes_same_file(self):
        """工作区 == 安装目录时不应重复注入同一份 AGENT.md。"""
        tools = ToolRegistry(self.global_root, {})
        tools.set_global_root(self.global_root)
        runner = build_runner(tools)
        text = runner._load_agent_md()
        self.assertEqual(text.count("全局约定：所有项目通用"), 1)

    def test_skill_catalog_project_overrides_global(self):
        cat = {s["name"]: s for s in self.tools.skill_list()}
        self.assertEqual(len(cat), 3)                       # shared 只留一份
        self.assertEqual(cat["shared"]["scope"], "project")
        self.assertEqual(cat["shared"]["description"], "项目覆盖版技能")
        self.assertEqual(cat["global-only"]["scope"], "global")

    def test_skill_read_global_and_project(self):
        r = asyncio.run(self.tools.skill_read("global-only"))
        self.assertIn("只在全局", r["content"])
        self.assertEqual(r["scope"], "global")
        r2 = asyncio.run(self.tools.skill_read("shared"))
        self.assertEqual(r2["scope"], "project")

    def test_skill_read_unknown_raises(self):
        with self.assertRaises(ToolError):
            asyncio.run(self.tools.skill_read("nope"))

    def test_prompt_lists_skills_with_scope(self):
        runner = build_runner(self.tools)
        runner._skills = runner._load_skill_catalog()
        prompt = runner._system_prompt()
        self.assertIn("[全局] global-only", prompt)
        self.assertIn("[项目] shared", prompt)
        self.assertIn('skill_read "global-only"', prompt)

    def test_parse_skill_md_fallback(self):
        name, desc = parse_skill_md("dirname", "# 标题\n\n简介段落\n")
        self.assertEqual((name, desc), ("标题", "简介段落"))


class SelfUpgradeTests(unittest.TestCase):
    """自升级：白名单、写入门、快照作用域与回滚。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_self_"))
        self.install = self.tmp / "install"
        (self.install / "gantry").mkdir(parents=True)
        (self.install / "data").mkdir()
        (self.install / "config.json").write_text('{"k":1}', encoding="utf-8")
        self.target = self.install / "gantry" / "mod.py"
        self.target.write_text("原始代码\n", encoding="utf-8")
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.tools = ToolRegistry(self.ws, {"allow_self_upgrade": True})
        self.tools.set_global_root(self.install)
        self.store = SnapshotStore(self.tmp / "snaps")
        self.tools.set_snapshot_hook(
            lambda tool, rel, target: self.store.capture("_self_s1", rel, target, tool))

    def test_disabled_by_default(self):
        tools = ToolRegistry(self.ws, {})
        tools.set_global_root(self.install)
        self.assertFalse(tools.allow_self_upgrade)
        with self.assertRaises(ToolError):
            asyncio.run(tools.self_read_text("gantry/mod.py"))

    def test_write_outside_whitelist_refused(self):
        with self.assertRaises(ToolError):
            asyncio.run(self.tools.self_read_text("config.json"))
        with self.assertRaises(ToolError):
            asyncio.run(self.tools.self_read_text("data/agent.db"))
        with self.assertRaises(ToolError):
            asyncio.run(self.tools.self_read_text("../outside.txt"))

    def test_self_write_and_rollback(self):
        import base64
        payload = base64.b64encode("升级后的代码\n".encode()).decode()
        r = asyncio.run(self.tools.self_write_text_b64("gantry/mod.py", payload))
        self.assertEqual(r["written_chars"], len("升级后的代码\n"))
        self.assertEqual(self.target.read_text(encoding="utf-8"), "升级后的代码\n")
        self.assertTrue((self.install / "data" / ".self_upgrade_pending").is_file())
        # 回滚（scope = _self_s1）
        out = self.store.restore("_self_s1", self.install)
        self.assertEqual((out["restored"], out["failed"]), (1, 0))
        self.assertEqual(self.target.read_text(encoding="utf-8"), "原始代码\n")

    def test_self_replace_requires_existing_text(self):
        with self.assertRaises(ToolError):
            asyncio.run(self.tools.self_replace_text("gantry/mod.py", "不存在的文本", "x"))
        r = asyncio.run(self.tools.self_replace_text("gantry/mod.py", "原始", "改动"))
        self.assertEqual(r["replaced"], 1)
        self.assertIn("改动", self.target.read_text(encoding="utf-8"))

    def test_self_tools_registered_only_when_enabled(self):
        enabled = set(self.tools._tools)
        self.assertIn("self_write_text_b64", enabled)
        self.assertIn("skill_read", enabled)
        disabled = set(ToolRegistry(self.ws, {})._tools)
        self.assertNotIn("self_write_text_b64", disabled)

    def test_path_traversal_outside_install_refused(self):
        outside = self.tmp / "outside.py"
        outside.write_text("x", encoding="utf-8")
        with self.assertRaises(ToolError):
            asyncio.run(self.tools.self_read_text("../outside.py"))


if __name__ == "__main__":
    unittest.main()
