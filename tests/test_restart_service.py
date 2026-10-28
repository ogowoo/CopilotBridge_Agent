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
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry import lifecycle


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
    """exec_restart 的**当前契约**：spawn 新进程（同一解释器 + 绝对 start.py）后 os._exit(0)。

    历史：早先实现是 `os.execv`（本进程被替换），测试按"patch execv 期待 SystemExit"写；
    上游"修重启"改成 `Popen + os._exit`（Windows 上 execv 其实是新建进程再退旧的），
    测试没跟上 → 真的 spawn 了一个服务，把测试进程干掉，看起来像"卡死"。
    """

    def setUp(self):
        self._orig_argv = list(sys.argv)
        self._orig_exit = os._exit
        lifecycle._restarting = True
        self.calls = {}

        def fake_exit(code=0):
            self.calls["exit"] = code
            raise SystemExit(code)      # 让测试能继续（真实现会立刻结束进程）

        os._exit = fake_exit

    def tearDown(self):
        os._exit = self._orig_exit
        sys.argv[:] = self._orig_argv
        lifecycle.disarm()
        lifecycle.set_force_exit(True)
        lifecycle._atexit.clear()

    def test_exec_restart_spawns_entry_script(self):
        """必须 spawn `python <绝对路径>/start.py`，cwd 是项目根，且随后 os._exit(0)。"""
        seen = {}

        def fake_popen(args, **kw):
            seen["args"] = list(args)
            seen["cwd"] = kw.get("cwd")
            class _P:
                pass
            return _P()

        with mock.patch("subprocess.Popen", fake_popen):
            with self.assertRaises(SystemExit):
                lifecycle.exec_restart()

        self.assertEqual(Path(seen["args"][0]), Path(sys.executable))
        entry = Path(seen["args"][1])
        self.assertTrue(entry.is_file(), "第 2 个参数应是入口脚本")
        self.assertEqual(entry.name, "start.py")
        self.assertEqual(Path(seen["cwd"]).resolve(), lifecycle._project_root().resolve())
        self.assertEqual(self.calls.get("exit"), 0, "spawn 后必须 os._exit(0)")

    def test_entry_argv_does_not_leak_extra_args(self):
        """不传任何参数：start.py 不解析 argv，透传只会把 unittest 参数带进去。"""
        old = list(sys.argv)
        try:
            sys.argv = ["-m", "unittest", "discover", "-s", "tests", "-v"]
            argv = lifecycle._entry_argv()
        finally:
            sys.argv = old
        self.assertEqual(len(argv), 1, f"只应有入口脚本，实际 {argv}")
        self.assertTrue(argv[0].endswith("start.py"))

    def test_restart_hooks_run_before_spawn(self):
        ran = []
        lifecycle.on_restart(lambda: ran.append("close-db"))
        with mock.patch("subprocess.Popen", lambda *a, **k: object()):
            with self.assertRaises(SystemExit):
                lifecycle.exec_restart()
        self.assertEqual(ran, ["close-db"], "重启前收尾应执行")


class RestartApiTests(unittest.TestCase):
    def setUp(self):
        lifecycle.disarm()
        lifecycle.set_force_exit(False)
        from fastapi.testclient import TestClient
        from gantry.api import app
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
        """重启/终止服务已从「More」搬到**设置页的危险区**（低频操作不占菜单）。"""
        base = Path(__file__).resolve().parent.parent
        html = (base / "webui" / "index.html").read_text(encoding="utf-8")
        js = (base / "webui" / "app.js").read_text(encoding="utf-8")
        for act in ("restartService", "shutdownService"):
            # 静态 HTML 里可能没有（设置页的按钮是 JS 渲染的），所以两边都允许
            self.assertTrue(f'data-act="{act}"' in html or f'data-act="{act}"' in js,
                            f"{act} 入口缺失")
        self.assertIn("restartService:()=>restartService()", js, "ACTIONS 未注册")
        self.assertIn("/v1/restart", js)
        self.assertIn("waitServiceBack", js, "重启后要能自动重连")
        # 设置页里重启应挨着终止服务
        self.assertLess(js.index('data-act="restartService"'),
                        js.index('data-act="shutdownService"'),
                        "危险区里重启应排在终止服务之前")
        # More 菜单不再包含这两个危险动作
        i = html.index('id="moreMenu"')
        menu = html[i:html.index("</div></div>", i)]
        self.assertNotIn('data-act="restartService"', menu)
        self.assertNotIn('data-act="shutdownService"', menu)


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
