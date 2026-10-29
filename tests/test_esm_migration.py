# -*- coding: utf-8 -*-
"""app.js → ES 模块（ESM 解耦）的**终态**回归测试。

迁移完成后 `webui/app.js` 已删除，前端结构固定为：
  · `webui/js/*.js`       ES 模块（业务逻辑），`js/main.js` 是入口，把函数绑到 window，
                          让没改过的老调用点（data-act / 内联闭包）继续工作；
  · `webui/js/globals.js` **经典脚本**：跨模块共享的可变状态（模块顶层 var 不是全局，
                          别的模块裸名访问会 ReferenceError，实测把会话列表整片吞掉）；
  · `webui/*.js`          boot.js（首屏主题）/ i18n.js（三语言）仍是经典脚本。
本测试钉住这四条结构性约束，避免"又长回单体"或"状态飘回模块私有"。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gantry.ui_src import ui_js  # noqa: E402

MAIN = (ROOT / "webui" / "js" / "main.js").read_text(encoding="utf-8")
GLOBALS = (ROOT / "webui" / "js" / "globals.js").read_text(encoding="utf-8")
HTML = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
ROUTES = (ROOT / "gantry" / "routes_core.py").read_text(encoding="utf-8")


class RetirementTests(unittest.TestCase):
    def test_app_js_is_gone(self):
        self.assertFalse((ROOT / "webui" / "app.js").exists(),
                         "app.js 应已退役：逻辑都在 js/*.js 里了")
        self.assertNotIn('src="app.js"', HTML, "index.html 不该再引用 app.js")

    def test_index_loads_globals_before_i18n_and_module(self):
        order = [HTML.index('<script src="js/globals.js"></script>'),
                 HTML.index('<script src="i18n.js"></script>'),
                 HTML.index('<script type="module" src="js/main.js"></script>')]
        self.assertEqual(order, sorted(order),
                         "顺序必须是 globals.js → i18n.js → js/main.js（i18n 切语言会读 globals 里的状态）")

    def test_globals_is_classic_script(self):
        """globals.js 必须是经典脚本：写成模块就没有全局词法绑定，模块裸名访问会炸。"""
        self.assertNotIn("import ", GLOBALS.split("\n")[0])
        self.assertNotIn("export ", GLOBALS)
        for name in ("lastSteps", "lastMessages", "sessSig", "serviceStopped", "current"):
            self.assertRegex(GLOBALS, rf"(?m)^var {name}\b", f"globals.js 应声明共享状态 {name}")
            self.assertRegex(GLOBALS, rf"(?m)^var {name}\b")

    def test_modules_do_not_redeclare_shared_state(self):
        """共享状态不许在模块里再声明一份（模块私有 = 别的模块读到 ReferenceError）。"""
        js = ui_js()
        for name in ("lastSteps", "lastMessages", "sessSig", "serviceStopped"):
            redecl = [ln for ln in js.splitlines()
                      if ln.startswith(("let " + name, "var " + name, "const " + name))]
            self.assertLessEqual(len(redecl), 1, f"{name} 被重复声明: {redecl}")

    def test_main_binds_every_exported_function(self):
        """每个模块导出的函数都要在 main.js 绑到 window（否则 data-act 静默失效）。

        例外：被别的模块 `import { … }` 直接引用的（如 stepFilterInput）不需要 window 绑定。
        """
        exports = set()
        for f in sorted((ROOT / "webui" / "js").glob("*.js")):
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.startswith("export {"):
                    for part in line[len("export {"):].rstrip(" };").split(","):
                        name = part.strip()
                        if name and name[0].islower() and "=>" not in name:
                            exports.add(name)
        imported = set()
        for f in sorted((ROOT / "webui" / "js").glob("*.js")):
            for m in re.finditer(r"import \{([^}]*)\}", f.read_text(encoding="utf-8")):
                imported |= {x.strip() for x in m.group(1).split(",") if x.strip()}
        missing = sorted(n for n in exports if f"window.{n}" not in MAIN and n not in imported)
        self.assertEqual(missing[:8], [], f"这些导出没绑到 window: {missing[:8]}")


class RouteTests(unittest.TestCase):
    def test_module_route_allows_js_and_css_files(self):
        # 正则现在同时放行 js/<名字>.js 与 css/<名字>.css（样式也按块拆开了）
        self.assertRegex(ROUTES, r're\.fullmatch\(r"\(\?:js\|css\)/',
                         "/ui 资产路由要放行 js/*.js 与 css/*.css")
        self.assertIn("{asset:path}", ROUTES, "资产路由要支持子路径（js/main.js）")

    def test_module_route_still_blocks_traversal(self):
        self.assertIn("raise HTTPException(404", ROUTES)


class NoBareSharedStateTests(unittest.TestCase):
    def test_cross_module_sharing_is_clean(self):
        """跨模块裸名共享必须合规（状态在 globals.js，函数 export + window 绑定）。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "cross_module_refs", ROOT / "scripts" / "cross_module_refs.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)          # type: ignore[union-attr]
        r = mod.analyze(ROOT / "webui")
        self.assertEqual(r["dup"], [], "同名重复声明")
        self.assertEqual(r["state"], [], "共享状态应放 globals.js")
        self.assertEqual(r["func"], [], "函数应 export + 绑 window")
        self.assertEqual(r.get("toplevel", []), [],
                         "模块顶层不许调用只靠 window 绑定的函数（import 阶段会 ReferenceError）")


class TopLevelWindowCallTests(unittest.TestCase):
    """模块顶层调用"只靠 window 绑定"的函数 → import 阶段必然 ReferenceError。

    真实事故两次：`mountTeamCard()`、`try{syncComposer()}catch(e){}`（后者被空 catch 吞掉，
    表现为未选会话时出现两个输入框）。
    """

    @staticmethod
    def _helper():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "cross_module_refs", ROOT / "scripts" / "cross_module_refs.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)          # type: ignore[union-attr]
        return mod.toplevel_window_calls

    def test_catches_boot_call(self):
        fn = self._helper()
        hits = fn("try{syncComposer()}catch(e){}", {"syncComposer"}, set())
        self.assertTrue(hits, "启动时的裸调用必须被抓到")

    def test_catches_plain_statement(self):
        fn = self._helper()
        self.assertTrue(fn("syncComposer();", {"syncComposer"}, set()))

    def test_ignores_callback_and_reference(self):
        fn = self._helper()
        bound = {"syncComposer"}
        for src in ("window.addEventListener('resize',()=>syncComposer());",
                    "setInterval(syncComposer,1500);",
                    "foo(syncComposer);"):
            self.assertFalse(fn(src, bound, set()), f"不该误报: {src}")

    def test_ignores_function_body(self):
        fn = self._helper()
        src = "function go(){\n syncComposer();\n}"
        self.assertFalse(fn(src, {"syncComposer"}, set()), "函数体里调用是安全的（事件路径）")

    def test_ignores_imported_and_local(self):
        fn = self._helper()
        src = "syncComposer();"
        self.assertFalse(fn(src, {"syncComposer"}, {"syncComposer"}), "已 import 的不算")
        self.assertFalse(fn("try{memBtn()}catch(e){}", {"memBtn"}, {"memBtn"}),
                         "本模块自己声明/import 的不算")


if __name__ == "__main__":
    unittest.main()
