# -*- coding: utf-8 -*-
"""「继续对话」输入框的斜杠菜单（`/`）回归。

用户实测：在跟进框里打 `/` 什么也不出现（还以为工具没生效）。
根因：斜杠菜单只挂在「新任务」框（`#task` + `#slashMenu`），跟进框既没有
`data-act-input/key`，也没有自己的菜单容器；`slash.js` 里还写死了 `#task`。

现在两个输入框共用一套逻辑（`SLASH_PAIRS`），各自一个菜单容器。真实交互由
`scripts/smoke_slash.py` 在浏览器里验证（弹出 9 条 / ↓ 换选中 / Enter 只补全不发送）。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HTML = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
SLASH = (ROOT / "webui" / "js" / "slash.js").read_text(encoding="utf-8")
SETTINGS = (ROOT / "webui" / "js" / "settings.js").read_text(encoding="utf-8")
COMPOSER = (ROOT / "webui" / "js" / "composer.js").read_text(encoding="utf-8")


def fn_body(name: str) -> str:
    i = SLASH.find(f"function {name}(")
    assert i >= 0, f"slash.js 找不到 {name}"
    depth = 0
    for k in range(SLASH.find("{", i), len(SLASH)):
        if SLASH[k] == "{":
            depth += 1
        elif SLASH[k] == "}":
            depth -= 1
            if depth == 0:
                return SLASH[i:k + 1]
    raise AssertionError(f"{name} 大括号不配平")


class FollowupWiringTests(unittest.TestCase):
    def test_followup_has_slash_attrs(self):
        m = re.search(r'<textarea id="followup"[^>]*>', HTML)
        self.assertIsNotNone(m, "index.html 里找不到 #followup")
        self.assertIn('data-act-input="slashFollowInput"', m.group(0))
        self.assertIn('data-act-key="slashFollowKey"', m.group(0))

    def test_followup_has_own_menu(self):
        self.assertIn('id="slashMenuFollow"', HTML, "跟进框没有自己的斜杠菜单容器")
        m = re.search(r'<div id="slashMenuFollow"[^>]*>', HTML)
        self.assertIn("slashmenu", m.group(0), "菜单容器要带 .slashmenu 样式类")
        self.assertIn("hidden", m.group(0), "默认应是收起的")

    def test_actions_registered(self):
        for key in ("slashFollowInput", "slashFollowKey"):
            self.assertRegex(SETTINGS, rf"\b{key}\s*:", f"ACTIONS 里缺 {key}")
        # 两个输入框都要把元素传下去，否则菜单分不清是谁弹的
        self.assertIn("taskInput:(el)=>slashUpdate(false,el)", SETTINGS)
        self.assertIn("slashFollowInput:(el)=>slashUpdate(false,el)", SETTINGS)


class SlashModuleTests(unittest.TestCase):
    def test_pairs_declared(self):
        self.assertIn("SLASH_PAIRS", SLASH)
        self.assertIn("{input:'task', menu:'slashMenu'}", SLASH)
        self.assertIn("{input:'followup', menu:'slashMenuFollow'}", SLASH)

    def test_functions_use_pair_not_hardcoded_task(self):
        for name in ("slashUpdate", "slashKey", "slashPick"):
            body = fn_body(name)
            self.assertIn("_pair(", body, f"{name} 应通过 _pair() 取目标")
            self.assertNotIn("getElementById('task')", body,
                             f"{name} 里不该再写死 #task")

    def test_open_and_run_cover_all_menus(self):
        self.assertIn("SLASH_PAIRS", fn_body("slashOpen"),
                      "slashOpen() 要检查所有输入框的菜单，不能只看 #slashMenu")
        self.assertIn("_hideMenus", fn_body("slashRun"),
                      "执行命令后要关掉所有菜单（含跟进框那个）")

    def test_enter_goes_to_slash_first(self):
        i = COMPOSER.find("e.target.id==='followup'")
        self.assertGreater(i, 0)
        seg = COMPOSER[i:i + 700]
        self.assertIn("window.slashKey", seg, "Enter 必须先问斜杠菜单")
        self.assertLess(seg.index("window.slashKey"), seg.index("sendFollowup("),
                        "先判断菜单，再发送")


if __name__ == "__main__":
    unittest.main()
