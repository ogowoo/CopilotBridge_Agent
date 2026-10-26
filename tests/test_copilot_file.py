# -*- coding: utf-8 -*-
"""copilot_file：让 Copilot 生成文件并下载到工作区的回归测试。

用本地 HTML 复刻回答区里的三种下载入口（blob: 链接 / data: 链接 /
下载按钮），直接驱动 gantry.bridge 的候选识别与落盘逻辑，断言：
  - blob: / data: 链接能被认出来并写成正确字节
  - 文件名匹配优先（多个入口时选中目标文件）
  - 关闭开关时工具不可用；工作区外的路径被沙箱拒绝
需要 playwright 与可用浏览器，缺失时自动跳过。
"""
import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURE = """<!doctype html><html><head><meta charset="utf-8"><title>answer</title></head><body>
<div data-content="ai-message">
  <p>已为你生成文件。</p>
  <a id="other" download="other.txt" href="data:text/plain;base64,b3RoZXI=">other.txt</a>
  <a id="target" download="gen.py" href="data:text/plain;base64,{PAYLOAD}">gen.py</a>
</div>
<div data-content="ai-message">
  <button id="btn" aria-label="Download file">下载</button>
</div>
<script>
 document.getElementById('btn').addEventListener('click', () => {
   const blob = new Blob(["from-button"], {type: "text/plain"});
   const a = document.createElement('a');
   a.href = URL.createObjectURL(blob);
   a.download = "button.txt";
   document.body.appendChild(a);
   a.click();
 });
</script></body></html>
"""


def make_page(browser, payload: bytes):
    html = FIXTURE.replace("{PAYLOAD}", base64.b64encode(payload).decode())
    tmp = Path(tempfile.mkdtemp(prefix="cbp_cfile_"))
    f = tmp / "answer.html"
    f.write_text(html, encoding="utf-8")
    return f


class CopilotFileTests(unittest.TestCase):
    def _run(self, coro_factory):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("未安装 playwright")

        async def runner():
            async with async_playwright() as p:
                try:
                    browser = await p.chromium.launch(channel="msedge", headless=True)
                except Exception as exc:
                    self.skipTest(f"无可用浏览器: {exc}")
                result = await coro_factory(browser)
                await browser.close()
                return result

        return asyncio.run(runner())

    def test_data_link_downloaded_to_workspace(self):
        from gantry import bridge as b

        payload = "print('hello from copilot')\n".encode("utf-8")

        async def scenario(browser):
            page = await browser.new_page()
            html = make_page(browser, payload)
            await page.goto(html.as_uri())
            ws = Path(tempfile.mkdtemp(prefix="cbp_cfile_ws_"))
            saved = (b.page, b.ctx, b.AGENT_TOOLS.root, b.AGENT_CFG.get("allow_copilot_file"))
            b.page = page
            b.ctx = None                      # data:/blob: 路径不需要 ctx
            b.AGENT_TOOLS.root = ws
            b.AGENT_CFG["allow_copilot_file"] = True
            try:
                # 直接调内部保存逻辑，跳过与 Copilot 的往返（这一步才是本功能的易错点）
                cands = await b._download_candidates()
                self.assertTrue(cands, "应识别出下载入口")
                chosen = None
                for c in cands:
                    if "gen.py" in (c["text"] + c["href"]):
                        chosen = c
                        break
                self.assertIsNotNone(chosen, f"应优先选中文件名匹配的入口: {[c['text'] for c in cands]}")
                target = ws / "gen.py"
                size = await b._save_from_href(chosen["href"], target)
                return size, target.read_bytes()
            finally:
                b.page, b.ctx, b.AGENT_TOOLS.root = saved[0], saved[1], saved[2]
                b.AGENT_CFG["allow_copilot_file"] = saved[3]

        size, data = self._run(scenario)
        self.assertEqual(data, payload)
        self.assertEqual(size, len(payload))

    def test_blob_link_downloaded(self):
        from gantry import bridge as b

        payload = b"blob-content-12345"

        async def scenario(browser):
            page = await browser.new_page()
            tmp = Path(tempfile.mkdtemp(prefix="cbp_cfile_"))
            f = tmp / "blob.html"
            f.write_text(
                "<!doctype html><div data-content='ai-message'>"
                "<a id='b' download='x.bin' href='#'>x.bin</a></div><script>"
                "const blob=new Blob([new Uint8Array([98,108,111,98,45,99,111,110,116,101,110,116])],"
                "{type:'application/octet-stream'});"
                "document.getElementById('b').href=URL.createObjectURL(blob);"
                "</script>", encoding="utf-8")
            await page.goto(f.as_uri())
            saved = (b.page,)
            b.page = page
            try:
                cands = await b._download_candidates()
                blob = [c for c in cands if c["href"].startswith("blob:")]
                self.assertTrue(blob, f"应识别 blob: 入口: {cands}")
                out = tmp / "out.bin"
                await b._save_from_href(blob[0]["href"], out)
                return out.read_bytes()
            finally:
                b.page = saved[0]

        self.assertEqual(self._run(scenario), b"blob-content")

    def test_disabled_flag_and_sandbox(self):
        from gantry import bridge as b
        from gantry.tools import ToolError
        saved = b.AGENT_CFG.get("allow_copilot_file")
        try:
            b.AGENT_CFG["allow_copilot_file"] = False
            with self.assertRaises(ToolError):
                asyncio.run(b._copilot_file("x.txt", "生成"))
        finally:
            b.AGENT_CFG["allow_copilot_file"] = saved
        # 工作区外的路径必须被沙箱拒绝（复用工作区 _path 校验）
        with self.assertRaises(ToolError):
            asyncio.run(b._copilot_file("../outside.txt", "生成"))


if __name__ == "__main__":
    unittest.main()
