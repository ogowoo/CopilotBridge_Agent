# -*- coding: utf-8 -*-
"""power-automate 技能测试：可发现、可读、模板 JSON 可解析、要点覆盖。

技能本体在安装目录 skills/power-automate/ 下，测试用临时目录当工作区（项目级），
把仓库根设为 global_root 来发现它，不动真实工作区数据。
"""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.tools import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "power-automate"


class PowerAutomateSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gantry-pa-skill-"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.tools = ToolRegistry(self.ws, {})
        self.tools.set_global_root(REPO_ROOT)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_skill_list_discovers_power_automate(self):
        cat = {s["name"]: s for s in self.tools.skill_list()}
        self.assertIn("power-automate", cat)
        desc = cat["power-automate"]["description"]
        self.assertIn("Power Automate", desc)

    def test_skill_read_returns_content(self):
        r = asyncio.run(self.tools.skill_read("power-automate"))
        self.assertEqual(r["name"], "power-automate")
        self.assertTrue(len(r["content"]) > 200)
        self.assertIn("definition.json", r["content"])

    def test_templates_are_valid_json_with_required_keys(self):
        flow = json.loads((SKILL_DIR / "templates" / "flow-definition.json").read_text(encoding="utf-8"))
        self.assertIn("triggers", flow)
        self.assertIn("actions", flow)
        self.assertIn("manual", flow["triggers"])
        self.assertTrue(flow["actions"])                      # 至少一个动作
        conns = json.loads((SKILL_DIR / "templates" / "connections.json").read_text(encoding="utf-8"))
        self.assertIn("shared_teams", conns)
        self.assertIn("/providers/Microsoft.PowerApps/apis/", conns["shared_teams"]["id"])

    def test_skill_md_covers_same_tenant_and_delivery_dir(self):
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("make.powerautomate.com", text)         # 同租户 automate 环境
        self.assertIn("同租户", text)
        self.assertIn("flows/<flow-name>/", text)             # 交付目录
        self.assertIn("json.load", text)                      # 校验要求


if __name__ == "__main__":
    unittest.main()
