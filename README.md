# CopilotBridge Agent

> **无 Copilot 也能用**：浏览器按需启动——服务启动不碰浏览器，WebUI 与提供商模型（DeepSeek/Kimi/OpenAI 等）立即可用；只有用到 Copilot 时才拉起它，界面顶部会显示状态并提供「重试浏览器」。**注意**：lazy 模式下，重启后的第一个 Copilot 任务要等浏览器拉起与 SSO （企业租户可达数分钟），这段时间不算任务超时；提供商模型不受影响。

把 **Microsoft 365 Copilot 网页版** 包装成本地服务的桥接器：对外提供 **OpenAI 兼容的 Chat API**，并内置一个能读写本地文件、执行验证闭环的 **长期任务 Agent**。模型调用不经过任何官方 API——由 Playwright 自动化你本机的 Edge 浏览器，直接使用你已登录的 Copilot 网页会话。

```
客户端 ──HTTP──> FastAPI (app.py) ──Playwright──> Edge 持久化 Profile ──> m365.cloud.microsoft/chat
                     │
                     ├─ /v1/chat/completions      普通对话（OpenAI 协议，支持流式）
                     ├─ /v1/agent/completions     Agent 模式（工具调用 + 长任务）
                     ├─ /v1/agent/sessions/*      会话管理 / 暂停 / 恢复 / 删除
                     └─ /ui                       内置 Web 管理台
```

> 第一次用：看下面 **快速开始**（4 条命令）→ **操作手册**（WebUI 怎么用、AGENT.md/skills 怎么写、怎么回滚、怎么自升级、故障排查）。

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

未配置 Key 的模型在 `/v1/models` 里会标注"（未配置 API Key）"。自定义任意 OpenAI 兼容提供商（含自建网关）：

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

`markers` 的配置值会**并入**内置默认列表（不是覆盖），任一命中即生效，共 5 组：

```json
"markers": {
  "assistant_turn": ["Copilot said:"],
  "user_turn": ["You said:"],
  "answer_end": ["\nSources\n", "\nMessage Copilot\n"],
  "sources": ["Sources"],
  "ignored_lines": ["Copy", "复制", "更多"]
}
```

- `assistant_turn` / `user_turn`：回合分隔文本，用于定位最新回答与统计用户回合数——**界面中文化导致读取超时时，优先在这里追加中文锚点**；
- `answer_end`：回答正文的结束标记（来源区、输入提示等）；
- `sources`：回答中出现即视为生成完成的区块标题；
- `ignored_lines`：从回答中剔除的按钮残留文本。

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
| `max_service_unavailable` | `30` | 限流退避重试上限，超过后转入安全暂停 |
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
| GET | `/ui` | Web 管理台 |

## 操作手册

### 1. WebUI 怎么用

打开 `http://127.0.0.1:8765/ui`（建议强刷 `Ctrl+F5` 确保加载最新前端）。

