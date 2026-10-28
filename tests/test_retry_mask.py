# -*- coding: utf-8 -*-
"""限流提示：全页面遮罩（含换模型按钮）、不再撑大间距、英文完整。

用户两条反馈：
- "这个弹窗导致 live stream 与输入框之间的距离撑大了"
- "这个弹窗能是个全页面遮罩不？选择其它提供商模型继续的选项直接在那个全页面遮罩上显示也可以选择"
- "你可以看到这个弹窗还有中文，我当前在英文模式下的"

## 当时的三个问题
1. **撑大间距**：`.main` 底部有一圈 `padding-bottom: calc(26px + var(--retry-h) + ...)`,
   `--retry-h` = 浮卡高度 → 浮卡一出现，live stream 与输入框之间就多出一大块空白；
2. **只是右下角浮卡**：容易挡内容，也不是"全页面遮罩",换模型还得先开下拉再点确认；
3. **标题硬编码中文**：`isProvider?'⏳ API 服务限流…':'⏳ Copilot 限流…'` 没走 `tr()`,
   英文模式下只有副标题是英文、标题仍是中文。

## 现在的做法
- 整块搬进 `#retryMask`（`.mask.retrymask`,全屏暗化 + 居中 `.retrydlg` 卡片）；
- `.main` 不再为它预留高度（只按右下角小标 `retryChip` 预留）;
- 提供商模型直接列成按钮（`data-act="swapModelResume" data-arg="<model>"`）,点一下
  就切换并继续；"稍后再看（后台继续等待）"收起为小标；点遮罩空白处同样收起；
- 标题/按钮/静态文案全部走 `tr()`,i18n.js 补齐词条；
- `updateRetryCountdown` 增加**局部快照守卫**:SSE 只带部分字段时不再把遮罩收掉。
"""
from gantry.ui_src import ui_js
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HTML = (BASE / "webui" / "index.html").read_text(encoding="utf-8")
API = ((BASE / "gantry" / "api.py").read_text(encoding="utf-8") + "".join((BASE / "gantry" / n).read_text(encoding="utf-8") for n in ("routes_core.py", "routes_config.py", "routes_agents.py", "routes_sessions.py")))
APP = ui_js()
I18N = (BASE / "webui" / "i18n.js").read_text(encoding="utf-8")
_RAW = (BASE / "webui" / "style.css").read_text(encoding="utf-8")
CSS = re.sub(r"/\*[\s\S]*?\*/", " ", _RAW)
KEYS = [
    "服务限流/不可用，预计恢复",
    "Copilot 限流/不可用，预计恢复",
    "API 服务限流/不可用，预计恢复",
    "稍后再看（后台继续等待）",
    "用这个模型继续",
]


class RetryMaskStructureTests(unittest.TestCase):
    def test_countdown_lives_inside_mask(self):
        i = HTML.index('id="retryMask"')
        j = HTML.index("</div>\n</div>", i)          # 遮罩块结束
        self.assertIn('id="retryCountdown"', HTML[i:j], "倒计时卡必须在遮罩里")
        self.assertIn('class="mask retrymask"', HTML)
        # 小标已移到顶栏（不再是右下角浮层），点它重开遮罩
        self.assertIn('id="retryChip" class="retrychip"', HTML)
        self.assertIn('data-act="reopenRetry"', HTML)

    def test_provider_models_are_buttons_not_select(self):
        self.assertNotIn('id="retryModel"', HTML, "模型选择改成按钮列表，不再用下拉")
        self.assertIn('id="retrySwap"', HTML)

    def test_dismiss_button_wired(self):
        self.assertIn('id="retryMin"', HTML)
        self.assertIn('data-act="dismissRetry"', HTML)
        self.assertIn("dismissRetry:", APP)

    def test_no_reserved_band_for_floating_card(self):
        m = re.search(r"\.main\{[^}]*padding:[^;]*;", CSS)
        self.assertIsNotNone(m, "找不到 .main 的 padding 规则")
        self.assertNotIn("--retry-h", m.group(0),
                         ".main 不能再给限流卡预留高度（那会把 live stream 与输入框撑开）")

    def test_card_not_fixed_anymore(self):
        m = re.search(r"\.retry-fab\{([^}]*)\}", CSS)
        self.assertIsNotNone(m, "缺少 .retry-fab 规则")
        self.assertIn("position:static", m.group(1).replace(" ", ""),
                      "卡片应在遮罩里居中排布，不再是 fixed 浮层")
        self.assertRegex(CSS, r"\.retrymask\{[^}]*z-index:\s*7\d",
                         "遮罩层级要高于其他浮层")


