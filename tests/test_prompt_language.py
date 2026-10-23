# -*- coding: utf-8 -*-
"""发给 Copilot 的提示词要支持简体 / 繁體 / English。

用户："发给 copilot 的提示词是不是也可以同时支持简体/繁体/英文的？这样应该更符合多语言环境"

## 为什么必须跟界面语言一致
提示词的语言基本决定了模型的**回话语言**。以前界面三语、提示词永远是中文：
英文用户问英文，Agent 收到中文规则、被要求中文回话，于是回答也是中文；
繁体用户更别扭（简体系统提示词 + 繁体界面）。

## 实现
- 语言来源：`config.agent.prompt_language`（`auto`/`zh`/`tw`/`en`）优先,
  `auto` 时跟随当次请求的 `X-CBP-Lang`（API 中间件写进 contextvar）;
- 四段指令（协议 / 子代理分工 / 交付物表述 / 文件操作纪律）+ "回答语言"指令
  + "文件分流"一句，都按语言取；中文原文仍是唯一真源，英文是译本，
  繁体由 `cbp/s2t_data.py` 按字转换（该表**由 `webui/i18n.js` 的 S2T_CHARS 生成**）;
- `PromptRegistry.add()` 支持**零参可调用对象**,渲染时才求值（语言是 contextvar,
  import 时还不知道）。
"""
import re
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from cbp import prompt, prompt_lang  # noqa: E402
from cbp.s2t_data import S2T  # noqa: E402

API = ((BASE / "cbp" / "api.py").read_text(encoding="utf-8") + "".join((BASE / "cbp" / n).read_text(encoding="utf-8") for n in ("routes_core.py", "routes_config.py", "routes_agents.py", "routes_sessions.py")))
AGENT = (BASE / "cbp" / "agent.py").read_text(encoding="utf-8")


class ResolveTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(prompt_lang.normalize("zh"), "zh")
        self.assertEqual(prompt_lang.normalize("zh-CN"), "zh")
        self.assertEqual(prompt_lang.normalize("zh-Hant"), "tw")
        self.assertEqual(prompt_lang.normalize("en-US"), "en")
        self.assertEqual(prompt_lang.normalize(None), "zh")
        self.assertEqual(prompt_lang.normalize("nonsense"), "zh")

    def test_auto_follows_request(self):
        try:
            prompt_lang.set_request_lang("en")
            self.assertEqual(prompt_lang.effective_lang(None), "en")
            self.assertEqual(prompt_lang.effective_lang("auto"), "en")
            # 配置显式指定时压过请求
            self.assertEqual(prompt_lang.effective_lang("zh"), "zh")
            self.assertEqual(prompt_lang.effective_lang("tw"), "tw")
        finally:
            prompt_lang.set_request_lang("zh")

    def test_directive_three_languages(self):
        self.assertIn("简体中文", prompt_lang.directive("zh"))
        self.assertIn("繁體中文", prompt_lang.directive("tw"))
        self.assertIn("English", prompt_lang.directive("en"))


class LocalizeTests(unittest.TestCase):
    def test_protocol_per_language(self):
        try:
            prompt_lang.set_request_lang("zh")
            zh = prompt.agent_protocol()
            prompt_lang.set_request_lang("en")
            en = prompt.agent_protocol()
            prompt_lang.set_request_lang("tw")
            tw = prompt.agent_protocol()
        finally:
            prompt_lang.set_request_lang("zh")
        self.assertIn("你是本地任务 Agent", zh)
        self.assertIn("You are a local task agent", en)
        self.assertIn("本地任務 Agent", tw)
        # 工具名/参数不能被翻译
        for text in (zh, en, tw):
            self.assertIn("read_text", text)
            self.assertIn("write_text_b64", text)

    def test_all_four_blocks_have_english(self):
        for name, zh_text in (
            ("protocol", prompt.AGENT_PROTOCOL),
            ("team", prompt.AGENT_TEAM),
            ("deliverable", prompt.DELIVERABLE_RULE),
            ("file_ops", prompt.FILE_OPS_RULE),
        ):
            en = prompt_lang.EN.get(name) or ""
            self.assertTrue(en, f"{name} 缺少英文译本")
            self.assertFalse(re.search(r"[\u4e00-\u9fff]", en),
                             f"{name} 的英文译本里还混着中文")
            # 原文里的关键工具名要在译本里出现，避免翻丢规则
            for ident in ("read_text", "replace_text"):
                if ident in zh_text:
                    self.assertIn(ident, en)

    def test_routing_follows_language(self):
        zh_text = "本会话走 API 提供商"
        self.assertIn("API provider", prompt_lang.routing_text(False, zh_text, "en"))
        self.assertEqual(prompt_lang.routing_text(False, zh_text, "zh"), zh_text)
        self.assertIn("提供商", prompt_lang.routing_text(False, zh_text, "tw"))


