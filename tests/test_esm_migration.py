# -*- coding: utf-8 -*-
"""app.js 拆成 ES 模块（ESM 迁移）的回归测试。

策略：增量迁移——拆出去的块进 `webui/js/<块>.js`（ES 模块），`js/main.js` 把老代码
仍在调用的全局函数绑到 `window`，事件路径的调用点不用动。
这里钉住第一步：示波器块（scope.js）+ 入口（main.js）+ 路由（/ui/js/*.js）。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

APP = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
MAIN = (ROOT / "webui" / "js" / "main.js").read_text(encoding="utf-8")
SCOPE = (ROOT / "webui" / "js" / "scope.js").read_text(encoding="utf-8")
HTML = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
ROUTES = (ROOT / "gantry" / "routes_core.py").read_text(encoding="utf-8")


class ModuleFilesTests(unittest.TestCase):
    def test_module_files_exist(self):
        for f in ("state.js", "main.js", "scope.js"):
            self.assertTrue((ROOT / "webui" / "js" / f).is_file(), f"缺少 {f}")

    def test_scope_block_moved_out(self):
        """示波器块必须整体搬走：app.js 不再定义它，scope.js 里有。"""
        for sym in ("function scopeFrame", "function startScope", "function stopScope"):
            self.assertNotIn(sym, APP, f"app.js 仍定义 {sym}")
            self.assertIn(sym.replace("function ", "function "), SCOPE)
        # 私有符号不许残留
        for sym in ("_scopeRAF", "_scopeStyle", "_scopeColors", "_scopeCtx", "_scopeSeeds"):
            self.assertNotIn(sym, APP, f"app.js 残留 {sym}")

    def test_scope_module_exports(self):
        self.assertIn("export { startScope, stopScope, scopeFrame }", SCOPE)

    def test_main_binds_globals(self):
        """老代码（app.js 事件路径）靠 window 上的绑定继续工作。"""
        for name in ("startScope", "stopScope", "scopeFrame"):
            self.assertIn(f"window.{name}", MAIN, f"main.js 没把 {name} 绑到 window")
        self.assertIn("from './scope.js'", MAIN)
        self.assertIn("from './state.js'", MAIN)


class RouteAndHtmlTests(unittest.TestCase):
    def test_index_loads_modules_and_boot(self):
        self.assertIn('<script src="boot.js"></script>', HTML,
                      "boot.js 之前是死代码（写了没加载），现在必须加载")
        self.assertIn('<script type="module" src="js/main.js"></script>', HTML)

    def test_module_route_allows_js_files(self):
        self.assertIn('re.fullmatch(r"js/[0-9A-Za-z._-]+\\.js"', ROUTES,
                      "/ui 资产路由要放行 js/*.js")
        self.assertIn("{asset:path}", ROUTES, "资产路由要支持子路径（js/main.js）")

    def test_module_route_still_blocks_traversal(self):
        self.assertIn('raise HTTPException(404', ROUTES)


class CallSitesTests(unittest.TestCase):
    def test_app_js_still_calls_scope_functions(self):
        """app.js 的调用点照旧（靠 window 绑定）。"""
        self.assertIn("startScope()", APP)
        self.assertIn("stopScope()", APP)


if __name__ == "__main__":
    unittest.main()
