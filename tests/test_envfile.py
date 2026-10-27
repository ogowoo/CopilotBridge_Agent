# -*- coding: utf-8 -*-
"""`.env` 支持的回归测试（用户要的：保持读系统环境变量的功能）。

钉住三件事：
1. **系统环境变量优先**：已存在的 os.environ 不会被 .env 覆盖（用户明确要求）；
2. **占位符解析**：config.json 的 `"${VAR}"` 换成环境变量，没设的保留原样并 warning；
3. **明文敏感字段警告**：key/token/secret/password 的明文值要能被点名出来。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gantry.envfile import load_env_file, resolve_placeholders, warn_plaintext_secrets


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        self.tmp = Path(tempfile.mkdtemp(prefix="gantry_env_"))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_system_env_wins_over_dotenv(self):
        """用户明确要求：系统环境变量的读取功能必须保持，且系统 > .env。"""
        (self.tmp / ".env").write_text("MY_KEY=from_dotenv\nOTHER=dotenv_only\n", encoding="utf-8")
        os.environ["MY_KEY"] = "from_system"
        n = load_env_file(self.tmp / ".env")
        self.assertEqual(os.environ["MY_KEY"], "from_system", "系统已有的不能被 .env 覆盖")
        self.assertEqual(os.environ["OTHER"], "dotenv_only", "系统没有的要补进来")
        self.assertEqual(n, 1)

    def test_parses_comments_quotes_export(self):
        (self.tmp / ".env").write_text(
            '# 注释\n'
            'A=plain\n'
            'B="quoted value"\n'
            "C='single'\n"
            'export D=with_export\n'
            'E=value  # 行内注释\n'
            '\n'
            'not_a_line\n', encoding="utf-8")
        load_env_file(self.tmp / ".env")
        self.assertEqual(os.environ["A"], "plain")
        self.assertEqual(os.environ["B"], "quoted value")
        self.assertEqual(os.environ["C"], "single")
        self.assertEqual(os.environ["D"], "with_export")
        self.assertEqual(os.environ["E"], "value")

    def test_missing_file_is_noop(self):
        self.assertEqual(load_env_file(self.tmp / "nope.env"), 0)

    def test_placeholder_resolution(self):
        os.environ["REAL_KEY"] = "abc123"
        cfg = {"api_token": "${REAL_KEY}",
               "nested": {"providers": [{"key": "${REAL_KEY}"}]},
               "unknown": "${NOT_SET_VAR}",
               "plain": "keep-me"}
        out = resolve_placeholders(cfg)
        self.assertEqual(out["api_token"], "abc123")
        self.assertEqual(out["nested"]["providers"][0]["key"], "abc123")
        self.assertEqual(out["unknown"], "${NOT_SET_VAR}", "没设的变量保留原样（不许静默置空）")
        self.assertEqual(out["plain"], "keep-me")

    def test_warn_plaintext_secrets(self):
        cfg = {"api_token": "hardcoded-secret",
               "providers": {"ds": {"api_key": "${OK}"}},   # 引用式不算明文
               "nested": {"password": "literal"}}
        hits = warn_plaintext_secrets(cfg)
        self.assertIn("api_token", hits)
        self.assertIn("nested.password", hits)
        self.assertNotIn("providers.ds.api_key", hits, "${} 引用不算明文")

    def test_bridge_loads_env_before_config(self):
        """bridge 的加载顺序必须是：.env → config.json（否则占位符没东西可解析）。"""
        src = (ROOT / "gantry" / "bridge.py").read_text(encoding="utf-8")
        i_env = src.index("load_env_file")
        i_cfg = src.index('json.loads((BASE / "config.json")')
        self.assertLess(i_env, i_cfg, ".env 必须先于 config.json 加载")
        self.assertIn("resolve_placeholders", src)


if __name__ == "__main__":
    unittest.main()