class TraditionalTests(unittest.TestCase):
    def test_table_generated_from_frontend(self):
        self.assertGreater(len(S2T), 300, "简繁对照表条目过少，可能没从 i18n.js 生成")
        gen = BASE / "scripts" / "gen_s2t.py"
        self.assertTrue(gen.is_file(), "缺少生成脚本，表就无从复现")

    def test_convert(self):
        self.assertEqual(prompt_lang.to_tw("设置时间"), "設置時間")
        self.assertEqual(prompt_lang.to_tw("abc123"), "abc123", "英文/数字不应被改")
        self.assertEqual(prompt_lang.to_tw(""), "")

    def test_tw_differs_from_zh(self):
        zh = prompt.DELIVERABLE_RULE
        tw = prompt_lang.to_tw(zh)
        self.assertNotEqual(zh, tw, "繁体版必须真的变了")
        self.assertEqual(len(zh), len(tw), "按字转换不应改变长度")


class RegistryCallableTests(unittest.TestCase):
    def test_render_resolves_callable(self):
        reg = prompt.PromptRegistry()
        state = {"lang": "zh"}
        reg.add("a", lambda: "英文-PROTOCOL" if state["lang"] == "en" else "中文-PROTOCOL", order=0)
        self.assertIn("中文", reg.render())
        state["lang"] = "en"
        self.assertIn("英文", reg.render(), "可调用 section 应在渲染时求值")

    def test_add_rejects_bad_text(self):
        reg = prompt.PromptRegistry()
        with self.assertRaises(prompt.PromptError):
            reg.add("bad", 123)  # type: ignore[arg-type]

    def test_agent_registers_callables_and_directive(self):
        self.assertIn('reg.add("language", language_directive, order=-2000)', AGENT)
        for name, fn in (("protocol", "agent_protocol"), ("file_ops", "file_ops_rule"),
                         ("deliverable", "deliverable_rule"), ("team", "agent_team")):
            self.assertIn(f'reg.add("{name}", {fn}', AGENT,
                          f"{name} 必须注册**函数**（否则语言在 import 时就固定了）")


class ApiWiringTests(unittest.TestCase):
    def test_middleware_sets_lang(self):
        self.assertIn("_prompt_lang_middleware", API)
        self.assertIn("prompt_lang.set_request_lang", API)

    def test_config_entry(self):
        self.assertIn('"agent.prompt_language"', API)
        self.assertIn('["auto", "zh", "tw", "en"]', API)


class WholePromptTests(unittest.TestCase):
    """整段提示词：英文模式下中文占比必须明显下降（剩余是工具描述等结构化内容）。"""

    def _build(self, lang: str) -> str:
        from cbp import bridge as b
        from cbp.agent import AgentRunner
        import tempfile
        from cbp.session_store import SessionStore
        from cbp.snapshots import SnapshotStore
        tmp = Path(tempfile.mkdtemp())
        saved = (b.AGENT_SESSIONS, b.SNAPSHOTS)
        try:
            b.AGENT_SESSIONS = SessionStore(tmp / "a.db")
            b.SNAPSHOTS = SnapshotStore(tmp / "s")
            b.AGENT_TOOLS.set_root(tmp, False)
            prompt_lang.set_request_lang(lang)
            runner = AgentRunner(b.AGENT_TOOLS, None, b.AGENT_CFG,
                                 state_getter=lambda: {}, state_saver=lambda **k: None,
                                 step_saver=lambda *a, **k: None,
                                 file_event_saver=lambda *a, **k: None)
            return runner._system_prompt()
        finally:
            b.AGENT_SESSIONS, b.SNAPSHOTS = saved
            prompt_lang.set_request_lang("zh")

    def test_english_ratio_drops(self):
        cjk = re.compile(r"[\u4e00-\u9fff]")
        zh = self._build("zh")
        en = self._build("en")
        rz = len(cjk.findall(zh)) / max(1, len(zh))
        re_ = len(cjk.findall(en)) / max(1, len(en))
        self.assertLess(re_, rz, "英文模式的提示词中文占比必须下降")
        self.assertLess(re_, 0.06, f"英文模式中文占比仍然偏高: {re_:.0%}")
        self.assertIn("[Answer language] Reply in English", en, "回答语言指令必须在最前")
        self.assertTrue(en.strip().startswith("[Answer language]"))


