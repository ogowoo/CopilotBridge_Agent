# CopilotBridge Agent 技术文档

## 1. 文档目的

本文基于当前代码、配置、WebUI、测试和运维脚本，对项目架构、运行流程、数据与安全边界进行说明，并给出分级治理清单。项目目标是把 Microsoft 365 Copilot 网页会话封装为本地 OpenAI 兼容服务，同时提供可调用本地工具、持久化进度并跨 Copilot 会话接力的长期任务 Agent。

## 2. 系统边界

### 2.1 核心能力

- 普通对话：提供 `/v1/chat/completions`，支持流式与非流式响应。
- 模型管理：提供模型枚举、刷新和选择，并可接入 OpenAI 兼容外部提供商。
- Agent 执行：模型每轮只产生一个 JSON 动作，本地执行工具并回传结果，直至返回 final。
- 长任务治理：保存步骤、摘要、Token 估算、换会话记录和运行状态。
- 工作区工具：受根目录约束地读写文件、处理 Office 文档、执行 Python 语法检查与 unittest。
- M365 集成：通过浏览器登录态访问 Copilot；Outlook 默认使用 OWA 自动化，经典 Outlook COM 仅作兜底。
- 管理界面：提供任务创建、状态轮询、步骤时间线、暂停、恢复、删除、模型刷新和浏览器显示切换。

### 2.2 明确非目标

- 不是 Microsoft 官方 Copilot API，也不具备官方服务级别承诺。
- 不绕过租户权限、条件访问、多因素认证或网页登录要求。
- 不保证网页选择器长期稳定；Microsoft 页面更新可能导致自动化失效。
- 不应直接暴露到公网。当前设计更适合受控的单机、本地回环地址场景。

## 3. 总体架构

```text
OpenAI 客户端 / WebUI
        |
        v
FastAPI API 层 (app.py, cbp/api.py)
        |
        +--> 普通对话 --> Bridge --> Playwright --> Edge Profile --> M365 Copilot
        |
        +--> AgentRunner --> SessionStore (SQLite)
                  |             |
                  |             +--> sessions / steps / conv_stats
                  v
              ToolRegistry --> 工作区文件 / Office / Outlook / 校验测试
        |
        +--> ProviderRegistry --> OpenAI 兼容外部 API
```

主要模块职责：

- `app.py`：创建应用与装配核心对象。
- `start.py`：读取 `config.json` 的监听地址和端口并启动 Uvicorn。
- `cbp/api.py`：HTTP 路由、请求校验、流式响应、Agent 会话控制和 WebUI 服务。
- `cbp/bridge.py`：Copilot 页面自动化、模型菜单识别、消息发送、回合标记和回答收集。
- `cbp/agent.py`：动作解析、工具循环、上下文压缩、换会话接力、错误分类和验证门禁。
- `cbp/session_store.py`：SQLite 持久化、会话状态、步骤、统计与清理。
- `cbp/tools.py`：工作区沙箱、文件与 Office 工具、测试和 Outlook 适配。
- `cbp/providers.py`：外部 OpenAI 兼容提供商与模型路由。
- `cbp/owa.py`：OWA 页面自动化。
- `cbp/copilot_delete.py`：Copilot 远端会话删除辅助逻辑。
- `webui/index.html`：无构建步骤的单页管理控制台。


## 4. 关键运行流程

### 4.1 启动与首次登录

1. `start.py` 从根目录读取 `config.json`，再以 `uvicorn app:app` 启动服务。
2. 默认监听 `127.0.0.1:8765`，符合仅本机访问的安全边界。
3. 首次使用前运行 `python scripts/login.py`，以持久化 Edge Profile 打开 Copilot 登录页。
4. 登录态保存在 `EdgeProfile`。登录脚本会检测服务端口，避免服务运行时并发占用或删除 Profile。
5. `python scripts/login.py --reset` 会要求输入 `yes`，之后删除现有登录态并重新登录。

### 4.2 普通对话

1. 客户端调用 `/v1/chat/completions` 并提交 OpenAI 风格消息。
2. API 层按模型 ID 路由到 Copilot 网页桥接或外部兼容提供商。
3. Copilot 路径通过 Playwright 复用持久化 Edge 上下文，选择模型、发送消息并等待回答稳定。
4. 流式模式返回增量事件；非流式模式返回完整回答。
5. 每轮注入唯一回合标记，用于确认当前请求已被页面接收，降低误读上一轮回答的风险。

### 4.3 Agent 长任务

1. API 创建持久化 Session，由 AgentRunner 组装规则、目标、历史摘要和最近步骤。
2. 模型每轮只能返回 `tool` 或 `final` 两种 JSON 动作。
3. ToolRegistry 校验工具名、参数、权限开关和工作区路径后执行动作。
4. 执行结果作为新步骤写入 SQLite，并回传模型继续决策。
5. 达到阈值或网页长度上限时，系统压缩本地上下文或新建 Copilot 会话，并携带检查点继续。
6. 代码治理任务只有在 Python 语法检查和 unittest 都通过后才能宣称完成。

