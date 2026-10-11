import asyncio
import json
import shutil
import socket
import sys
from pathlib import Path
from playwright.async_api import async_playwright

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
PROFILE = BASE / "EdgeProfile"
PORT = int(CFG.get("server", {}).get("port", 8765))

GUIDE = """
================= 登录指引（多账号环境）=================
1. Windows 可能弹出账户选择器并自动推荐系统当前账号。
   如需使用其他账号（例如有 M365 订阅的账号），请点击
   「使用其他账户 / Use another account」再登录。
2. 如果页面已经用旧账号自动登录了，请在 Copilot 页面右上角
   头像菜单先「注销 / Sign out」，再用目标账号重新登录。
3. 登录态保存在 EdgeProfile 目录，之后启动服务免登录。
4. 确认 Copilot 可正常对话后，回到本窗口按 Enter 结束。
=========================================================
"""


def service_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return True
    except OSError:
        return False


def reset_profile():
    if service_running():
        print(f"检测到服务正在运行（端口 {PORT}）。EdgeProfile 被占用无法删除，请先停止服务。")
        sys.exit(1)
    confirm = input(f"将删除 {PROFILE}（其中保存的登录态），输入 yes 确认: ")
    if confirm.strip().lower() != "yes":
        print("已取消。")
        sys.exit(0)
    shutil.rmtree(PROFILE, ignore_errors=True)
    print("EdgeProfile 已删除，接下来会以全新浏览器打开登录页。")


async def main():
    if "--reset" in sys.argv:
        reset_profile()
    elif service_running():
        # 服务运行中开第二个持久化上下文会抢 EdgeProfile 锁，可能损坏登录态
        print(f"检测到服务正在运行（端口 {PORT}）。直接登录会导致 EdgeProfile 锁冲突。")
        print("请先停止服务再运行 login.py；或服务有窗口模式下直接在自动化窗口里登录。")
        sys.exit(1)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            channel=CFG.get("browser_channel", "msedge"),
            headless=False,
            viewport={"width": 1440, "height": 1000},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(CFG["copilot_url"], wait_until="domcontentloaded", timeout=120000)
        print(GUIDE)
        print("请完成登录，确认 Copilot 可正常对话后按 Enter。")
        await asyncio.to_thread(input)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
