# -*- coding: utf-8 -*-
"""系统托盘 + 后台启动（`python start.py` 派生 pythonw）的回归。

用户需求："python start.py 之后可以关掉终端（相当于 pythonw），托盘常驻，菜单里能启动
自动化浏览器、打开 WebUI，日志也要留档。"

Win32 那部分只能在 Windows 真机上验证（`python start.py --tray-selftest` 会真的建图标，
并用 Shell_NotifyIconGetRect 确认注册成功）；这里守住**纯逻辑**与**接线**：
菜单项都有实现、后台命令用 pythonw、日志真的落盘、start.py 不在导入期拉 uvicorn。
"""
import logging
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

START_SRC = (ROOT / "start.py").read_text(encoding="utf-8")
TRAY_SRC = (ROOT / "gantry" / "tray.py").read_text(encoding="utf-8")
ICON = ROOT / "webui" / "favicon.ico"   # 浏览器 favicon 与托盘同一份


class IconTests(unittest.TestCase):
    def test_icon_exists_and_is_ico(self):
        self.assertTrue(ICON.is_file(), "webui/favicon.ico 不存在（favicon 与托盘图标都会退回默认）")
        head = ICON.read_bytes()[:4]
        self.assertEqual(head, b"\x00\x00\x01\x00", "不是合法的 .ico 文件头")

    def test_icon_has_multiple_sizes(self):
        """多尺寸才会有清晰的 16px 小图标（否则任务栏里糊成一团）。"""
        data = ICON.read_bytes()
        count = int.from_bytes(data[4:6], "little")
        self.assertGreaterEqual(count, 4, f"图标只有 {count} 个尺寸")


    def test_index_references_the_same_icon(self):
        """页面 favicon 必须就是托盘用的那个文件（一份图标，两处展示）。"""
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('rel="icon"', html)
        self.assertIn('href="favicon.ico"', html, "favicon 应指向 webui/favicon.ico")
        self.assertNotIn("data:image/svg+xml", html, "别再用内联 SVG（与托盘的 G 不一致）")


class MenuTests(unittest.TestCase):
    def setUp(self):
        from gantry import tray

        self.tray = tray
        self.t = tray.Tray(port=9, root=ROOT)      # 端口 9 必无服务 → "未响应"分支

    @staticmethod
    def _flat(items):
        """把子菜单摊平：[(action, label)]（分隔线/信息行不算）。"""
        out = []
        for item in items:
            if item[0] == "-" or item[0] is None:
                continue
            if item[0] == "--submenu--":
                out.extend(item[2])
            else:
                out.append((item[0], item[1]))
        return out

    def test_menu_has_required_actions(self):
        actions = {a for a, _ in self._flat(self.tray.default_menu(self.t))}
        for need in ("open_webui", "start_browser", "restart", "quit_tray", "shutdown",
                     "open_logs", "open_data", "lang_zh", "lang_en"):
            self.assertIn(need, actions, f"托盘菜单缺少 {need}")

    def test_every_action_has_handler(self):
        for a, label in self._flat(self.tray.default_menu(self.t)):
            self.assertTrue(label.strip(), f"{a} 没有文案")
            if a == "quit_tray":
                continue                            # 这个走 _on_message 里的 stop()
            self.assertTrue(hasattr(self.tray.Tray, f"_a_{a}"), f"{a} 没有实现 _a_{a}()")

    def test_labels_are_short(self):
        """用户："不要那么宽"——菜单标签必须短（中文 ≤ 8 字，英文 ≤ 22 字符）。"""
        import re

        cjk = re.compile(r"[\u4e00-\u9fff]")
        for a, label in self._flat(self.tray.default_menu(self.t)):
            limit = 8 if cjk.search(label) else 22
            self.assertLessEqual(len(label), limit, f"{a} 的文案太长（{len(label)}）: {label}")

    def test_first_line_is_just_version(self):
        """状态/端口在 tooltip 里，菜单首行只留版本号（长状态行会把菜单撑宽）。"""
        head = self.tray.default_menu(self.t)[0]
        self.assertIsNone(head[0])
        text = head[1]
        self.assertNotIn("端口", text)
        self.assertNotIn("port", text.lower())
        self.assertLessEqual(len(text), 40)

    def test_port_lives_in_tooltip(self):
        """端口/状态放 tooltip（放进菜单会把它撑宽）。"""
        tip = self.t._tooltip()
        self.assertIn("9", tip, f"tooltip 应含端口: {tip}")
        self.assertTrue(any(w in tip for w in ("未响应", "运行中")), tip)

    def test_start_browser_uses_http_not_login_script(self):
        """托盘启动浏览器必须走服务的按需接口，不依赖登录脚本。"""
        fn = getattr(self.tray.Tray, "_a_start_browser")
        src = __import__("inspect").getsource(fn)
        self.assertIn("/v1/agent/browser", src)
        self.assertNotIn("subprocess", src, "不要在托盘里自己拉浏览器进程")
        self.assertNotIn("scripts", src, "不要依赖 scripts/ 下的脚本")


