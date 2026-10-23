# CopilotBridge Agent

> **无 Copilot 也能用**：浏览器按需启动——服务启动不碰浏览器，WebUI 与提供商模型（DeepSeek/Kimi/OpenAI 等）立即可用；只有用到 Copilot 时才拉起它，界面顶部会显示状态并提供「重试浏览器」。**注意**：lazy 模式下，重启后的第一个 Copilot 任务要等浏览器拉起与 SSO （企业租户可达数分钟），这段时间不算任务超时；提供商模型不受影响。

> **预览分支 `preview/multi-window`（10.3.0-preview.2）**：多窗口并行仍在收尾，已知问题与代价见 [docs/MULTI-WINDOW.md](docs/MULTI-WINDOW.md)；稳定线在 `main`（10.2.1，不含本功能），正式环境请用 `main`。

把 **Microsoft 365 Copilot 网页版** 包装成本地服务的桥接器：对外提供 **OpenAI 兼容的 Chat API**，并内置一个能读写本地文件、执行验证闭环的 **长期任务 Agent**。模型调用不经过任何官方 API——由 Playwright 自动化你本机的 Edge 浏览器，直接使用你已登录的 Copilot 网页会话。

```
客户端 ──HTTP──> FastAPI (app.py) ──Playwright──> Edge 持久化 Profile ──> m365.cloud.microsoft/chat
                     │
                     ├─ /v1/chat/completions      普通对话（OpenAI 协议，支持流式）
                     ├─ /v1/agent/completions     Agent 模式（工具调用 + 长任务）
                     ├─ /v1/agent/sessions/*      会话管理 / 暂停 / 恢复 / 删除
                     └─ /ui                       内置 Web 管理台
```

> **只想赶紧跑起来**：看 [一页版快速手册](docs/QUICKSTART.md)（安装 → 登录 → 启动 → 第一个任务），
> 也可以直接存一张图：[简体](docs/quickstart-zh.png) ｜ [繁體](docs/quickstart-zh-TW.png) ｜ [English](docs/quickstart-en.png)（`python scripts/make_quickstart_card.py` 可重新生成）。
> 细一步的：看下面 **快速开始**（4 条命令）→ **操作手册**（WebUI 怎么用、AGENT.md/skills 怎么写、怎么回滚、怎么自升级、故障排查）。

## 功能特性

- **OpenAI 兼容**：`/v1/chat/completions` 支持 stream / non-stream，可接入任何 OpenAI SDK 客户端；`/v1/models` 列出可用模型。模型列表以内置表为基础（Auto / Quick response / Think deeper / GPT / GPT-5.6 / Sol / Think，可在配置中扩展），并**在启动时自动打开 Copilot 模型菜单枚举选项（含 GPT 等父项下的二级子菜单）**——菜单里新出现的模型会自动并入（ID 形如 `copilot-gpt-5-7`），选择模型时也会自动展开子菜单定位；也可用 `POST /v1/models/refresh` 随时重扫。
- **API 提供商接入**：除 Copilot 网页自动化外，可直接接入任何 OpenAI 兼容 HTTP API（内置 OpenAI / DeepSeek / Kimi / Grok 预设，可自定义）。模型 ID 形如 `deepseek/deepseek-chat`，API Key 走环境变量；普通对话和 **Agent 模式（含工具循环）都可用**——JSON 动作协议是纯 prompt 驱动，与提供商无关。
- **Agent 模式**：Copilot 每回合返回一个 JSON 动作（调用工具或最终答复），本地执行工具后把结果送回，循环直到任务完成。**支持批量动作**：一次回答可带多个工具动作（`{"type":"actions","actions":[...]}` 或直接给动作数组），按顺序执行、每步单独记账——写多个小文件/多处小替换不必一块一个回合，省下大量 Copilot 回合；批量里不允许 `final`（完成必须单独返回，验证闭环不被绕过），动作数上限由 `max_actions_per_turn` 控制，批量被输出上限截断时仍会执行已完整的前几个动作。
- **15+ 个工作区工具**：目录/文件读写（Base64 写入防 JSON 转义损坏）、精确替换、复制/移动/删除、`validate_python`、`run_unittest`、`run_python`，全部限制在工作区根目录内。
- **Copilot 直接生成文件（`copilot_file`）**：让 Copilot 把结果生成为**可下载文件**，桥接再把文件下载进工作区——内容**不经过聊天消息**，因此完全绕开 Copilot 单条输出上限，一个大文件一个回合就能拿到，不再需要 b64 分块。支持 `blob:`/`data:` 链接与"下载"按钮（点击并用浏览器下载事件接住），落盘前照样存原像（可回滚）、写 `.py/.json/.html` 仍进验证闭环。开关：`agent.allow_copilot_file`（默认开）。找不到下载入口时会保存 `last_answer.html` 供排查，并提示改回分块写入。
- **两级项目上下文（全局 + 项目）**：`AGENT.md` 与 `skills/` 都有两个作用域——**全局**放在服务安装目录（对所有工作区生效），**项目**放在工作区根目录；两份合并注入，同名技能/约定项目级优先，同一文件自动去重。全局技能位于工作区之外，用 `skill_read` 工具读取（`skill_list` 列清单）。`AGENT.md` 单份默认 8000 字符截断（`agent_md_max_chars`），SKILL.md 支持 YAML frontmatter（`name`/`description`），缺省回退"首个一级标题 + 首个段落"。本仓库自身即带全局 [AGENT.md](AGENT.md) 与示例技能。
- **服务自升级（可选，默认关闭）**：`agent.allow_self_upgrade: true` 后，Agent 可用 `self_read_text`/`self_write_text_b64`/`self_append_text_b64`/`self_replace_text` 直接修改**服务自身代码**（即使工作区是别的项目），白名单限定 `cbp/`、`webui/`、`scripts/`、`docs/`、`tests/` 与若干顶层文件，`config.json`/`data/`/`EdgeProfile/` 禁止触碰；写完必须 `self_validate` + `self_unittest` 双通过（提前 final 会被 `validation_gate` 拦下），并提供 `self_rollback` 工具回滚本次自升级。
- **改动快照与两级回滚（自我升级 failback）**：写操作**执行前**把目标原像存入 `data/snapshots/<sid>/`（工作区）与 `data/snapshots/_self_<sid>/`（服务代码），同一路径保留最早状态**并按 Copilot 回合计版本（一次 complete = 一个版本）**，因此可整任务回滚到"任务开始前"，也可按回合回滚。入口：WebUI「更多 ▾ → 回滚任务改动…／回滚服务自升级…」、`POST /v1/agent/sessions/{sid}/rollback`（可带 `{version}`）、`POST /v1/agent/self/rollback?last=true`，以及**服务/WebUI 起不来时的离线通道** `python scripts/rollback.py [--scope self] --last --yes [--rev N]`（`app.py` 导入失败会自动打印指引）。自升级写盘会落 `data/.self_upgrade_pending` 标记，服务重启后自动归档。
- **M365 文件与软件集成**：
  - **Office 文档工具**：`read_excel`/`write_excel`（openpyxl）、`read_docx`/`write_docx`（python-docx）、`read_pptx`（python-pptx）、`read_pdf`（pypdf）——Agent 可直接分析/修改 xlsx、docx、pptx、pdf；
  - **Copilot 文件上传**：`copilot_attach` 工具把工作区文件挂到 Copilot 网页版附件框并取回分析结果，用于图片/扫描件/复杂版式等本地无法解析的文件；
  - **Outlook 集成**：`outlook_unread`/`outlook_read`/`outlook_search`/`outlook_send`。**默认走 OWA 网页自动化**（复用桥接浏览器与 SSO 登录态；New Outlook 无 COM、EWS 2026 起被微软禁用、Graph 需管理员授权，网页版是当前唯一可行路径）；装了经典 Outlook 的机器自动保留 COM 兜底。读取默认开，发送由 `allow_outlook_send` 单独控制。OWA 模式下 `outlook_read` 的参数按主题关键词定位（没有 entry_id 概念）。
- **长任务韧性**：
  - 每个回合注入唯一标记 `[CBP-TURN:...]`，确认送达后才读取回答，杜绝读到上一回合的旧答案；
  - 严格区分三类失败——**限流**（原地指数退避重试）、**对话达到长度上限**（总结进度后切换新会话重发）、**回答格式错误**（容错解析 + 压缩上下文后原会话继续）；
  - **会话治理模型**：M365 Copilot 的对话长度上限不公开且动态变化，本地压缩只缩短后续提示、**不会缩短远端对话**，因此治理依赖组合拳——本地持续压缩 + 保守阈值主动换会话 + 页面上限检测被动兜底 + 检查点接力（摘要/修改文件/验证结果带入新会话）；
  - **幂等保护**：换会话接力或回合重发时，与上一成功动作完全相同的非幂等操作（append/move/copy）会被跳过，避免重复修改文件；
  - **实测校准**：每个 Copilot 会话结束时记录实测数据（回合数/估算 Token/字符数/结束原因/模型/URL）存入 `conv_stats` 表，会话详情接口可查，用于持续校准本地阈值；
  - 修改代码后强制验证闭环：`validate_python` 与 `run_unittest` 都通过才允许宣称完成。
