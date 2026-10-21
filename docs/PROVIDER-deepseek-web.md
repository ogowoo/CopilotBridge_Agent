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
| **B. 做成 CBP 插件**（本仓库已实现） | 已落地 | 同上，但模型发现/健康状态在界面里可见 | ✅ 推荐：既省事又不用改核心 |
| **C. 原生接入 Copilot 桥接那套**（自己逆向 DeepSeek） | 几天 + 长期跟着上游改 | PoW/TLS/封号都要自己扛 | ❌ 不建议：纯粹是重复造轮子 |

**为什么 B 不改核心**：CBP 早就有 "OpenAI 兼容提供商" 抽象
（`cbp/providers.py` 的 `ApiProvider` + `PROVIDERS`），任何 OpenAI 协议的服务
都能直接变成 `提供商名/模型名`。而插件系统暴露了 `ctx.register_provider()`，
所以"新增一个模型来源"天然属于插件层，不需要碰 `cbp/bridge.py` 的浏览器桥接。

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

# 2) 在 CBP 里启用插件（插件管理 → deepseek-web → 启用），必要时改 base_url / api_key

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
- **并发**：免费账号约 2 并发，多子代理并发跑会排队（CBP 的 `subagent_parallel` 会自动限）。
- **上下文**：反代的多轮复用是"代理侧缓存"（靠 `X-Conversation-Id`/`user`），
  与 CBP 的会话/rollover 不是一回事；长任务仍靠 CBP 自己的摘要+换会话。
- **工具调用**：反代用 DSML 注入实现 function calling，但 **CBP 的 Agent 循环不需要它**——
  我们把工具协议写在提示词里（`AGENT_PROTOCOL`），模型只要能输出 JSON 就能用工具，
  所以哪怕反代完全不支持 tools，Agent 模式照样能跑。

## 五、如果以后要"原生"接（不推荐，留档）

真想做在进程内，需要补的东西（按我们现在的架构）：

1. `cbp/web_deepseek.py`：PoW 求解（引入 WASM 或纯 Python SHA3 实现）+ token 刷新；
2. TLS 指纹（curl_cffi 或 Playwright `route` 改 header）——否则 WAF 拦；
3. 把反代的 SSE 解析搬到我们自己的 `send()`/`wait_answer()` 生命周期里；
4. 封号/限流要接进我们已有的 `suspended`（限流挂起）与 `transient` 事件模型。

成本远大于收益：上面第 1~3 步正是那 4 个项目做过的事，而且它们会持续跟进上游变化。