### 4.4 会话治理与恢复

- SessionStore 持久化任务状态、步骤、摘要、Token 估算、远端会话索引和结束统计。
- 本地上下文压缩不会缩短 Copilot 远端对话，因此必须结合主动换会话、页面上限检测和检查点接力。
- auto_resume 用于服务重启后恢复未完成任务；暂停只阻止后续循环，不等于取消正在进行的浏览器请求。
- 本地 Session 删除与远端 Copilot 对话删除是独立操作，远端删除失败时应保留诊断结果。
- scripts/calibrate.py 读取 conv_stats，按模型和结束原因分析实际容量，辅助校准压缩与换会话阈值。

## 5. Outlook 集成与诊断

- 默认路径是 OWA 网页自动化，经典 Outlook COM 仅作 Windows 环境兜底。
- scripts/outlook_selftest.py 依次检查 pywin32、COM 初始化、MAPI、收件箱和未读过滤。
- 自检同时对比线程池调用与专用 COM 线程 _ComWorker，用于定位后台线程无消息泵、权限不一致、会话隔离和 Outlook 忙等问题。
- 自检只读取邮件，不验证发送。发送能力必须继续由 allow_outlook_send 独立控制。
- （已修复 2026-10-12）自检脚本曾使用 Path 但未导入 pathlib.Path，执行到专用 COM 线程验证步骤时会触发 NameError。

## 6. 配置与权限边界

- server.host 默认绑定 127.0.0.1；若改为非回环地址，必须同时设置强 API Token、主机防火墙和可信反向代理。
- api_token 为空时不提供有效访问控制，仅适合单用户本机环境。
- workspace 与 workspace_roots 决定文件工具可访问范围，应使用专用目录，禁止指向用户主目录或系统盘根目录。
- allow_delete、allow_python、allow_office、allow_outlook 和 allow_outlook_send 是高风险能力开关，生产配置应遵循最小权限原则。
- 外部提供商密钥通过环境变量读取，不应写入配置文件、日志、WebUI 或数据库。
- diagnostics 有助于定位网页自动化问题；diagnostic_html 可能保存页面内容和业务数据，不应长期启用。
- 当前实际配置启用了 Outlook 读取，而示例配置默认关闭；部署文档必须明确这是环境差异，不应直接复制实际配置。

## 7. 数据、状态与生命周期

- SQLite 数据库保存 Agent 会话、步骤、错误、摘要和会话容量统计，是任务恢复的核心状态源。
- EdgeProfile 保存 Microsoft 登录态，敏感级别高于普通配置文件，应限制本机账户访问并排除出代码仓库和普通备份共享。
- 诊断文件可能包含页面 HTML、提示词、回答和邮件相关内容，必须设置保留周期和定期清理机制。
- 已完成会话按 completed_session_retention_days 清理；仍需验证异常退出、暂停和失败状态是否受到一致的容量上限约束。
- max_sessions、总步骤数、单会话步骤数和换会话次数共同限制资源消耗，但不能替代磁盘容量监控。

## 8. 安装、运行与运维

1. 使用受支持的 Python 环境创建独立虚拟环境。
2. 安装 requirements.txt 中的依赖，并完成 Playwright 浏览器组件准备。
3. 从 config.example.json 复制生成本机 config.json，不要覆盖已有运行配置。
4. 运行 python scripts/login.py 完成 Edge 持久化 Profile 登录。
5. 运行 python start.py，访问 /ui 和 /v1/models 做基础检查。
6. 创建只读测试任务，确认会话持久化、暂停恢复和浏览器回合标记正常。
7. 若启用 Outlook，先运行只读自检，再逐项开放读取能力；发送能力保持关闭，除非存在明确审批和审计要求。
8. 定期备份 SQLite，备份前应停止写入或使用 SQLite 在线备份方式，避免直接复制活跃数据库造成不一致。

安装脚本目前直接调用 pip 和 playwright install chromium，但运行配置使用 Edge channel。该行为增加下载体积且无法保证 Edge 本体存在，安装流程应改为显式检测 Python、Edge、依赖和目录权限，并输出可操作的失败原因。

## 9. 可观测性与故障定位

建议统一记录以下字段：本地 Session ID、Copilot 会话索引、回合标记、模型、提供商、步骤号、动作类型、耗时、重试次数、结束原因和错误分类。日志不得记录 API Key、完整 Cookie、认证头或邮件正文。

常见故障定位顺序：

1. 检查服务端口、配置解析和 SQLite 可写性。
2. 检查 EdgeProfile 是否被其他进程占用，以及 Copilot 页面是否仍处于登录状态。
3. 检查回合标记是否送达，再区分等待超时、限流、页面长度上限和动作格式错误。
4. 检查模型菜单或网页选择器是否因页面更新失效。
5. 检查工作区权限、工具开关和路径沙箱拒绝记录。
6. 对 Outlook 问题先运行只读自检，并区分 OWA 页面问题与 COM 线程问题。
7. 使用 scripts/calibrate.py 分析真实会话结束样本后再调整 Token 阈值，避免仅凭单次任务修改配置。
