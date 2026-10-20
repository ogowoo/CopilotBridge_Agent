# -*- coding: utf-8 -*-
"""工具栏收敛成一行（用户选的方案 2）：主操作只留「恢复 / 取消任务」。

## 为什么
800×545 这类矮窗口里，顶栏原本排**两行**（暂停/恢复/压缩上下文 + 继续执行/
切换到页面/打开会话/深研），加上 KPI 就把主区吃到只剩几十像素——
对话区再也不会有人看得见。

## 做法
- 主操作只留 `恢复`(control:resume) 与 `取消任务`(control:cancel),外加 `更多 ▾`;
- 其余全部收进「更多」菜单：暂停、压缩上下文、继续执行、切换到页面、打开会话、深研;
- `#webGrp` 保留在菜单里（提供商模型仍要整体隐藏这两个动作）;
- 菜单里加分组/分隔线（`.menugrp` / `.mensep`）,避免和归档/删除/重启/终止混在一起。

## 顺带修掉的回归
`@media(max-width:1100px){.main{overflow:auto}}` 会让主列退回"整列滚动",内部不再
收缩 —— 1024×600 实测对话区只有 67px。改成保持 `display:flex` + `overflow:hidden`,
并把 `#sessionView` 的 `flex-basis` 从 `auto` 改成 `0%`（auto 会按内容撑高）。
KPI 压缩断点同步提到 1100px（1024 宽时 5 张卡原本排两行、白占 167px）。
"""
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HTML = (BASE / "webui" / "index.html").read_text(encoding="utf-8")
_RAW = (BASE / "webui" / "style.css").read_text(encoding="utf-8")
CSS = re.sub(r"/\*[\s\S]*?\*/", " ", _RAW)


def _actions_block() -> str:
    i = HTML.index('<div class="actions">')
    j = HTML.index("</header>")
    return HTML[i:j]


def _primary_buttons() -> list:
    """工具栏里**不在菜单内**的按钮文案。"""
    block = _actions_block()
    i_menu = block.index('<div class="menuList"')
    outside = block[:i_menu]
    return re.findall(r">([^<>]{1,12})</button>", outside)


class ToolbarCompactTests(unittest.TestCase):
    def test_only_two_primary_actions(self):
        prim = [x.strip() for x in _primary_buttons() if x.strip()]
        self.assertIn("恢复", prim)
        self.assertIn("取消任务", prim)
        self.assertIn("更多 ▾", prim)
        for moved in ("暂停", "压缩上下文", "继续执行", "切换到页面", "打开会话", "深研"):
            self.assertNotIn(moved, prim,
                             f"「{moved}」应收进「更多」菜单，不该占顶栏一行")

    def test_moved_actions_live_in_menu(self):
        block = _actions_block()
        menu = block[block.index('<div class="menuList"'):]
        for act in ('data-arg="pause"', 'data-arg="compact"', 'data-act="continueAgent"',
                    'data-act="activate"', 'data-act="openCopilot"', 'data-act="deepResearch"'):
            self.assertIn(act, menu, f"{act} 应移入「更多」菜单")

    def test_web_group_still_wrappable(self):
        # 提供商会话要能整体隐藏这两个动作：保留 #webGrp 包裹（JS 按 id 隐藏）
        block = _actions_block()
        self.assertIn('id="webGrp"', block)
        self.assertIn('id="btnActivate"', block)
        self.assertIn('id="btnOpenChat"', block)

    def test_menu_groups_styled(self):
        self.assertIn(".menugrp", CSS)
        self.assertIn(".mensep", CSS)


class MainColumnRegressionTests(unittest.TestCase):
    def test_main_not_scrolling_at_1100(self):
        m = re.search(r"@media\(max-width:1100px\)\{[^}]*\.main\{([^}]*)\}", CSS)
        self.assertIsNotNone(m, "缺少 ≤1100px 的 .main 规则")
        body = m.group(1).replace(" ", "")
        self.assertIn("overflow:hidden", body,
                      "≤1100px 不能让 .main 退回整列滚动（会让对话区被压到几十像素）")
        self.assertIn("display:flex", body)

    def test_sessionview_basis_zero(self):
        # 可能有多条 #sessionView 规则：取**最后一条**（层叠里生效的那条）
        all_rules = [r.replace(" ", "") for r in re.findall(r"#sessionView\{([^}]*)\}", CSS)]
        self.assertTrue(all_rules, "找不到 #sessionView 规则")
        self.assertTrue(any("flex:110%" in r for r in all_rules),
                        "#sessionView 的 flex-basis 必须是 0%（auto 会按内容撑高、不收缩）；"
                        f"实际规则: {all_rules}")

    def test_kpi_compact_breakpoint_covers_1024(self):
        m = re.search(r"@media\(max-width:1100px\)\{[^}]*\.kpis\{([^}]*)\}", CSS)
        self.assertIsNotNone(m, "KPI 压缩断点应覆盖 1024 宽（原本 900 时 1024 会排两行）")
        self.assertIn("repeat(5,minmax(0,1fr))", m.group(1).replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
