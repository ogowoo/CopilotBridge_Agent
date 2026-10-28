# -*- coding: utf-8 -*-
"""telegram 插件的单元测试（不碰真 Telegram API，全部 mock）。

钉住：
1. 白名单：没进 allowed_chat_ids 的 chat 一律忽略（安全问题——bot 是公开的）；
2. 会话映射：同一 chat 复用 sid，/new 强制开新 sid；
3. 命令分发：/new /status /todo /cancel /model /answer /help；
4. token 缺失时 setup 不炸、不启动轮询；
5. manifest 声明与实际一致。
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import plugins.telegram.plugin as tg  # noqa: E402


class FakeCtx:
    def __init__(self, config=None, state=None):
        self.config = config or {}
        self.state = state or {}
        self.logs = []
        self.apis = []

    def log(self, msg):
        self.logs.append(str(msg))

    def save_state(self, **kw):
        self.state.update(kw)

    def register_api(self, method, path, handler):
        self.apis.append((method, path, handler))


def run(coro):
    return asyncio.run(coro)


class TokenAndGatingTests(unittest.TestCase):
    def setUp(self):
        tg._RUNS.clear()

    def test_token_from_config_then_env(self):
        ctx = FakeCtx({"token": "cfg-token"})
        with mock.patch.dict("os.environ", {}, clear=False):
            self.assertEqual(tg._token(ctx), "cfg-token")
        ctx2 = FakeCtx({})
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "env-token"}):
            self.assertEqual(tg._token(ctx2), "env-token")

    def test_allowed_parses_list_and_csv(self):
        ctx = FakeCtx({"allowed_chat_ids": ["1", "2"]})
        self.assertEqual(tg._allowed(ctx), {"1", "2"})
        with mock.patch.dict("os.environ", {"TELEGRAM_ALLOWED_CHAT_IDS": "3, 4 "}):
            ctx2 = FakeCtx({})
            self.assertEqual(tg._allowed(ctx2), {"3", "4"})

    def test_unauthorized_chat_ignored(self):
        ctx = FakeCtx({"allowed_chat_ids": ["42"]})
        sent = []
        with mock.patch.object(tg, "_send", side_effect=lambda c, cid, t: sent.append((cid, t))):
            with mock.patch.object(tg, "_run_task", new=mock.AsyncMock()):
                run(tg._handle_update(ctx, {"message": {"chat": {"id": 99}, "text": "hi"}}))
        self.assertEqual(sent, [], "未授权 chat 不应受理")
        self.assertTrue(any("忽略未授权" in m for m in ctx.logs))


class SessionMappingTests(unittest.TestCase):
    def setUp(self):
        tg._RUNS.clear()

    def test_same_chat_reuses_sid(self):
        ctx = FakeCtx({"default_model": "copilot-auto"})
        s1 = tg._sid_for(ctx, "7")
        s2 = tg._sid_for(ctx, "7")
        self.assertEqual(s1, s2)
        self.assertTrue(s1.startswith("tg-7-"))

    def test_new_forces_new_sid(self):
        ctx = FakeCtx({})
        s1 = tg._sid_for(ctx, "7")
        s2 = tg._sid_for(ctx, "7", new=True)
        self.assertNotEqual(s1, s2)


class CommandTests(unittest.TestCase):
    def setUp(self):
        tg._RUNS.clear()
        self.ctx = FakeCtx({"allowed_chat_ids": ["7"], "default_model": "copilot-auto"})
        self.sent = []
        patcher = mock.patch.object(tg, "_send",
                                    side_effect=lambda c, cid, t: self.sent.append((cid, t)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_new_without_arg_tells_usage(self):
        run(tg._cmd(self.ctx, "7", "/new"))
        self.assertTrue(any("用法" in t for _, t in self.sent))

    def test_new_runs_task(self):
        with mock.patch.object(tg, "_run_task", new=mock.AsyncMock()) as m:
            run(tg._cmd(self.ctx, "7", "/new 跑一遍测试"))
            m.assert_awaited_once()
            self.assertEqual(m.await_args.args[2], "跑一遍测试")

    def test_status_reports_model(self):
        run(tg._cmd(self.ctx, "7", "/status"))
        self.assertTrue(any("copilot-auto" in t for _, t in self.sent))

    def test_model_switch_persists(self):
        run(tg._cmd(self.ctx, "7", "/model dsweb/deepseek-reasoner"))
        self.assertEqual(tg._model_for(self.ctx, "7"), "dsweb/deepseek-reasoner")

    def test_cancel_patches_state(self):
        import gantry.bridge as b
        with mock.patch.object(b.AGENT_SESSIONS, "patch") as p:
            run(tg._cmd(self.ctx, "7", "/cancel"))
            p.assert_called()
        self.assertTrue(any("已取消" in t for _, t in self.sent))

    def test_unknown_command_falls_through_to_task(self):
        handled = run(tg._cmd(self.ctx, "7", "/nosuchcmd"))
        self.assertFalse(handled, "未知命令应返回 False（外层按普通文字跑任务）")


class SetupTeardownTests(unittest.TestCase):
    """setup 里会 create_task（需要事件循环）：生产环境在 lifespan 里天然有，
    测试里手动建一个并活到断言完，否则会 RuntimeError: no running event loop。"""

    def setUp(self):
        tg._POLL_TASK = None
        tg._RUNS.clear()
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        tg.teardown(FakeCtx({}))
        self.loop.close()
        asyncio.set_event_loop(None)

    def test_setup_without_token_stays_idle(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            ctx = FakeCtx({})
            tg.setup(ctx)
            self.assertIsNone(tg._POLL_TASK, "无 token 不应启动轮询")
            self.assertTrue(any("待命" in m for m in ctx.logs))
            self.assertTrue(ctx.apis, "status 接口必须注册（面板要用）")

    def test_setup_with_token_starts_polling(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok"}):
            ctx = FakeCtx({})
            tg.setup(ctx)
            self.assertIsNotNone(tg._POLL_TASK)
            self.assertFalse(tg._POLL_TASK.done())

    def test_teardown_cancels(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok"}):
            ctx = FakeCtx({})
            tg.setup(ctx)
            tg.teardown(ctx)
            self.assertIsNone(tg._POLL_TASK)


class ManifestTests(unittest.TestCase):
    def test_manifest_shape(self):
        import json
        m = json.loads((ROOT / "plugins" / "telegram" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(m["name"], "telegram")
        g = m["gantry"]
        self.assertIn("events", g["permissions"])
        self.assertIn("storage", g["permissions"])
        self.assertIn("api", g["permissions"])


if __name__ == "__main__":
    unittest.main()
