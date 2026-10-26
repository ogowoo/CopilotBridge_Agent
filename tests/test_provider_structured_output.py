"""提供商结构化输出：能力映射、response_format 生成、不支持时自动降级。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.agent import AGENT_ACTION_SCHEMA
from gantry.providers import ApiProvider, JSON_MODE_BY_PROVIDER, build_providers


def mk(name="deepseek", **cfg):
    base = {"base_url": "https://example.com/v1", "api_key": "x", "models": ["m"]}
    base.update(cfg)
    return ApiProvider(name, base)


class StructuredOutputTests(unittest.TestCase):
    def test_json_mode_defaults_by_provider(self):
        provs = build_providers({"providers": {"deepseek": {}, "openai": {}}}).keys()
        self.assertIn("deepseek", provs)
        self.assertEqual(JSON_MODE_BY_PROVIDER["openai"], "json_schema")
        self.assertEqual(JSON_MODE_BY_PROVIDER["deepseek"], "json_object")
        # 未知提供商走 auto（先试 json_object，失败自动降级）
        self.assertEqual(mk("unknown-vendor").json_mode, "auto")

    def test_explicit_config_wins(self):
        provs = build_providers({"providers": {"deepseek": {"json_mode": "off"}}})
        self.assertEqual(provs["deepseek"].json_mode, "off")

    def test_json_hint_shapes(self):
        self.assertEqual(mk(json_mode="off")._json_hint(AGENT_ACTION_SCHEMA), {})
        self.assertEqual(mk(json_mode="auto")._json_hint(None), {})
        obj = mk(json_mode="json_object")._json_hint(AGENT_ACTION_SCHEMA)
        self.assertEqual(obj["response_format"]["type"], "json_object")
        sch = mk(json_mode="json_schema")._json_hint(AGENT_ACTION_SCHEMA)
        rf = sch["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        self.assertEqual(rf["json_schema"]["schema"]["properties"]["type"]["enum"],
                         ["tool", "final", "actions"])
        self.assertIn("type", rf["json_schema"]["schema"]["required"])

    def test_schema_is_flat_for_strict_mode(self):
        """strict 模式常不支持顶层 oneOf：我们的 schema 必须保持扁平。"""
        self.assertEqual(AGENT_ACTION_SCHEMA["type"], "object")
        self.assertNotIn("oneOf", AGENT_ACTION_SCHEMA)
        self.assertNotIn("anyOf", AGENT_ACTION_SCHEMA)


if __name__ == "__main__":
    unittest.main()
