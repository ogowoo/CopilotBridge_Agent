# -*- coding: utf-8 -*-
"""超长会话打开也要快（用户："会话过长的时候连打开 session 都卡"）。

## 实测（1500 步 + 500 条消息，详情 JSON 2.8MB）
冷打开：首帧 6.4s、浏览器长任务 5.5s。拆开看：
- `renderSteps` 0.9s —— 时间线在**隐藏的页签**里，不该现在建；
- `renderToolLog` 0.9s —— 它把全部 1500 步建进**收起的浮层**；
- `renderStream` 1.2s —— 消息 Markdown + 高亮 + 500 个节点插入/布局；
- 详情被 load + 第一次 runtime **重复拉取并重渲染**一次。

## 修法（界面与交互不变）
- 时间线（`#steps`）只在「步骤时间线」页签首次打开时渲染（`renderStepsMaybe`）；
- 调用记录浮层只在展开时建行（`fillToolLogBody`）；
- `load()` 刚渲染完详情后，第一次 runtime 跳过重复拉取（`skipDetailOnce`）；
- `.msg` / `.step` 加 `content-visibility:auto`，离屏节点跳过布局/绘制；
- 贴底改为短时间反复贴底（`pinChatBottom`），因为 content-visibility 下高度是估算值。
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from gantry.ui_src import ui_css, ui_js

APP = ui_js()
CSS = ui_css()


class LazyTimelineTests(unittest.TestCase):
    def test_steps_deferred_when_timeline_tab_not_active(self):
        i = APP.index("function renderStepsMaybe(steps)")
        seg = APP[i:i + 700]
        self.assertIn("liveTabName!=='timeline'", seg)
        self.assertIn("stepsPending=true", seg)
        self.assertNotIn("renderSteps(lastStepsRaw)", seg[:seg.index("}")] if False else seg[:200])

    def test_tab_switch_flushes_pending_steps(self):
        i = APP.index("function flushStepsIfPending")
        self.assertIn("renderSteps(lastStepsRaw)", APP[i:i + 200])
        j = APP.index("function liveTab(n)")
        seg = APP[j:j + 900]
        self.assertIn("flushStepsIfPending()", seg)

    def test_load_uses_lazy_renderer(self):
        i = APP.index("async function load(id)")
        seg = APP[i:i + 3000]
        self.assertIn("renderStepsMaybe(", seg)
        self.assertNotIn("renderSteps(d.steps);lastStepCount", seg)

    def test_pending_state_survives_view_cache(self):
        self.assertIn("stepsPending,lastStepsRaw", APP.replace(" ", ""))
        i = APP.index("function restoreView(cut)")
        seg = APP[i:i + 1600]
        self.assertIn("stepsPending=!!cut.stepsPending", seg.replace(" ", ""))


class LazyToolLogTests(unittest.TestCase):
    """调用记录改成行内展开后，仍要"没展开不建 DOM，展开才建"。"""

    def test_body_not_built_eagerly(self):
        i = APP.index("function renderToolLog(steps,opts)")
        seg = APP[i:i + 3200]
        # 只在展开时建 DOM，且按签名 + 防抖（每次快照全量重建会把机器拖死）
        self.assertIn("if(!body.hidden){", seg)
        self.assertIn("toolLogBodySig", seg)
        self.assertIn("setTimeout(", seg)
        self.assertNotIn("for(const s of all)body.appendChild", seg)

    def test_body_render_is_capped(self):
        i = APP.index("function fillToolLogBody(")
        seg = APP[i:i + 900]
        self.assertIn("toolLogSteps.slice(-300)", seg, "上千步只画最近 300 条")
        self.assertIn("document.hidden", seg, "后台标签页不渲染")

    def test_opening_builds_body(self):
        i = APP.index("function toggleToolLog(")
        seg = APP[i:i + 1200]
        self.assertIn("renderToolLog(toolLogSteps,{force:true})", seg.replace(" ", ""))
        self.assertIn("body.hidden=!open", seg.replace(" ", ""))

    def test_inline_expansion_not_popover(self):
        self.assertNotIn("positionToolLogPop", APP)
        self.assertNotIn("toolLogPop", APP)


class DetailFetchOnceTests(unittest.TestCase):
    def test_load_marks_skip(self):
        i = APP.index("async function load(id)")
        self.assertIn("skipDetailOnce=id", APP[i:i + 3200])

    def test_runtime_consumes_skip(self):
        i = APP.index("async function applyRuntime")
        seg = APP[i:i + 4200]
        self.assertIn("skipDetailOnce===myId", seg)
        self.assertIn("skipDetailOnce=null", seg)


class LayoutCssTests(unittest.TestCase):
    def test_content_visibility_for_offscreen_items(self):
        flat = CSS.replace(" ", "")
        self.assertIn(".chatlog.msg{content-visibility:auto", flat)
        self.assertIn("#steps.step{content-visibility:auto", flat)

    def test_pin_bottom_retries(self):
        i = APP.index("function pinChatBottom")
        seg = APP[i:i + 900]
        self.assertIn("chatPinUntil=Date.now()+1500", seg)
        self.assertIn("setTimeout(tick,60)", seg)
        self.assertIn("chatPinUntil=0", APP, "用户滚动时要立即停手")


if __name__ == "__main__":
    unittest.main()
