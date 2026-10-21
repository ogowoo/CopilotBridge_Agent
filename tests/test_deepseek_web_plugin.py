# -*- coding: utf-8 -*-
"""deepseek-web 插件自测：能否被真实 PluginHost 加载、注册提供商、面板与接口可用。

用 httpx 打到一个**本地假的 OpenAI 兼容反代**（标准库起 HTTP server），
验证探测逻辑与模型前缀（dsweb/<model>）都正确。
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _FakeProxy(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容反代：/v1/models 返回两个模型。"""

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({"object": "list", "data": [
                {"id": "deepseek-default"}, {"id": "deepseek-reasoner"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):  # 静音
        pass


class PluginLoadTests(unittest.TestCase):
    def setUp(self):
        os.environ["CBP_PLUGIN_NO_TIMERS"] = "1"
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_dsweb_"))
        srv = HTTPServer(("127.0.0.1", 0), _FakeProxy)
        self.port = srv.server_address[1]
        self.srv = srv
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    def tearDown(self):
        try:
            self.srv.shutdown()
        except Exception:
            pass

    def test_probe_normalize_and_prefix(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dsweb_plugin", ROOT / "plugins" / "deepseek-web" / "plugin.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        res = asyncio.run(mod.probe(f"http://127.0.0.1:{self.port}/v1", "sk-x"))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["models"], ["deepseek-default", "deepseek-reasoner"])
        self.assertEqual(mod.normalize_models({"data": [{"id": "a"}, {"name": "b"}, "c"]}),
                         ["a", "b", "c"])
        self.assertEqual(mod.normalize_models("nonsense"), [])

        # 反代没起时：ok=False，但有可读错误
        bad = asyncio.run(mod.probe("http://127.0.0.1:9/v1", "sk-x", timeout=1.0))
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["error"])

    def test_setup_registers_provider_panel_and_api(self):
        from cbp.plugins import PluginHost
        from cbp.providers import ApiProvider

        host = PluginHost(ROOT, registry=None, providers={},
                          provider_factory=lambda name, cfg: ApiProvider(name, cfg))
        # 把插件目录指到临时目录：只放这一个插件，避免加载仓库里其它插件
        plug_dir = self.tmp / "deepseek-web"
        plug_dir.mkdir()
        (plug_dir / "plugin.py").write_text(
            (ROOT / "plugins" / "deepseek-web" / "plugin.py").read_text(encoding="utf-8"),
            encoding="utf-8")
        manifest = json.loads((ROOT / "plugins" / "deepseek-web" / "plugin.json")
                              .read_text(encoding="utf-8"))
        manifest["cbp"]["config"]["base_url"] = f"http://127.0.0.1:{self.port}/v1"
        (plug_dir / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                              encoding="utf-8")
        host.plugin_root = self.tmp

        info = host.load("deepseek-web")
        self.assertEqual(str(info.get("status")), "loaded", info)
        self.assertIn("dsweb", host.providers, "提供商没注册上")
        prov = host.providers["dsweb"]
        self.assertTrue(str(prov.base_url).endswith("/v1"))
        self.assertIn("deepseek-reasoner", prov.models)

        panels = host.panels()
        self.assertTrue(any(p.get("plugin") == "deepseek-web" and p.get("id") == "status"
                            for p in panels), panels)

        handler = host.routes.get(("deepseek-web", "GET", "api/status"))
        self.assertIsNotNone(handler, "接口没注册上")
        out = asyncio.run(handler())
        self.assertTrue(out["reachable"], out)
        self.assertEqual(out["model_ids"],
                         ["dsweb/deepseek-default", "dsweb/deepseek-reasoner"])

    def test_setup_with_dead_proxy_registers_no_models(self):
        """反代没起：提供商仍注册（便于面板诊断），但**不列出模型**。"""
        from cbp.plugins import PluginHost
        from cbp.providers import ApiProvider

        host = PluginHost(ROOT, registry=None, providers={},
                          provider_factory=lambda name, cfg: ApiProvider(name, cfg))
        plug_dir = self.tmp / "deepseek-web"
        plug_dir.mkdir(exist_ok=True)
        (plug_dir / "plugin.py").write_text(
            (ROOT / "plugins" / "deepseek-web" / "plugin.py").read_text(encoding="utf-8"),
            encoding="utf-8")
        manifest = json.loads((ROOT / "plugins" / "deepseek-web" / "plugin.json")
                              .read_text(encoding="utf-8"))
        manifest["cbp"]["config"]["base_url"] = "http://127.0.0.1:9/v1"   # 必然连不上
        (plug_dir / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                              encoding="utf-8")
        host.plugin_root = self.tmp
        host.load("deepseek-web")
        self.assertIn("dsweb", host.providers)
        self.assertEqual(list(host.providers["dsweb"].models), [],
                         "反代不可达时不应把模型列出来")


if __name__ == "__main__":
    unittest.main()
