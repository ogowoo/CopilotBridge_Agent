# CopilotBridge Agent

把 **Microsoft 365 Copilot 网页版** 包装成本地服务的桥接器：对外提供 **OpenAI 兼容的 Chat API**，并内置一个能读写本地文件、执行验证闭环的 **长期任务 Agent**。模型调用不经过任何官方 API——由 Playwright 自动化你本机的 Edge 浏览器，直接使用你已登录的 Copilot 网页会话。

```
客户端 ──HTTP──> FastAPI (app.py) ──Playwright──> Edge 持久化 Profile ──> m365.cloud.microsoft/chat
                     │
                     ├─ /v1/chat/completions      普通对话（OpenAI 协议，支持流式）
                     ├─ /v1/agent/completions     Agent 模式（工具调用 + 长任务）
                     ├─ /v1/agent/sessions/*      会话管理 / 暂停 / 恢复 / 删除
                     └─ /ui                       内置 Web 管理台
```

## 功能特性

- **OpenAI 兼容**：`/v1/chat/completions` 支持 stream / non-stream，可接入任何 OpenAI SDK 客户端；`/v1/models` 列出可用模型。模型列表以内置表为基础（Auto / Quick response / Think deeper / GPT / GPT-5.6 / Sol / Think，可在配置中扩展），并**在启动时自动打开 Copilot 模型菜单枚举选项（含 GPT 等父项下的二级子菜单）**——菜单里新出现的模型会自动并入（ID 形如 `copilot-gpt-5-7`），选择模型时也会自动展开子菜单定位；也可用 `POST /v1/models/refresh` 随时重扫。
- **API 提供商接入**：除 Copilot 网页自动化外，可直接接入任何 OpenAI 兼容 HTTP API（内置 OpenAI / DeepSeek / Kimi / Grok 预设，可自定义）。模型 ID 形如 `deepseek/deepseek-chat`，API Key 走环境变量；普通对话和 **Agent 模式（含工具循环）都可用**——JSON 动作协议是纯 prompt 驱动，与提供商无关。
- **Agent 模式**：Copilot 每回合返回一个 JSON 动作（调用工具或最终答复），本地执行工具后把结果送回，循环直到任务完成。
- **15+ 个工作区工具**：目录/文件读写（Base64 写入防 JSON 转义损坏）、精确替换、复制/移动/删除、`validate_python`、`run_unittest`、`run_python`，全部限制在工作区根目录内。
- **项目级约定与技能（AGENT.md / skills）**：工作区根目录的 `AGENT.md`（或 `agent.md`）会**全量注入系统提示**（默认 8000 字符截断，可用 `agent_md_max_chars` 调整），把项目规范、常用命令、禁忌事项直接交给 Agent；`skills/<名字>/SKILL.md` 目录式技能采用**渐进式披露**——提示里只注入技能名/一行简介/文件路径，模型判断与任务相关时再用 `read_text` 读取 SKILL.md 全文，多个技能也不会挤占上下文。SKILL.md 支持 YAML frontmatter（`name`/`description`），缺省时回退"首个一级标题 + 首个段落"。本仓库自身即带 [AGENT.md](AGENT.md) 与示例技能。
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
- **Web 管理台**：`/ui` 提供任务发起、实时进度、步骤时间线、暂停/恢复/取消、会话归档与远程 Copilot 会话删除（带同 Session 标记防误删）。

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
| `max_service_unavailable` | `30` | 限流退避重试上限，超过后转入安全暂停 |
| `service_retry_base_seconds` / `service_retry_max_seconds` | `30` / `600` | 限流指数退避的起始/上限 |
| `max_read_chars` / `max_write_chars` | `120000` / `200000` | 工具单次读写上限 |
| `allow_delete` | `false` | 是否开放 `delete_path` 工具 |
| `allow_python` | `false` | 是否开放 `run_python` 工具（**可执行任意代码，谨慎开启**） |
| `allow_office` | `true` | 是否开放 Office 文档工具（Excel/Word/PPT/PDF） |
| `allow_outlook` | `false` | 是否开放 Outlook 读取工具（未读/正文/搜索） |
| `allow_outlook_send` | `false` | 是否开放 `outlook_send`（会真实发出邮件，**谨慎开启**） |
| `python_timeout_seconds` | `20` | `run_python` 超时 |

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
| GET | `/v1/agent/sessions/{sid}/runtime` | 实时运行状态 |
| POST | `/v1/agent/sessions/{sid}/messages` | 在会话中追加消息（Agent 或普通对话模式） |
| POST | `/v1/agent/sessions/{sid}/actions/{pause\|resume\|cancel\|compact\|rollover\|archive}` | 会话控制 |
| POST | `/v1/agent/sessions/{sid}/activate` | 把自动化浏览器切到该会话页面 |
| DELETE | `/v1/agent/sessions/{sid}?delete_remote=true&force_local=true` | 删除会话（可选连带删除 Copilot 网页会话） |
| GET/POST | `/v1/agent/workspace` | 查询/切换工具工作区 |
| GET/POST | `/v1/agent/browser` | 查询/切换浏览器窗口显示（无头 ↔ 有窗口；带登录态重启浏览器上下文，`persist=true` 可写回 config.json） |
| GET | `/ui` | Web 管理台 |

## 安全警告

1. **API 鉴权**：默认绑 `127.0.0.1` 仅本机可达；配置 `api_token` 后所有 `/v1/*` 端点要求 Bearer 令牌。绑非回环地址且未配置 token 时**服务拒绝启动**。`agent.workspace_roots` 可进一步把工具工作区限制在白名单根目录内。
2. **间接 Prompt Injection**：Agent 读取的工作区文件内容会进入 Copilot 上下文，而返回的动作会被直接执行。**不要让 Agent 处理来源不可信的文件**；开启 `allow_python` / `allow_delete` 后风险升级为任意代码执行与文件删除。
3. **登录态敏感**：`EdgeProfile/` 目录包含你的 Copilot 登录 Cookie，已被 `.gitignore` 排除，请勿外泄。
4. **容错写文件**：对 Copilot 返回的未转义写文件动作有正则容错恢复，恢复结果可能不完美；重要文件请先备份，`.py/.json/.html` 有语法+单测验证闭环兜底，其他类型没有。

## 已知限制

- 依赖 Copilot 页面的可访问性文本（如 `Copilot said:`）与 DOM 结构，**Microsoft 前端更新可能导致桥接失效**，出错时查看 `last_page.txt` 诊断快照；
- 普通对话的流式输出基于公共前缀 diff，若 Copilot 重写已输出文本可能出现重复/缺失（Agent 模式不受影响）;
- 所有请求共享一把全局锁串行执行（同一时刻只能有一个回合在浏览器中进行）。

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
webui/            零依赖 Web 管理台（index.html 结构 + style.css + app.js）
AGENT.md          项目级 Agent 约定（工作区根目录存在时自动注入系统提示）
skills/           目录式技能库（每个技能一个 SKILL.md，按需加载）
scripts/          工具脚本（login.py 登录向导、calibrate.py 阈值校准、
                  outlook_selftest.py COM 自检）
tests/            单元测试（python -m unittest discover -s tests）
```

## 免责声明

本项目通过浏览器自动化使用 Copilot 网页版，仅供个人学习与研究。请遵守 Microsoft 365 Copilot 的使用条款，勿用于高频率、商业化或违反服务条款的用途。
