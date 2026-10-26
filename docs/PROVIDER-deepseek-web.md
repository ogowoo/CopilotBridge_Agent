# 用 DeepSeek 网页版当免费/备用模型的三种接法（调研 + 我们的插件）

> 目标：像 Copilot 那样"用网页版当模型"，但 DeepSeek 那边**已经有成熟反代**，
> 我们不该重复逆向。本文给出调研结论、三种接法的代价，以及本仓库已经落地的插件。

## 一、调研：GitHub 上现成的实现

按"能不能直接用"排序（都要求你有 DeepSeek 账号）：

| 项目 | 语言/形态 | 对外协议 | 关键能力 | 备注 |
|---|---|---|---|---|
| [Fly143/deepseek-free-api](https://github.com/Fly143/deepseek-free-api) | Python + curl_cffi，**纯 HTTP**（无浏览器） | OpenAI `/v1/chat/completions`、`/v1/models`、`/v1/responses`、Anthropic `/v1/messages` | PoW 自动求解（Node WASM + Python 回退）、token 401 自动重登、动态模型发现、`reasoning_content`、Vision、文本文件上传、DSML 工具调用 | **README 顶部有封号警告**：1 天 → 3 天 → 8 天 → 永久；单账号并发≈2；token≈24h |
| [snake-aabb-wtf/deepseek-web2api-free](https://github.com/snake-aabb-wtf/deepseek-web2api-free) | Python/FastAPI（含 WebUI 账号池） | OpenAI + Anthropic | StreamSieve 流式筛分、DSML 工具调用、多账号轮询、客户端限流、token 统计 | 单模型 `deepseek-chat`，思考/联网由 `THINKING`/`SEARCH` 控制；Windows 有 `start.bat` |
| [CJackHwang/ds2api](https://github.com/CJackHwang/ds2api) | Go，可编译单文件/Docker/Vercel | OpenAI/Claude/Google 三家格式 | 多账号轮换、DSML 工具调用格式（被上面项目参考） | 部署最省事 |
| [NIyueeE/ds-free-api](https://github.com/NIyueeE/ds-free-api) | Rust（Playwright/Chrome 自动化） | OpenAI + Anthropic | DSML 工具调用 | Fly143 版就是它的 Python 重写（去掉了浏览器） |
| [evanoooo/DeepSeek-Free-API](https://github.com/evanoooo/DeepSeek-Free-API)、[BaiMeou/ds2api](https://github.com/BaiMeou/ds2api/)、[lumingya/universal-web-api](https://github.com/lumingya/universal-web-api) | 杂 | OpenAI | 通用"任意网页转 API" | 后两个更偏通用框架，不是 DeepSeek 专用 |

**共同的技术事实**（决定了我们该不该自己写）：

1. DeepSeek 网页端聊天接口要 **PoW**（`DeepSeekHashV1`，官方 WASM），请求前先
   `POST /api/v0/chat/create_pow_challenge` 再带 `x-ds-pow-response`；
2. 直接 HTTP 还得**伪装 Chrome TLS 指纹**（curl_cffi），否则被 WAF 拦；
3. token 约 **24 小时**过期，需要保存密码自动重登；
4. 没有官方 function calling，工具调用全靠 **DSML 提示词注入 + 解析**（和我们在
   Copilot 里用的 JSON 动作协议是同一类办法）；
5. **封号风险真实存在**，且随违规次数升级——这类方案只适合个人试验/备用。

## 二、三种接法，以及为什么选第 2 种

| 接法 | 工作量 | 风险/维护 | 结论 |
|---|---|---|---|
| **A. 只当外部提供商**（跑反代 → `config.json` 的 `providers` 加一条） | 5 分钟，**零代码** | 反代自己维护、自己承担封号 | 适合"我就想用一下" |
| **B. 做成 Gantry 插件**（本仓库已实现） | 已落地 | 同上，但模型发现/健康状态在界面里可见 | ✅ 推荐：既省事又不用改核心 |
| **C. 原生接入 Copilot 桥接那套**（自己逆向 DeepSeek） | 几天 + 长期跟着上游改 | PoW/TLS/封号都要自己扛 | ❌ 不建议：纯粹是重复造轮子 |

**为什么 B 不改核心**：Gantry 早就有 "OpenAI 兼容提供商" 抽象
（`gantry/providers.py` 的 `ApiProvider` + `PROVIDERS`），任何 OpenAI 协议的服务
都能直接变成 `提供商名/模型名`。而插件系统暴露了 `ctx.register_provider()`，
所以"新增一个模型来源"天然属于插件层，不需要碰 `gantry/bridge.py` 的浏览器桥接。

## 三、已实现：`plugins/deepseek-web`

```
plugins/deepseek-web/
├── plugin.json   # 权限：providers / api / ui；默认 base_url=http://127.0.0.1:8000/v1
└── plugin.py     # setup(): 注册提供商 dsweb + 面板 + GET api/status 探测
```

它做三件事：

1. **注册提供商**：把反代注册成 `dsweb`，于是模型 ID 是 `dsweb/deepseek-reasoner`、
   `dsweb/deepseek-default`…（与内置官方 `deepseek/…` 并存，互不影响）；
2. **健康探测**：`GET {base_url}/models`，4 秒超时；连不上时给出可读错误
   （httpx 空消息会退化成类名，不会只显示一行空白）；
3. **界面可见**：插件面板「DeepSeek 网页版反代」用 kv 视图显示
   `reachable / latency_ms / models_in_proxy / model_ids / hint`。

### 用法

```powershell
# 1) 起反代（任选其一；以 Fly143 版为例，需 Node.js 供 PoW）
docker run -d -p 8000:8000 -v ${PWD}/config.json:/app/config.json `
  ghcr.io/fly143/deepseek-free-api:latest
# 或：git clone 后 ./deploy.sh --bg（管理面板 http://localhost:8000/admin 里登录 DeepSeek 账号）

# 2) 在 Gantry 里启用插件（插件管理 → deepseek-web → 启用），必要时改 base_url / api_key

# 3) 选模型：dsweb/deepseek-reasoner（思考）/ dsweb/deepseek-default（直答）
```

### 不想装插件时（接法 A，零代码）

`config.json` 里直接加提供商即可，效果一样（只是没有健康面板）：

```json
{
  "providers": {
    "dsweb": {
      "base_url": "http://127.0.0.1:8000/v1",
      "api_key": "sk-dsapi",
      "models": ["deepseek-default", "deepseek-reasoner"],
      "api": "openai",
      "timeout_seconds": 300,
      "description": "DeepSeek 网页版反代"
    }
  }
}
```

## 四、注意事项

- **封号**：反代 README 明写"不建议使用"；DeepSeek 的处罚会累计升级。别拿主账号试。
- **延迟**：PoW 每次 1~3 秒；非流式要先等完整 SSE 再合并，长回答会更慢——
  这就是 `timeout_seconds` 默认给到 300 的原因。
- **并发**：免费账号约 2 并发，多子代理并发跑会排队（Gantry 的 `subagent_parallel` 会自动限）。
- **上下文**：反代的多轮复用是"代理侧缓存"（靠 `X-Conversation-Id`/`user`），
  与 Gantry 的会话/rollover 不是一回事；长任务仍靠 Gantry 自己的摘要+换会话。
- **工具调用**：反代用 DSML 注入实现 function calling，但 **Gantry 的 Agent 循环不需要它**——
  我们把工具协议写在提示词里（`AGENT_PROTOCOL`），模型只要能输出 JSON 就能用工具，
  所以哪怕反代完全不支持 tools，Agent 模式照样能跑。

## 五、如果以后要"原生"接（不推荐，留档）

真想做在进程内，需要补的东西（按我们现在的架构）：

1. `gantry/web_deepseek.py`：PoW 求解（引入 WASM 或纯 Python SHA3 实现）+ token 刷新；
2. TLS 指纹（curl_cffi 或 Playwright `route` 改 header）——否则 WAF 拦；
3. 把反代的 SSE 解析搬到我们自己的 `send()`/`wait_answer()` 生命周期里；
4. 封号/限流要接进我们已有的 `suspended`（限流挂起）与 `transient` 事件模型。

成本远大于收益：上面第 1~3 步正是那 4 个项目做过的事，而且它们会持续跟进上游变化。

## 六、本机接入记录（已跑通到"只差登录"）

实际在这台机器上做过的事，照着抄即可：

| 项 | 值 |
|---|---|
| 反代源码 | `C:\Tools\Test4All\dsweb-proxy`（Fly143/deepseek-free-api，独立 venv） |
| 反代进程 | `.\.venv\Scripts\python.exe proxy.py`，监听 **http://127.0.0.1:8000** |
| 管理面板 | **http://127.0.0.1:8000/admin**，Basic 认证默认 **admin / admin** |
| 凭证文件 | `dsweb-proxy/config.json`（含 DeepSeek token，甚至明文密码——**别提交、别外传**） |
| Gantry 插件 | `plugins/deepseek-web`（已在运行中的服务里 enable 并 loaded） |
| Gantry 侧状态 | `GET /v1/plugins/deepseek-web/api/status` |

当前实测状态（尚未登录 DeepSeek）：

```json
{"reachable": true, "ready": false, "models_in_proxy": [],
 "model_ids": [], "hint": "反代在线但没有可用模型：打开它的管理面板登录 DeepSeek 账号后刷新"}
```

此时从 Gantry 调 `dsweb/deepseek-default` 会拿到反代的明确报错（不是静默失败）：

```
503 没有可用账号，请先访问 /admin 添加并登录账号
```

### 还需要做的一步（只有这一步需要你的账号）

1. 打开 http://127.0.0.1:8000/admin （Basic 认证 `admin` / `admin`）；
2. 用**手机号或邮箱 + 密码**登录你的 DeepSeek 账号（登录后 token 自动写进 `config.json`）；
   - 若提示注册/登录被 WAF 拦（大陆 IP 常见），给反代配一个海外代理再试；
3. 回到 Gantry → 插件管理 → **deepseek-web** → 打开面板刷新（或调一次 `api/status`）；
   模型会出现：`dsweb/deepseek-reasoner`、`dsweb/deepseek-default`、`dsweb/deepseek-search`…
4. 之后在"新任务/继续对话"的模型下拉里直接选 `dsweb/…` 即可跑。

### 停止 / 重启反代

```powershell
# 停（按端口找进程）
Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
# 起（后台，日志在 dsweb-proxy/proxy.out.log）
cd C:\Tools\Test4All\dsweb-proxy
Start-Process .\.venv\Scripts\python.exe -ArgumentList proxy.py -WindowStyle Hidden `
  -RedirectStandardOutput proxy.out.log -RedirectStandardError proxy.err.log
```

### 续：模型一开始"看不见"的原因与修复

插件在**反代还没有模型**时载入，注册的是空列表；之后没人再注册一次，所以界面上一直看不到
`dsweb/*`（`/v1/models` 里也没有）。现在插件自带**后台刷新线程**（默认 20 秒，只在模型
集合变化时重新注册一次；`CBP_PLUGIN_NO_TIMERS=1` 可关），加上 `api/status` 每次探测成功
也会重新注册——所以顺序不再重要：反代后起、或登录后才有模型，都会自动出现。

界面侧只需**刷新页面**（模型下拉在启动时读一次 `/v1/models`）；下拉旁边的 ↻ 也能重读。

### 端到端实测（本机，反代未登录态）

| 测试 | 结果 |
|---|---|
| `GET /v1/models` | 4 个：`dsweb/deepseek-default` / `deepseek-reasoner` / `deepseek-search` / `deepseek-reasoner-search`，UI 新建任务下拉与「切换模型」下拉都能选到 |
| 非流式对话 `dsweb/deepseek-reasoner` | **1.9 s** 返回「接入成功」 |
| 流式对话 `dsweb/deepseek-default` | 9 行 SSE，`data: {...chat.completion.chunk}` 正常 |
| Agent 任务（工具循环） | **7.7 s**，2 步：`write_text` → `read_text` 均 ok，final 正确总结 |

> 注意：当时 `/api/config` 显示 **token 为空、accounts 池 0**——也就是反代在**未登录**下也能聊
> （游客态），但这条路径限流更紧（我最早对 `chat.deepseek.com` 的 HEAD 就吃到 429）。
> 想稳定就去 `/admin` 登录账号。
