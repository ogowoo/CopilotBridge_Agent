# -*- coding: utf-8 -*-
"""Agent 中途提问：回答完不能还挂着，重复回答不能报错。

用户："我晚了好久才回答这个提醒，但是回答之后这个还在，再回答就提示没有 pending 的提问。"

## 根因（两处）
1. **后端从不清 `question` 字段**：`_ask_user_via_webui` 用
   `runtime_update(question={...})` 把它写进 `RUNTIME`,回答后只
   `EXTRA_ANSWERS.pop(sid)` —— `question` 一直留着。前端每秒轮询 `/runtime`
   拿到同一份陈旧快照 → **提问条每次都重新画出来**；
2. `/answer` 在没有挂起提问时返回 **404「该会话没有等待回答的问题」**,
   前端把它当失败 toast（还保留提问条）→ 条子永久挂在界面上。

## 修法
- `_ask_user_via_webui` 的 `finally` 里 `runtime_update(sid, question=None)`;
- `/answer` 成功即清 `question`；404 分支也顺手清（陈旧字段）；
- 前端加 `questionAnswered` 记账（key = 会话|问题文本）：答过的提问即使快照残留
  也不再显示；404/409 或"没有等待回答"文案 → 视为已完成，收起提问条并提示
  「该提问已回答或已过期，已收起」,而不是报错。

## 实测（真服务 + Edge）
| 场景 | 结果 |
|---|---|
| 弹出提问 | `display:flex` ✓ |
| 提交（后端无挂起 → 404） | `display:none` ✓，且不报错 |
| 陈旧快照重放同一问题 | 保持 `display:none` ✓ |
| 新问题 | 正常 `display:flex` ✓ |
| 后端 `/runtime` 的 `question` | 404 之后变成 `null` ✓
"""
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
APP = (BASE / "webui" / "app.js").read_text(encoding="utf-8")
BRIDGE = (BASE / "cbp" / "bridge.py").read_text(encoding="utf-8")
API = ((BASE / "cbp" / "api.py").read_text(encoding="utf-8") + "".join((BASE / "cbp" / n).read_text(encoding="utf-8") for n in ("routes_core.py", "routes_config.py", "routes_agents.py", "routes_sessions.py")))


class BackendClearsQuestionTests(unittest.TestCase):
    def test_ask_user_finally_clears_question(self):
        i = BRIDGE.index("async def _ask_user_via_webui")
        seg = BRIDGE[i:i + 2000]
        self.assertIn("EXTRA_ANSWERS.pop(sid, None)", seg)
        self.assertIn("question=None", seg,
                      "回答/超时后必须把 question 清掉，否则前端轮询会把提问条重画")

    def test_answer_endpoint_clears_question(self):
        i = API.index('@router.post("/v1/agent/sessions/{sid}/answer")')  # 端点已拆到 routes_sessions.py
        seg = API[i:i + 1200]
        self.assertIn("question=None", seg, "/answer 成功必须立即清 question")
        # 404 分支也要清（陈旧字段），否则用户只能靠刷新页面
        j = seg.index("raise HTTPException(404")
        self.assertIn("question=None", seg[:j], "404 分支也要顺手清 question")


class FrontendQuestionBarTests(unittest.TestCase):
    def test_answered_set_exists(self):
        self.assertIn("questionAnswered", APP)
        self.assertIn("function questionKey", APP)

    def test_stale_question_not_reshown(self):
        i = APP.index("function showQuestionBar(q){")
        seg = APP[i:i + 400]
        self.assertIn("questionAnswered.has(", seg,
                      "已答过的提问不能再显示（陈旧快照会反复推同一份）")

    def test_404_treated_as_done(self):
        i = APP.index("const submit=async()=>{")
        seg = APP[i:i + 1600]
        self.assertIn("404", seg, "404 要特殊处理成'已完成'")
        self.assertIn("questionAnswered.add(", seg)
        self.assertIn("display='none'", seg.replace(" ", ""), "要把提问条收起来")

    def test_real_error_still_errors(self):
        """真正的失败（500 等）仍要报错并保留输入，不能被 404 分支吞掉。"""
        i = APP.index("const submit=async()=>{")
        seg = APP[i:i + 1600]
        self.assertIn("提交失败", seg)
        self.assertIn("ok.disabled=false", seg.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
