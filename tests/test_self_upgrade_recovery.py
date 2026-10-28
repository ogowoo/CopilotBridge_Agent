# -*- coding: utf-8 -*-
"""自升级任务 task-mvs4un2x 的失败复盘 → 回归测试。

那次任务（120 步）的失败分布：

| 现象 | 次数 | 真因 |
| --- | --- | --- |
| `self_read_text() got an unexpected keyword argument 'line_start'` | 1 | 普通 `read_text` 支持按行读，`self_read_text` 只支持字符偏移——模型按直觉传参就 TypeError |
| `invalid_action`（模型写了合法意图但 JSON 解析失败） | 10 | ① 字符串值里未转义的 `"`（`type==="bool"`）；② 正则里的 `\(` 不是合法 JSON 转义 |
| `self_replace_text 未找到要替换的原文本` | 6 | 模型凭记忆拼 `old`，差一个空格就失败，只能反复重读重拼 |
| `list_directory('..') [sandbox: FS_SANDBOX_ESCAPE]` | 1 | 自升级模式下普通文件工具仍锁在工作区，报错没告诉它该用 self_* 工具 |
| `read_text: 文件不存在 webui/app.js` | 2 | 前端已拆成 `webui/js/*.js`（本次重构），旧路径自然不存在 |

本文件钉住前四类（第五类是重构后的既成事实，靠工具文档与 self_find_files 自查）。
"""
import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gantry.agent_parse import ParseMixin  # noqa: E402
from gantry.tools import ToolRegistry, ToolError  # noqa: E402

# ── 真实失败原文（摘自该会话的 raw_tail）──────────────────────────────────
RAW_UNESCAPED_QUOTE = (
    '{"type":"tool","tool":"self_search_text","arguments":'
    '{"query":"function _cfgInput|checkbox|type==="bool"","path":"webui/app.js",'
    '"max_results":60},"reason":"定位设置页布尔项渲染函数"}')
RAW_REGEX_BACKSLASH = (
    '{"type":"tool","tool":"self_search_text","arguments":'
    '{"query":"startScope\\(|stopScope\\(|scopeFrame\\(|teamScope","path":"webui/js",'
    '"max_results":80},"reason":"定位运行时刷新链路"}')


class ActionParseRepairTests(unittest.TestCase):
    def test_unescaped_quotes_recovered(self):
        """字符串值里的 `"` 没转义 → 以前整条报 invalid_action（这一步白烧一个回合）。"""
        act = ParseMixin._parse(RAW_UNESCAPED_QUOTE)
        self.assertEqual(act.get("type"), "tool")
        self.assertEqual(act.get("tool"), "self_search_text")
        self.assertIn('type==="bool"', act["arguments"]["query"])

    def test_regex_backslash_recovered(self):
        """`\\(` 不是合法 JSON 转义 → 修复后仍保留正则语义（反斜杠不能丢）。"""
        act = ParseMixin._parse(RAW_REGEX_BACKSLASH)
        self.assertEqual(act.get("tool"), "self_search_text")
        self.assertEqual(act["arguments"]["query"],
                         r"startScope\(|stopScope\(|scopeFrame\(|teamScope")

    def test_legal_json_untouched(self):
        """正常动作不能被修复逻辑改坏（转义、路径、正则混在一起）。"""
        raw = ('{"type":"tool","tool":"write_text_b64","arguments":'
               '{"path":"a/b.py","content_b64":"aGVsbG8="},'
               '"reason":"换行\\n要保留，引号\\"也要保留"}')
        act = ParseMixin._parse(raw)
        self.assertEqual(act["tool"], "write_text_b64")
        self.assertEqual(act["arguments"]["path"], "a/b.py")
        self.assertIn('换行\n要保留', act["reason"])
        self.assertIn('引号"也要保留', act["reason"])


class SelfTextToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_recover_"))
        self.install = self.tmp / "install"
        (self.install / "gantry").mkdir(parents=True)
        (self.install / "data").mkdir()
        self.mod = self.install / "gantry" / "mod.py"
        self.mod.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.tools = ToolRegistry(self.ws, {"allow_self_upgrade": True})
        self.tools.set_global_root(self.install)

    def test_read_text_accepts_line_window(self):
        """普通 read_text 有 line_start/line_count，self_read_text 也必须认。"""
        r = asyncio.run(self.tools.self_read_text("gantry/mod.py", line_start=2, line_count=2))
        self.assertIn("2: line2", r["lines"])
        self.assertIn("3: line3", r["lines"])
        self.assertNotIn("line1", r["lines"])
        self.assertEqual(r["total_lines"], 4)
        self.assertTrue(r["numbered"])

    def test_read_text_char_window_still_works(self):
        # self_read_text 用 newline="" 打开（保留 CRLF，替换时才不会悄悄改行尾），
        # 所以字符偏移要按**原始字节解码后**的位置算，不能用 read_text 的翻译结果。
        raw = self.mod.read_bytes().decode("utf-8")
        offset = raw.index("line2")
        r = asyncio.run(self.tools.self_read_text("gantry/mod.py", offset=offset, limit=5))
        self.assertEqual(r["content"], "line2")
        self.assertEqual(r["total_lines"], 4)

    def test_replace_tolerates_crlf_and_indent(self):
        """文件是 CRLF、old 是 LF 且行首缩进被重排过 → 仍应替换成功。"""
        self.mod.write_bytes("def f():\n    return 42\n".replace("\n", "\r\n").encode())
        r = asyncio.run(self.tools.self_replace_text(
            "gantry/mod.py", "def f():\n    return 42", "def f():\n    return 43"))
        self.assertEqual(r["replaced"], 1)
        self.assertIn("return 43", self.mod.read_text(encoding="utf-8"))

    def test_replace_failure_reports_nearest_source(self):
        """找不到原文时，必须把**最接近的真实行**递回去，模型一步就能改对。"""
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(self.tools.self_replace_text(
                "gantry/mod.py", "line2\nline3x\nline4", "x"))
        msg = str(ctx.exception)
        self.assertIn("最接近的原文", msg)
        self.assertRegex(msg, r"第 \d+")
        self.assertIn("line2", msg)          # 真实原文片段
        self.assertIn("line_start=", msg)    # 并给出可复制的读取参数


class SandboxHintTests(unittest.TestCase):
    def test_self_upgrade_hint_points_to_self_tools(self):
        """自升级模式下普通文件工具越界时，报错要指明该用 self_* 工具。"""
        tmp = Path(tempfile.mkdtemp(prefix="cbp_hint_"))
        ws = tmp / "ws"
        ws.mkdir()
        tools = ToolRegistry(ws, {"allow_self_upgrade": True})
        tools.set_global_root(tmp / "install")
        msg = tools._sandbox_denied("FS_SANDBOX_ESCAPE", "..", "越界")
        self.assertIn("self_find_files", msg)
        self.assertIn("self_read_text", msg)
        # 没开自升级时不该出现这句（否则误导普通任务）
        plain = ToolRegistry(ws, {})
        plain.set_global_root(tmp / "install")
        msg2 = plain._sandbox_denied("FS_SANDBOX_ESCAPE", "..", "越界")
        self.assertNotIn("self_find_files", msg2)