| 位置 | 操作 |
|---|---|
| 侧栏顶部 **＋ 新任务** | 弹出对话框：填 Session ID（可留空）、工作目录（可留空＝用默认工作区）、选模型、写任务内容，选 **Agent 执行** 或 **普通对话**，点「启动任务」 |
| 侧栏 **SESSIONS** | 会话列表（状态圆点：绿=运行、黄=等待/暂停、红=失败、蓝=完成）；`筛选…` 输入框按标题/ID 过滤；右侧三个图标：📖 使用手册、▣ 切换自动化浏览器窗口显示、◐ 轮换主题（7 套，刷新保持） |
| 侧栏左缘细长拉手 | 收起/展开整个侧栏（会话列表占满宽度） |
| 顶部操作条 | 暂停 / 恢复 / 压缩上下文 / 继续执行 / 切换到页面（把自动化 Edge 跳到该会话）/ 打开会话（在你自己浏览器打开）/ 取消任务 / 更多 ▾（归档、回滚任务改动、回滚服务自升级、删除记录、连同 Copilot 会话删除） |
| 顶栏右侧**模型选择器** | 显示**当前会话自己的模型**，可直接切换；落库到该会话，**下一回合生效、服务重启后仍沿用**。选项来自 `/v1/models` 的可用模型；若会话原先的模型已不在菜单里，会标注"（菜单里未找到）" |
| 顶栏 ID 芯片 | 显示当前会话 ID，点击复制（删除前对号用） |
| KPI 条 | 状态、总步骤、当前会话步骤、会话切换次数、TOKEN 估算（带预算进度条） |
| 实时面板（四选项卡） | **当前执行状态**（阶段/等待详情/计数）、**当前回答**（模型最新回答，结构化动作自动转 JSON 视图、普通回答按 Markdown 渲染；提供商流式返回时**逐字增长**；有更新时页签出现绿点）、**最近发送的内容**（发给模型的原文尾部）、**继续对话**（见下）|
| 对话阅读体验 | 消息卡片（头像/角色名/时间戳/悬停复制）；助手消息按内容形态**可视化**：JSON 动作 → **工具卡**（工具名 + 原因 + 参数 JSON 视图，`content_b64` 显示为"[Base64 内容，共 N 字符]"）、批量动作 → 一卡多工具、`{"type":"final"}` → 正文按 **Markdown** 渲染并标「最终答复」、其它 JSON → JSON 视图、```json 代码块也用 JSON 视图；解析不了的 JSON 明确标「JSON 解析失败（原文）」而不是装作没事 |
| Markdown 代码块 | 对齐 Copilot 观感：左侧**行号**（横向滚动时固定不跑）、**语言显示名**（PowerShell/Python/…）、**软换行**（默认折行，可在「…」里关掉）、复制图标与「**…**」菜单（复制代码 / **下载为文件**（按语言给扩展名）/ 行号开关 / 换行开关 / 折叠）。**点击行号选中整行**（可多行），代码条随即出现「**复制选中 N 行**」（复制文本不含行号）。**语法高亮 11 种语言**（python/js/ts/json/bash/powershell/sql/yaml/html/css + ```diff/patch：新增绿、删除红、@@ 块头蓝）；超 22 行折叠，偏好记住选择 |
| 图片与附件 | Markdown 的 `![alt](url)` 渲染为图（**仅 https/http 与 data:image**,svg 与 `javascript:` 不放行）、点击放大，断链显示"图片加载失败"占位而不是破图图标；**粘贴的图片附件**在消息下方显示缩略图、点击放大（消息行记录文件名，经 `GET /v1/uploads/{name}` 供图，走统一鉴权），**会话列表**对带附件的会话显示 📎 角标 |
| 其余阅读辅助 | 长回答超 1800 字折叠；**🔍 对话内搜索**（高亮命中、Enter/Shift+Enter 跳转；不会命中行号列）；新到达消息渐显（尊重 `prefers-reduced-motion`）。全部零依赖手写，不引任何 CDN（离线可用） |
| ⏻ 终止服务 / 退出 | 侧栏右上电源图标（**任何状态下都可达**）与会话「更多」菜单里都有；点击弹**二次确认**（说明正在跑的任务会中断、状态已落库可恢复）。后端 `POST /v1/shutdown` **先回响应再停机**（否则前端看到连接重置像失败），走 uvicorn **优雅停机**（`should_exit`，退出码 0），拿不到 server 对象时退回 SIGINT，并始终有"宽限期 8 秒后硬退出"兜底——按钮必须真的能停掉服务。停机后页面停止一切轮询并显示"服务已终止"横幅 |
| 提供商流式输出 | `stream_provider_agent`（默认 `true`）开启时，提供商回合走流式：增量边收边写 `answer_preview`,经 **SSE 实时推送**到界面（见下），是真的逐字增长。**流式仍带 `response_schema`**（结构化动作协议的解析率不牺牲）；只在**一个增量都没收到**时才退回非流式（已收到内容再重发会造成重复计费/两份回答） |
| SSE 实时推送 | WebUI 用 `EventSource` 连 `GET /v1/agent/sessions/{sid}/events`,由服务端**推送** runtime 快照、队列变化、新消息、会话列表变化——正常情况下不再轮询。连接建立先送一份当前快照；15 秒无事件发一次心跳（`: ping`）防中间设备掐断；断开自动退订（不留内存）并**退回轮询**（保留 350ms 快速轮询 + 按退避重连）。`api_token` 走 `?token=`（EventSource 不能带自定义头，鉴权中间件已支持） |
| 📄 初始提示词 | 面板头部按钮，查看该会话**首回合实际发给模型的完整提示词**（系统提示词 + 工具清单 + 项目约定 + 验证闭环规则 + 任务）——排查"Agent 为什么这么做"的第一现场。首回合内容会落库（`initial_prompt`），不再被后续回合覆盖；**旧会话**未落库时按当前工作区**近似重建**并在标题里标注 |
| 消息队列 | 继续对话发送的消息**入队即返回**，不阻塞界面；任务正在跑时再发消息会排队显示"已排队（前面还有 N 条）"，处理完自动把回复刷进聊天区。聊天页队列条只列**等待中**的消息（正在处理的那条不显示，进度看运行状态面板），可**逐条编辑/删除**（✎ / ×，与消息历史同步）。运行状态面板显示"队列: N 条待处理"。支持粘贴图片（截图）随消息一起发送 |
| ⚡ 强行插入（**Ctrl+J**） | 任务跑着的时候，用 **Ctrl+J**（或聊天区「⚡ 强行插入」按钮）插入一条指令，并**立即中断当前回合**：提供商会话直接取消那次 HTTP 请求（无状态，取消即"这轮没发生"）；Copilot 网页会话**只在回答尚未开始生成时**停下等待（一旦已开始生成就让它答完，避免丢掉回答、以及"被放弃的回答"与新指令应答错位）。插入方式可选（记住上次选择）：**立即遵守**（`queue=step`:进 next-step 队列，追加到当前执行中，且**批量执行途中抢先放弃剩余动作**）/ **整轮重排**（`queue=turn`:当前回合作废、按新指令重新规划本回合）。插入会写进消息历史并在时间线留下 `user_inject`/`user_interrupt`/`step_preempted` 步骤；会话空闲时自动转为排队跟进 |
| 新话题直接生成文件 | Copilot 网页通道的**首轮**会附一条建议：任务要产出新文件时，优先一次 `copilot_file(path, instruction)` 让 Copilot 生成并下载——文件内容不经过聊天消息，不受单条输出上限约束（大文件一个回合拿到，不必 `write_text_b64` 分块写、也不会截断）。只在首轮、且仅 Copilot 通道（provider 会话没这个工具，提示了只会白烧回合）；**不写进系统提示词**（那里要保持逐字稳定以命中 prompt cache）。另：`copilot_file` 已归入 core 写工具、**不参与关键字裁剪**——原先它在 copilot 组（关键字=图片/截图/附件），任何"生成文件"类任务都会把它裁掉，模型看不到该工具，等于把这条路堵死 |
| 步骤时间线 | 每步带时间与状态；「查看结果」展开完整参数/结果 JSON；右上「⬇ 导出」下载完整时间线 JSON（排查问题首选） |

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
- 报"没有找到下载入口"时看服务目录的 `last_answer.html`（回答区现场快照），把结构发维护者；同时 Agent 会自动改用分块写入

