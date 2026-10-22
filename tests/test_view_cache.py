# -*- coding: utf-8 -*-
"""从子代理返回父会话不能"整棵重建"（用户："从 subagent 返回到 live stream 的时候会很卡"）。

## 实测（父会话 120 条消息 + 400 步）
返回一次 `load()` 约 1.3–1.5s，而且是**同步阻塞**：
- `renderStream`（Markdown 解析 + 语法高亮 120 条消息）= 1.0s
- `renderSteps`（400 步，每步含结果视图）= 0.44s
- 浏览器长任务 1000ms（期间点击/滚动全部无响应）

## 修法
切走时把 `#chatlog` / `#steps` / 调用记录一行的 DOM 节点摘进 `DocumentFragment` 缓存
（最近 3 个会话），切回来直接 append，并恢复 `renderedSteps` / `streamSig` /
`toolLogSig` / 筛选状态 / 滚动位置，随后 runtime 只做增量对账。

实测返回耗时：**1300ms → 121ms**（长任务 1000ms → ~260ms）。
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

APP = (BASE / "webui" / "app.js").read_text(encoding="utf-8")


class ViewCacheSourceTests(unittest.TestCase):
    def test_cache_bounded(self):
        self.assertIn("const viewCache=new Map()", APP)
        self.assertIn("const VIEW_CACHE_MAX=3", APP)
        self.assertIn("while(viewCache.size>VIEW_CACHE_MAX)", APP)

    def test_load_uses_cache_before_network(self):
        i = APP.index("async function load(id)")
        seg = APP[i:i + 1600]
        self.assertIn("viewCache.get(id)", seg)
        self.assertLess(seg.index("viewCache.get(id)"), seg.index("await api("),
                        "命中缓存必须发生在网络请求之前，否则照样卡在等待/重建")

    def test_stash_captures_scroll_before_moving_nodes(self):
        i = APP.index("function stashView(sid)")
        seg = APP[i:i + 1400]
        flat = seg.replace(" ", "")
        self.assertIn("constscrollTop=chat.scrollTop", flat,
                      "清空容器后 scrollHeight=0，scrollTop 会被夹到 0（实测）")
        self.assertLess(flat.index("constscrollTop=chat.scrollTop"),
                        flat.index("while(chat.firstChild)"),
                        "滚动位置必须在搬走节点之前读")

    def test_restore_keeps_incremental_render_state(self):
        i = APP.index("function restoreView(cut)")
        seg = APP[i:i + 1400]
        for key in ("renderedSteps=cut.renderedSteps", "streamSig=cut.streamSig",
                    "toolLogSig=cut.toolLogSig", "stepFilterKey=cut.stepFilterKey"):
            self.assertIn(key.replace(" ", ""), seg.replace(" ", ""),
                          f"恢复后要还原 {key}，否则下一次增量渲染会重复/重建")
        self.assertIn("fillStepTools()", seg)

    def test_restore_keeps_scroll(self):
        i = APP.index("function restoreView(cut)")
        seg = APP[i:i + 1400]
        self.assertIn("chat.scrollTop=cut.scroll", seg.replace(" ", ""))

    def test_session_chrome_reapplied(self):
        i = APP.index("function applySessionChrome")
        seg = APP[i:i + 1400]
        for key in ("title", "sidChip", "status", "setPill", "setKpis",
                    "parentOfCurrent", "sessionView"):
            self.assertIn(key, seg, f"恢复缓存时漏了会话外壳字段: {key}")

    def test_completion_card_not_reused_from_fragment(self):
        i = APP.index("function stashView(sid)")
        seg = APP[i:i + 1400]
        self.assertIn("#changesCard", seg, "完成卡片要按会话重新拉，不能跟着旧 DOM 走")

    def test_new_task_stashes_current_view(self):
        i = APP.index("function resetView()")
        seg = APP[i:i + 500]
        self.assertIn("stashView(renderedSid)", seg)


if __name__ == "__main__":
    unittest.main()