class I18nTests(unittest.TestCase):
    """托盘菜单三语言：键齐全、标签短、可切换、写回 config.json。"""

    def test_all_langs_complete(self):
        from gantry.tray import TEXT

        keys = set(TEXT["zh"])
        self.assertEqual(keys, set(TEXT["tw"]), "繁中缺词条")
        self.assertEqual(keys, set(TEXT["en"]), "英文缺词条")

    def test_menu_built_per_lang(self):
        from gantry import tray as tray_mod

        need = ("open_webui", "start_browser", "restart", "shutdown")
        for lang in ("zh", "tw", "en"):
            t = tray_mod.Tray(port=9, root=ROOT, lang=lang)
            labels = {a: lab for a, lab in MenuTests._flat(tray_mod.default_menu(t))}
            self.assertEqual(set(labels) >= set(need), True, f"{lang} 菜单不全")
            self.assertEqual(t.lang, lang)
            for a in need:
                self.assertTrue(labels[a].strip())

    def test_set_language_persists_to_config(self):
        """切换语言要写 config.json 的 ui.language，下次启动还是它。"""
        import json
        import tempfile

        from gantry import tray as tray_mod

        tmp = Path(tempfile.mkdtemp(prefix="gantry_tray_cfg_"))
        (tmp / "config.json").write_text(json.dumps({"server": {"port": 9}}), encoding="utf-8")
        t = tray_mod.Tray(port=9, root=tmp, lang="zh")
        t.set_language("en")
        self.assertEqual(t.lang, "en")
        cfg = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["ui"]["language"], "en")
        # 再次构造时应当读到 en
        t2 = tray_mod.Tray(port=9, root=tmp)
        self.assertEqual(t2.lang, "en")

    def test_detect_lang_env_override(self):
        import os

        from gantry import tray as tray_mod

        old = os.environ.get("GANTRY_LANG")
        os.environ["GANTRY_LANG"] = "en"
        try:
            self.assertEqual(tray_mod.detect_lang(ROOT), "en")
        finally:
            if old is None:
                os.environ.pop("GANTRY_LANG", None)
            else:
                os.environ["GANTRY_LANG"] = old


class DetachTests(unittest.TestCase):
    def test_detach_command_uses_pythonw_and_tray_child(self):
        import start

        cmd = start.build_detach_command([])
        self.assertTrue(cmd[1].endswith("start.py"), cmd)
        self.assertEqual(cmd[2], "--tray-child", "派生出的子进程必须带 --tray-child")
        self.assertTrue(cmd[0].lower().endswith("pythonw.exe") or "python" in cmd[0].lower())

    def test_pythonw_preferred_when_present(self):
        import start

        exe = start._pythonw()
        if exe:
            self.assertTrue(exe.lower().endswith("pythonw.exe"), exe)

    def test_no_uvicorn_import_at_module_time(self):
        """`--help` / `--print-menu` / `--tray-selftest` 不该被 uvicorn 拖住。"""
        for line in START_SRC.splitlines():
            if line.strip() == "import uvicorn":
                self.assertTrue(line.startswith(" "), "uvicorn 必须在函数里 import（顶层会拖慢所有子命令）")

    def test_print_menu_runs_without_service(self):
        import subprocess

        r = subprocess.run([sys.executable, str(ROOT / "start.py"), "--print-menu"],
                           capture_output=True, text=True, encoding="utf-8", timeout=90,
                           cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertIn("open_webui", r.stdout)
        self.assertIn("start_browser", r.stdout)


class LogTests(unittest.TestCase):
    def test_setup_logging_writes_file_and_is_idempotent(self):
        from gantry import logcfg

        tmp = Path(tempfile.mkdtemp(prefix="gantry_log_"))
        p1 = logcfg.setup_logging(tmp, console=False)
        p2 = logcfg.setup_logging(tmp, console=False)
        self.assertEqual(p1, p2, "重复配置应指向同一个日志文件")
        logging.getLogger("gantry.test").info("hello-tray-log")
        self.assertTrue(p1.is_file())
        self.assertIn("hello-tray-log", p1.read_text(encoding="utf-8"))
        # 不能叠加 handler（每个进程只有一个文件 handler，否则日志重复）
        files = [h for h in logging.getLogger().handlers if getattr(h, "_gantry_file", False)]
        self.assertEqual(len(files), 1, f"文件 handler 有 {len(files)} 个")
        # 收拾干净，别影响其它测试
        for h in list(logging.getLogger().handlers):
            if getattr(h, "_gantry_file", False) or getattr(h, "_gantry_console", False):
                logging.getLogger().removeHandler(h)

    def test_log_env_override(self):
        from gantry import logcfg

        tmp = Path(tempfile.mkdtemp(prefix="gantry_logenv_"))
        import os

        old = os.environ.get("GANTRY_LOG_DIR")
        os.environ["GANTRY_LOG_DIR"] = str(tmp / "custom")
        try:
            d = logcfg.log_dir(ROOT)
        finally:
            if old is None:
                os.environ.pop("GANTRY_LOG_DIR", None)
            else:
                os.environ["GANTRY_LOG_DIR"] = old
        self.assertEqual(d, tmp / "custom", "GANTRY_LOG_DIR 应生效（便于把日志放到别处）")
        self.assertTrue(d.is_dir())

    def test_tray_uses_file_logger(self):
        """托盘动作要留痕，否则"点了没反应"没法查。"""
        for frag in ("托盘图标已添加", "托盘：启动自动化浏览器", "托盘：重启服务"):
            self.assertIn(frag, TRAY_SRC, f"托盘日志缺少：{frag}")


if __name__ == "__main__":
    unittest.main()