class RetryI18nAndLogicTests(unittest.TestCase):
    def test_title_uses_tr(self):
        i = APP.index("function updateRetryCountdown")
        seg = APP[i:i + 3000]
        self.assertIn("tr('Copilot 限流/不可用，预计恢复')", seg)
        self.assertIn("tr('API 服务限流/不可用，预计恢复')", seg)
        self.assertNotIn("'⏳ Copilot 限流/不可用，预计恢复'", seg,
                         "标题不能再是裸中文字面量")

    def test_english_keys_present(self):
        for k in KEYS:
            self.assertIn(f'"{k}"', I18N, f"i18n.js 缺少英文词条: {k}")

    def test_model_buttons_call_swap(self):
        i = APP.index("function fillRetrySwap")
        seg = APP[i:i + 1400]
        self.assertIn('data-act="swapModelResume"', seg)
        self.assertIn("data-arg=", seg)
        self.assertIn("swapModelResume:(el)=>swapModelResume(el", APP,
                      "动作要能把按钮上的模型 id 传进去")

    def test_partial_snapshot_guard(self):
        i = APP.index("function updateRetryCountdown")
        seg = APP[i:i + 1200]
        self.assertIn("_hasField", seg,
                      "SSE 局部快照不能把遮罩收掉（要沿用已知的 resumeAt）")

    def test_mask_toggled_with_card(self):
        i = APP.index("function updateRetryCountdown")
        seg = APP[i:i + 2600]
        self.assertIn("mask.style.display='flex'", seg.replace(" ", ""))
        self.assertIn("function hideRetryUI", APP)
        j = APP.index("function hideRetryUI")
        self.assertIn("retryMask", APP[j:j + 500], "hideRetryUI 也要收遮罩")

    def test_backdrop_click_dismisses(self):
        self.assertIn("dismissRetryOverlay", APP)
        i = APP.index("getElementById('retryMask').addEventListener")
        self.assertIn("retryMask", APP[i:i + 200])


if __name__ == "__main__":
    unittest.main()


class ServiceUnavailableBadgeTests(unittest.TestCase):
    """"短暂退避"（服务不可用但不进限流窗口）也必须看得见。

    用户实测："都没看见弹窗。。。。" —— 那三条 `服务暂时不可用（第 1 次）` 走的是短退避，
    后端**不写 retry_resume_at**,于是遮罩永远不出现，界面上什么都不显示。
    现在用顶栏小标：持续可见、不打断操作，恢复后自动消失。
    """

    def test_badge_function_exists(self):
        self.assertIn("function showServiceBadge", APP)

    def test_checked_before_clearing(self):
        """必须先判"服务不可用"再判"窗口清空"，否则又被提前 return 掉。"""
        i = APP.index("function updateRetryCountdown")
        seg = APP[i:i + 1200]
        a = seg.index("if(showServiceBadge())")
        b = seg.index("_hasField&&!r.retry_resume_at){retryDismissedAt=null;hideRetryUI")
        self.assertLess(a, b, "showServiceBadge 必须在『明确清空』之前判断")

    def test_badge_uses_last_error(self):
        i = APP.index("function showServiceBadge")
        seg = APP[i:i + 900]
        self.assertIn("service_error", seg)
        self.assertIn("service_waiting", seg)

    def test_badge_is_inline_in_topbar(self):
        self.assertIn('id="retryChip" class="retrychip"', HTML)
        m = re.search(r"\.retrychip\{([^}]*)\}", CSS)
        self.assertIsNotNone(m, "缺少 .retrychip 样式")
        self.assertIn("inline-flex", m.group(1).replace(" ", ""))

    def test_badge_yields_to_countdown(self):
        """有明确恢复时间点（retry_resume_at）时，badge 不得抢倒计时——
        否则用户只能看到 ⚠ 图标，永远不知道还要等多久（issue #5 实测）。"""
        i = APP.index("function showServiceBadge")
        seg = APP[i:i + 600]
        # 必须先判 retry_resume_at，再判服务错误
        a = seg.index("retry_resume_at")
        b = seg.index("service_error")
        self.assertLess(a, b, "showServiceBadge 必须先判 retry_resume_at 再判 service_error")

    def test_no_floating_chip_left(self):
        self.assertNotIn(".retry-chip{", CSS, "旧的右下角浮层样式应已删除")
        self.assertNotIn("body .retry-chip", CSS)
        self.assertNotIn('class="retry-chip"', HTML)

    def test_runtime_exposes_service_error(self):
        self.assertIn('"service_error"', API)
        self.assertIn('"service_waiting"', API)
