# -*- coding: utf-8 -*-
"""重启服务（WebUI 菜单）：优雅退出 → 就地 exec 重启。

用户要"重启服务的菜单，方便更新用"。要点：
1. HTTP 层**先返回响应**再延迟触发（否则前端拿到连接重置，看起来像失败）；
2. 重启必须真的重启——优雅停机超时要**执行重启**而不是硬退出（否则用户等来一个死进程）；
3. 用 `os.execv` 就地替换进程映像：监听端口随之释放，没有"新进程抢端口"的竞态；
4. exec 不可用时退回"派生新进程 + 退出自己"；
5. 重启后页面能自动重连（前端轮询 /health）。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import lifecycle


class LifecycleRestartTests(unittest.TestCase):
    def setUp(self):
        lifecycle.disarm()
        lifecycle.set_force_exit(False)   # 测试里禁止真硬退出/重启
        lifecycle.set_shutdown_hook(None)

    def tearDown(self):
        lifecycle.disarm()
        lifecycle.set_force_exit(True)
        lifecycle.set_shutdown_hook(None)

    def test_request_restart_sets_both_flags(self):
        calls = []
        lifecycle.set_shutdown_hook(lambda: calls.append(1))
        how = lifecycle.request_restart(force_after=30)
        self.assertEqual(how, "uvicorn")
        self.assertTrue(lifecycle.restart_requested())
        self.assertTrue(lifecycle.shutdown_pending(), "重启也要走停机流程")
        self.assertEqual(calls, [1], "应调用注入的优雅停机钩子")

    def test_restart_is_idempotent(self):
        lifecycle.set_shutdown_hook(lambda: None)
        self.assertEqual(lifecycle.request_restart(force_after=30), "uvicorn")
        self.assertEqual(lifecycle.request_restart(force_after=30), "already")

    def test_shutdown_then_restart_is_ignored(self):
        lifecycle.set_shutdown_hook(lambda: None)
        lifecycle.request_shutdown(force_after=30)
        self.assertEqual(lifecycle.request_restart(force_after=30), "already")
        self.assertFalse(lifecycle.restart_requested(), "已经要停机了，不该改成重启")

    def test_no_signal_when_force_exit_disabled(self):
        """禁用兜底时连信号都不发（否则测试进程被打断——实测踩过）。"""
        sent = []
        orig = os.kill
        os.kill = lambda pid, sig: sent.append((pid, sig))
        try:
            lifecycle.set_force_exit(False)
            how = lifecycle.request_restart(force_after=30)
        finally:
            os.kill = orig
            lifecycle.set_force_exit(True)
        self.assertEqual(how, "none")
        self.assertEqual(sent, [], "禁用兜底时不应发信号")

    def test_restart_falls_back_to_signal_without_hook(self):
        """没有注入钩子时退回信号——但测试里必须桩掉 os.kill。

        注意：真发 SIGINT 是打给**本进程**的，会直接把测试进程打死（我第一次就
        这么干了，unittest 没有任何摘要、退出码 2）。生产路径需要真发信号，
        测试只需要确认"走到信号分支"。
        """
        sent = []
        orig = os.kill
        os.kill = lambda pid, sig: sent.append((pid, sig))
        try:
            lifecycle.set_force_exit(True)      # 显式开启，才能走到信号分支
            how = lifecycle.request_restart(force_after=30)
        finally:
            os.kill = orig
            lifecycle.set_force_exit(False)
        self.assertEqual(how, "signal")
        self.assertEqual(len(sent), 1, "应尝试发送一次信号")

    def test_restart_requested_survives_for_entry_to_see(self):
        """start.py 在 server.run() 返回后靠这个标志决定是否 exec 重启。"""
        lifecycle.set_shutdown_hook(lambda: None)
        lifecycle.request_restart(force_after=30)
        self.assertTrue(lifecycle.restart_requested())

    def test_entry_argv_points_at_existing_script(self):
        argv = lifecycle._entry_argv()
        self.assertTrue(argv, "重启命令行不能为空")
        self.assertTrue(Path(argv[0]).is_file(), f"入口脚本不存在: {argv[0]}")


    def test_disarm_cancels_pending_timer(self):
        """disarm 之后，排队中的兜底定时器不能再执行重启/退出。"""
        import time as _t
        lifecycle.set_shutdown_hook(lambda: None)
        original = lifecycle._force_seconds
        lifecycle._force_seconds = 0.5
        try:
            lifecycle.request_restart(force_after=0.5)
            lifecycle.disarm()
            _t.sleep(0.9)                  # 让定时器醒来
        finally:
            lifecycle._force_seconds = original
        self.assertFalse(lifecycle.restart_requested())
        self.assertFalse(lifecycle.shutdown_pending())


class ScheduleTests(unittest.TestCase):
    """HTTP 层用的延时触发必须可取消（否则会在用例之后反噬测试进程）。"""

    def setUp(self):
        lifecycle.disarm()
        lifecycle.set_force_exit(False)

    def tearDown(self):
        lifecycle.disarm()
        lifecycle.set_force_exit(True)

    def test_schedule_is_cancellable(self):
        import time as _t
        armed = []
        real = lifecycle.request_restart
        lifecycle.request_restart = lambda *a, **k: armed.append("restart")
        try:
            self.assertTrue(lifecycle.schedule("restart", 0.4))
            lifecycle.disarm()             # 取消
            _t.sleep(0.7)                  # 让它该触发的时间过去
        finally:
            lifecycle.request_restart = real
        self.assertEqual(armed, [], "取消后不应再触发")

    def test_schedule_fires_when_not_cancelled(self):
        import time as _t
        armed = []
        real = lifecycle.request_restart
        lifecycle.request_restart = lambda *a, **k: armed.append("restart")
        try:
            lifecycle.schedule("restart", 0.05)
            _t.sleep(0.4)
        finally:
            lifecycle.request_restart = real
            lifecycle.disarm()
        self.assertEqual(armed, ["restart"])

    def test_duplicate_schedule_ignored(self):
        real = lifecycle.request_restart
        lifecycle.request_restart = lambda *a, **k: None
        try:
            self.assertTrue(lifecycle.schedule("restart", 5))
            self.assertFalse(lifecycle.schedule("restart", 5), "同类型只排一次")
        finally:
            lifecycle.request_restart = real
            lifecycle.disarm()


class ExecRestartTests(unittest.TestCase):
    """exec 真的会用同一解释器重跑入口脚本（用桩替换 os.execv 观察参数）。"""

    def setUp(self):
        self._orig_execv = os.execv
        self._orig_argv = list(sys.argv)
        lifecycle._restarting = True

    def tearDown(self):
        os.execv = self._orig_execv
        sys.argv[:] = self._orig_argv
        lifecycle.disarm()
        lifecycle.set_force_exit(True)
        lifecycle._atexit.clear()

    def test_exec_restart_execs_entry_script(self):
        seen = {}

        def fake_execv(program, args):
            seen["program"] = program
            seen["args"] = list(args)
            raise SystemExit(0)          # 模拟 exec 成功（不会返回）

        os.execv = fake_execv
        with self.assertRaises(SystemExit):
            lifecycle.exec_restart()
        self.assertEqual(seen["program"], sys.executable)
        self.assertEqual(seen["args"][0], sys.executable)
        self.assertTrue(Path(seen["args"][1]).is_file(), "第 2 个参数应是入口脚本")

    def test_restart_hooks_run_before_exec(self):
        ran = []
        lifecycle.on_restart(lambda: ran.append("close-db"))

        def fake_execv(*_a):
            raise SystemExit(0)

        os.execv = fake_execv
        with self.assertRaises(SystemExit):
            lifecycle.exec_restart()
        self.assertEqual(ran, ["close-db"], "重启前收尾应执行")


class RestartApiTests(unittest.TestCase):
    def setUp(self):
        lifecycle.disarm()
        lifecycle.set_force_exit(False)
        from fastapi.testclient import TestClient
        from cbp.api import app
        self.c = TestClient(app)

    def tearDown(self):
        lifecycle.disarm()
        lifecycle.set_force_exit(True)

    def test_restart_endpoint_returns_immediately(self):
        r = self.c.post("/v1/restart")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body.get("restarting"))
        self.assertEqual(body.get("note_code"), "restart_soon")

    def test_restart_endpoint_reports_already(self):
        lifecycle._restarting = True
        r = self.c.post("/v1/restart")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("already"))
        self.assertEqual(r.json().get("note_code"), "restart_pending")


class UiWiringTests(unittest.TestCase):
    def test_menu_and_action_registered(self):
        base = Path(__file__).resolve().parent.parent
        html = (base / "webui" / "index.html").read_text(encoding="utf-8")
        js = (base / "webui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-act="restartService"', html, "菜单项缺失")
        self.assertIn("restartService:()=>restartService()", js, "ACTIONS 未注册")
        self.assertIn("/v1/restart", js)
        self.assertIn("waitServiceBack", js, "重启后要能自动重连")
        # 菜单里重启应挨着终止服务
        self.assertLess(html.index('data-act="restartService"'),
                        html.index('data-act="shutdownService"'))


class StartScriptRetryTests(unittest.TestCase):
    """重启瞬间新旧进程可能交叠：入口要能重试绑定，而不是直接死掉。"""

    def test_serve_retries_on_bind_failure(self):
        import importlib.util, io, contextlib
        spec = importlib.util.spec_from_file_location(
            "cbp_start_mod", str(Path(__file__).resolve().parent.parent / "start.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tries = {"n": 0}
        mod.BIND_RETRY_DELAY = 0

        class FakeServer:
            def run(self):
                tries["n"] += 1
                if tries["n"] < 3:
                    raise SystemExit(1)      # 模拟端口占用
                return None                  # 第三次成功

        mod.build_server = lambda: FakeServer()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            mod.serve()
        self.assertEqual(tries["n"], 3, "应在两次失败后成功")
        self.assertIn("重试", err.getvalue())

    def test_serve_gives_up_after_limit(self):
        import importlib.util, io, contextlib
        spec = importlib.util.spec_from_file_location(
            "cbp_start_mod2", str(Path(__file__).resolve().parent.parent / "start.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.BIND_RETRY_DELAY = 0
        mod.BIND_RETRIES = 2

        class AlwaysFail:
            def run(self):
                raise SystemExit(1)

        mod.build_server = lambda: AlwaysFail()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                mod.serve()


if __name__ == "__main__":
    unittest.main()
