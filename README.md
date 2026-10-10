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

- **OpenAI 兼容**：`/v1/chat/completions` 支持 stream / non-stream，可接入任何 OpenAI SDK 客户端；`/v1/models` 列出可用模型。模型列表以内置表为基础（Auto / Quick response / Think deeper / GPT / GPT-5.6 / Sol / Think，可在配置中扩展），并**在启动时自动打开 Copilot 模型菜单枚举选项**——菜单里新出现的模型会自动并入（ID 形如 `copilot-gpt-5-7`），也可用 `POST /v1/models/refresh` 随时重扫。
- **Agent 模式**：Copilot 每回合返回一个 JSON 动作（调用工具或最终答复），本地执行工具后把结果送回，循环直到任务完成。
- **15 个工作区工具**：目录/文件读写（Base64 写入防 JSON 转义损坏）、精确替换、复制/移动/删除、`validate_python`、`run_unittest`、`run_python`，全部限制在工作区根目录内。
- **长任务韧性**：
  - 每个回合注入唯一标记 `[CBP-TURN:...]`，确认送达后才读取回答，杜绝读到上一回合的旧答案；
  - 严格区分三类失败——**限流**（原地指数退避重试）、**对话达到长度上限**（总结进度后切换新会话重发）、**回答格式错误**（容错解析 + 压缩上下文后原会话继续）；
  - 上下文压缩与多会话接力（rollover），单任务最长可跨 25 个 Copilot 会话；
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
python login.py

# 4. 启动服务（默认 http://127.0.0.1:8765）
python start.py
```

启动后打开 `http://127.0.0.1:8765/ui` 使用管理台，或访问 `/docs` 查看 API 文档。

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
| `server.host` / `server.port` | `127.0.0.1` / `8765` | 监听地址，**不要改成 0.0.0.0（见安全警告）** |
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
| `session_store` | `data/agent.db` | SQLite 会话库路径 |
| `max_total_steps` | `1000` | 单任务总步数上限（硬顶 10000） |
| `max_steps_per_conversation` | `160` | 单个 Copilot 会话内步数上限（触发换会话） |
| `max_conversation_rollovers` | `25` | 最多切换多少个 Copilot 会话 |
| `context_token_budget` | `128000` | 估算上下文预算 |
| `compact_at_tokens` | `76000` | 达到即压缩历史为摘要 |
| `rollover_at_tokens` | `104000` | 达到即换新会话（需 `auto_rollover`）；须满足 `0 < compact < rollover ≤ budget` |
| `keep_recent_steps` | `8` | 压缩/换会话时保留的最近步数 |
| `max_summary_tokens` | `5000` | 长期摘要估算上限 |
| `max_context_chars` | `420000` | 字符数兜底阈值 |
| `auto_rollover` | `false` | 是否按本地阈值**主动**换会话；默认只在 Copilot 提示对话已满时才换 |
| `min_steps_before_rollover` | `30` | 主动换会话的最小步数门槛 |
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
| GET | `/ui` | Web 管理台 |

## 安全警告

1. **API 无鉴权**：所有接口对本机任何进程开放。请保持 `server.host` 为 `127.0.0.1`；如必须暴露到局域网，请自行在前面加反向代理鉴权。
2. **间接 Prompt Injection**：Agent 读取的工作区文件内容会进入 Copilot 上下文，而返回的动作会被直接执行。**不要让 Agent 处理来源不可信的文件**；开启 `allow_python` / `allow_delete` 后风险升级为任意代码执行与文件删除。
3. **登录态敏感**：`EdgeProfile/` 目录包含你的 Copilot 登录 Cookie，已被 `.gitignore` 排除，请勿外泄。
4. **容错写文件**：对 Copilot 返回的未转义写文件动作有正则容错恢复，恢复结果可能不完美；重要文件请先备份，`.py/.json/.html` 有语法+单测验证闭环兜底，其他类型没有。

## 已知限制

- 依赖 Copilot 页面的可访问性文本（如 `Copilot said:`）与 DOM 结构，**Microsoft 前端更新可能导致桥接失效**，出错时查看 `last_page.txt` 诊断快照；
- 普通对话的流式输出基于公共前缀 diff，若 Copilot 重写已输出文本可能出现重复/缺失（Agent 模式不受影响）;
- 所有请求共享一把全局锁串行执行（同一时刻只能有一个回合在浏览器中进行）。

## 项目结构

```
app.py            FastAPI 服务 + Playwright 浏览器自动化
agent.py          Agent 主循环、JSON 动作协议、容错解析、压缩/换会话
tools.py          工作区工具集（路径沙箱）
session_store.py  SQLite 会话持久化与中断恢复
webui/index.html  零依赖单文件 Web 管理台
login.py          首次登录向导
install.py        依赖安装
start.py          服务入口
```

## 免责声明

本项目通过浏览器自动化使用 Copilot 网页版，仅供个人学习与研究。请遵守 Microsoft 365 Copilot 的使用条款，勿用于高频率、商业化或违反服务条款的用途。
