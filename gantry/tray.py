# -*- coding: utf-8 -*-
"""系统托盘（Windows，纯 ctypes，不加第三方依赖）。

为什么自己写而不是用 pystray：本项目要求"零依赖运行"（requirements 里只有 web 相关 +
pywin32 可选），而托盘只需要 user32/shell32 的几个 API。这里用**消息窗 + Shell_NotifyIcon**
实现：右键出菜单、双击打开控制台、气泡提示结果。

线程模型：托盘在自己的线程里跑 `GetMessageW` 循环（uvicorn 要在主线程装信号处理器，
所以服务占主线程、托盘放 worker 线程）。`stop()` 用 `PostThreadMessageW(WM_QUIT)` 收尾。

菜单里的动作都走**本地 HTTP 接口**（`/v1/agent/browser`、`/v1/restart`、`/v1/shutdown`），
所以托盘不需要 import 服务内部对象，也天然和"服务是否活着"解耦：
服务还没起来时点菜单只会得到一条"服务未响应"的气泡，不会崩。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable

log = logging.getLogger("gantry.tray")

# ── Win32 常量 ──────────────────────────────────────────────────────────────
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_NULL = 0x0000
WM_QUIT = 0x0012
WM_USER = 0x0400
WM_TRAY = WM_USER + 20
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NIIF_ERROR = 0x1, 0x3

MF_STRING, MF_SEPARATOR, MF_GRAYED, MF_POPUP = 0x0, 0x800, 0x1, 0x10
TPM_LEFTALIGN, TPM_BOTTOMALIGN = 0x0, 0x20
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100

IDI_APPLICATION = 32512
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
HWND_MESSAGE = -3


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
        ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128), ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
        ("guidItem", wt.BYTE * 16), ("hBalloonIcon", wt.HICON),
    ]


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                ("guidItem", wt.BYTE * 16)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [("hWnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
                ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt", wt.POINT)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


def _user32():
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    u.DefWindowProcW.restype = ctypes.c_void_p
    u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    u.CreateWindowExW.restype = wt.HWND
    u.DestroyWindow.argtypes = [wt.HWND]
    u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    u.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
    u.LoadIconW.restype = wt.HICON
    u.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int,
                             ctypes.c_int, wt.UINT]
    u.LoadImageW.restype = wt.HANDLE
    u.CreatePopupMenu.restype = wt.HMENU
    u.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
    u.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, wt.HWND, wt.LPVOID]
    u.TrackPopupMenu.restype = ctypes.c_int
    u.DestroyMenu.argtypes = [wt.HMENU]
    u.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    u.SetForegroundWindow.argtypes = [wt.HWND]
    u.GetMessageW.argtypes = [ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT]
    u.GetMessageW.restype = ctypes.c_int
    u.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    u.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    u.DispatchMessageW.restype = ctypes.c_void_p
    u.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
    u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    u.DestroyIcon.argtypes = [wt.HICON]
    return u


def _shell32():
    s = ctypes.WinDLL("shell32", use_last_error=True)
    s.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    s.Shell_NotifyIconW.restype = wt.BOOL
    # 查图标矩形：能区分"真的加上了"与"其实没加"（Win11 折叠区里肉眼看不到）
    s.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(NOTIFYICONIDENTIFIER),
                                          ctypes.POINTER(wt.RECT)]
    s.Shell_NotifyIconGetRect.restype = ctypes.c_long
    return s


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    k.GetModuleHandleW.restype = wt.HINSTANCE
    # 线程 id 在 kernel32（user32 里没有这个导出——写错会在第一次 selftest 就炸）
    k.GetCurrentThreadId.argtypes = []
    k.GetCurrentThreadId.restype = wt.DWORD
    return k


# ── 菜单动作（全部走本地 HTTP，避免依赖服务内部对象）──────────────────────────
# ── 多语言文案 ─────────────────────────────────────────────────────────────
# 托盘是原生菜单，拿不到浏览器的 localStorage（WebUI 语言存在那边），所以语言来源是：
#   config.json 的 ui.language（首选，显式可控）> GANTRY_LANG 环境变量 > Windows 界面语言。
TEXT = {
    "zh": {
        "open_webui": "打开控制台",
        "start_browser": "启动自动化浏览器",
        "open_logs": "打开日志",
        "open_data": "打开数据目录",
        "restart": "重启服务",
        "quit_tray": "退出托盘",
        "shutdown": "停止服务并退出",
        "running": "运行中",
        "down": "未响应",
        "tip": "{title} {ver} · {state} · 端口 {port}",
        "starting_browser": "正在启动自动化浏览器…",
        "browser_ready": "浏览器已就绪（{mode}）",
        "headless": "无头",
        "visible": "可见窗口",
        "restarting": "正在重启服务…",
        "restarted": "服务已重启",
        "failed": "操作失败",
        "no_service": "服务未响应：{err}",
        "language": "语言",
    },
    "tw": {
        "open_webui": "開啟控制台",
        "start_browser": "啟動自動化瀏覽器",
        "open_logs": "開啟日誌",
        "open_data": "開啟資料目錄",
        "restart": "重新啟動服務",
        "quit_tray": "結束托盤",
        "shutdown": "停止服務並結束",
        "running": "執行中",
        "down": "無回應",
        "tip": "{title} {ver} · {state} · 埠 {port}",
        "starting_browser": "正在啟動自動化瀏覽器…",
        "browser_ready": "瀏覽器已就緒（{mode}）",
        "headless": "無介面",
        "visible": "可見視窗",
        "restarting": "正在重新啟動服務…",
        "restarted": "服務已重新啟動",
        "failed": "操作失敗",
        "no_service": "服務無回應：{err}",
        "language": "語言",
    },
    "en": {
        "open_webui": "Open console",
        "start_browser": "Start automation browser",
        "open_logs": "Open log",
        "open_data": "Open data folder",
        "restart": "Restart service",
        "quit_tray": "Quit tray",
        "shutdown": "Stop service and quit",
        "running": "running",
        "down": "not responding",
        "tip": "{title} {ver} · {state} · port {port}",
        "starting_browser": "Starting the automation browser…",
        "browser_ready": "Browser ready ({mode})",
        "headless": "headless",
        "visible": "visible window",
        "restarting": "Restarting the service…",
        "restarted": "Service restarted",
        "failed": "Action failed",
        "no_service": "Service not responding: {err}",
        "language": "Language",
    },
}


LANG_LABELS = {"zh": "简体中文", "tw": "繁體中文", "en": "English"}


def detect_lang(root: Path | None = None) -> str:
    """决定托盘语言：config.json `ui.language` > GANTRY_LANG > Windows 界面语言。"""
    import os

    def norm(v: str) -> str:
        v = (v or "").strip().lower().replace("_", "-")
        if v.startswith("en"):
            return "en"
        if v in ("tw", "zh-tw", "zh-hk", "zh-mo", "zh-hant"):
            return "tw"
        if v.startswith("zh"):
            return "zh"
        return ""

    if root:
        try:
            cfg = json.loads((Path(root) / "config.json").read_text(encoding="utf-8"))
            v = norm(str((cfg.get("ui") or {}).get("language") or ""))
            if v:
                return v
        except Exception:                       # noqa: BLE001
            pass
    v = norm(os.environ.get("GANTRY_LANG") or "")
    if v:
        return v
    try:                                        # Windows 界面语言
        import ctypes

        lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        primary, sub = lid & 0x3FF, (lid >> 10) & 0x3F
        if primary == 0x04:                     # 中文：区分简繁
            return "tw" if sub in (0x01, 0x03) else "zh"
        # 产品默认中文（WebUI 的 gantry-lang 默认也是 zh）；英文/其它语言可在托盘菜单里切，
        # 选择会写进 config.json 的 ui.language，下次启动仍然是它。
        return "zh"
    except Exception:                           # noqa: BLE001
        return "zh"


def http_json(url: str, payload=None, timeout: float = 8.0) -> dict:
    """POST/GET 本地接口。服务没起来时抛 OSError，由调用方转成气泡提示。"""
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 (本地地址)
        return json.loads(r.read().decode("utf-8") or "{}")


class Tray:
    """托盘图标 + 右键菜单。

    menu_spec: [(action, 中文label) | ("-",) | (None, 纯信息行)]；每次弹出时重建，
               所以可以显示"服务：运行中/未响应"这类动态文案。
    """

    def __init__(self, *, port: int, root: Path, title: str = "Gantry",
                 menu_spec: Callable[["Tray"], list] | None = None,
                 icon_path: Path | None = None, lang: str | None = None) -> None:
        self.port = int(port)
        self.root = Path(root)
        self.title = title
        self.lang = (lang if lang in TEXT else detect_lang(self.root))
        self.text = TEXT[self.lang]
        self._menu_spec = menu_spec or default_menu
        self._icon_path = icon_path
        self._user32 = self._shell32 = self._kernel32 = None
        self._hwnd = None
        self._hicon = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._quitting = False

    # ── 生命周期 ──
    def start(self) -> "Tray":
        """在后台线程里建图标并开始消息循环。"""
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._run, name="gantry-tray", daemon=True)
        self._thread.start()
        return self

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """等图标真正加上（selftest/启动确认用）。"""
        ok = self._ready.wait(timeout)
        if self._error:
            raise RuntimeError(f"托盘初始化失败：{self._error}") from self._error
        return ok

    def stop(self, timeout: float = 3.0) -> None:
        self._quitting = True
        if self._thread_id:
            try:
                self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        if self._thread:
            self._thread.join(timeout)

    # ── 消息循环 ──
    def _run(self) -> None:
        try:
            self._create_window()
            self._add_icon()
            self._ready.set()
            self._pump()
        except BaseException as exc:                    # noqa: BLE001 - 交给 wait_ready 抛
            self._error = exc
            self._ready.set()
            log.exception("托盘线程异常退出")
        finally:
            self._remove_icon()

    def _create_window(self) -> None:
        u = self._user32 = _user32()
        self._shell32 = _shell32()
        self._kernel32 = _kernel32()
        self._thread_id = self._kernel32.GetCurrentThreadId()
        hinst = self._kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._on_message)       # 必须保引用，否则被回收
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = "GantryTrayWnd"
        if not u.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (1410,):                      # 1410 = 类已注册（重复 start）
                raise OSError(f"RegisterClassW 失败：{err}")
        self._hwnd = u.CreateWindowExW(0, "GantryTrayWnd", self.title, 0, 0, 0, 0, 0,
                                       ctypes.c_void_p(HWND_MESSAGE), None, hinst, None)
        if not self._hwnd:
            raise OSError(f"CreateWindowExW 失败：{ctypes.get_last_error()}")

    def _load_icon(self):
        u = self._user32
        if self._icon_path and Path(self._icon_path).is_file():
            h = u.LoadImageW(None, str(self._icon_path), IMAGE_ICON, 0, 0,
                             LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
        return u.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        return nid

    def _add_icon(self) -> None:
        self._hicon = self._load_icon()
        nid = self._nid()
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self._tooltip()[:127]
        if not self._shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise OSError(f"Shell_NotifyIcon 失败：{ctypes.get_last_error()}")
        log.info("托盘图标已添加（pid=%s）", os.getpid())

    def _remove_icon(self) -> None:
        try:
            if self._hwnd:
                nid = self._nid()
                self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._hicon:
                self._user32.DestroyIcon(self._hicon)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._hwnd:
                self._user32.DestroyWindow(self._hwnd)
        except Exception:  # noqa: BLE001
            pass

    def _pump(self) -> None:
        msg = MSG()
        while True:
            got = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got in (0, -1):                          # 0=WM_QUIT，-1=错误
                break
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                ev = int(lparam) & 0xFFFF
                if ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
                elif ev == WM_LBUTTONDBLCLK:
                    self.act("open_webui")
            elif msg == WM_COMMAND:
                self.act(int(wparam) & 0xFFFF)
            elif msg == WM_DESTROY:
                self._user32.PostQuitMessage(0)
        except Exception:                               # noqa: BLE001
            log.exception("托盘消息处理失败")
        return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ── 菜单 ──
    def _show_menu(self) -> None:
        u = self._user32
        spec = self._menu_spec(self)
        menu = u.CreatePopupMenu()
        cmds: dict[int, str] = {}
        next_id = 1000
        try:
            for item in spec:
                if item and item[0] == "-":
                    u.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    continue
                if item and item[0] == "--submenu--":   # (标记, 标签, [(action, 标签)])
                    sub = u.CreatePopupMenu()
                    for sact, slab in item[2]:
                        cmds[next_id] = sact
                        u.AppendMenuW(sub, MF_STRING, next_id, slab)
                        next_id += 1
                    u.AppendMenuW(menu, MF_POPUP, int(sub or 0), item[1])
                    continue
                action, label = item[0], item[1]
                if action is None:                       # 纯信息行
                    u.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, label)
                    continue
                cmds[next_id] = action
                u.AppendMenuW(menu, MF_STRING, next_id, label)
                next_id += 1
            pt = wt.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            u.SetForegroundWindow(self._hwnd)            # 否则点别处菜单不消失
            cmd = u.TrackPopupMenu(
                menu, TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON
                      | TPM_NONOTIFY | TPM_RETURNCMD,
                pt.x, pt.y, 0, self._hwnd, None)
            u.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            u.DestroyMenu(menu)
        action = cmds.get(int(cmd or 0))
        if action:
            self.act(action)

    # ── 动作 ──
    def balloon(self, title: str, text: str, error: bool = False) -> None:
        try:
            nid = self._nid()
            nid.uFlags = NIF_INFO
            nid.szInfo = (text or "")[:255]
            nid.szInfoTitle = (title or "")[:63]
            nid.dwInfoFlags = NIIF_ERROR if error else NIIF_INFO
            self._shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:  # noqa: BLE001
            log.debug("气泡提示失败", exc_info=True)

    def set_language(self, lang: str) -> None:
        """切换托盘语言并写回 config.json（`ui.language`），下次启动沿用。"""
        if lang not in TEXT:
            return
        self.lang = lang
        self.text = TEXT[lang]
        try:
            cfg_path = self.root / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg.setdefault("ui", {})["language"] = lang
            tmp = cfg_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            tmp.replace(cfg_path)
            log.info("托盘语言已切换为 %s（已写入 config.json ui.language）", lang)
        except Exception as exc:                 # noqa: BLE001
            log.warning("写 config.json 失败（本次会话内仍生效）：%s", exc)

    def _status(self) -> tuple[str, str]:
        """(状态文案, 版本号)；服务没起来就是"未响应"。"""
        try:
            h = http_json(self.base_url() + "/health", timeout=1.2)
            ok = h.get("status") == "ok"
            return (self.text["running"] if ok else self.text["down"],
                    str(h.get("version") or ""))
        except Exception:                        # noqa: BLE001
            return self.text["down"], ""

    def _tooltip(self) -> str:
        state, ver = self._status()
        return self.text["tip"].format(title=self.title,
                                       ver=("v" + ver) if ver else "",
                                       state=state, port=self.port)

    def icon_rect(self) -> tuple[int, int, int, int] | None:
        """通知区里这个图标的矩形；没加上/查不到返回 None。

        注：Win11 把新图标收进折叠区时，这里仍能拿到矩形（坐标落在折叠面板内），
        所以它是"图标是否真的注册成功"的可靠判据。
        """
        try:
            ident = NOTIFYICONIDENTIFIER()
            ident.cbSize = ctypes.sizeof(NOTIFYICONIDENTIFIER)
            ident.hWnd = self._hwnd
            ident.uID = 1
            rect = wt.RECT()
            hr = self._shell32.Shell_NotifyIconGetRect(ctypes.byref(ident),
                                                       ctypes.byref(rect))
            if hr != 0:
                return None
            return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        except Exception:                            # noqa: BLE001
            return None

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def act(self, action: str) -> None:
        """菜单动作入口：耗时的一律丢线程，别卡住消息循环（否则菜单会僵住）。"""
        if action == "quit_tray":
            log.info("托盘：退出（服务继续运行）")
            self.stop()
            return
        fn = getattr(self, f"_a_{action}", None)
        if fn is None:
            log.warning("托盘：未知动作 %s", action)
            return
        threading.Thread(target=self._guard, args=(action, fn), daemon=True).start()

    def _guard(self, action: str, fn) -> None:
        try:
            fn()
        except Exception as exc:                        # noqa: BLE001
            log.warning("托盘动作 %s 失败：%s", action, exc)
            self.balloon("操作失败", f"{action}: {exc}"[:200], error=True)

    # 每个动作一个方法，便于 _a_<action> 反射
    def _a_open_webui(self) -> None:
        url = self.base_url() + "/ui/"
        log.info("托盘：打开控制台 %s", url)
        webbrowser.open(url)

    def _a_start_browser(self) -> None:
        log.info("托盘：启动自动化浏览器（按需拉起，无需先跑登录脚本）")
        self.balloon(self.title, self.text["starting_browser"])
        try:
            r = http_json(self.base_url() + "/v1/agent/browser", {}, timeout=180)
        except Exception as exc:                        # noqa: BLE001
            self.balloon(self.text["failed"], self.text["no_service"].format(err=str(exc)[:160]), error=True)
            return
        mode = self.text["headless"] if r.get("headless") else self.text["visible"]
        self.balloon(self.title, self.text["browser_ready"].format(mode=mode))

    def _a_toggle_browser(self) -> None:
        try:
            cur = http_json(self.base_url() + "/v1/agent/browser")
        except Exception as exc:                        # noqa: BLE001
            self.balloon(self.text["failed"], self.text["no_service"].format(err=str(exc)[:160]), error=True)
            return
        target = not bool(cur.get("headless"))
        try:
            http_json(self.base_url() + "/v1/agent/browser", {"headless": target}, timeout=120)
            self.balloon(self.title, self.text["browser_ready"].format(
                mode=self.text["headless"] if target else self.text["visible"]))
        except Exception as exc:                        # noqa: BLE001
            self.balloon(self.text["failed"], str(exc)[:160], error=True)

    def _a_open_logs(self) -> None:
        from .logcfg import log_file

        p = log_file(self.root)
        log.info("托盘：打开日志 %s", p)
        os.startfile(str(p))                            # noqa: S606 (Windows 专用)

    def _a_open_log_dir(self) -> None:
        from .logcfg import log_dir

        os.startfile(str(log_dir(self.root)))           # noqa: S606

    def _a_open_data(self) -> None:
        os.startfile(str(self.root / "data"))           # noqa: S606

    def _a_open_install(self) -> None:
        os.startfile(str(self.root))                    # noqa: S606

    def _a_restart(self) -> None:
        log.info("托盘：重启服务")
        self.balloon(self.title, self.text["restarting"])
        try:
            http_json(self.base_url() + "/v1/restart", {}, timeout=20)
        except Exception as exc:                        # noqa: BLE001
            self.balloon(self.text["failed"], str(exc)[:160], error=True)
            return
        time.sleep(1.5)
        self.balloon(self.title, self.text["restarted"])

    def _a_lang_zh(self) -> None:
        self.set_language("zh")

    def _a_lang_tw(self) -> None:
        self.set_language("tw")

    def _a_lang_en(self) -> None:
        self.set_language("en")

    def _a_shutdown(self) -> None:
        log.info("托盘：停止服务并退出")
        try:
            http_json(self.base_url() + "/v1/shutdown", {}, timeout=20)
        except Exception as exc:                        # noqa: BLE001
            log.warning("停止服务请求失败（可能已经停了）：%s", exc)
        self.stop()


def default_menu(tray: Tray) -> list:
    """托盘菜单：**短**。状态/版本/端口都在图标 tooltip 里，菜单第一行只放版本号。

    之前把 "Gantry vX · 运行中 · 端口 8765" 当菜单首行，菜单被撑得很宽（用户："不要那么宽"）。
    现在只保留 8 条动作，标签去掉括号注解。
    """
    t = tray.text
    state, ver = tray._status()
    head = f"{tray.title} v{ver}" if ver else f"{tray.title} · {state}"
    return [
        (None, head),
        ("-",),
        ("open_webui", t["open_webui"]),
        ("start_browser", t["start_browser"]),
        ("-",),
        ("open_logs", t["open_logs"]),
        ("open_data", t["open_data"]),
        ("-",),
        ("restart", t["restart"]),
        ("-",),
        ("--submenu--", t["language"],
         [("lang_zh", LANG_LABELS["zh"]), ("lang_tw", LANG_LABELS["tw"]),
          ("lang_en", LANG_LABELS["en"])]),
        ("quit_tray", t["quit_tray"]),
        ("shutdown", t["shutdown"]),
    ]


def selftest(port: int, root: Path, seconds: float = 1.5) -> int:
    """冒烟：真的建图标 → 发一条气泡 → 收尾。用于验证 Win32 调用在本机可用。"""
    tray = Tray(port=port, root=root, menu_spec=default_menu)
    tray.start()
    if not tray.wait_ready(5.0):
        print("FAIL: 托盘未就绪")
        return 1
    tray.balloon("Gantry 托盘自检", "如果你能看到这条气泡，说明托盘能工作")
    time.sleep(0.3)
    rect = tray.icon_rect()
    time.sleep(max(0.2, seconds - 0.3))
    tray.stop()
    if not rect:
        print("FAIL: 图标注册了但 Shell_NotifyIconGetRect 查不到（可能被系统忽略）")
        return 1
    x, y, w, h = rect
    print(f"OK: 托盘图标已创建（矩形 {w}×{h} @ {x},{y}），并已正常退出")
    return 0