if __name__ == "__main__":
    unittest.main()


class ToolDescriptionTests(unittest.TestCase):
    """工具描述/参数提示也要三语：模型是**读这些描述**挑工具的。"""

    @classmethod
    def setUpClass(cls):
        import tempfile
        from cbp import bridge as b
        from cbp.session_store import SessionStore
        from cbp.snapshots import SnapshotStore
        cls.tmp = Path(tempfile.mkdtemp())
        cls.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS)
        b.AGENT_SESSIONS = SessionStore(cls.tmp / "a.db")
        b.SNAPSHOTS = SnapshotStore(cls.tmp / "s")
        b.AGENT_TOOLS.set_root(cls.tmp, False)
        cls.tools = b.AGENT_TOOLS
        cls.bridge = b

    @classmethod
    def tearDownClass(cls):
        cls.bridge.AGENT_SESSIONS, cls.bridge.SNAPSHOTS = cls.saved
        prompt_lang.set_request_lang("zh")

    def test_no_chinese_left_in_english_mode(self):
        cjk = re.compile(r"[\u4e00-\u9fff]")
        prompt_lang.set_request_lang("en")
        items = self.tools.descriptions()
        self.assertGreater(len(items), 20, "工具清单为空？")
        bad = []
        for x in items:
            d = str(x.get("description") or "")
            a = " ".join(str(v) for v in (x.get("arguments") or {}).values())
            if cjk.search(d) or cjk.search(a):
                bad.append(x["name"])
        self.assertEqual(bad, [], f"英文模式下这些工具的描述还是中文: {bad}")

    def test_every_tool_and_argument_is_covered(self):
        """新增工具/参数必须同时补英文，否则会在英文界面悄悄露出中文。"""
        from cbp.tool_i18n import ARG_EN, DESC_EN
        items = self.tools.descriptions()
        missing_desc = [x["name"] for x in items if x["name"] not in DESC_EN]
        missing_args = [f'{x["name"]}.{k}' for x in items
                        for k in (x.get("arguments") or {}) if f'{x["name"]}.{k}' not in ARG_EN]
        self.assertEqual(missing_desc, [], "这些工具缺英文描述")
        self.assertEqual(missing_args, [], "这些参数缺英文提示")

    def test_identifiers_untouched(self):
        prompt_lang.set_request_lang("en")
        items = self.tools.descriptions()
        names = {x["name"] for x in items}
        self.assertIn("read_text", names)
        self.assertIn("subagent_many", names)
        for x in items:            # 参数名不能被翻译
            for k in (x.get("arguments") or {}):
                self.assertRegex(k, r"^[a-z_][a-z0-9_]*$")

    def test_chinese_mode_unchanged(self):
        prompt_lang.set_request_lang("zh")
        zh = {x["name"]: x["description"] for x in self.tools.descriptions()}
        prompt_lang.set_request_lang("en")
        en = {x["name"]: x["description"] for x in self.tools.descriptions()}
        self.assertNotEqual(zh["read_text"], en["read_text"], "英文模式必须真的换成英文")
        prompt_lang.set_request_lang("zh")
        again = {x["name"]: x["description"] for x in self.tools.descriptions()}
        self.assertEqual(zh, again, "切回中文必须还原原文")

    def test_traditional_conversion(self):
        prompt_lang.set_request_lang("tw")
        items = {x["name"]: x["description"] for x in self.tools.descriptions()}
        joined = "".join(items.values())
        # 具体词断言（按转换器实际输出，不猜字）：转换→轉換、缩进→縮進
        self.assertIn("轉換", joined)
        self.assertIn("縮進", joined)
        self.assertNotIn("转换", joined, "繁体模式下不该再有简体『转换』")
        self.assertNotIn("缩进", joined, "繁体模式下不该再有简体『缩进』")
        # 多音字按词转：复杂→複雜（不是「復雜」）
        self.assertIn("複雜", joined)
        self.assertNotIn("復雜", joined)