**什么时候用哪个**：内容由 Copilot 生成（新写一个大文件/Office 文档）→ `copilot_file` 最省；内容来自本地已有材料、需要精确控制（逐字节一致的改写、二进制搬运）→ 本地工具 + 批量动作（`{"type":"actions",...}`,一次回答多个小动作）更稳。

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
   - 也**不允许**内联 `<script>`：首屏主题初始化已外置为 `webui/boot.js`；新增静态资源必须加进 `cbp/api.py` 里 `/ui/{asset}` 的白名单（目前是 `style.css` / `app.js` / `boot.js`）。
   - `style-src` 仍保留 `'unsafe-inline'`：页面大量使用 `style="..."` 属性与动态注入的 `<style>`，影响面小于脚本，暂不收紧。

## 已知限制

- 依赖 Copilot 页面的可访问性文本（如 `Copilot said:`）与 DOM 结构，**Microsoft 前端更新可能导致桥接失效**，出错时查看 `last_page.txt` 诊断快照；
- 普通对话的流式输出基于公共前缀 diff，若 Copilot 重写已输出文本可能出现重复/缺失（Agent 模式不受影响）;
- 所有请求共享一把全局锁串行执行（同一时刻只能有一个回合在浏览器中进行）；
- 回滚是**任务/作用域级**的：同一任务里混做多件不相关的改动，回滚会整块回退，没法只挑其中一件；
- 快照只覆盖经工具层的写操作；快照超限（单文件 8MB／单会话 800 条）的路径不记录、也就无法回滚。

## 项目结构

```
app.py            组装入口（shim：from cbp.api import app，uvicorn 加载点）
start.py          服务入口
install.py        依赖安装
cbp/              核心包
  api.py            HTTP 路由层：FastAPI 应用与全部端点，只做请求/响应适配
  bridge.py         配置、运行状态、Copilot 浏览器自动化核心与会话编排
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

## 沙箱拒绝是结构化的

越界/敏感路径不再只回一句"拒绝访问"，而是给出错误码（`FS_SANDBOX_ESCAPE` /
`FS_SANDBOX_DENYLIST` / `FS_SANDBOX_NOT_ALLOWED`）、当前模式、原因与**可行动方案**
（改用工作区相对路径 / 开启自升级 / 让用户把文件放进工作区）。工具参数写错时也会
列出该工具的**合法参数名**，避免模型反复试探。

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
