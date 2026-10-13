# -*- coding: utf-8 -*-
"""模型菜单扫描回归测试：二级子菜单必须能被枚举到。

背景（真实故障）：Copilot 的模型菜单里 "GPT" 是子菜单父项，但它并不总带
`aria-haspopup="menu"`（可能只有 aria-haspopup="true"、aria-expanded，或仅一个
chevron 图标）。旧实现只认 "menu"，于是把 GPT 当叶子跳过、二级菜单里的
"GPT-5.6 Sol Quick response / Think deeper" 永远扫不出来。

本测试用本地 HTML 复刻该结构（父项无 aria-haspopup="menu"，悬停才飞出子菜单），
直接驱动 cbp.bridge 的扫描逻辑，断言：
  - 二级菜单项被枚举进 labels；
  - 父项 "GPT" 不被当作可选模型（叶子集合里没有它）。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURE = """<!doctype html><html><head><meta charset="utf-8"><style>
 body{font-family:sans-serif}
 [role=menu]{position:absolute;background:#fff;border:1px solid #ccc;padding:6px;width:260px}
 [role=menuitem],[role=menuitemradio]{padding:8px;display:block}
 .chev{float:right}
 #submenu{left:280px;top:120px}
</style></head><body>
<button id="modelBtn" aria-haspopup="menu" aria-label="Auto, Model Selector">Auto</button>
<div role="menu" id="menu" hidden>
  <div role="menuitemradio" aria-checked="true">Auto
    <div>Decides how long to think</div></div>
  <div role="menuitemradio">Quick response
    <div>Answers right away</div></div>
  <div role="menuitemradio">Think deeper
    <div>Think longer for better answers</div></div>
  <!-- 关键：父项没有 aria-haspopup="menu"，只有 chevron 图标 -->
  <div role="menuitem" id="gpt" aria-haspopup="true">GPT
    <span class="chev">&#8250;</span>
    <div>OpenAI</div></div>
</div>
<div role="menu" id="submenu" hidden>
  <div role="menuitemradio">GPT-5.6 Sol Quick response</div>
  <div role="menuitemradio">GPT-5.6 Sol Think deeper</div>
</div>
<script>
 const menu=document.getElementById('menu'), sub=document.getElementById('submenu');
 document.getElementById('modelBtn').addEventListener('click', ()=>{menu.hidden=!menu.hidden});
 const gpt=document.getElementById('gpt');
 gpt.addEventListener('mouseenter', ()=>{sub.hidden=false});
 gpt.addEventListener('mouseleave', ()=>{sub.hidden=true});
 document.addEventListener('keydown', e=>{if(e.key==='Escape'){sub.hidden=true;menu.hidden=true}});
</script></body></html>
"""


class ModelMenuScanTests(unittest.TestCase):
    def test_submenu_items_discovered(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("未安装 playwright")

        tmp = Path(tempfile.mkdtemp(prefix="cbp_menu_"))
        html = tmp / "menu.html"
        html.write_text(FIXTURE, encoding="utf-8")

        async def run():
            from cbp import bridge as b
            async with async_playwright() as p:
                try:
                    browser = await p.chromium.launch(channel="msedge", headless=True)
                except Exception:
                    try:
                        browser = await p.chromium.launch(headless=True)
                    except Exception as exc:
                        self.skipTest(f"无可用浏览器: {exc}")
                pg = await browser.new_page()
                await pg.goto(html.as_uri())
                # 只替换与页面导航/按钮定位相关的部分，扫描逻辑本身保持原样
                orig = (b.page, b.ensure_copilot_page, b.model_button)
                b.page = pg

                async def _fake_ensure():
                    return pg.url

                async def _fake_button():
                    return pg.locator("#modelBtn")

                b.ensure_copilot_page = _fake_ensure
                b.model_button = _fake_button
                saved_models = dict(b.MODELS)
                try:
                    result = await b.discover_models()
                    known = {mid: list(item["ui_names"]) for mid, item in b.MODELS.items()}
                finally:
                    b.page, b.ensure_copilot_page, b.model_button = orig
                    b.MODELS.clear()
                    b.MODELS.update(saved_models)
                    b.DISCOVERED_LABELS = None
                    await browser.close()
                return result, known

        result, known = asyncio.run(run())
        self.assertTrue(result["scanned"], f"扫描应成功: {result}")
        labels = result["labels"]
        self.assertIn("GPT-5.6 Sol Quick response", labels)
        self.assertIn("GPT-5.6 Sol Think deeper", labels)
        self.assertIn("Auto", labels)
        # 父项不是可选模型
        self.assertNotIn("GPT", labels)
        # 二级菜单项应可用：要么本次新增，要么已内置在模型表
        for label in ("GPT-5.6 Sol Quick response", "GPT-5.6 Sol Think deeper"):
            self.assertTrue(any(label in names for names in known.values()),
                            f"二级菜单模型 {label} 应可用；模型表: {known}")


class ModelAvailabilityTests(unittest.TestCase):
    """菜单实况可见性：二级叶子存在 => 其父家族不算"菜单里没有"。"""

    def test_family_prefix_match(self):
        from cbp import bridge as b
        saved = b.DISCOVERED_LABELS
        try:
            b.DISCOVERED_LABELS = {"auto", "quick response", "think deeper",
                                   "gpt-5.6 sol quick response",
                                   "gpt-5.6 sol think deeper"}
            self.assertTrue(b.copilot_model_available(b.MODELS["copilot-auto"]))
            # 二级叶子可见 => GPT 家族（含父项）都不该被隐藏
            self.assertTrue(b.copilot_model_available(b.MODELS["copilot-gpt"]))
            self.assertTrue(b.copilot_model_available(b.MODELS["copilot-gpt-5-6"]))
            self.assertTrue(b.copilot_model_available(
                b.MODELS["copilot-gpt-5-6-sol-think-deeper"]))
            # 完全没出现的模型仍应隐藏
            self.assertFalse(b.copilot_model_available(b.MODELS["copilot-sol"]))
        finally:
            b.DISCOVERED_LABELS = saved

    def test_no_scan_means_all_available(self):
        from cbp import bridge as b
        saved = b.DISCOVERED_LABELS
        try:
            b.DISCOVERED_LABELS = None
            for item in b.MODELS.values():
                self.assertTrue(b.copilot_model_available(item))
        finally:
            b.DISCOVERED_LABELS = saved


if __name__ == "__main__":
    unittest.main()
