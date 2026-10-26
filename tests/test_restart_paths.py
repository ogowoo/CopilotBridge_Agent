# -*- coding: utf-8 -*-
"""重启路径回归测试（用户："重启的功能会因为路径问题导致重启失败"）。

覆盖：
1. `_entry_argv()` 在 `python -m gantry.cli serve` / 包内脚本启动时，**不能**返回包内 .py，
   必须返回项目根的 start.py（否则 exec 后相对导入 ImportError，进程直接死）；
2. 没有 start.py（装成纯包）时退回 `-m gantry.cli serve`；
3. `exec_restart()` 必须把 cwd 切回项目根（`app:app`、config.json、data/ 都依赖）；
4. `start.py` 的端口优先取 `GANTRY_PORT`（`gantry serve --port` 以前静默无效）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gantry import lifecycle  # noqa: E402


class EntryArgvTests(unittest.TestCase):
    def test_always_uses_project_start_py(self):
        argv = lifecycle._entry_argv()
        self.assertTrue(argv, "不能返回空")
        first = Path(argv[0])
        self.assertTrue(first.is_absolute(), f"入口必须是绝对路径：{argv}")
        self.assertEqual(first.name, "start.py")
        self.assertEqual(first, ROOT / "start.py")

    def test_never_returns_a_file_inside_the_package(self):
        argv = lifecycle._entry_argv()
        # 最容易踩的坑：-m 启动时 sys.argv[0] 指向 gantry/cli.py
        for part in argv:
            if str(part).endswith(".py"):
                self.assertNotIn(str(ROOT / "gantry"), str(Path(part).resolve()),
                                 f"入口不能是包内脚本：{part}")

    def test_simulated_module_launch(self):
        """模拟 `python -m gantry.cli serve`：sys.argv[0] 是包内 cli.py。"""
        old = list(sys.argv)
        try:
            sys.argv = [str(ROOT / "gantry" / "cli.py"), "serve"]
            argv = lifecycle._entry_argv()
            self.assertEqual(Path(argv[0]), ROOT / "start.py")
            self.assertNotIn("serve", argv[1:], "不应把子命令当脚本参数带过去")
        finally:
            sys.argv = old

    def test_fallback_to_module_when_no_start_py(self):
        import tempfile
        from unittest import mock

        fake_root = Path(tempfile.mkdtemp(prefix="gantry_root_"))
        (fake_root / "gantry").mkdir()
        (fake_root / "gantry" / "cli.py").write_text("", encoding="utf-8")
        with mock.patch.object(lifecycle, "_project_root", return_value=fake_root):
            argv = lifecycle._entry_argv()
        self.assertEqual(argv[:3], ["-m", "gantry.cli", "serve"])

    def test_raises_when_nothing_usable(self):
        import tempfile
        from unittest import mock

        empty = Path(tempfile.mkdtemp(prefix="gantry_empty_"))
        with mock.patch.object(lifecycle, "_project_root", return_value=empty):
            with self.assertRaises(RuntimeError):
                lifecycle._entry_argv()


class ExecRestartChdirTests(unittest.TestCase):
    def test_exec_restart_chdirs_to_project_root(self):
        src = (ROOT / "gantry" / "lifecycle.py").read_text(encoding="utf-8")
        i = src.index("def exec_restart()")
        body = src[i:i + 1800]
        self.assertIn("os.chdir(root)", body, "exec 前必须切回项目根")
        self.assertIn("cwd=str(root)", body, "spawn 兜底也要用项目根")

    def test_start_py_puts_root_on_sys_path(self):
        s = (ROOT / "start.py").read_text(encoding="utf-8")
        self.assertIn("sys.path.insert(0, str(BASE))", s)

    def test_start_py_prefers_port_env(self):
        s = (ROOT / "start.py").read_text(encoding="utf-8")
        self.assertIn('_env("PORT")', s, "端口要优先取 GANTRY_PORT")
        self.assertIn("port=_port", s)


if __name__ == "__main__":
    unittest.main()
