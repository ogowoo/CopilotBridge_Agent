# -*- coding: utf-8 -*-
"""限流挂起（suspended）：只有 Copilot 网页模型才挂起；挂起窗口里可换提供商模型继续。"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gantry.ui_src import ui_js

import gantry.agent as A  # noqa: E402
from gantry.tools import ToolRegistry  # noqa: E402


class SuspendTests(unittest.TestCase):
    def _run(self, exc, model, config=None):
        async def chat(prompt, model=None, force_new=False, **kw):
            raise exc

        saved = []
        cfg = {"max_service_unavailable": 1, "service_retry_base_seconds": 1,
               "service_retry_max_seconds": 1, "service_retry_window_seconds": 1}
        cfg.update(config or {})
        state_box = {"state": "running"}

        def saver(**kw):
            state_box.update(kw)

        runner = A.AgentRunner(
            tools=ToolRegistry(Path(tempfile.mkdtemp(prefix="cbp_susp_")), {}),
            chat=chat, config=cfg,
            state_getter=lambda: dict(state_box),
            state_saver=saver,
            step_saver=lambda st: saved.append(dict(st)),
            file_event_saver=lambda ev: None,
            approval_check=lambda *a, **k: True,
        )
        res = asyncio.run(runner.run("测试挂起", model))
        return res, state_box, saved

    def test_copilot_rate_limit_suspends(self):
        res, state, saved = self._run(A.CopilotRateLimited("rate limited"), "copilot-auto")
        self.assertEqual(str(state.get("state")), "suspended",
                         "Copilot 网页模型限流应挂起，而不是在窗口里空转")
        self.assertEqual(str(state.get("suspend_reason")), "rate_limit")
        self.assertTrue(state.get("retry_resume_at"), "要记下窗口终点，后台据此自动继续")
        self.assertIn("挂起", str(res.content))

    def test_provider_rate_limit_does_not_suspend(self):
        """提供商模型有自己的 429 语义（window_seconds=0）——不挂起。"""
        exc = A.CopilotRateLimited("too many requests")
        exc.window_seconds = 0
        res, state, saved = self._run(exc, "deepseek/deepseek-chat")
        self.assertNotEqual(str(state.get("state")), "suspended",
                            "提供商模型不应进入挂起态")

    def test_suspend_can_be_disabled(self):
        res, state, saved = self._run(A.CopilotRateLimited("rate limited"), "copilot-auto",
                                      {"rate_limit_suspend": False})
        self.assertNotEqual(str(state.get("state")), "suspended")


class SuspendWiringTests(unittest.TestCase):
    def test_state_registered_and_resume_allowed(self):
        from gantry.session_store import SessionStore
        self.assertIn("suspended", SessionStore.STATES, "状态白名单要包含 suspended")

    def test_api_accepts_suspended_resume_and_periodic_task(self):
        api = ((ROOT / "gantry" / "api.py").read_text(encoding="utf-8") + "".join((ROOT / "gantry" / n).read_text(encoding="utf-8") for n in ("routes_core.py", "routes_config.py", "routes_agents.py", "routes_sessions.py")))
        self.assertIn('"suspended"', api, "resume 要接受 suspended")
        self.assertIn("periodic_suspend_resume", api, "要有窗口到点的自动继续巡检")
        self.assertIn("agent.rate_limit_suspend", api, "开关要进配置白名单")

    def test_frontend_offers_provider_swap(self):
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        app = ui_js()
        self.assertIn('id="retrySwap"', html, "挂起遮罩要有换模型入口")
        # 换模型入口已从"下拉 + 确认按钮"改成**按钮列表**（用户要求在全屏遮罩上直接点），
        # 所以按钮是 JS 渲染的：断言动作与参数，而不是断言 HTML 里的 select。
        self.assertIn('data-act="swapModelResume"', app)
        self.assertIn("data-arg=", app)
        self.assertIn("function swapModelResume(", app)
        self.assertIn("/model", app)
        self.assertIn("window.__MODELS", app, "要能筛出 provider/model")
        # 限流提示已改成全页面遮罩：卡片必须在遮罩里，否则又变成挡内容的浮层
        self.assertIn('id="retryMask"', html)
        self.assertIn('class="mask retrymask"', html)


if __name__ == "__main__":
    unittest.main()
