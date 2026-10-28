# -*- coding: utf-8 -*-
"""权限档位（仿 DSH）：read-only / workspace-write / danger-full-access。

后端一直有 SANDBOX_MODES 与 /v1/agent/sandbox，本轮补的是**界面入口**：
输入框上方的 chip + 三档菜单（含说明与勾选态），并在设置页也能改。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gantry.ui_src import ui_js

API = ((ROOT / "gantry" / "api.py").read_text(encoding="utf-8") + "".join((ROOT / "gantry" / n).read_text(encoding="utf-8") for n in ("routes_core.py", "routes_config.py", "routes_agents.py", "routes_sessions.py")))
HTML = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
APP = ui_js()
I18N = (ROOT / "webui" / "i18n.js").read_text(encoding="utf-8")


class BackendContractTests(unittest.TestCase):
    def test_three_modes(self):
        from gantry.bridge import SANDBOX_MODES
        self.assertEqual(tuple(SANDBOX_MODES),
                         ("read-only", "workspace-write", "danger-full-access"))

    def test_endpoints_exist_and_validate(self):
        self.assertIn('@router.get("/v1/agent/sandbox")', API)
        self.assertIn('@router.post("/v1/agent/sandbox")', API)
        self.assertIn("mode 必须是", API, "非法档位要被拒绝")
        self.assertIn("persist", API, "要能落库")

    def test_config_schema_has_sandbox_mode(self):
        self.assertIn('"sandbox_mode": {"kind": "enum"', API)
        self.assertIn('b.AGENT_TOOLS.sandbox_mode = val', API,
                      "从设置页改也要作用到运行中的工具层")


class FrontendTests(unittest.TestCase):
    def test_chip_and_menu_markup(self):
        self.assertIn('id="sandboxChip"', HTML)
        self.assertIn('id="sandboxMenu"', HTML)
        for mode in ("read-only", "workspace-write", "danger-full-access"):
            self.assertIn(f'data-act="pickSandbox" data-arg="{mode}"', HTML)
        # 三档的展示名要出现在页面里（并走 i18n）
        for label in ("仅可查看", "工作区内修改", "完全权限"):
            self.assertIn(label, HTML)

    def test_chip_in_new_task_composer_too(self):
        """新建任务（空态）的按钮行里也要有权限入口——用户要求"这里也加一个"。"""
        self.assertIn("nc-bar", HTML)
        # 第二份实例不带 id，用 class + data-sandbox-* 标记，且必须有 .sandboxchip
        self.assertEqual(HTML.count('class="menu sandboxpick"'), 2,
                         "会话内与新建任务各一份权限菜单")
        self.assertIn('data-sandbox-chip="new"', HTML)
        self.assertIn('data-sandbox-menu="new"', HTML)
        i = HTML.find('class="nc-bar"')
        j = HTML.find("</div>", HTML.find("nc-right", i))
        seg = HTML[i:i + 4000]
        self.assertIn("sandboxpick", seg, "新建任务栏里要有权限 chip")

    def test_js_wiring(self):
        for needle in ("function renderSandbox(", "function pickSandbox(",
                       "function loadSandbox(", "/v1/agent/sandbox",
                       "document.querySelectorAll('.sandboxchip')"):
            self.assertIn(needle, APP, f"缺少 {needle}")
        self.assertIn("pickSandbox:(el)=>pickSandbox", APP, "ACTIONS 未注册")
        self.assertIn("loadSandbox()", APP, "启动时要读当前档位")

    def test_i18n_keys(self):
        for key in ("仅可查看", "工作区内修改", "完全权限", "权限档位：工具能改什么（点开选择）"):
            self.assertIn('"' + key + '"', I18N, f"缺少词条 {key}")


if __name__ == "__main__":
    unittest.main()