class PendingMarkerGateTests(unittest.TestCase):
    """启动时的"待生效自升级"自检门。

    真事（2026-10-29 07:06）：任务被取消，半成品留在盘上（settings.js 读 4 个从没加过
    的元素 id）；启动只把标记改名 `.applied` 并打印"已加载上一轮自升级改动"——
    坏代码被无声当成成功。现在：自检不过就**保留**标记。
    """

    def _marker(self, rel: str) -> tuple[Path, str]:
        tmp = Path(tempfile.mkdtemp(prefix="cbp_pending_"))
        (tmp / "data").mkdir()
        marker = tmp / "data" / ".self_upgrade_pending"
        marker.write_text(f"1793228809.0\t{rel}\n", encoding="utf-8")
        return tmp, str(marker)

    def test_syntax_error_blocks_archive(self):
        tmp, _ = self._marker("gantry/broken.py")
        (tmp / "gantry").mkdir(exist_ok=True)
        (tmp / "gantry" / "broken.py").write_text("def f(:\n  pass\n", encoding="utf-8")
        from gantry import bridge
        old_base, old_marker = bridge.BASE, bridge.SELF_UPGRADE_MARKER
        bridge.BASE = tmp
        bridge.SELF_UPGRADE_MARKER = tmp / "data" / ".self_upgrade_pending"
        bridge.log = lambda *a, **k: None
        try:
            issues = bridge.validate_pending_self_upgrade()
            self.assertTrue(any("语法错误" in x for x in issues), issues)
            res = bridge.mark_self_upgrade_applied()
            self.assertEqual(res["applied"], [])
            self.assertTrue(res["issues"])
            self.assertTrue(bridge.SELF_UPGRADE_MARKER.is_file(),
                            "自检不过时标记必须保留，否则会被当成已生效")
        finally:
            bridge.BASE, bridge.SELF_UPGRADE_MARKER = old_base, old_marker

    def test_missing_file_blocks_archive(self):
        tmp, _ = self._marker("gantry/nope.py")
        from gantry import bridge
        old_base, old_marker = bridge.BASE, bridge.SELF_UPGRADE_MARKER
        bridge.BASE = tmp
        bridge.SELF_UPGRADE_MARKER = tmp / "data" / ".self_upgrade_pending"
        bridge.log = lambda *a, **k: None
        try:
            issues = bridge.validate_pending_self_upgrade()
            self.assertTrue(any("不存在" in x for x in issues), issues)
        finally:
            bridge.BASE, bridge.SELF_UPGRADE_MARKER = old_base, old_marker

    def test_missing_element_id_blocks_archive(self):
        """半成品前端改动（读了不存在的元素 id）→ 自检报出来，且标记保留。"""
        tmp, _ = self._marker("webui/js/settings.js")
        (tmp / "webui" / "js").mkdir(parents=True)
        (tmp / "webui" / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
        # 模拟事故：settings.js 读 setTeamScopeAmp，但页面里从来没有这个元素
        (tmp / "webui" / "js" / "settings.js").write_text(
            "const a=document.getElementById('setTeamScopeAmp');\n", encoding="utf-8")
        from gantry import bridge
        old_base, old_marker = bridge.BASE, bridge.SELF_UPGRADE_MARKER
        bridge.BASE = tmp
        bridge.SELF_UPGRADE_MARKER = tmp / "data" / ".self_upgrade_pending"
        bridge.log = lambda *a, **k: None
        try:
            issues = bridge.validate_pending_self_upgrade()
            self.assertTrue(any("setTeamScopeAmp" in x for x in issues), issues)
            res = bridge.mark_self_upgrade_applied()
            self.assertTrue(res["issues"])
            self.assertTrue(bridge.SELF_UPGRADE_MARKER.is_file())
        finally:
            bridge.BASE, bridge.SELF_UPGRADE_MARKER = old_base, old_marker

    def test_clean_pending_is_archived(self):
        tmp, _ = self._marker("gantry/ok.py")
        (tmp / "gantry").mkdir(exist_ok=True)
        (tmp / "gantry" / "ok.py").write_text("x = 1\n", encoding="utf-8")
        (tmp / "webui").mkdir(exist_ok=True)
        (tmp / "webui" / "index.html").write_text("<html></html>", encoding="utf-8")
        from gantry import bridge
        old_base, old_marker = bridge.BASE, bridge.SELF_UPGRADE_MARKER
        bridge.BASE = tmp
        bridge.SELF_UPGRADE_MARKER = tmp / "data" / ".self_upgrade_pending"
        bridge.log = lambda *a, **k: None
        try:
            res = bridge.mark_self_upgrade_applied()
            self.assertEqual(res["issues"], [])
            self.assertEqual(len(res["applied"]), 1)
            self.assertFalse((tmp / "data" / ".self_upgrade_pending").exists())
            self.assertTrue((tmp / "data" / ".self_upgrade_pending.applied").exists())
        finally:
            bridge.BASE, bridge.SELF_UPGRADE_MARKER = old_base, old_marker


if __name__ == "__main__":
    unittest.main()
