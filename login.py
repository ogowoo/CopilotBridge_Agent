import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))

async def main():
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(BASE / "EdgeProfile"),
            channel=CFG.get("browser_channel", "msedge"),
            headless=False,
            viewport={"width": 1440, "height": 1000},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(CFG["copilot_url"], wait_until="domcontentloaded", timeout=120000)
        print("请完成登录，确认 Copilot 可正常对话后按 Enter。")
        await asyncio.to_thread(input)
        await ctx.close()

if __name__ == "__main__":
    asyncio.run(main())
