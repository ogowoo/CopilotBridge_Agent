> **预览版（分支 `preview/multi-window`,版本 10.3.0-preview.2）**
> 这个分支上的多窗口并行仍在收尾：第六节列出本轮已收尾的问题，**窗口级实测**仍需在真实
> 登录态下手动复核，**不要**用在正式环境。稳定线在 `main`（版本 10.2.1），不含本功能。

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
| `ACTIVE_SESSION_ID` | 只保留"最近活跃"的展示用途；真正判定用 `current_session()`（ContextVar）与 `session_running(sid)`（窗口池 + 提供商逻辑槽，两者都查） |
| `web_copilot_allowed()` | 回落改读**任务级**注册表，提供商会话禁用 `copilot_*` 工具的行为在并行下依然正确 |
| 普通对话 `/v1/chat/completions` | 也走 `_window`（每个请求一个临时 `chat-*` 窗口 id）：以前只拿全局锁、直接驱动共享页，会和窗口 #1 上的任务抢标签页 |
| 管理操作 | 删除远端会话 / 扫模型菜单 / 激活页面 / 调试下载都**独占用一个窗口**；切 headless、切工作区、回滚在"有任务在跑"时拒绝 |
| 子代理扇出 | `subagent_many`：提供商 → `agent.subagent_parallel`（默认 4）；网页通道 → 窗口数 − 1（父任务自己占一个），每个子代理**独占一个标签页** |
| 子代理（无父页面时） | `_drive_child` 发现上下文里没有页面（用户入口 `fanout`、CLI）就自己 `acquire` 一个窗口；有父页面时复用父页面（父任务正等着它，另开窗口会把自己等死） |
| 可观测 | `/v1/agent/sessions/{sid}/runtime` 新增 `windows: {max, windows, busy, waiting, running}`；`busy` 用窗口池判定 |

## 三、怎么用

1. 设置页 → **执行** → 「Copilot 自动化窗口数」填 2–4，保存（立即生效，不用重启）。
2. 之后：
   - 新任务 / 继续对话会各自占一个窗口，互不排队；普通对话（`/v1/chat/completions`）也占一个；
   - `subagent_many` 在网页通道下最多同时跑 **窗口数 − 1** 个子代理（父任务占着 1 个窗口，
     子代理各开一个标签页）；窗口数 = 1 时仍是顺序执行；
   - 并发超过窗口数时任务排队，队列上限 `max(4, 窗口数×3)`，超出直接返回"窗口全忙"；
     排队**等 30 秒**还拿不到窗口也会返回"窗口全忙"（不会无限期挂着）。
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
- **同一会话不要并发提交两个回合**：窗口池不按会话去重（按会话加锁会在"暂停中的 runner
  一直持着窗口"时死锁）。WebUI 的消息队列已按会话串行化；直接调 API 时调用方要保证同一
  `session_id` 串行，否则两个窗口会同时打开同一会话、互相抢状态。

## 六、本轮收尾（2026-10-22）

第一版多窗口漏掉的一批"旧锁语义"已改掉，并补了回归测试：

1. **普通对话通道**：`/v1/chat/completions` 以前只拿全局 `lock` 直接驱动共享页 →
   现在走 `_window`，每个请求占一个临时窗口（`chat-*`）；窗口全忙时流式返回
   `{"type":"window_busy"}`、非流式 409。
2. **死掉的全局锁判定**：`b.lock.locked()` 在去掉全局锁后恒为 False，导致
   `/health` 的 busy、运行中插入指令（Ctrl+J）、删除会话的运行权、`/runtime` 的 busy
   全部失效。现在统一用 `session_running(sid)`（窗口 + 提供商逻辑槽）与
   `any_window_busy()` / `any_task_busy()`。
3. **提供商会话的漏判**：`resume_session_task` 只看 `WINDOWS`，提供商会话（占 `SLOTS`）
   会被重复拉起第二个 runner → 改用 `session_running`。
4. **删除会话**：先 `_window` 独占一个标签页再点侧栏，取到窗口后复检运行权；
   以前会和任务抢页、或被别的回合导航走。
5. **窗口池竞态**：并发 `acquire` 可能各建一个窗口、超过上限 → 加建窗锁；
   重复 `release` 会把同一窗口发两次 → `in_pool` 标记；页面被关掉后自动摘除。
6. **子代理网页并行**：`subagent_many` 以前只给提供商并行，网页通道永远顺序（文档却写着
   "= 窗口数"）。现在按窗口数 − 1 并行，且子代理**不再继承父任务的页面上下文**。
7. **任务级运行状态**：`runtime_update` / `ask_user_question` 回落改用 `current_session()`，
   并行时不再把 A 的进度写到 B 的界面上。
8. **"思考中"占位文本被当成答案**（实测抓到）：提问后回答区先出现
   `Checking that now… / Lining things up… / Digging in…`，随后**整段替换**成真答案。
   流式通道按公共前缀增长输出，占位文本一旦发出，真答案的前缀为空、永远发不出去，
   调用方只拿到占位文本。现在用两个信号剔除：① 已知短语表；② **结构信号**——
   回答区上方最后一行与 `Copilot said:` 后那行相同，就说明答案还没开始。
9. **「重试浏览器」卡在 needs_login**：启动失败会留下 about:blank 页；复用分支直接
   return、不再导航，重试永远起不来（而且只停 Playwright 不清 `ctx`，下次复用死上下文）。
   现在复用分支会确保页面在 Copilot 上，失败路径把 `ctx` 一并清掉。
10. **`/health` 暴露窗口池**：新增 `windows: {max, windows, busy, waiting, running}`，
   排障时能直接看到开了几个窗口、几个在忙。

### 本轮实测（真实登录态，2026-10-22）

```
单条普通对话          ALPHA-1234 / 42（此前会返回 "Checking that now…"）
两条普通对话并行      wall 16.2s（11.9s + 16.1s，串行约 24s），窗口池 peak=2
两条 Agent 任务并行   wall 51.5s / 44.5s，/health windows.busy peak=2，均成功
窗口数 2 运行时         /health: {"max":2,"windows":2,"busy":0,"waiting":0}
```

