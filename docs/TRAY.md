# 托盘与后台启动

```
python start.py                 # 默认：派生后台进程（相当于 pythonw），终端可以关掉
python start.py --foreground    # 前台：日志同时打终端（Ctrl+C 结束）
python start.py --no-tray       # 不起托盘（服务/CI 用）
python start.py --lang en       # 指定托盘语言（zh / tw / en）
python start.py --print-menu    # 只打印托盘菜单（不建图标，排障用）
python start.py --tray-selftest # 建一下图标再退出，验证 Win32 托盘可用
```

## 为什么不用先跑 `scripts/login.py`

托盘**不依赖浏览器**：它只跟本地 HTTP 接口打交道。菜单里的「启动自动化浏览器」调用
`POST /v1/agent/browser`（按需拉起，登录态沿用 `EdgeProfile`）。所以流程是：

1. `python start.py` → 后台服务 + 托盘图标；
2. 右键托盘 →「启动自动化浏览器」→ 第一次会让你在自动化窗口里登录一次；
3. 之后「打开控制台」进 WebUI 就能用。

## 菜单项（三语言）

托盘语言来源：`config.json` 的 `ui.language` > `GANTRY_LANG` > Windows 界面语言；
默认中文（与 WebUI 默认一致）。菜单里「语言 ▸」可以直接切换，选择会写回
`config.json`，下次启动保持。

| 动作 | 作用 |
| --- | --- |
| 打开控制台 | 默认浏览器打开 `http://127.0.0.1:<port>/ui/` |
| 启动自动化浏览器 | `POST /v1/agent/browser`（按需启动，不需要 login.py） |
| 打开日志 / 打开数据目录 | `data/logs/gantry.log`（5 MB × 7 滚动）/ `data/` |
| 重启服务 | `POST /v1/restart`（优雅停机后就地重启，托盘跟着重建） |
| 退出托盘 | 只退出托盘，服务继续跑 |
| 停止服务并退出 | `POST /v1/shutdown`，服务与托盘一起结束 |

图标 tooltip 显示 `Gantry vX · 运行中 · 端口 8765`——状态和端口放这里，菜单就不必为了
显示它们而变宽。

## 日志

后台模式没有终端，日志全部走文件：`data/logs/gantry.log`（`GANTRY_LOG_DIR` 可改目录，
`GANTRY_LOG_LEVEL` 可改级别）。里面既有 uvicorn 的启动/访问日志，也有托盘的
「托盘图标已添加 / 托盘：启动自动化浏览器 / 托盘：重启服务」这类动作记录——
"点了没反应"时可以据此排查。

## 实现要点

- `gantry/tray.py`：纯 ctypes（user32/shell32）实现消息窗 + `Shell_NotifyIconW`，
  不加第三方依赖；菜单每次弹出时重建（状态是活的）。
- 服务占**主线程**（uvicorn 要装信号处理器），托盘跑在 worker 线程里，
  `stop()` 用 `PostThreadMessageW(WM_QUIT)` 收尾。
- 后台派生用同目录的 `pythonw.exe` + `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`，
  子进程带 `GANTRY_TRAY_CHILD=1`（重启时环境变量会继承，所以重启后仍是托盘模式）。
- 单实例：启动前先探 `/health`，已经在跑就只打印地址，不重复派生。
- 图标是 `assets/gantry.ico`（8 个尺寸，`scripts/make_tray_icon.py` 生成）。
  Win11 默认把新图标收进折叠区：`Shell_NotifyIconGetRect` 能证明它注册成功；
  想让它常驻可见，去「设置 → 个性化 → 任务栏 → 其他系统托盘图标」里打开。