- **会话持久化**：SQLite（WAL）保存步骤时间线、文件变更哈希、对话消息；服务重启后可从检查点继续。
- **Web 管理台**：`/ui` 提供任务发起（按钮＋弹窗）、实时进度（执行状态/当前回答/发送内容/继续对话 四选项卡）、全宽步骤时间线（可导出 JSON）、暂停/恢复/取消、会话归档与远程 Copilot 会话删除（带同 Session 标记防误删与对账终验）。详见[操作手册](#1-webui-怎么用)。

## 快速开始

环境要求：Windows + Microsoft Edge + Python 3.11+（其他平台将 `browser_channel` 改为 `chrome` 或去掉该配置使用内置 Chromium）。

```powershell
# 1. 安装依赖（pip + Playwright 浏览器）
python install.py

# 2. 复制并按需修改配置
copy config.example.json config.json

# 3. 登录 Copilot（会打开 Edge，登录完成后回车）
python scripts/login.py

# 4. 启动服务（默认 http://127.0.0.1:8765）
python start.py
```

启动后打开 `http://127.0.0.1:8765/ui` 使用管理台，或访问 `/docs` 查看 API 文档。

### 多账号：换用有 M365 订阅的账号

登录态保存在 `EdgeProfile/`，Windows 的账户选择器（WAM）常会**自动推荐系统当前登录的普通账号**，导致桥接用错账号。切换方法（任选其一）：

1. **直接在自动化窗口里换**（服务有窗口模式运行时）：在自动化 Edge 的 Copilot 页面右上角头像处「注销」，再用订阅账号登录——登录态自动保存，无需重启服务；
2. **重新走登录向导**（先停服务）：`python scripts/login.py`，弹出账户选择器时务必点「使用其他账户 / Use another account」，输入订阅账号；
3. **登录态已混乱时彻底重来**：`python scripts/login.py --reset` 删除 EdgeProfile 后全新登录。

### 普通对话（OpenAI SDK）

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="unused")
resp = client.chat.completions.create(
    model="copilot-auto",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

### API 提供商（DeepSeek / Kimi / OpenAI / Grok …）

模型 ID 用 `提供商/模型` 格式，例如 `deepseek/deepseek-chat`、`kimi/kimi-k2-0905-preview`、`openai/gpt-4o`、`grok/grok-4`。**API Key 通过环境变量配置**（不写进配置文件、不进 git）：

```powershell
$env:DEEPSEEK_API_KEY="sk-..."   # 或 MOONSHOT_API_KEY / OPENAI_API_KEY / XAI_API_KEY
```

未配置 Key 的模型在 `/v1/models` 里会标注"（未配置 API Key）"。

> 想把 **DeepSeek 网页版**也当模型用（免费/备用）：见 [三种接法与已落地的插件](docs/PROVIDER-deepseek-web.md)——起一个反代 + 启用 `deepseek-web` 插件即可，模型 ID 形如 `dsweb/deepseek-reasoner`。自定义任意 OpenAI 兼容提供商（含自建网关）：

```json
"providers": {
  "myapi": {
    "base_url": "https://example.com/v1",
    "api_key_env": "MYAPI_KEY",
    "models": ["my-model"],
    "description": "自建网关"
  }
}
```

提供商模型在所有入口都可用：OpenAI 兼容对话（流式透传）、WebUI 新任务（普通对话/Agent 执行）、Session 跟进。普通对话用 Session 的消息记录作上下文；Agent 模式复用同一套 JSON 动作协议与工具循环。提供商限流/5xx 会自动走与 Copilot 相同的指数退避重试。

**代理（重要）**：浏览器访问 API 走的是 **Windows 系统代理**，而 Python 的 httpx 默认只读 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量、**不看系统代理**——这会造成"浏览器能打开、服务却报提供商不可用"的假故障。现在提供商请求默认**跟随系统代理**（读注册表），传输层失败且代理是自动探测来的，还会**直连重试一次**。可显式配置：

```json
"providers": {
  "deepseek": { "proxy": "http://127.0.0.1:10809" },   // 显式代理
  "kimi":     { "use_system_proxy": false }            // 强制直连（代理故障时用）
}
```

**一条命令自检连通性**（会分别打印"经代理/直连"两条路径的真实结果）：

```powershell
python scripts/provider_selftest.py                    # 全部已配置提供商
python scripts/provider_selftest.py --provider deepseek
```

### Agent 任务

```bash
curl -X POST http://127.0.0.1:8765/v1/agent/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "copilot-auto",
    "messages": [{"role": "user", "content": "分析工作区项目并生成 TODO 清单"}],
    "workspace": "C:/path/to/project"
  }'
```

## 配置说明（config.json）

### 顶层

| 字段 | 默认 | 说明 |
|---|---|---|
| `copilot_url` | `https://m365.cloud.microsoft/chat/` | Copilot 入口地址 |
| `browser_channel` | `msedge` | Playwright 浏览器通道（`msedge` / `chrome` / 省略用内置 Chromium） |
| `headless` | `false` | 无头模式；启动失败会自动回退有窗口模式 |
| `reply_timeout_seconds` | `180` | **无进展**超时：回答连续这么久没有新文本才判超时 |
| `reply_hard_timeout_seconds` | `1200` | 单回合硬上限（兜底，防止无限等待） |
| `stable_seconds` | `3` | 回答文本稳定多久视为生成结束 |
| `poll_interval_seconds` | `0.35` | 页面轮询间隔 |
| `diagnostics` / `diagnostic_html` | `true` / `false` | 出错时保存 `last_page.txt`（/ `.html`）快照 |
| `default_model` | `copilot-auto` | 默认模型 ID |
| `server.host` / `server.port` | `127.0.0.1` / `8765` | 监听地址。**绑非回环地址且未设 api_token 时服务拒绝启动** |
| `api_token` | 空 | 设置后所有 `/v1/*` 端点要求 `Authorization: Bearer <token>`（或 `?token=`） |
| `models` | `{}` | 扩展模型：`{"copilot-custom": {"ui_names": ["Custom"], "description": "..."}}` |
| `markers` | 见下 | Copilot 页面可访问性文本锚点，界面语言不同时在此追加文案 |

`markers` 的配置值会**并入**内置默认列表（不是覆盖），任一命中即生效。
内置默认已含 **英文 + 中文 + 日文**（`Copilot said:` / `You said:` / `Sources` /
`出典` / `あなたの発言:` / `Copilot の回答:` 等），另外按钮/浮层文案
（`stop` / `new_chat` / `send_aria` / `dismiss`）也可在这里追加：

```json
"markers": {
  "assistant_turn": ["Copilot said:", "Copilot の回答:"],
  "user_turn": ["You said:", "あなたの発言:"],
  "answer_end": ["\nSources\n", "\nMessage Copilot\n", "\n出典\n"],
  "sources": ["Sources", "出典"],
  "ignored_lines": ["Copy", "复制", "更多", "コピー", "その他"],
  "stop": ["Stop generating", "停止生成", "生成を停止"],
  "new_chat": ["New chat", "新建聊天", "新しいチャット"],
  "send_aria": ["Send", "发送", "送信"],
  "dismiss": ["Accept", "接受", "同意する"]
}
```

- `assistant_turn` / `user_turn`：回合分隔文本，用于定位最新回答与统计用户回合数——**界面非英文/中文导致读取超时时，优先在这里追加对应语言锚点**；
- `answer_end`：回答正文的结束标记（来源区、输入提示等）；
- `sources`：回答中出现即视为生成完成的区块标题；
- `ignored_lines`：从回答中剔除的按钮残留文本；
- `stop` / `new_chat` / `send_aria` / `dismiss`：停止生成、新建对话、发送按钮、弹窗关闭的文案锚点（日文系统实测"能发出去但收不到回信"就是这组锚点没命中）。

> 日文等其它语言的默认文案是**按常见写法内置**的，若测试租户用了不同措辞，
> 直接用 `markers` 追加即可，不必改代码。

### agent 段

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 关闭后 Agent 接口返回 503 |
| `workspace` | `workspace` | 工具根目录（相对项目目录） |
| `workspace_roots` | `[]` | 工作区允许根目录白名单（绝对路径数组）；非空时切换工作区只能切到白名单内，防止沙箱被整体搬移 |
| `session_store` | `data/agent.db` | SQLite 会话库路径 |
| `max_total_steps` | `1000` | 单任务总步数上限（硬顶 10000） |
| `max_steps_per_conversation` | `80` | 单个 Copilot 会话内步数上限（触发换会话） |
| `max_conversation_rollovers` | `25` | 最多切换多少个 Copilot 会话 |
| `context_token_budget` | `80000` | 估算上下文预算（本地估算值，**不代表 M365 真实上限**） |
| `compact_at_tokens` | `35000` | 达到即压缩历史为摘要 |
| `rollover_at_tokens` | `55000` | 达到即换新会话（需 `auto_rollover`）；须满足 `0 < compact < rollover ≤ budget` |
| `keep_recent_steps` | `8` | 压缩/换会话时保留的最近步数 |
| `max_summary_tokens` | `5000` | 长期摘要估算上限 |
| `max_context_chars` | `240000` | 字符数兜底阈值 |
| `auto_rollover` | `true` | 建议开启：本地压缩不会缩短远端对话，保守阈值主动换会话是主要防线；页面上限检测做被动兜底 |
| `min_steps_before_rollover` | `20` | 主动换会话的最小步数门槛 |
| `auto_compact` | `true` | 达到阈值时自动压缩上下文；`false` 时仅保留手动压缩 |
| `auto_resume` | `true` | 服务重启后自动恢复上次被中断（非用户暂停）的会话 |
| `session_timeout_seconds` | `86400` | `ready`/`waiting` 会话超过该时长未活动则标记为 `expired` |
| `completed_session_retention_days` | `30` | 已结束（完成/取消/失败/过期）会话超过该天数后自动归档 |
| `max_sessions` | `100` | 未归档会话数量上限，超出时从最久未更新的开始归档 |
| `max_parse_failures` | `5` | 连续解析失败多少次后强制压缩上下文 |
| `max_actions_per_turn` | `4` | 一次回答里最多执行几个动作（批量协议，硬顶 12）；调大能省回合但更容易被 Copilot 输出上限截断 |
| `allow_copilot_file` | `true` | 是否开放 `copilot_file`（让 Copilot 直接生成文件并下载进工作区，内容不走聊天消息、不受输出上限约束）。**注意**：`copilot_file` 与 `copilot_attach` 依赖 Copilot 网页会话，因此在使用 API 提供商模型（如 `deepseek/…`）的会话里**自动禁用**（既不注入也不执行，避免白烧回合） |
| `copilot_file_model` | `""` | `copilot_file` 生成文件时用的具体模型。会话模型是 `Auto` 时，Auto 通常只聊天、不生成下载入口（实测），配置此项（如 `copilot-gpt-5-6-sol-think-deeper`）即可自动改用；留空则沿用会话模型并在失败时明确提示原因 || `max_service_unavailable` | `30` | 限流退避重试上限，超过后转入安全暂停 |
| `service_retry_base_seconds` / `service_retry_max_seconds` | `30` / `600` | 限流指数退避的起始/上限 |
| `service_retry_window_seconds` | `3660` | **Copilot 网页**的限流窗口：首次超限后到可再次使用大致要等这么久（实测约 1 小时，多加 1 分钟余量），**居中倒计时卡片**锚定"首次超限 + 该窗口"。**API 提供商不用这个窗口**（它们的 429/5xx/网络抖动通常几秒~几分钟就恢复，套 1 小时会误导），而是按本轮退避显示"下次重试"；卡片标题按当前模型通道分别显示"Copilot 限流/不可用"或"API 服务限流/不可用" |
| `max_read_chars` / `max_write_chars` | `120000` / `200000` | 工具单次读写上限 |
| `allow_delete` | `false` | 是否开放 `delete_path` 工具 |
| `allow_python` | `false` | 是否开放 `run_python` 工具（**可执行任意代码，谨慎开启**） |
| `allow_office` | `true` | 是否开放 Office 文档工具（Excel/Word/PPT/PDF） |
| `allow_memory_write` | `true` | 是否允许 Agent 用 `memory_write` 沉淀**长期记忆**（跨会话事实/偏好/结论，自动按相关性检索注入；`memory_forget` 可删除） |
| `browser_startup` | `lazy` | 浏览器启动策略：`lazy`=按需启动（没有 Copilot 也能用 WebUI 与提供商模型）；`eager`=服务启动即预热 |
| `allow_skill_write` | `true` | 是否允许 `skill_create` 沉淀技能（项目级写入工作区 `skills/`；全局级还需 `allow_self_upgrade`） |
| `allow_outlook` | `false` | 是否开放 Outlook 读取工具（未读/正文/搜索） |
| `allow_outlook_send` | `false` | 是否开放 `outlook_send`（会真实发出邮件，**谨慎开启**） |
| `outlook_url` | `https://outlook.office.com/mail/` | OWA 自动化入口（`allow_outlook` 时使用） |
| `python_timeout_seconds` | `20` | `run_python` 超时 |
| `snapshot_retention` | `50` | 保留多少个会话的改动快照，超出按最旧清理 |
| `agent_md_max_chars` | `8000` | 单份 `AGENT.md` 注入上限（全局、项目各自计算） |
| `allow_self_upgrade` | `false` | 是否允许 Agent 修改**服务自身代码**（`self_*` 工具，**高权限，理解风险后再开**） |
| `self_upgrade_paths` | 见 `config.example.json` | 自升级可触达的路径白名单（安装目录相对路径） |

## 命令行（headless）

```bash
pip install -e .                    # 之后可用 cbp 命令（也可以 python -m cbp.cli）

cbp serve --port 8765               # 启动服务（等价 python start.py）
cbp status                          # 健康检查
cbp run "把 docs 里的死链列出来" --model kimi-coding/k3
cbp run "重构 X" --plan             # 计划模式：只读探索 → 产出方案后停
cbp run "分析这个仓库" --sandbox read-only --workspace C:/proj
cbp sessions --limit 10             # 会话列表
cbp tools                           # 62 个工具
cbp plugins list|enable|disable|reload [name]
cbp mcp list                        # MCP server 状态
cbp mcp add --name fs --command npx --arg -y --arg @modelcontextprotocol/server-filesystem --arg C:/data
cbp sandbox read-only --persist     # 切沙箱档位并写回 config.json
```

- 统一参数：`--url`（默认 `http://127.0.0.1:8765`，或 `CBP_URL`）、`--token`（或 `CBP_TOKEN`）
- CLI **不在进程内启动浏览器**：它调同一个服务的 HTTP 接口，和 WebUI 看到同一份事实
- `--json` 输出原始响应，方便脚本消费；`--quiet` 不打印步骤摘要
- 连不上服务时返回非零退出码并说明先 `cbp serve`

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（含忙碌状态） |
| GET | `/v1/models` | 模型列表 |
| POST | `/v1/models/refresh` | 重新枚举 Copilot 模型菜单，并入新模型 |
| POST | `/v1/chat/completions` | OpenAI 兼容对话 |
| POST | `/v1/agent/completions` | 发起/继续 Agent 任务 |
| GET | `/v1/agent/sessions` | 会话列表 |
| GET | `/v1/agent/sessions/{sid}` | 会话详情（步骤/文件事件/消息） |
| GET | `/v1/agent/sessions/{sid}/runtime` | 实时运行状态（无 SSE 时的轮询接口） |
| GET | `/v1/uploads/{name}` | 取"粘贴附件"图片（`data/uploads` 下的文件名；严格文件名校验，禁分隔符与 `..`；走 api_token 鉴权，前端 img 标签用 `?token=`） |
| POST | `/v1/shutdown` | **终止服务**（WebUI 的 ⏻ 按钮）。先回响应再停机；优雅停机（uvicorn `should_exit`），拿不到 server 时退回 SIGINT，宽限期后硬退出兜底；幂等 |
| GET | `/v1/agent/sessions/{sid}/events` | **SSE 实时推送**（`text/event-stream`）：先送一份 runtime 快照，随后推送 `runtime`/`queue`/`messages`/`session` 事件；15 秒无事件发心跳；客户端断开自动退订。`api_token` 可用 `?token=` |
| POST | `/v1/agent/sessions/{sid}/messages` | 在会话中追加消息（Agent 或普通对话模式）；**入队即返回** `queued/position`，后台按序处理（要等当前这轮跑完才起新一轮） |
| POST | `/v1/agent/sessions/{sid}/inject` | **强行插入指令**（DSH Ctrl+J 式）：写进 `pending` 并**立即中断当前回合**——提供商会话取消底层 HTTP 请求（无状态，取消即"这轮没发生"）；Copilot 网页会话**只在回答尚未开始生成时**中止等待（已开始生成就让它答完，避免丢回答/错位）。随后按新指令继续；会话空闲时自动转为排队跟进 |
| GET | `/v1/agent/sessions/{sid}/queue` | 查看该会话队列（等待项 + 正在处理的一条，后者标 `processing`；界面只显示等待项） |
| POST | `/v1/agent/sessions/{sid}/queue/{item_id}` | 修改排队中消息的内容 / 模型（与消息历史同步） |
| DELETE | `/v1/agent/sessions/{sid}/queue/{item_id}` | 删除排队中消息（与消息历史同步） |
| POST | `/v1/agent/sessions/{sid}/actions/{pause\|resume\|cancel\|compact\|rollover\|archive}` | 会话控制 |
| POST | `/v1/agent/sessions/{sid}/activate` | 把自动化浏览器切到该会话页面 |
| DELETE | `/v1/agent/sessions/{sid}?delete_remote=true&force_local=true` | 删除会话（可选连带删除 Copilot 网页会话） |
| POST | `/v1/agent/sessions/{sid}/model` | 切换该会话使用的模型（落库，下一回合生效，服务重启后仍沿用） |
| GET | `/v1/agent/sessions/{sid}/snapshots` | 任务改动快照清单（可回滚的路径/类型 + 回合版本列表 `versions`） |
| GET | `/v1/agent/sessions/{sid}/prompt` | 查看该会话的提示词：`initial_prompt`（首回合完整内容，含系统提示词/工具清单/项目约定）与 `last_prompt`；旧会话未落库时按当前工作区近似重建并置 `rebuilt=true` |
| POST | `/v1/agent/sessions/{sid}/rollback` | 回滚该任务的工作区改动；可选 body `{version:N}` 撤销第 N 回合之后的改动（运行中返回 409） |
| GET | `/v1/agent/self/status` | 自升级状态（是否启用、白名单、待重启标记、快照作用域） |
| GET | `/v1/agent/self/snapshots?sid=` | 自升级改动快照清单 |
| POST | `/v1/agent/self/rollback?sid=&last=` | 回滚服务自身代码（`last=true` 取最近一次） |
| GET/POST | `/v1/agent/workspace` | 查询/切换工具工作区 |
| GET/POST | `/v1/agent/browser` | 查询/切换浏览器窗口显示（无头 ↔ 有窗口；带登录态重启浏览器上下文，`persist=true` 可写回 config.json） |
| GET | `/v1/agent/browse?path=` | 一层目录列举（只列子目录），供工作目录选择器；不传 path 从当前工作区开始 |
| POST | `/v1/agent/browse/mkdir` | 在选择器里新建子目录（只允许单层名，拒绝分隔符/`..`） |
| POST | `/v1/agent/pick-directory` | 系统文件夹对话框（native）。**注**：由服务托管启动的进程若继承了非交互窗口站，对话框不可见——超时 15s 返回 400，WebUI 只用应用内浏览 |
| POST | `/v1/agent/deep-research` | 一键深研：`{query, model, angles}` → 证据包 + 一次直连综合 → 带来源的报告（落成 `research-*` 会话 + 工作区 md） |
| GET | `/v1/plugins` | 插件列表（已加载/启用状态、权限、贡献点） |
| POST | `/v1/plugins/{name}/{action}` | 插件操作：`enable` / `disable` / `reload` |
| ANY | `/v1/plugins/{plugin}/api/{path}` | 插件自定义接口（如 `web-search` 的 `api/search`） |
| GET | `/ui` | Web 管理台 |

## 操作手册

- `sandbox_mode`：`read-only` / `workspace-write`（默认）/ `danger-full-access`。
  `read-only` 下所有变更类工具（写文件/执行命令/office 写/发邮件…）在执行前就被拒绝；
  `danger-full-access` 允许写到工作区之外，但**服务安装目录仍受保护**（除非开 `allow_self_upgrade`）。
- 运行时可用 `GET/POST /v1/agent/sandbox` 查询/切换（`persist:true` 写回 config.json）。

### 1. WebUI 怎么用

打开 `http://127.0.0.1:8765/ui`（建议强刷 `Ctrl+F5` 确保加载最新前端）。

| 位置 | 操作 |
|---|---|
| 侧栏顶部 **＋ 新任务** | **打开新会话编辑器**（不再是弹窗）：大输入框 + 模型 + 工作目录（📁 打开目录选择器，默认 workspace）+ **Agent 执行 / 普通对话** + 「开始」；`Enter` 发送、`Shift+Enter` 换行 |
| 输入框里的 **`/` 命令** | 输入 `/` 弹出命令面板（↑↓ 选择、Tab/Enter 补全、Esc 关闭）：`/research 问题`（深研）、`/goal 目标`（目标模式）、`/plan 任务`（计划模式）、`/agent`（切 Agent 模式）、`/chat`（切普通对话）、`/help`（列出命令）。带参数直接回车即执行，不带参数则补全后等输入 |
| 侧栏 **SESSIONS** 的工作区分组 | 会话按**工作区**分组（组头：折叠箭头 + 文件夹图标 + 名称 + 会话数）；点组头折叠/展开（状态存 localStorage）；组头右侧 **＋** = 在该工作区新建会话；工作区名字悬停看完整路径 |
| 子代理会话的归集 | 子会话**折叠在父会话下**（父行左侧 `▸`，点开缩进显示，状态存 localStorage）；删除父会话会**级联删除**子代理，不留孤儿 |
| 侧栏 **SESSIONS** | 会话列表（行首来源标记：⏱ 定时任务 / 🔎 深研 / 子 子代理；状态圆点：绿=运行、黄=等待/暂停、红=失败、蓝=完成）；`筛选…` 按标题/ID/**工作区**过滤；图标行：**＋新会话**、**📁新建工作区**、📖 使用手册、▣ 切换自动化浏览器窗口、◐ 轮换主题（**默认跟随系统**；循环：跟随系统 → 深色 → 琥珀 → 墨玉 → 浅色 → 护眼米 → 薄荷，3 暗 / 3 亮，选择刷新保持，系统主题变化时自动跟随） 等 |
| 侧栏左缘细长拉手 | 收起/展开整个侧栏（会话列表占满宽度） |
| 顶部操作条 | 暂停 / 恢复 / 压缩上下文 / **深研**（多角度搜索 + 抓正文 → 带来源报告，新会话）/ 继续执行 / 切换到页面（把自动化 Edge 跳到该会话）/ 打开会话（在你自己浏览器打开）/ 取消任务 / 更多 ▾（插件管理、归档、回滚任务改动、回滚服务自升级、删除记录、连同 Copilot 会话删除） |
| 顶栏右侧**模型选择器** | 显示**当前会话自己的模型**，可直接切换；落库到该会话，**下一回合生效、服务重启后仍沿用**。选项来自 `/v1/models` 的可用模型；若会话原先的模型已不在菜单里，会标注"（菜单里未找到）" |
| 顶栏 ID 芯片 | 显示当前会话 ID，点击复制（删除前对号用） |
| KPI 条 | 状态、总步骤（含子代理）、当前会话步骤、会话（第几个 Copilot 对话，与侧栏同一口径）、TOKEN 估算（含子代理，带预算进度条） |
| 目标 / 计划条 | 计划模式下显示方案状态；有目标时显示 `🎯 目标（第 r/max 轮 · 状态）`，有 todo 时显示 `☑ 计划 完成数/总数`；无目标/无计划时整条隐藏 |
| 实时面板（四选项卡） | **当前执行状态**（阶段/等待详情/计数）、**当前回答**（模型最新回答，结构化动作自动转 JSON 视图、普通回答按 Markdown 渲染；提供商流式返回时**逐字增长**；有更新时页签出现绿点）、**最近发送的内容**（发给模型的原文尾部）、**继续对话**（见下）|
| 消息编辑 / 删除 | 对话里每条已落库的消息悬停出现 ✎ / 🗑：编辑改内容、删除移除记录。**诚实语义**：只改本地历史，已发给网页 Copilot 的内容无法撤回；**提供商会话**会同步内存里的对话历史（`_PROVIDER_AGENT_HISTORY` 是增量累积而非每回合重建，不同步的话模型下一回合仍会看到旧文本），同步结果在提示里说明（`history_synced`），内存里已被裁剪找不到时如实告知而不是假装成功 |
| 🔍 跨会话搜索 | 侧栏搜索图标打开面板（再点 / Esc / 跳转后自动收起），搜**所有会话的对话正文**（后端 `GET /v1/search`，LIKE 通配符已转义——搜 `100%`/`_` 是字面量），结果列出会话标题、角色、时间与命中片段；**点结果即跳转**到该会话并把那条消息滚到视野中间高亮一下。归档会话不参与搜索 |
| 🏷 会话标签与分组 | 会话卡片悬停出现 🏷：设标签（留空取消）。侧栏列表**按标签分组**（有标签的在前、未打标签的收在最后一组），筛选框同时匹配标题/ID/状态/标签。标签是纯本地组织手段（`POST /v1/agent/sessions/{sid}/tag`），不参与 Agent 逻辑 |
| 时间线筛选 | 时间线头部两个下拉：**按工具**（选项自动来自当前步骤里出现过的工具）与**按状态**（仅成功 / 仅失败与被拦 / 仅事件），计数显示为 `m/n 条（筛选）`。默认无筛选时仍是增量渲染（展开状态不丢），筛选变化才整表重建 |
| 对话区流式气泡 | 提供商流式输出除了「当前回答」面板，**对话区也会显示一个「正在生成」临时气泡**（虚线边框 + 临时标签，来源就是 `answer_preview` 的 SSE 推送），正式消息落库后自动被真实消息替换。刻意与正式消息样式区分，避免把流到一半的 JSON 动作误当成最终答复 |
| ⬇ 导出对话 Markdown | 聊天栏「导出 MD」把当前对话导出为 `.md`（会话 ID、模型、导出时间、消息数头部 + 按角色分段 + 时间 + 附件引用），便于粘贴到别处或归档 |
| 工具结果双视图 | 时间线每步的「查看结果」提供 **渲染 / JSON** 两个视图：渲染视图把短标量排成键值行、长文本与对象用**和对话完全同一套代码块**渲染（语言按参数里的文件扩展名或工具名推断，如 `cbp/demo.py` → Python；`validate_python` → Python），因此同样有语言标签、语法高亮、行号、折叠、复制与「…」菜单；JSON 视图保留原有的结构化浏览 |
| 🌐 多语言（简 / 繁 / 英） | 侧栏语言按钮循环切换（`简 → 繁 → EN`），选择记在 localStorage，刷新后保持；切换即重画会话列表、对话、时间线与各面板。**覆盖范围**：全部静态界面 + 全部动态文案（toast、对话框、面板、记忆/技能、回滚与删除明细、演示数据、代码块与时间线标签）。**实现取舍**：以中文原文当 key（`tr('取消任务')`），缺翻译回落原文——不会出现 key 对不上导致界面空白；英文手写，**繁体由简繁短语表 + 字符表自动转换**并叠加台湾用语覆盖表（記憶體/檔案/設定/匯出/停機/強行/手冊…）。只翻译展示文案，**判定逻辑里的字符串一律不翻**；**JS 生成的 HTML 属性走 DOM 层翻译**（`translateAttrs()`,不改源码——把模板/拼接串里的属性包成 `tr()` 极易写坏，实测踩过三次）。后端与 Agent 协议文案保持中文，主要 API 的 note 带 `note_code`,前端据此本地化 |
| ⚡ 子代理并行扇出 | **API 提供商模型真并行**：`subagent_many` / `POST /v1/agent/fanout` 会把 2~6 个互不依赖的子任务按并发上限（`agent.subagent_parallel`，默认 4）同时跑，结果保序汇总、单项失败不影响其余；**网页 Copilot 通道按自动化窗口数并行**（`agent.copilot_windows`，1–4）：每个子代理独占一个标签页，父任务自己占一个，所以并发上限是「窗口数 − 1」；窗口数 = 1 时仍是顺序执行（与以前一致）。并行的前提是把「当前会话 / 是否允许网页工具 / 子代理递归深度 / 页面」从全局变量改成**任务级上下文**（contextvars），否则并发子代理会互相覆盖 |
| 对话阅读体验 | 消息卡片（头像/角色名/时间戳/悬停复制）；助手消息按内容形态**可视化**：JSON 动作 → **工具卡**（工具名 + 原因 + 参数 JSON 视图，`content_b64` 显示为"[Base64 内容，共 N 字符]"）、批量动作 → 一卡多工具、`{"type":"final"}` → 正文按 **Markdown** 渲染并标「最终答复」、其它 JSON → JSON 视图、```json 代码块也用 JSON 视图；解析不了的 JSON 明确标「JSON 解析失败（原文）」而不是装作没事 |
| Markdown 代码块 | 对齐 Copilot 观感：左侧**行号**（横向滚动时固定不跑）、**语言显示名**（PowerShell/Python/…）、**软换行**（默认折行，可在「…」里关掉）、复制图标与「**…**」菜单（复制代码 / **下载为文件**（按语言给扩展名）/ 行号开关 / 换行开关 / 折叠）。**点击行号选中整行**（可多行），代码条随即出现「**复制选中 N 行**」（复制文本不含行号）。**语法高亮 11 种语言**（python/js/ts/json/bash/powershell/sql/yaml/html/css + ```diff/patch：新增绿、删除红、@@ 块头蓝）；超 22 行折叠，偏好记住选择 |
| 图片与附件 | Markdown 的 `![alt](url)` 渲染为图（**仅 https/http 与 data:image**,svg 与 `javascript:` 不放行）、点击放大，断链显示"图片加载失败"占位而不是破图图标；**粘贴的图片附件**在消息下方显示缩略图、点击放大（消息行记录文件名，经 `GET /v1/uploads/{name}` 供图，走统一鉴权），**会话列表**对带附件的会话显示 📎 角标 |
| 其余阅读辅助 | 长回答超 1800 字折叠；**🔍 对话内搜索**（高亮命中、Enter/Shift+Enter 跳转；不会命中行号列）；新到达消息渐显（尊重 `prefers-reduced-motion`）。全部零依赖手写，不引任何 CDN（离线可用） |
| ⏻ 终止服务 / 退出 | 侧栏右上电源图标（**任何状态下都可达**）与会话「更多」菜单里都有；点击弹**二次确认**（说明正在跑的任务会中断、状态已落库可恢复）。后端 `POST /v1/shutdown` **先回响应再停机**（否则前端看到连接重置像失败），走 uvicorn **优雅停机**（`should_exit`，退出码 0），拿不到 server 对象时退回 SIGINT，并始终有"宽限期 8 秒后硬退出"兜底——按钮必须真的能停掉服务。停机后页面停止一切轮询并显示"服务已终止"横幅 |
| 提供商流式输出 | `stream_provider_agent`（默认 `true`）开启时，提供商回合走流式：增量边收边写 `answer_preview`,经 **SSE 实时推送**到界面（见下），是真的逐字增长。**流式仍带 `response_schema`**（结构化动作协议的解析率不牺牲）；只在**一个增量都没收到**时才退回非流式（已收到内容再重发会造成重复计费/两份回答） |
| SSE 实时推送 | WebUI 用 `EventSource` 连 `GET /v1/agent/sessions/{sid}/events`,由服务端**推送** runtime 快照、队列变化、新消息、会话列表变化——正常情况下不再轮询。连接建立先送一份当前快照；15 秒无事件发一次心跳（`: ping`）防中间设备掐断；断开自动退订（不留内存）并**退回轮询**（保留 350ms 快速轮询 + 按退避重连）。`api_token` 走 `?token=`（EventSource 不能带自定义头，鉴权中间件已支持） |
| 📄 初始提示词 | 面板头部按钮，查看该会话**首回合实际发给模型的完整提示词**（系统提示词 + 工具清单 + 项目约定 + 验证闭环规则 + 任务）——排查"Agent 为什么这么做"的第一现场。首回合内容会落库（`initial_prompt`），不再被后续回合覆盖；**旧会话**未落库时按当前工作区**近似重建**并在标题里标注 |
| 🧾 任务完成卡片 | 任务跑完（completed/failed/cancelled）后，对话区底部出现「本次改动 N 个文件 +X −Y」：逐文件列出路径与行级增删（对齐 DSH 的 *Edited N files*），改动大的排前面；超过 6 个折叠为「全部 N 个文件」。点一行**看 unified diff**（复用查看浮层，可复制）；`present` 登记的交付物以 chip 列出。数据来自**写操作前的快照原像**（不依赖 git），二进制文件（xlsx/docx/图片）只列路径。接口：`GET /v1/agent/sessions/{sid}/changes`、`GET …/changes/diff?path=`（只允许快照里记录过的路径，杜绝任意读文件） |
| 消息队列 | 继续对话发送的消息**入队即返回**，不阻塞界面；任务正在跑时再发消息会排队显示"已排队（前面还有 N 条）"，处理完自动把回复刷进聊天区。聊天页队列条只列**等待中**的消息（正在处理的那条不显示，进度看运行状态面板），可**逐条编辑/删除**（✎ / ×，与消息历史同步）。运行状态面板显示"队列: N 条待处理"。支持粘贴图片（截图）随消息一起发送 |
| ⚡ 强行插入（**Ctrl+J**） | 任务跑着的时候，用 **Ctrl+J**（或聊天区「⚡ 强行插入」按钮）插入一条指令，并**立即中断当前回合**：提供商会话直接取消那次 HTTP 请求（无状态，取消即"这轮没发生"）；Copilot 网页会话**只在回答尚未开始生成时**停下等待（一旦已开始生成就让它答完，避免丢掉回答、以及"被放弃的回答"与新指令应答错位）。插入方式可选（记住上次选择）：**立即遵守**（`queue=step`:进 next-step 队列，追加到当前执行中，且**批量执行途中抢先放弃剩余动作**）/ **整轮重排**（`queue=turn`:当前回合作废、按新指令重新规划本回合）。插入会写进消息历史并在时间线留下 `user_inject`/`user_interrupt`/`step_preempted` 步骤；会话空闲时自动转为排队跟进 |
| 新话题直接生成文件 | Copilot 网页通道的**首轮**会附一条建议：任务要产出新文件时，优先一次 `copilot_file(path, instruction)` 让 Copilot 生成并下载——文件内容不经过聊天消息，不受单条输出上限约束（大文件一个回合拿到，不必 `write_text_b64` 分块写、也不会截断）。只在首轮、且仅 Copilot 通道（provider 会话没这个工具，提示了只会白烧回合）；**不写进系统提示词**（那里要保持逐字稳定以命中 prompt cache）。另：`copilot_file` 已归入 core 写工具、**不参与关键字裁剪**——原先它在 copilot 组（关键字=图片/截图/附件），任何"生成文件"类任务都会把它裁掉，模型看不到该工具，等于把这条路堵死 |
| 步骤时间线 | 每步带时间与状态；「查看结果」展开为**渲染 / JSON 双视图**（见上）；右上「⬇ 导出」下载完整时间线 JSON（排查问题首选） |
| PATCH / DELETE | `/v1/agent/sessions/{sid}/messages/{mid}` | 编辑 / 删除一条消息（只改本地历史；提供商会话同步内存上下文，返回 `history_synced`） |
| GET | `/v1/search?q=&limit=` | 跨会话搜索对话正文（LIKE 通配符已转义；排除归档会话；limit 上限 100） |
| POST | `/v1/agent/sessions/{sid}/tag` | 设置会话标签（`{"tag": "..."}`,空串取消；列表按标签分组用） |

### 长期记忆（跨会话）

除了 AGENT.md 与 skills，Agent 还有一套**长期记忆**：把以往任务里验证过的零散事实
累积下来，后续相关任务自动检索注入（关键词+中文二元组匹配，零依赖）。

- 写入：Agent 在收尾时自行调用 `memory_write`；也可让它在任务中随时记。
- 检索：`memory_search`；提示词里只注入**最相关的几条**（不是全量），冲突时以当前事实为准。
- 删除：`memory_forget`，或在 WebUI/接口 `DELETE /v1/agent/memory/{id}`。
- 作用域：`global`（所有任务）与 `project`（仅当前工作区），互不泄露。
- 管理：`GET /v1/agent/memory?query=&scope=` 查看清单与统计；上限 500 条/作用域，
  超出按 pinned → 使用次数 → 更新时间淘汰；重复写入自动合并。
- 什么时候用记忆而不是 AGENT.md：需要**每回合全文遵守**的硬约定写 AGENT.md；
  「做过什么、踩过什么坑」这类可检索经验写记忆。

### 2. 写 AGENT.md 与 skills（两级作用域）

| 作用域 | 位置 | 生效范围 |
|---|---|---|
| **全局** | 服务安装目录：`AGENT.md`、`skills/<名字>/SKILL.md` | 所有工作区、所有任务 |
| **项目** | 工作区根目录：`AGENT.md`、`skills/<名字>/SKILL.md` | 仅该项目 |

- 两份 `AGENT.md` **合并注入**（各自 ≤8000 字符，可用 `agent_md_max_chars` 调整），同路径自动去重，**项目级优先**
- 技能**渐进式披露**：提示里只放「名称 + 一行简介」，Agent 判断相关时用 `skill_read` 取全文；同名技能项目级覆盖全局
- **Agent 可自行沉淀技能**：任务收尾时它会先 `skill_list` 查有没有相关技能，没有就用 `skill_create` 新建（默认写项目级 `工作区/skills/<名字>/SKILL.md`；要写全局 `skills/` 必须开启 `allow_self_upgrade`）。技能创建即时生效（下次任务上下文即出现），无需重启；关闭入口用 `allow_skill_write: false`
- SKILL.md 支持 YAML frontmatter，缺省回退「首个一级标题 + 首个段落」：

```markdown
---
name: webui-change
description: 修改 WebUI 时的结构规范与验证清单
---

# WebUI 修改规范
（正文：Agent 读到全文后照着执行）
```

- Agent 也可以**自己更新**这两类文件（沉淀有效结论），这类改动会在时间线标记 `instruction_update`，且可通过回滚撤销

### 3. 回滚（改了东西想还原）

写操作**执行前**会自动把目标文件原像存入 `data/snapshots/`。版本按 **Copilot 回合**计数——一次 complete 产生一个版本（同回合内的多次写算同一个版本），因此既能把整任务**回滚到开始前**，也能**回滚到某个回合**（撤销该回合之后的改动）。三个通道：

```powershell
# ① WebUI：会话「更多 ▾ → 回滚任务改动…」（有多个回合版本时会分组列出，
#    输入 0/留空 = 整任务回滚；输入版本号 = 回滚到该回合；运行中会拒绝，先暂停）

# ② HTTP API
curl -X POST http://127.0.0.1:8765/v1/agent/sessions/<sid>/rollback -H "Content-Type: application/json" -d '{"version":3}'
#    不带 body 或 version 为空 = 整任务回滚

# ③ 离线 CLI（服务/WebUI 起不来时也能用）
python scripts/rollback.py --list                      # 看有哪些可回滚
python scripts/rollback.py --last --yes                # 回滚最近一次任务的工作区改动
python scripts/rollback.py --sid <id> --versions        # 列出可选的回合版本点
python scripts/rollback.py --sid <id> --rev 3 --yes     # 回滚到回合 3
python scripts/rollback.py --scope self --last --yes   # 回滚最近一次「服务自升级」
```

规则：修改过的文件还原内容、任务新建的文件被删除；单路径保留的**回合版本**上限 6（最早 + 最近几个），单文件 8MB、单会话 800 条（超限记入 `skipped`，那些路径不回滚）。

### 4. 服务自升级（默认关闭）

`config.json` 中把 `agent.allow_self_upgrade` 设为 `true` 并重启后，Agent 获得 `self_*` 工具，可以修改**服务自身的代码**（即使当前工作区是别的项目）：

```powershell
# 开启后确认状态
curl http://127.0.0.1:8765/v1/agent/self/status
```

给 Agent 的任务示例：「读 cbp/tools.py，把 outlook_search 的 max_count 上限从 100 提到 200，然后自升级验证并跑单测」。

内置护栏：

1. **白名单**：`cbp/`、`webui/`、`scripts/`、`docs/`、`tests/` + 若干顶层文件；`config.json`、`data/`、`EdgeProfile/`、`.git/` 一律拒绝
2. **验证门**：写完必须 `self_validate`（全安装目录语法检查）+ `self_unittest`（服务自身单测）双通过，提前返回 final 会被 `validation_gate` 拦下
3. **独立快照**：自升级改动存 `data/snapshots/_self_<会话>/`，与工作区改动分开回滚
4. **重启标记**：写盘落 `data/.self_upgrade_pending`，你重启服务后标记自动归档，启动日志会打印「已加载上一轮自升级改动」
5. **失败兜底**：若升级把服务改到起不来，`app.py` 导入失败时会直接打印回滚命令；用 `python scripts/rollback.py --scope self --last --yes` 还原（该命令不依赖服务）

> ⚠️ 自升级等于把服务代码交给模型改。建议：先确保 `tests/` 能跑；一次只让 Agent 做一件事；升级后先重启看能否正常启动与跑测试，确认无误再继续下一个改动。

### 5. 大文件：让 Copilot 直接生成再下载
写大文件的传统路径是 b64 分块（每块 ≤3000 字符，受 Copilot 单条输出上限约束）。更省事的是让 Copilot 自己生成文件、桥接下载：

```json
{"type":"tool","tool":"copilot_file","arguments":{
  "path": "minesweeper.py",
  "instruction": "实现一个可运行的扫雷游戏，命令行版，带难度选择"}}
```

- 内容**不经过聊天消息**，所以没有输出上限、没有截断、没有分块，一个回合拿到完整文件
- 桥接会认 `blob:`/`data:` 链接与"下载"按钮两种入口；多个入口时按文件名优先
- 落盘前照常存原像（可回滚）；`.py/.json/.html` 仍需 `validate_python` + `run_unittest` 通过才算完成
- 关掉它：`agent.allow_copilot_file: false`
- **模型要求**：生成下载入口需要**非 Auto** 模型（Auto 通常只在聊天里回答）。会话模型是 Auto 时，配置 `agent.copilot_file_model`（例如 `copilot-gpt-5-6-sol-think-deeper`）会自动改用；没配则工具会明确报"Auto 不产生下载入口"，Agent 可据此回退分块写入。
- 报"没有找到下载入口"时看服务目录的 `last_answer.html`（回答区现场快照），把结构发维护者；同时 Agent 会自动改用分块写入

**什么时候用哪个**：内容由 Copilot 生成（新写一个大文件/Office 文档）→ `copilot_file` 最省；内容来自本地已有材料、需要精确控制（逐字节一致的改写、二进制搬运）→ 本地工具 + 批量动作（`{"type":"actions",...}`,一次回答多个小动作）更稳。

#### 一次成型模式（新会话首轮，省回合）
新会话用 Copilot 时，Agent 的首轮提示会要求"**一次成型**"，而不是逐步探索：

1. 直接 `copilot_file` 让 Copilot 一次生成**完整文件**（多文件就连续多次调用），不要 list/read 逐个探索、也不要 b64 分块拼；
2. 文件拿到后**本地**立刻跑起来验证（`run_python`/`validate_python`/`run_unittest`）；
3. 出错**先本地修**（`read_text` 定位 + `replace_text` 改），本地修两轮仍不行才带着**具体报错**定向问 Copilot（只改出错那一处，不整篇重生成）。

目标是一次任务 **1~3 个 Copilot 回合**交付。协议里也加了"本地闭环"规则：运行、测试、小修这类本地能做的事不回问 Copilot。系统提示词本身保持逐字稳定（prompt cache），首轮提示只加在当轮用户消息里。

### 6. 扩展工具（参考 DSH 的工具面，独立模块）

`cbp/tools_extra.py` 是**解耦的扩展工具模块**（删掉文件 + 取消 bridge 里一行调用即可整体下线），
按组合并提供逐组开关（`agent.tools_extra`）：

| 组 | 工具 | 说明 |
|---|---|---|
| `search` | `grep` | 按**内容**正则搜索（`find_files` 只按文件名），返回 `文件:行号:内容` |
| `web` | `web_fetch` | 抓 HTTP(S) URL 正文（HTML 自动去标签）；默认**拒绝内网/本地地址**（SSRF 防护） |
| `shell` | `pwsh` | 执行 PowerShell 命令（默认**需要一次性审批**）；`run_in_background=true` 起后台任务 |
| `shell` | `job_list` / `job_output` / `job_kill` | 后台任务列表 / 读输出（可等待）/ 终止 |
| `plan` | `todo_write` | 任务清单（长任务跟踪进度） |
| `plan` | `present` | 声明交付物（最多 4 个文件，供 WebUI 展示） |
| `vision` | `read_image` | 图片元数据（格式/尺寸/大小）+ 交给 Copilot 视觉描述 |
| `ask` | `ask_user_question` | 任务中途向用户提问并**阻塞等待**；WebUI 会弹提问条，提交后继续 |

配置（`config.json` → `agent`）：

```json
{
  "allow_shell": true,             // pwsh 总开关（false 整体禁用）
  "shell_timeout_seconds": 120,
  "allow_web": true,               // web_fetch 总开关
  "allow_private_web": false,      // true 才允许抓内网/本地地址
  "question_timeout_seconds": 600, // ask_user_question 等待上限
  "tools_extra": {"search": true, "web": true, "shell": true,
                  "plan": true, "vision": true, "ask": true}
}
```

安全约定：`pwsh` 与 `delete_path`/`outlook_send`/`run_python` 一样走**一次性审批**
（`approval_policy: "ask"` 时 WebUI 逐次授权，用后即焚）；`read_image` 的视觉分析与
`ask_user_question` 依赖 Copilot 网页/WebUI，没有对应通道时优雅降级（返回元数据/明确报错）。

### 5.6.2 插件安装 / 升级 / 卸载（版本化）

```bash
cbp plugins install ./my-plugin           # 本地目录
cbp plugins install ./pkg.zip             # zip（拒绝 ../ 越界条目，防 zip-slip）
cbp plugins install https://x/y.git       # git clone --depth 1
cbp plugins registry                      # 已安装版本/来源/备份
cbp plugins uninstall my-plugin           # 卸载（默认留备份）
```

HTTP 等价：`POST /v1/plugins/install {source, name?, force?}`、`POST /v1/plugins/uninstall`、
`GET /v1/plugins/registry`。

- **版本比较**：新版覆盖旧版并**自动备份**到 `data/plugin-backups/<name>-<时间>/`
- **降级默认拒绝**（避免误把新功能换回旧版），确需降级用 `force=true` / `--force`
- 安装后立即加载（工具/接口/面板一起生效），无需重启
- 卸载会先摘运行时贡献（工具/路由/面板）再删目录

### 5.6.1 插件 UI：声明式面板

插件可以往界面加**面板**（`permissions` 需含 `ui`），但只能声明式：

```python
ctx.register_panel({"id": "schedules", "title": "定时任务", "view": "table",
                    "api": {"method": "GET", "path": "api/list"}})
```

- `view` ∈ `kv` / `list` / `table` / `json` / `markdown`；插件只给**数据接口**，
  渲染由前端完成——**不接受插件注入 HTML/JS**（插件是第三方代码，等于开放 XSS 面）
- 面板入口：插件管理弹窗 →「插件面板」，或 `GET /v1/plugins/panels`
- 卸载插件会同时摘掉它的面板（不留点开就 404 的入口）
- 内置示例：`schedule` 的「定时任务」表、`mcp` 的「MCP 服务器」JSON

### 5.7 斜杠命令（`/`）

新会话输入框以 `/` 开头即进入命令模式（面板可键盘操作）：

| 命令 | 别名 | 作用 |
|---|---|---|
| `/research <问题>` | `/深研`、`/dr` | 走深研：多角度搜索 + 抓正文 → 带来源的报告（新会话） |
| `/agent` | `/执行` | 切到 **Agent 执行** 模式 |
| `/chat` | `/对话` | 切到 **普通对话** 模式 |
| `/help` | `/?`、`/帮助` | 重新列出全部命令 |
| `/goal <目标>` | `/目标` | **目标模式**：先 `create_goal` 再分多轮推进，目标未完成时返回 final 会自动续轮（轮次上限默认 10），达成/受阻时 `update_goal` 收尾 |
| `/plan <任务>` | `/计划` | **计划模式（强制只读）**：探索阶段写操作会被 `SANDBOX_PLAN_MODE` 拒绝；模型用 `exit_plan_mode(plan)` 提交方案即**结束本轮**，等你确认后再执行 |
| `/server.模板名 参数…` | — | **MCP prompt 模板**：自动按 MCP server 提供的模板生成命令，渲染结果载入输入框（未配置 MCP 时不出现） |

- 命令注册在 `webui/app.js` 的 `SLASH_COMMANDS`，加新命令只需追加一条 `{id,label,aliases,desc,run}`
- 不带参数回车 = 补全等待输入；带参数回车 = 立即执行
- 面板文案走 i18n（i18n 审计会主动展开面板检查）

### 5.6 插件系统（与 DSH 插件语义兼容）

CBP 的插件是"**语义兼容** DSH"而不是"直接跑 DSH 插件"：DSH 插件是 npm 包 + Cordis
（JS `apply(ctx)`）在 Node 里跑；CBP 是 Python 服务。对齐的是**契约**——同形清单、
同一套贡献点、同样的加载→apply→卸载生命周期、同样的 `id + disabled + config` 启停语义。
（要直跑 JS 插件需要内嵌 Node，不在本系统目标内。）

**清单**（`plugins/<name>/plugin.json`，也接受 `package.json` 里放 `"cbp"` 字段，
`"dsh"` 字段原样透传供迁移参考）：

```json
{
  "name": "web-search",
  "version": "0.1.0",
  "description": "联网搜索",
  "dsh":   {"bundle": {"patch": "./cordis.patch.yml"}},
  "cbp": {
    "entry": "plugin.py",
    "permissions": ["tools", "api"],
    "contributes": {"tools": ["web_search"], "api": ["GET api/search"]},
    "config": {"backend": "auto"}
  }
}
```

**入口**（`plugin.py`）：

```python
def setup(ctx):                      # ctx ≈ DSH/Cordis 的 ctx
    ctx.register_tool("web_search", handler, "说明", {"query": "..."})
    ctx.register_api("GET", "api/search", handler)   # → /v1/plugins/<name>/api/search
    ctx.on("step_done", callback)                    # session_start / step_done / session_end
def teardown(ctx): ...
```

**ctx 能力**：`register_tool` / `register_provider` / `register_api` / `on` /
`state` + `save_state` / `read_text` + `write_text`（限插件目录）/ `log`。
权限 `tools/providers/api/ui/events/storage/files` 必须在清单里声明，否则调用报错。

**管理**：WebUI「更多 ▾ → 插件管理…」可启用/禁用/重载；对应 API
`GET /v1/plugins`、`POST /v1/plugins/{name}/{enable|disable|reload}`。
启停记录在 `data/plugins.json`，**不需要重启服务**（禁用即摘掉工具/提供商/路由/事件）。

**内置插件（现代 bot 的核心能力，按需启用）**

| 插件 | 能力 | 依赖 |
|---|---|---|
| `web-search` | `web_search` 联网搜索 | DuckDuckGo 免 key；可选 BRAVE/TAVILY/SERPER_API_KEY |
| `research` | `deep_research` 多角度并行搜索 + 抓正文，返回证据包 | 无 |
| `schedule` | `schedule_add/list/remove/run` 定时任务（every / daily / once），到点在**独立新会话**跑任务；`workspace` 指定工作目录（默认当前工作区） | 无 |
| `image-gen` | `image_generate` 文生图，落盘到 `workspace/generated/` | DASHSCOPE_API_KEY 或 OPENAI_API_KEY |
| `mcp` | **MCP 客户端**：连接 MCP server（stdio / HTTP），把工具与资源注册成 `mcp__<server>__<tool>` | 无（本地进程或 HTTP 端点） |

**Combined = Grok 式 bot**：实时搜索（web-search）+ 深研（research）+ 定时任务（schedule）+
生图（image-gen）+ 跨会话记忆（memory）+ 子代理/编排（subagent/ralph/workflow）。

> 生图需要**有效的** API key：本机现有 `DASHSCOPE_API_KEY` 实测 401（无效），
> 换一个有效 key 或改用 `OPENAI_API_KEY` 即可；插件会返回结构化错误而不是 500。

**内置示例：`web-search`**（现代 bot 的核心能力）
- 工具 `web_search(query, max_results=6)`：DuckDuckGo 免 key；配了
  `BRAVE_API_KEY` / `TAVILY_API_KEY` / `SERPER_API_KEY` 会自动优选
- 接口 `GET /v1/plugins/web-search/api/search?q=...&n=6`

### 5.8 MCP（Model Context Protocol）

CBP 当 **MCP 客户端**：连上外部 MCP server，把它的工具与资源注册进 Agent，模型就能像用内置
工具一样调用。支持两种传输：

| 传输 | 配置 | 说明 |
|---|---|---|
| **stdio** | `{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","C:/data"]}` | 本地进程，主流方式 |
| **HTTP** | `{"url":"https://host/mcp","headers":{"Authorization":"Bearer ..."}}` | streamable HTTP；响应是 `application/json` 或 `text/event-stream`(SSE) 都能解析，自动维护 `Mcp-Session-Id` |

```bash
# stdio
curl -X POST http://127.0.0.1:8765/v1/plugins/mcp/api/add \
  -H 'Content-Type: application/json' \
  -d '{"name":"fs","spec":{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","C:/data"]}}'

# HTTP + 鉴权头
curl -X POST http://127.0.0.1:8765/v1/plugins/mcp/api/add \
  -H 'Content-Type: application/json' \
  -d '{"name":"remote","spec":{"url":"https://example.com/mcp","headers":{"Authorization":"Bearer xxx"}}}'

curl http://127.0.0.1:8765/v1/plugins/mcp/api/list            # 连接状态 / 工具 / 资源
curl -X POST http://127.0.0.1:8765/v1/plugins/mcp/api/call \
  -H 'Content-Type: application/json' \
  -d '{"server":"fs","tool":"read_file","arguments":{"path":"C:/data/a.txt"}}'
curl 'http://127.0.0.1:8765/v1/plugins/mcp/api/resources?server=fs'          # 资源清单
curl 'http://127.0.0.1:8765/v1/plugins/mcp/api/resources?server=fs&uri=file:///C:/data/a.txt'  # 读资源
```

**能力映射**
- `tools/list` → 每个工具注册成 `mcp__<server>__<tool>`，参数由 `inputSchema` 自动转成参数说明
- `tools/call` → `content[].type=text` 拼成结果；`isError` 转成工具错误
- `resources/list` → 每个 server 额外注册一个只读工具 `mcp__<server>__read_resource(uri)`，
  工具说明里列出可用 uri（避免把几十个资源各注册成一个工具，撑爆工具表）
- `resources/read` → 文本直接返回；二进制（`blob`）给出 mime 与 base64 长度
- 通用工具：`mcp_list`（状态）、`mcp_call`（手动调）、`mcp_read_resource`（读资源）
- `prompts/list` → 每个有模板的 server 注册 `mcp__<server>__use_prompt(name, arguments)`；
  同时 **WebUI 自动生成斜杠命令**：`/server.模板名 参数1 参数2 …`，渲染结果直接填进输入框
- `prompts/get` → 消息序列拼成文本（`[role] 内容`）
- 通用工具：`mcp_get_prompt(server, name, arguments)`

**健壮性**
- 单个 server 起不来只记录在 `errors`，不影响其它 server 与内置工具
- `resources/list` 属于可选能力：用 **8 秒短超时**探测（未实现的 server 可能不回包，
  不探测会让连接阶段白等 30 秒）
- 配置持久化在 `data/plugins.json` 的 `mcp.state.servers`，重启自动重连

> 尚未接入：旧版 **HTTP+SSE 双端点**传输（2024-11-05 老规范，只支持 streamable HTTP）。

### 5.3.4 待办清单：从 TODO 文件自动同步 + 按清单派子代理

用户实测痛点（pyshot 整改）：说"按 todo 完成整改"之后，**面板清单一直 0/5**，
而子代理面板里那 4 个还是上一阶段的遗留，"像摆设"。

**① TODO 文件自动同步**（`cbp/todo_sync.py`）
- 每步开始时按 **mtime** 检查工作区里的 `*todo*.md`（也认 `.txt`），解析 `- [ ] / - [x]`
  复选框（没有方框时退化成 `- 项` 列表），写进会话 `todos`；
- **不覆盖进度**：同一项已是 `completed` 不会被文件里的 `[ ]` 拉回；文件里删掉的
  已完成项会保留；文件消失时**不清空**现有清单；
- 之前 CBP 完全不看 `pyshot_todo.md`（23KB 的计划文件），面板只认 `todo_write`。

**② 按清单自动派子代理**（`cbp/auto_team.py`，**默认关闭**）
- 条件：`agent.auto_team_enabled=true`、存在 **≥2 个** `pending` 且**作用域不重叠**
  （按文件路径/标识符判断互不依赖）、当前没有子代理在跑；
- 动作：把这几项转成自足的任务文本（目标、验收、汇报格式）交给 `fanout`，
  子代理 id 记回对应待办，清单里能看到"谁在跑"；
- 为什么默认关：每个子代理都是一条独立 Copilot 对话，**成倍消耗额度**。

配置项：

```json
{"agent": {"auto_team_enabled": false, "auto_team_max_agents": 4, "auto_team_max_steps": 30}}
```

**③ 子代理面板可折叠**：标题栏「折叠 / 展开」，状态记在 `localStorage`，刷新后保持。

### 5.3.3 测试脚本的浏览器回收

连续跑 UI 审计/截图脚本会在系统里攒下泄漏的 Playwright 浏览器（实测 **21 个** msedge，
每个都带 `--user-data-dir=%TEMP%\playwright_*`）。它们既吃内存，也会让人误以为
"有程序在捣乱"。`scripts/reap_browsers.py` 按年龄回收（默认 >15 分钟的才杀，
不影响正在跑的脚本），`scripts/regression.py` 启动时会自动调用它。

```bash
python scripts/reap_browsers.py            # 只报告
python scripts/reap_browsers.py --kill     # 回收
```

### 5.3.2 长命令与"看不到在跑什么"

- **`pwsh` 超时会终止整棵进程树**：只 `proc.kill()` 会留下孙进程（`python`/`git`/`pip`）
  继续持有 stdout 管道，`communicate()` 永远等不到 EOF——实测一条 `pip install PySide6`
  把任务卡住 10 分钟、超时形同虚设。现在 `taskkill /T /F`（POSIX `killpg`）+ 5s 兜底
  取回部分输出，错误里直接提示"换策略：拆小命令 / 用 run_in_background + job_output"。
- **实时面板显示正在执行的步骤**：`runtime.phase` 在 step 开始时置为 `executing`
  （`正在执行第 N 步 · 工具名`），结束后回到 `thinking`。以前整个执行期间界面都停在
  上一轮的 "waiting · 等待 Copilot 回答"，看着像挂死。
- **时间线并入实时面板**：`当前执行状态 / 当前回答 / 最近发送内容 / 继续对话 / 步骤时间线`
  五个标签共用一个面板（像 DSH 一屏看全），时间线在该标签内滚动；筛选与导出照旧。

### 5.3.1 编辑工具：容错匹配 + 补丁（治"改文件失败→回合爆炸"）

痛点：模型抄片段时缩进（tab/空格、层级）、行尾空白稍有出入，`replace_text` 就匹配失败，
于是"读一遍—猜一遍"循环，实测把一次小改拖成几十上百回合、token 直接爆掉。

- **`replace_text` 多级降级匹配**：精确 → 行尾空白 → **行首缩进弹性** → 空白折叠。
  唯一命中即采用，结果里回传 `match` 策略并在 `note` 里说明"这次是容错匹配"；
  **新片段按文件实际缩进重排**（tab 文件不会写成 4 空格、嵌套层级不会错位）。
- **失败诊断**：不再只说"未找到"——给出**首个差异的行/列 + 两侧原文**，让模型一轮改对。
- **`apply_patch(path, patch|edits)`**：一次调用多处修改，**原子**（任一块失败则整体不写）。
  支持 Codex 风格补丁（`*** Begin Patch` / `*** Update File:` / `@@` 分块，
  ` `=上下文 `-`=旧 `+`=新）或简单 `edits: [{old,new,count?}]`；
  每块同样走容错匹配，结果回报每块的 `match` 策略与行号。
- 已有 `replace_lines`（按行号改，完全不依赖匹配）与 `convert_indentation`（批量换缩进），
  三者分工：**一处**用 `replace_text`、**多处**用 `apply_patch`、**整段/缩进**用 `replace_lines`。

实测（真实回合，DeepSeek）：tab 缩进 + 行尾空格的 `maths.py` 加三处注释 →
**10 步、0 失败**；对抗性场景（模型给 4 空格片段改 tab 文件）→ `match=indentation`、
结果仍是 tab；两处补丁一次性原子应用。

### 5.4.0 显式扇出（不依赖模型判断）

```bash
cbp fanout --task "审计 workspace/core" --task "审计 workspace/webui" --model kimi-coding/k3
```
或在输入框里：`/fanout 任务1 | 任务2 | 任务3`

- 每个任务各派**一个子代理**（独立会话），跑完把结果写回一个父会话（`fan-*`）并在界面里打开
- 提供商模型可并行；网页通道仍按顺序跑（`fanout` 不占父窗口，子代理各自独占窗口，但默认窗口数 = 1）；单项失败不影响其余，失败原因写进父会话消息
- 上限 8 个；`POST /v1/agent/fanout` 是同一能力，供脚本/CI 调用
- 与"模型自己决定要不要用子代理"互补：提示词 + 运行时提示让它想起来，这个入口让它**必定发生**

### 5.4.1 对抗式审计（多个审计员 + 裁判，循环到只剩小问题）

`/audit 审计目标说明`（或对话框输入 `/audit` 回车）：后台开一个 `audit-*` 会话：

1. **N 名审计员**（默认 3 个不同角度：正确性 / 健壮性 / 安全 / 一致性）各自只读审查，
   只报「有证据」的问题（文件 + 行号 + 分级 p0/p1/p2），输出 JSON 清单；
2. **裁判**亲自用 `read_text` 核验每条证据，合并去重、剔除误报、重新分级，
   给出 verdict：`clean`（没有）/ `minor`（仅剩 p2）/ `major`（还有 p0/p1）；
3. major 且开了自动修复（默认开）→ 派一个修复子代理逐个修（最小改动 + 验证闭环），
   再进入下一轮；**直至 clean/minor 或轮次用完**（默认 3 轮，上限 8）；
4. 每轮报告与最终汇总写进审计会话的消息里（点开会话就能看）。

网页通道顺序执行（窗口数 1 时不抢父窗口）；提供商通道按 `agent.subagent_parallel` 并行。

HTTP：`POST /v1/agent/audit {objective, target?, auditors?, max_rounds?, auto_fix?, model?}`
后台执行，返回 `{session_id, started}`。

### 5.4 子代理：新建 / 继续 / 分叉

| 工具 | 作用 |
|---|---|
| `subagent(task, description, model, max_steps)` | 新建子代理（独立会话/独立上下文），跑完返回结果 |
| `subagent_send(session_id, message, ...)` | **继续**已有子代理（同一上下文追加一轮；也接受 `agent_id`） |
| `subagent_fork(session_id, description, keep_messages)` | 从某会话**分叉**出新子代理（复制最近对话，试另一条路） |
| `subagent_list(parent_id, limit)` | 列出子代理（状态/步数/标签） |

- 子会话记录 user/assistant 消息，面板点进去能看到"问了什么、答了什么"
- 子代理深度上限 1（子代理里不能再开子代理，防递归）
- 继承父会话的会话级授权（不重复弹审批）
- HTTP：`POST /v1/agent/subagents/{sid}/send`、`POST /v1/agent/subagents/{sid}/fork`

> 实现注意：`subagent_send` **不能**走 `run_agent`——父回合正持着自己的自动化窗口，
> 再去抢同一个窗口会死锁（实测卡满 600 秒客户端超时）；它与 `subagent` 共用
> `_drive_child` 自建 runner（有父页面时复用父页面，没有时自己占一个窗口）。

### 5.5 子代理面板（看有哪些子代理、在跑什么）

用 `subagent` / `ralph` / `workflow` 派生出的子代理会以 `sub-xxxxxxxxxx` 独立会话运行。
WebUI 在 KPI 区下方有**子代理面板**（有子代理时才出现）：

- 每行：状态点 + 子代理描述 + 状态/步数/当前阶段
- 点一行 → 切到该子会话看完整时间线；点「刷新」立即拉取
- 数据来自 `GET /v1/agent/subagents`（可按 `?parent=<sid>` 过滤）
- **没有就不显示**：面板与待办条都只在当前会话确实有内容时出现；点「+ 新任务」或切换会话会
  立即清空上一轮的待办/子代理（包括外层空卡片），不等新会话的第一份 runtime 快照。

子会话会记录 `parent_id` 与 `description`（会话表新增两列，自动迁移）。子代理**继承父会话的
会话级授权**（父授权了 pwsh，子代理不再弹），但逐参数的一次性授权不继承。

### 6. 常用运维命令
```powershell
python start.py                              # 启动服务（默认 127.0.0.1:8765）
python scripts/login.py                      # 登录向导（--reset 彻底重来；服务运行时会拒绝执行）
python -m unittest discover -s tests         # 回归测试
python scripts/calibrate.py                  # 会话阈值校准（读 data/agent.db 的实测样本）
python scripts/outlook_selftest.py           # Outlook 分步自检（含线程池/专用COM线程对照）
python scripts/rollback.py --list            # 查看可回滚的任务与自升级记录
```

### 7. 故障排查

| 现象 | 处理 |
|---|---|
| 服务起不来、导入报错 | 多半是自升级改坏了：`python scripts/rollback.py --scope self --last --yes` 后重启 |
| 写文件只写了一半、写文件中断 | WebUI 导出该会话时间线发维护者；确认用的是 b64 分块（每块 ≤3000 字符）；大文件优先改用 `copilot_file` 让 Copilot 直接生成下载 |
| 任务卡在「等待服务」 | 限流/服务不可用，指数退避自动重试；界面中央出现**倒计时卡片**（锚定首次超限 + `service_retry_window_seconds`，默认 1 小时 1 分钟，标题按 Copilot / API 提供商分别措辞）显示预计恢复时间。卡片**可点 × 收起**为右下角小标（点小标可再展开），不会挡住顶部控件，因此限流期间照样能切换会话模型；新一轮限流会自动重新展开。长时间不恢复会安全暂停，服务恢复后点「恢复」。若提示的是"消息未能发送"而非限流，那不是限流——点「切换到页面」查看或「重试浏览器」重建页面 |
| 页面找不到输入框/模型按钮 | 登录态失效：停服务后 `python scripts/login.py`（必要时 `--reset`） |
| 频繁换会话/上下文被压缩 | 跑 `scripts/calibrate.py`，按建议调整 `compact_at_tokens`/`rollover_at_tokens` |
| Outlook 报「不可用」或 0x8000400x | 跑 `scripts/outlook_selftest.py` 定位；COM 路径必须走专用消息泵线程（`_ComWorker`），OWA 路径看登录态 |
| 模型列表刷新后看不到某些模型 | 点 ↻ 看提示：`scanned=false` 说明没扫成；扫成了但缺少二级菜单模型时，把服务目录的 `last_menu.html` / `last_menu.txt`（每次扫描自动落盘的菜单现场）发维护者 |
| 浏览器层异常 | 看服务目录 `last_page.txt`（出错时的整页文本快照），必要时在 `config.json` 的 `markers` 追加界面文案锚点 |
| 删会话报「部分失败」 | 看结果弹窗的 `verified-absent` 标记；仍失败可点「重试远程并强制清理本地记录」 |

## 安全警告

1. **API 鉴权**：默认绑 `127.0.0.1` 仅本机可达；配置 `api_token` 后所有 `/v1/*` 端点要求 Bearer 令牌。绑非回环地址且未配置 token 时**服务拒绝启动**。`agent.workspace_roots` 可进一步把工具工作区限制在白名单根目录内。
2. **间接 Prompt Injection**：Agent 读取的工作区文件内容会进入 Copilot 上下文，而返回的动作会被直接执行。**不要让 Agent 处理来源不可信的文件**；开启 `allow_python` / `allow_delete` 后风险升级为任意代码执行与文件删除。
3. **登录态敏感**：`EdgeProfile/` 目录包含你的 Copilot 登录 Cookie，已被 `.gitignore` 排除，请勿外泄。
4. **容错写文件**：对 Copilot 返回的未转义写文件动作有正则容错恢复，恢复结果可能不完美；重要文件请先备份，`.py/.json/.html` 有语法+单测验证闭环兜底，其他类型没有。
5. **自升级是高权限开关**：`allow_self_upgrade=true` 后 Agent 能改服务自身代码（白名单内）。虽然每次改动前都有快照、验证不过会被门拦住、且有离线回滚通道，但它仍是"让模型改运行中的服务"——只在你能接受这个前提、并且测试套件可用的前提下开启；不要在工作区里处理来源不可信的文件时同时开着自升级。
6. **凭据与快照**：`data/snapshots/` 保存了被改文件的**改动前原像**，其中可能包含敏感内容（例如曾写入过密钥的文件），外发调试数据前先确认；`data/` 与 `EdgeProfile/` 都不要提交或分享。
7. **跨站写入（CSRF）**：服务默认无 token 时，任意网页都能用 `fetch(..., {mode:'no-cors'})` 向 `127.0.0.1` 发简单请求（不触发 CORS 预检），从而取消任务、回滚服务自身代码、删会话——**读不到响应不等于没有副作用**。因此内置 `origin_guard` 中间件：写方法（POST/PUT/PATCH/DELETE）按浏览器信号判定，`Sec-Fetch-Site: cross-site|same-site` 或 `Origin` 非本机/异端口一律 **403**；不带 `Origin`/`Sec-Fetch-*` 的非浏览器客户端（curl、评测脚本）照常放行。可用 `agent.csrf_protect: false` 关闭（不推荐），`agent.csrf_allowed_origins` 加白名单。
8. **安全响应头与 CSP**：所有响应带 `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`X-Frame-Options: DENY`（防点击劫持——本机场景下"诱导点删除/回滚"是现实威胁）与 CSP。CSP 为
   `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`。
   **`script-src` 不含 `'unsafe-inline'`，这是硬约束**：
   - 页面里**不允许**再写内联事件处理器（`onclick="..."`）：一律用 `data-act="名称"`（输入类用 `data-act-input`，键盘类用 `data-act-key`），并在 `webui/app.js` 的 `ACTIONS` 表里注册实现——分发器会按 `data-act` 调用。写错名字不会报错、只是"点了没反应"，所以有第 14 条不变量**双向核对** `data-act` ↔ `ACTIONS`（缺实现 / 有死条目都会失败）。
   - 也**不允许**内联 `<script>`：首屏主题初始化已外置为 `webui/boot.js`；新增静态资源必须加进 `cbp/api.py` 里 `/ui/{asset}` 的白名单（目前是 `style.css` / `app.js` / `boot.js` / `i18n.js`）。第 15 条不变量会核对"页面引用 ⊂ 白名单"，漏加会立刻失败。
   - `style-src` 仍保留 `'unsafe-inline'`：页面大量使用 `style="..."` 属性与动态注入的 `<style>`，影响面小于脚本，暂不收紧。

## 界面多语言（简 / 繁 / EN）

侧栏工具栏的「简 / 繁 / EN」按钮循环切换，选择存在 `localStorage.cbp-lang`。实现要点与**约束**：

- **以中文原文当 key**：`tr('取消任务')` 查 `webui/i18n.js` 的 `I18N_EN` 表；缺翻译时**回落中文**，不会出现空白或 `key.not.found`。繁体由「短语覆盖表 + 字形转换表」自动生成。
- **静态界面**：给元素加 `data-i18n` / `data-i18n-title` / `data-i18n-ph` / `data-i18n-aria`；此外还有一遍“DOM 直译”（叶子节点文本与 `title/placeholder/aria-label` 按字典替换）。
- **动态内容必须自己调 `tr()`**：对话、时间线、回答、日志等容器在 `I18N_SKIP_IDS` 里被排除（其中可能有模型输出或用户输入，恰好等于某个 key 就会被误翻）；切语言后由 `rerenderForLang()` 重画动态区域。
- **禁止把 `tr(...)` 写成字符串字面量**：`'tr(重试浏览器)'` 会让界面直接显示 `tr(重试浏览器)`。这类错误**语法合法、单测也覆盖不到**，因此加了第 16 条不变量静态拦截（`cbp/invariants.py`）。
- **覆盖率审计**：`python scripts/ui_i18n_audit.py` 三语言逐面扫描界面外壳（按钮/标签/页签/表头/提示/弹窗/占位符），报告三类问题：英文模式仍含中文、`tr(`/`data-i18n` 等原始 key 泄漏、**繁化漏字**（用独立维护的简繁对照表检测，不依赖 i18n.js 自己的表——真实事故里「浏」不在表内，用表自身当检测集完全看不出来）。
- **覆盖审计**：`python scripts/i18n_coverage.py` 在英文模式下逐个打开 12 个界面状态
  （首页/新任务/手册/插件/面板/工作区/全局搜索/更多/记忆/会话更多/填充态/模型）并采集**可见**中文，
  当前 **0 处**；`ui_i18n_audit.py` 只扫静态外壳，两者互补（实测前者多抓出 30 处：记忆面板 28 + 主题标题 + 工作区选择）。
- **后端错误文案**：`HTTPException` 的 detail 由全局处理器按 `X-CBP-Lang` 翻译（`cbp/i18n.py` 精确表 + 模式表，
  未知文案回落中文）；不变量「后端错误文案都有英文翻译」守着新增的 raise。
- **使用手册**：中英**两套完整模板**（`MANUAL_HTML` / `MANUAL_HTML_EN`，各 18 节），按界面语言整篇切换，含自动目录与低频章节折叠（折叠章节名也随语言走）；不变量「手册中英两版章节一致」防只改一边。
- **插件自带译文**：插件在 `plugin.json` 声明 `cbp.i18n: {en: {...}, tw: {...}}`，
  `GET /v1/plugins/i18n` 汇总**已加载**插件，前端启动/启停插件时合并进字典 → 第三方插件的中文也能翻
- **繁体术语表**：`I18N_TW_OVERRIDE` 按词修正（文件→檔案、数据→資料、插件→外掛、服务器→伺服器…），
  应用时**按词长降序**，否则"服务"会吃掉"服务器"（实测踩到）
- **系统生成的标题**：深研会话标题按 `X-CBP-Lang` 生成（`[Research] …` / `[深研] …`）；cron 等无请求上下文的仍用中文
- **静态属性**：不变量「静态属性文案都有英文键」静态核对 `index.html` 里含中文的
  `title/placeholder/aria-label/alt` 是否都有词条（实测补了 6 条：跟进消息、输入、收起、限流中…）
- **已知取舍**：后端与 Agent 协议文案（系统提示词、工具描述、步骤 reason/错误原文）一律保持中文，它们与协议和评测绑定。繁体是**字形级**自动转换，专有名词可能不完美，需要精修时往 `I18N_TW_OVERRIDE` 短语表里加。

## 已知限制

- 依赖 Copilot 页面的可访问性文本（如 `Copilot said:`）与 DOM 结构，**Microsoft 前端更新可能导致桥接失效**，出错时查看 `last_page.txt` 诊断快照；
- 普通对话的流式输出基于公共前缀 diff，若 Copilot 重写已输出文本可能出现重复/缺失（Agent 模式不受影响）;
- 默认 1 个自动化窗口（`agent.copilot_windows: 1`）：同一时刻只有一个回合在浏览器中进行；
  本预览分支可开到 1–4 个窗口并行，见 [docs/MULTI-WINDOW.md](docs/MULTI-WINDOW.md)（有额度与内存代价）；
- 回滚是**任务/作用域级**的：同一任务里混做多件不相关的改动，回滚会整块回退，没法只挑其中一件；
- 快照只覆盖经工具层的写操作；快照超限（单文件 8MB／单会话 800 条）的路径不记录、也就无法回滚。
- **生图需要有效 key**：`DASHSCOPE_API_KEY`（通义万相）或 `OPENAI_API_KEY`；本机现有的 DashScope
  key 实测 401，未配有效 key 时 `image_generate` 返回结构化错误而不是崩溃；
- **系统文件夹对话框在服务里不可用**：由服务托管的进程会继承非交互窗口站，PowerShell 的
  `FolderBrowserDialog` 能创建窗口却不在可见桌面上（请求会挂住）。因此工作目录选择器只用
  **应用内浏览**（`/v1/agent/browse`）；`/v1/agent/pick-directory` 保留但 15s 超时返回 400；
- **深研是"检索 + 一次综合"**：不做多轮 follow-up 追问；来源以搜索引擎返回为准，不保证可访问性。

## 项目结构

```
app.py            组装入口（shim：from cbp.api import app，uvicorn 加载点）
start.py          服务入口
install.py        依赖安装
cbp/              核心包
  api.py            HTTP 路由层：FastAPI 应用与全部端点，只做请求/响应适配
  bridge.py         配置、运行状态、回合编排与会话编排（facade 重导出各子模块）
  page.py           页面自动化：新对话、模型菜单、发消息、取回答/流式更新
  browser.py        浏览器生命周期与窗口池（多窗口并行、提供商逻辑槽）
  answer.py         回答解析：回合标记、噪声/思考占位剔除、服务不可用识别
  provider.py       外部提供商通道（HTTP 回合、历史裁剪）
  queue.py          会话消息队列（排队/取消/插入）
  orchestration.py  ralph / workflow / 对抗式审计
  subagents.py      子代理派发、驱动、续跑与 fork
  copilot_delete.py Copilot 侧栏会话删除流程（URL 身份 + 标记同属校验 + 对账终验）
  owa.py            Outlook 网页版（OWA）自动化后端
  agent.py          Agent 主循环、JSON 动作协议、容错解析、压缩/换会话
  tools.py          工作区工具集（路径沙箱、Office 文档、Outlook 路由）
  providers.py      API 提供商接入层（OpenAI/DeepSeek/Kimi/Grok 等）
  session_store.py  SQLite 会话持久化、中断恢复、会话实测统计
  snapshots.py      改动前原像快照与任务级回滚（自我升级 failback）
webui/            零依赖 Web 管理台（index.html 结构 + style.css + app.js）
AGENT.md          项目级 Agent 约定（工作区根目录存在时自动注入系统提示）
skills/           目录式技能库（每个技能一个 SKILL.md，按需加载）
scripts/          工具脚本（login.py 登录向导、calibrate.py 阈值校准、
                  outlook_selftest.py COM 自检、rollback.py 离线回滚）
tests/            单元测试（python -m unittest discover -s tests）
```

## 免责声明

本项目通过浏览器自动化使用 Copilot 网页版，仅供个人学习与研究。请遵守 Microsoft 365 Copilot 的使用条款，勿用于高频率、商业化或违反服务条款的用途。

## 破坏性操作的一次性审批（fail-closed）

威胁模型：`delete_path` / `outlook_send` / `run_python` 一旦被模型误调用后果不可回滚。
因此默认 `agent.approval_policy: "ask"`：**每次**执行前都要用户下达一次授权，
授权精确到「工具 + 参数哈希」，**用后即焚**（没有 allow-always，也没有撤销存储）。

```powershell
# 查看待授权（含会话记录的 approval_pending）
curl http://127.0.0.1:8765/v1/agent/sessions/<sid>/approvals
# 下达一次性授权（不传 args_hash 时按 approval_pending 精确授权）
curl -X POST -H "Content-Type: application/json" \
  -d '{"tool":"delete_path"}' http://127.0.0.1:8765/v1/agent/sessions/<sid>/approvals
```

未获授权时 Agent 不会执行该操作：它会记录一条 `approval_required` 步骤、把状态置为
`waiting`（并在 runtime 里写 `approval_pending`），然后停下等你授权；授权后点继续即可。
把 `approval_policy` 设为 `"never"` 表示你明确承担这些操作的风险（不推荐）。

### 授权范围：once / session

实测痛点：`pwsh` 被拦后模型改了命令措辞，参数哈希变化 → 又弹一次授权（逐参数太碎）。

`agent.approval_scope` 控制授权范围：

| 值 | 含义 | 适用 |
|---|---|---|
| `"once"`（默认） | 精确到「工具 + 参数」，**用后即焚** | 最严格；破坏性操作少时 |
| `"session"` | 一次授权后，**该工具在本会话内**都放行（会话结束即失效；不跨会话、不跨工具） | 连续 shell 任务，比 `never` 安全 |

```powershell
# 本会话级授权（scope=session 时 WebUI 按钮自动走这条）
curl -X POST -H "Content-Type: application/json" \
  -d '{"tool":"pwsh","scope":"session"}' http://127.0.0.1:8765/v1/agent/sessions/<sid>/approvals
```

WebUI 的授权横幅会按 `approval_scope` 显示不同措辞（"仅对这一次执行有效" /
"仅对本会话有效"）；runtime 快照里带 `approval_scope` 供前端判断。

## 沙箱拒绝是结构化的

越界/敏感路径不再只回一句"拒绝访问"，而是给出错误码（`FS_SANDBOX_ESCAPE` /
`FS_SANDBOX_DENYLIST` / `FS_SANDBOX_NOT_ALLOWED`）、当前模式、原因与**可行动方案**
（改用工作区相对路径 / 开启自升级 / 让用户把文件放进工作区）。工具参数写错时也会
列出该工具的**合法参数名**，避免模型反复试探。

## 整体回归（真跑各能力）

单测/审计是白盒；这个脚本**真跑一遍面向用户的能力**（用真实模型），几分钟给出结论：

```bash
python scripts/regression.py                 # 默认 kimi-coding/k3，跑完自动清理
python scripts/regression.py --skip-slow     # 跳过深研/单测/布局审计（约 1.5 分钟）
python scripts/regression.py --model copilot-auto --keep   # 保留现场排查
```

覆盖 22 项：健康检查、工具面完整性（62 个）、沙箱三档、**计划模式（提交方案且不写文件）**、
普通写入任务、子代理创建/继续/分叉、深研（证据+报告）、定时任务执行、MCP（stdio 工具可用 +
移除后摘除）、插件安装/卸载、插件面板、CLI 只读命令、单测、不变量、UI 冒烟、i18n/布局/对比度审计。

结果写到 `data/regression/report.json`，失败时退出码非零（可接 CI）。

## 运行时不变量检查（提交前必跑）

```powershell
python scripts/invariants.py        # 失败退出码 1；--json 供 CI 使用
```

它检查那些"坏了不一定报错、但会让行为错误"的隐式契约：工具分组表与注册表一致、
core 组最小工具面、steps 表 append-only、检查点表与状态取值、AgentRunner 可选回调
向后兼容、评测断言名与实现一致、提示词工具清单单一数据源、破坏性工具审批契约。

## 崩溃恢复时的检查点语义

- **发请求前**登记回合（`turn_results.status=inflight`），成功后转 `done`；
- **工具派发前**登记步骤（`step_dispatch`），成功/失败/未知分别收尾；
- 恢复时若发现"已派发但未收尾"的步骤，**绝不原样重放**：先让模型只读核对现状
  （对应 DSH 的 `Its outcome is unknown.`），避免重复副作用。

## 超大工具结果会落盘（spill）

单条工具结果超过 `agent.spill_threshold_chars`（默认 20000 字符）时，**完整内容**写入
工作区 `.spill/<会话>/<步骤>-<工具>.json`，上下文里只保留"保头保尾"的摘要 + 路径提示；
模型需要全文时用 `read_text` 读那个路径（路径在工作区内，因此沙箱允许、用户也能直接看）。
好处：不再为了省 token 而**丢掉信息**——以前只有一个截断值，现在是"摘要 + 可取的全文"。
上限由 `spill_max_bytes` 控制（默认 2MB）；设为 `spill_threshold_chars: 0` 可整体关闭。

## 超时的诚实语义（结果未知 ≠ 没发生）

COM（Outlook）调用**无法真正取消**：超时只表示"我们不再等"，线程里的调用仍在执行。
旧实现在超时后直接放弃等待、外层还可能重试一次——等于在"第一次可能已经发出去"的情况下
再来一遍。现在改为：

1. 用 `asyncio.shield` 保护底层调用，**不抛弃**它，并给它挂回调：迟到的结果会写进日志
   （成功会明确提示"副作用很可能已发生"）；
2. 超时**绝不重试**同一动作；
3. 记下"结果未知"并**阶段性封锁不可幂等动作**（`outlook_send`/`delete_path`/`move_path`/
   `append_text`/`copy_path`），直到任一**只读动作**成功（例如 `outlook_unread`、
   `read_text`、`list_directory`）——即"先核对现状再说"；
4. 封锁期间相关动作会返回 `[TOOL_TIMEOUT_UNKNOWN]` 的结构化错误，说明该做什么。

超时上限可用 `agent.outlook_timeout_seconds`（默认 60 秒）调整。

## 事件日志与投影（P2-10）

会话的所有关键动作都会写成 **append-only 事件**（`events` 表，共 13 个发点）：
状态迁移、回合开始/结束、工具派发/收尾、工具结果入库（`step_recorded`）、
文件改动（`file_event`，来自 `file_events` 的真实写路径）、队列进出（`queue`，只记数量不记正文）、
消息元信息（`message`，仅 WebUI 对话）、审批下达/取用/拒绝。它现在是**旁路记录**（运行时仍读数据库
列），用途是排障、审计，以及为将来"状态可由日志重建"铺路。

```powershell
# 历史事件（一次性返回；kinds 可逗号分隔过滤）
curl "http://127.0.0.1:8765/v1/agent/sessions/<sid>/event-log?limit=50&kinds=turn_begin,turn_done"
# 由事件重建的状态快照 + 与数据库列的一致性核对（consistency 为空=一致）
curl "http://127.0.0.1:8765/v1/agent/sessions/<sid>/projection"
```

### 巡检：投影与列是否一致（P2-10 第三期前的验收）

```powershell
python scripts/health.py            # 巡检全部会话；--limit / --session / --json 可选
```

它逐个会话把**事件流重放**成状态，再与数据库列交叉核对，并额外检查**事件覆盖**
（表里有记录却没有对应事件 = 覆盖缺口）。输出区分四类：

| 类别 | 含义 | 是否致命 |
|---|---|---|
| 一致 | 投影与列完全相符 | — |
| **不一致** | 有事件却重建不出列里的事实 = 真缺口 | ✅ 退出码 1 |
| **覆盖告警** | 表里有记录但没有对应事件（多为功能上线前的历史会话） | ❌ 默认仅告警；`--strict` 视为失败 |
| 早于事件功能 | 0 事件的历史会话 | ❌ 跳过不计 |

设计取舍：一致性是**致命项**（说明日志不足以重建状态），覆盖缺口是**告警项**——
否则每逢新增事件类型，所有历史会话都会让 CI 变红，指标就失去意义。

### 读取路径开关（P2-10 第三期）

默认 `columns`（读数据库列）；设为 `projection` 时，**"结果未知步骤"的判定改读事件投影**：

```powershell
# config.json: "agent": { "event_source": "projection" }
# 或临时用环境变量（便于灰度验证，不必改配置）
$env:CBP_EVENT_SOURCE = "projection"
```

切换是**可回退的小步**：投影与列不一致时会**记告警并自动回退到列**（安全优先），
`/health` 会显示当前生效的 `event_source`。已实测：投影模式下评测 7/7、事件核对全绿，
与列模式行为一致。

会话删除会**级联清掉事件**（隐私优先），但"某会话何时被删"会写进独立的 `audit` 表
（只记 kind + session_id，不含内容），删除事实不随会话消失。

⚠️ 不要用普通 GET 调 `/v1/agent/sessions/{sid}/events`——那是 **SSE 长连接**（前端
EventSource 用），普通请求会一直挂着直到断开，这是设计如此；要历史事件请用上面的
`event-log`。

## 程序化工具调用 `run_code`（默认关闭）

让模型写一段代码，在**独立子进程**里批量调用**只读**工具（`read_text` / `list_directory` /
`find_files` / `skill_list` / `memory_search` / `tool_list`），最后用 `final(值)` 回传结果。
适合「读很多文件再汇总」这类分析任务：工具循环要 N 个远端回合，`run_code` 只要 1 个。

**实测（同一任务、deepseek/deepseek-chat）**

| 文件数 | 关闭 run_code | 开启 run_code |
|---|---|---|
| 4 | 5 步 / 4.3s | 5 步（**模型未采纳**，仍走工具循环） |
| 12 | **17 步** / 10.2s | **7 步**（find_files → run_code ×2 → read_text ×3）/ 8.9s |

即：小规模不划算（模型自己不会用），规模上来后回合数减少约 59%。

**安全边界（v1 刻意保守）**：只暴露只读工具（**无任何写入/删除/发信**）；子进程隔离；
硬限额——超时 `run_code_timeout_seconds`（默认 30s）、工具调用次数 `run_code_max_calls`
（默认 50）、回传上限 `run_code_max_output_chars`（默认 20000）；工具侧路径沙箱照旧生效。
子进程内的工具错误是**具名异常 ToolError**，模型代码可捕获后优雅收尾。

开启方式：`config.json` 的 `agent.allow_run_code: true`（默认 false）。建议同时把 `run_code`
加进 `approval_required_tools`，让它像 `run_python` 一样逐次授权。

### 评测的方差处理

模型是随机的：同一任务可能 3 步完成、也可能 28 步。因此评测支持在 `tests/evals/tasks.json`
里给单个任务声明 `"retries": 1`（失败后**重试一次**，两次都失败才算失败）——这样既能压掉
单次抽样的假失败，又不会掩盖真实回归。当前只有 `code-with-validation` 声明了重试。
