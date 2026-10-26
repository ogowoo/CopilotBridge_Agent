# -*- coding: utf-8 -*-
"""明文写入策略：文档可明文，代码必须 b64 分块。

来自真实时间线（`for debug/gantry-task-mveq9t5g-timeline-*.json`）:模型用 PowerShell
heredoc 写了 5 次 20KB 级的 Markdown（step 60–62、70–78），原因之一是提示词要求
"代码/JSON/HTML/Markdown 一律 b64"——让模型为一份报告产 27KB Base64，太贵，
于是它绕道 shell，最后被 PowerShell 的编码坑咬（step 144 → 149 整文件乱码）。

策略因此分两类：
- **代码类**（.py/.json/.html/.css/... 超过 `plain_code_limit`=1500 字符）→ 拒绝
  明文，要求 b64 分块（源码塞进 JSON 会被转义/截断搞坏）；
- **文档类**（.md/.txt/.log/.csv …）→ 允许明文写，长文分块；
- **b64 路径永远放行**（它本来就是代码大文件的正路）。
"""
import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.tools import ToolError, ToolRegistry


class PlainTextPolicyTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cbp_plain_"))
        self.r = ToolRegistry(self.ws, {})

    def _write(self, name, content):
        return asyncio.run(self.r.write_text(name, content))

    # ── 文档类：允许明文 ────────────────────────────────────────────
    def test_markdown_plain_write_allowed(self):
        body = "# 报告\n" + "内容段落。\n" * 800          # ≈ 5.6k 字符
        out = self._write("report.md", body)
        self.assertEqual(out["written_chars"], len(body))

    def test_txt_and_log_allowed(self):
        self.assertEqual(self._write("a.txt", "x" * 4000)["written_chars"], 4000)
        self.assertEqual(self._write("run.log", "y\n" * 3000)["written_chars"], 6000)

    def test_markdown_plain_append_allowed(self):
        asyncio.run(self.r.append_text("d.md", "第一段\n" * 500))
        asyncio.run(self.r.append_text("d.md", "第二段\n" * 500))
        self.assertGreater((self.ws / "d.md").stat().st_size, 4000)

    # ── 代码类：超限拒绝 ───────────────────────────────────────────
    def test_code_plain_write_over_limit_rejected(self):
        with self.assertRaises(ToolError) as ctx:
            self._write("big.py", "x = 1\n" * 400)       # 2400 字符
        msg = str(ctx.exception)
        self.assertIn("write_text_b64", msg, "错误信息要指路到 b64")
        self.assertIn("1500", msg)

    def test_code_plain_append_over_limit_rejected(self):
        self._write("s.py", "print(1)\n")
        with self.assertRaises(ToolError):
            asyncio.run(self.r.append_text("s.py", "print(2)\n" * 300))

    def test_short_code_file_allowed(self):
        """短文件仍可明文写（提示词允许"新文件或完整短文件"）。"""
        self.assertEqual(self._write("small.py", "print('hi')\n" * 20)["written_chars"], 240)

    def test_code_extensions_covered(self):
        for name in ("a.json", "b.html", "c.css", "d.yml", "e.ps1", "f.ts"):
            with self.assertRaises(ToolError, msg=f"{name} 应受明文限制"):
                self._write(name, "z" * 2000)

    # ── b64 路径：永远放行 ─────────────────────────────────────────
    def test_b64_write_bypasses_limit(self):
        big = "y = 2\n" * 800                            # 4800 字符
        out = asyncio.run(self.r.write_text_b64(
            "big2.py", base64.b64encode(big.encode()).decode()))
        self.assertEqual(out["written_chars"], len(big))
        self.assertEqual((self.ws / "big2.py").read_text(encoding="utf-8"), big)

    def test_b64_append_bypasses_limit(self):
        chunk = "z = 3\n" * 500
        asyncio.run(self.r.write_text_b64("m.py", base64.b64encode(b"a=1\n").decode()))
        asyncio.run(self.r.append_text_b64("m.py", base64.b64encode(chunk.encode()).decode()))
        self.assertTrue((self.ws / "m.py").read_text(encoding="utf-8").endswith(chunk))

    # ── 可配置 ────────────────────────────────────────────────────
    def test_limit_is_configurable(self):
        r2 = ToolRegistry(self.ws, {"plain_code_limit": 50})
        with self.assertRaises(ToolError):
            asyncio.run(r2.write_text("t.py", "a=1\n" * 20))


class PromptPolicyWordingTests(unittest.TestCase):
    def test_prompt_mentions_docs_exemption(self):
        from gantry.prompt import AGENT_PROTOCOL
        self.assertIn("文档类", AGENT_PROTOCOL)
        self.assertIn(".md", AGENT_PROTOCOL)
        self.assertIn("4000", AGENT_PROTOCOL, "文档明文的单次上限要写出来")
        self.assertIn("1500", AGENT_PROTOCOL, "代码明文的硬上限要写出来")

    def test_tool_description_states_split(self):
        ws = Path(tempfile.mkdtemp(prefix="cbp_desc_"))
        reg = ToolRegistry(ws, {})
        descs = {d["name"]: d["description"] for d in reg.descriptions()}
        self.assertIn("文档类", descs["write_text"])
        self.assertIn("b64", descs["append_text"])


if __name__ == "__main__":
    unittest.main()
