# -*- coding: utf-8 -*-
"""Outlook COM 自检：在跑服务的机器上、用服务同一个 Python 环境运行。

    python scripts/outlook_selftest.py    # 从项目根目录运行

逐步检查并把 COM 错误翻译成可操作的结论：
  pywin32 导入 -> CoInitialize -> Dispatch -> MAPI -> 收件箱 -> 未读过滤
每一步失败即停，打印 HRESULT 对应的典型原因（权限不一致/会话隔离/
未安装/被安全软件拦截等），并给出下一步建议。
"""
import sys
from pathlib import Path

STEPS = []


def ok(msg):
    STEPS.append(("OK", msg))
    print(f"  [OK] {msg}")


def fail(msg, advice="", exit=True):
    STEPS.append(("FAIL", msg))
    print(f"  [FAIL] {msg}")
    if advice:
        print(f"  建议: {advice}")
    if exit:
        sys.exit(1)


HRESULT_HINTS = {
    0x80040154: "未注册 Outlook COM 类——本机未安装 Outlook/Office",
    0x80080005: "Outlook 拒绝自动化调用——最常见原因：Outlook 与本脚本运行权限不一致"
                "（一个用管理员、一个不用）或不同 Windows 用户。让两者同用户同提权状态再试",
    0x8000401A: "非交互式会话无法启动 Outlook——必须在已登录用户的桌面会话里运行",
    0x80070005: "访问被拒绝——Outlook 安全策略或杀毒软件拦截了 COM 自动化",
    0x80010105: "RPC 通信失败——Outlook 卡死，重启 Outlook",
    0x80004004: "操作被中止——Outlook 忙/有模态对话框/安全确认被取消，稍后重试或重启 Outlook",
}


def explain(exc):
    code = None
    try:
        code = int(exc.args[0]) & 0xFFFFFFFF
    except Exception:
        pass
    if code:
        hint = HRESULT_HINTS.get(code, "未识别错误，把完整输出发给维护者")
        return f"0x{code:08X}: {exc}\n  典型原因: {hint}"
    return repr(exc)


def main():
    print("== Outlook COM 自检 ==\n环境:", sys.executable, "\n")

    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        fail(f"pywin32 未安装: {exc}", "pip install pywin32")
    ok("pywin32 导入")

    pythoncom.CoInitialize()
    try:
        try:
            app = win32com.client.Dispatch("Outlook.Application")
        except Exception as exc:
            fail(f"Dispatch(Outlook.Application) 失败\n  {explain(exc)}")
        ok("Outlook.Application 获取")

        try:
            ns = app.GetNamespace("MAPI")
        except Exception as exc:
            fail(f"GetNamespace(MAPI) 失败\n  {explain(exc)}")
        ok("MAPI 命名空间")

        try:
            inbox = ns.GetDefaultFolder(6)
            count = inbox.Items.Count
        except Exception as exc:
            fail(f"收件箱访问失败\n  {explain(exc)}")
        ok(f"收件箱可访问（{count} 条）")

        try:
            unread = inbox.Items.Restrict("[Unread] = true").Count
        except Exception as exc:
            fail(f"未读过滤失败\n  {explain(exc)}")
        ok(f"未读过滤可用（{unread} 封未读）")

        # 第 6 步：复现服务端的调用模式（asyncio.to_thread 线程池 + 每线程
        # CoInitialize + 遍历条目）。主线程全过但服务报 0x80004004 时，差异
        # 就在这一层：后台线程无消息泵，Outlook 需要 UI 交互的调用会被中止。
        print("\n-- 服务端同款调用模式复现（线程池线程）--")
        try:
            import asyncio

            async def threaded_probe():
                def work():
                    import pythoncom
                    import win32com.client
                    pythoncom.CoInitialize()
                    try:
                        app = win32com.client.Dispatch("Outlook.Application")
                        ns = app.GetNamespace("MAPI")
                        items = ns.GetDefaultFolder(6).Items
                        items.Sort("[ReceivedTime]", True)
                        out = 0
                        for i, item in enumerate(items):
                            if i >= 20:
                                break
                            _ = (str(item.Subject), str(item.ReceivedTime), str(item.SenderName))
                            out += 1
                        return out
                    finally:
                        pythoncom.CoUninitialize()

                return await asyncio.wait_for(asyncio.to_thread(work), timeout=60)

            n = asyncio.run(threaded_probe())
            ok(f"线程池模式遍历收件箱前 {n} 封（与服务 _outlook_run 同路径）")
        except Exception as exc:
            # 预期失败：这一步就是复现服务端报错的。继续跑第 7 步看新方案是否修好。
            STEPS.append(("FAIL", "threaded"))
            print(f"  [FAIL] 线程池模式复现失败（预期内，复现服务端现象）\n  {explain(exc)}")

        # 第 7 步：用修复后的专用 COM 线程(_ComWorker)再跑一遍——预期通过。
        print("\n-- 专用 COM 线程模式验证（服务端新代码路径）--")
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from cbp.tools import _ComWorker

            async def worker_probe():
                def work():
                    import win32com.client
                    app = win32com.client.Dispatch("Outlook.Application")
                    ns = app.GetNamespace("MAPI")
                    items = ns.GetDefaultFolder(6).Items
                    items.Sort("[ReceivedTime]", True)
                    out = 0
                    for i, item in enumerate(items):
                        if i >= 20:
                            break
                        _ = (str(item.Subject), str(item.ReceivedTime), str(item.SenderName))
                        out += 1
                    return out

                return await asyncio.wait_for(_ComWorker.get().submit(work), timeout=60)

            n = asyncio.run(worker_probe())
            ok(f"专用 COM 线程模式遍历 {n} 封（新代码 _ComWorker 路径）")
        except Exception as exc:
            fail(f"专用 COM 线程模式也失败\n  {explain(exc)}",
                 "把完整输出发给维护者")

        # 对照结论
        results = dict(STEPS)
        if results.get("FAIL") == "threaded":
            print("\n== 诊断确认：线程池模式失败、专用 COM 线程模式通过 ==")
            print("== 服务端 git pull 换新代码并重启后，Outlook 工具即正常 ==")
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

    print("\n== 全部通过：服务端 Outlook 工具应可正常工作 ==")
    print("若服务端仍失败，请核对服务进程的启动用户/提权状态与 Outlook 是否一致。")


if __name__ == "__main__":
    main()
