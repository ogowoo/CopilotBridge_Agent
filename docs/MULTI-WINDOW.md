> **预览版（分支 `preview/multi-window`,版本 10.3.0-preview.1）**
> 这个分支上的多窗口并行仍在收尾：已知还有问题没解决，**不要**用在正式环境。
> 稳定线在 `main`（版本 10.2.1），不含本功能。

# Copilot 通道多窗口并行（`agent.copilot_windows`）

> 目标：**要并行几个任务，就开几个自动化窗口**。以前整个桥接只有 1 个 page + 1 把全局锁，
> 所有任务（含子代理）排成一列；现在改成窗口池，数量可在设置里调（1–4）。

## 一、为什么可以并行

- 一个"窗口" = **同一个持久化浏览器上下文里的一个标签页**（`ctx.new_page()`）；
  登录态共享，不需要开第二个浏览器进程、不用重新登录。
- 每个标签页有自己的一套 Copilot 会话、模型选择、URL、输入框 —— 互不干扰。
- 任务之间唯一的共享点是工具注册表里的 per-task 状态（工作区根目录、沙箱档位、计划模式、
  记忆作用域、当前会话 id）。这些现在由 **ContextVar + 每个任务一份 `ToolRegistry` 克隆** 隔离。

## 二、实现要点

| 位置 | 做法 |
|---|---|
| `cbp/window_pool.py` | `WindowPool`：懒创建窗口、每窗口一把 `asyncio.Lock`、`acquire/release`、排队上限、`set_max()` 动态增减 |
| `cbp/bridge.py` `page` | 由全局变量改成 `_PageProxy`：`page.xxx` 按**任务上下文**解析到该任务的窗口页（57 处调用点零改动） |
| `_window(sid)` | 任务级上下文管理器：取窗口 → 绑 `_PAGE_CTX`/`_SESSION_CTX` → 克隆工具注册表 → 结束后归还 |
| `run_agent` | `async with lock:` → `async with _window(sid, need_page=...)`：网页模型取真实窗口，API 提供商模型取无页面的逻辑槽（`SLOTS`，真并行） |
| 普通对话 `continue_session_message` | 同样走 `_window`，所以聊天和 Agent 任务可以同时在不同窗口里跑 |
| 模型缓存 `SELECTED_MODEL` | 按窗口存（`Window.selected_model`），两个窗口各自记得自己选了什么模型 |
| `ACTIVE_SESSION_ID` | 只保留"最近活跃"的展示用途；真正判定用 `current_session()`（ContextVar）与 `WINDOWS.is_running(sid)` |
| `web_copilot_allowed()` | 回落改读**任务级**注册表，提供商会话禁用 `copilot_*` 工具的行为在并行下依然正确 |
| 子代理扇出 | `subagent_many`：提供商 → `agent.subagent_parallel`（默认 4）；网页通道 → `web_subagent_parallel()` = 窗口数 |
| 可观测 | `/v1/agent/sessions/{sid}/runtime` 新增 `windows: {max, windows, busy, waiting, running}`；`busy` 用窗口池判定 |

## 三、怎么用

1. 设置页 → **执行** → 「Copilot 自动化窗口数」填 2–4，保存（立即生效，不用重启）。
2. 之后：
   - 新任务 / 继续对话会各自占一个窗口，互不排队；
   - `subagent_many` 在网页通道下最多同时跑 N 个子代理（N = 窗口数）；
   - 并发超过窗口数时任务排队，队列上限 `max(4, 窗口数×3)`，超出直接返回"窗口全忙"。
3. 改回 1 = 回到原来的串行行为（空闲窗口会被自动关掉，正在跑的不打断）。

## 四、实测

机制验证（独立进程 + 临时 profile，避免动服务在用的 `EdgeProfile`）：

```
两个页面独立: A B                      # 两个标签页各有自己的 window 变量
预热拿到窗口: [1, 2] | 池: max=2, windows=2
并发两个任务: 0.60s（串行约 1.2s）| 窗口 [1, 2] | 不同: True
并行结论: ✅ 真并行
```

设置页验证：控件渲染为数字框（min=1/max=4，默认显示 1）；改成 2 → `/v1/config` 立即返回 2，
`WINDOWS.set_max(2)` 生效；再改回 1 恢复。

## 五、注意与代价

- **额度**：并行窗口 = 同时开多条 Copilot 对话，每个任务都在消耗额度；网页通道限流时是
  "一起被限"，所以窗口数 2–3 通常够用，4 已经偏激进。
- **内存/CPU**：每个标签页一个渲染进程；无头模式下 3–4 个窗口大约多占几百 MB。
- **限流挂起**（`agent.rate_limit_suspend`）是**按窗口**判定的：某个窗口被限流，它自己的任务
  挂起，其它窗口不受影响。
- **模型选择**是每窗口的：不同窗口可以选不同模型（比如一个 reasoner 跑长任务、一个 default 快答）。
- **会话 URL**：同一会话被两个窗口同时打开会互相抢状态——所以窗口池按会话取用，同一会话不会
  同时占两个窗口（`run_agent` 的会话级排队保证）。
