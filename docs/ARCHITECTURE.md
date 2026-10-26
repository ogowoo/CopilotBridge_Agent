# Gantry 架构基线

> 状态：现行架构基线  
> 适用仓库：Gantry

## 1. 系统定位

Gantry 是本地 Agent 自动化平台。系统通过 FastAPI 提供 OpenAI 兼容接口，以 Playwright 驱动本机 Edge 中的 M365 Copilot 网页版，同时支持 OpenAI 兼容 API Provider。平台还集成长任务 Agent、工具执行、审批、SQLite 会话持久化、快照回滚、自升级和 WebUI。

主要代码证据：

- 服务入口：app.py、start.py、install.py
- API：gantry/api.py、gantry/routes_*.py
- 运行态：gantry/bridge.py、gantry/lifecycle.py
- Agent：gantry/agent.py、gantry/agentrun.py、gantry/agent_prompt.py、gantry/agent_parse.py、gantry/agent_text.py
- 工具：gantry/tools.py、gantry/tools_*.py
- 持久化：gantry/session_store.py
- 浏览器与 Provider：gantry/browser.py、gantry/page.py、gantry/provider.py、gantry/providers.py
- 管理界面：webui/

## 2. 架构目标

- 提供统一的本地 OpenAI 兼容 API。
- 统一浏览器通道与外部 API Provider。
- 支持可恢复、可审计的长任务 Agent 循环。
- 集中治理文件、进程、Office、Outlook、技能、记忆和自升级工具。
- 通过审批、能力开关、路径沙箱、验证门和快照降低副作用风险。
- 持久化会话、消息、步骤和状态，支持暂停、恢复与回滚。

## 3. 总体组件图

mermaid
flowchart TB
    Client[API Client / WebUI] --> API[FastAPI API Layer]
    API --> Bridge[Bridge Runtime Facade]
    API --> AgentRoutes[Agent Routes]
    Bridge --> Browser[Playwright + Edge]
    Browser --> M365[M365 Copilot Web]
    Bridge --> Providers[OpenAI-compatible Providers]
    AgentRoutes --> Runner[Agent Run Loop]
    Runner --> Prompt[Prompt Assembly]
    Runner --> Parser[Action Parser]
    Runner --> Registry[Tool Registry]
    Registry --> Tools[File / Process / Office / Outlook / Skills]
    Runner --> Store[(SQLite Session Store)]
    Registry --> Snapshots[(Workspace Snapshots)]
    API --> UI[WebUI]


## 4. 组件分层

### 4.1 启动与安装层

- install.py 负责安装准备和依赖引导。
- start.py 读取配置并启动 Uvicorn。
- app.py 保持根目录入口轻量，主要实现下沉到 gantry/。
- pyproject.toml 和 requirements.txt 定义项目及运行依赖。

### 4.2 API 层

- gantry/api.py 创建 FastAPI 应用并装配中间件、生命周期和路由。
- gantry/routes_core.py 处理健康检查、模型和聊天等核心接口。
- gantry/routes_agents.py 处理 Agent 创建与执行。
- gantry/routes_sessions.py 管理会话、消息、步骤和运行状态。
- gantry/routes_config.py 管理配置读取、校验和更新。

### 4.3 运行态与通道层

- gantry/bridge.py 连接 API、浏览器、Provider、Agent、工具和存储。
- gantry/lifecycle.py 管理启动和关闭阶段的资源。
- gantry/browser.py、gantry/page.py、gantry/provider.py 实现 Edge 与 M365 Copilot 网页通道。
- gantry/providers.py 实现 OpenAI 兼容 API Provider。

### 4.4 Agent 与工具层

- gantry/agentrun.py 推进任务轮次并处理终止状态。
- gantry/agent_prompt.py 装配提示词、项目约定和工具定义。
- gantry/agent_parse.py 解析 JSON 动作并执行受控容错。
- gantry/agent_text.py 负责 token 估算、压缩和长上下文治理。
- gantry/tools.py 通过多个 mixin 统一组装 ToolRegistry。

## 5. 启动链路

1. 用户执行 python start.py。
2. 启动入口读取配置并准备运行环境。
3. gantry/api.py 创建 FastAPI 应用并注册中间件与路由。
4. gantry/lifecycle.py 初始化会话库、Bridge、Provider、浏览器和后台任务。
5. 服务默认监听本机回环地址，并向客户端提供 WebUI 与兼容 API。
6. 关闭时释放后台任务、浏览器资源和数据库连接。

## 6. 普通聊天请求链路

mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI
    participant B as Bridge
    participant W as Edge/M365 Copilot
    participant P as API Provider
    C->>A: Chat completion request
    A->>A: Auth, CSRF and validation
    A->>B: Normalized request
    alt Browser channel
        B->>W: Submit prompt through page
        W-->>B: Web response or stream
    else API Provider channel
        B->>P: OpenAI-compatible request
        P-->>B: API response or stream
    end
    B-->>A: Normalized completion
    A-->>C: JSON or SSE


浏览器通道复用本机登录态，但依赖外部 DOM、页面时序和窗口状态。API Provider 通道由 gantry/providers.py 处理，需要治理 Token、超时、重试、速率限制和供应商差异。

## 7. Agent JSON 工具循环

1. gantry/routes_agents.py 接收任务并创建或恢复会话。
2. gantry/agentrun.py 读取状态并推进轮次。
3. gantry/agent_prompt.py 合并系统规则、项目约定、历史和工具 Schema。
4. 模型返回单个 JSON 动作或批量动作。
5. gantry/agent_parse.py 解析动作并对轻微格式偏差执行受控容错。
6. ToolRegistry 校验工具名、参数、能力开关和审批状态。
7. 工具结果写入步骤时间线和 SQLite。
8. Agent 判断继续、等待审批、暂停、失败或完成。

mermaid
sequenceDiagram
    participant U as User
    participant R as Agent Route
    participant L as Run Loop
    participant M as Model
    participant X as Action Parser
    participant T as Tool Registry
    participant S as Session Store
    U->>R: Submit goal
    R->>L: Create or resume run
    loop Until terminal state
        L->>S: Load state and history
        L->>M: Prompt and tool schemas
        M-->>L: JSON action
        L->>X: Parse and validate
        X-->>L: Normalized action
        alt Approval required
            L->>S: Persist waiting state
        else Executable
            L->>T: Execute tool
            T-->>L: Result
            L->>S: Persist step and state
        end
    end
    L-->>R: Final state
    R-->>U: Result or event stream


## 8. Provider 与浏览器双通道

- 浏览器通道由 gantry/browser.py、gantry/page.py 和 gantry/provider.py 实现。
- API Provider 通道由 gantry/providers.py 实现。
- 两条通道应转换为统一的内部 completion、错误和流式语义。
- 日志不得泄露 API Token、Cookie、浏览器登录态或敏感提示内容。
- M365 Copilot 页面 DOM 变化是浏览器通道的固有外部风险，应通过诊断快照和选择器冒烟测试治理。

## 9. SQLite 持久化与数据流

gantry/session_store.py 保存会话、消息、步骤和运行状态，是任务恢复的持久化基线。运行期间还存在内存状态、SSE 事件、浏览器状态和 WebUI 状态，应使用统一状态定义。

mermaid
flowchart LR
    Request[User Request] --> Run[Agent Run]
    Run --> Model[Model Channel]
    Model --> Action[JSON Action]
    Action --> Tools[Tool Registry]
    Tools --> Workspace[Workspace]
    Tools --> External[Browser or Provider]
    Run --> DB[(SQLite)]
    Tools --> DB
    DB --> Events[SSE and WebUI]
    Workspace --> Snapshots[(Snapshots)]


SQLite 应具备清晰的连接所有权、显式关闭、Schema 版本、迁移、完整性检查和 WAL 维护策略。专项测试中观察到未关闭连接的 ResourceWarning，说明对象克隆或测试清理路径需要优先治理。

## 10. 工具注册与权限裁剪

gantry/tools.py 和 gantry/tools_*.py 统一注册文件、进程、Office、Outlook、技能、记忆及自升级能力。工具按任务上下文裁剪后提供给模型，以减少提示词体积。

每项工具应明确参数 Schema、风险等级、副作用、审批要求、配置开关、工作区边界、快照策略和验证要求。关键词裁剪存在表达变化导致必要工具缺席的风险，建议增加按需工具发现兜底。

## 11. 审批机制

- 高风险操作的审批由运行状态机控制，而不是仅依赖提示词。
- 审批应绑定会话、工具、参数指纹和有效期。
- 参数改变后旧审批必须失效。
- 只有真实 waiting 状态可暴露审批请求。
- 恢复执行前重新检查能力开关和路径边界。
- 审批记录进入审计时间线，但不得保存密钥或敏感正文。

## 12. 工作区沙箱

文件工具通过路径解析限制在工作区内，修改、复制、移动和删除前应保存快照。删除能力受 allow_delete 控制，并禁止删除工作区根目录。

路径沙箱不能隔离进程。启用 Python 或 PowerShell 后，子进程拥有服务账户可访问的操作系统权限。因此执行能力应默认关闭、独立审批，并限制运行时间、输出大小和进程树；长期应迁移到低权限 worker 或操作系统隔离环境。

## 13. 快照与回滚

- gantry/snapshots.py 保存工作区文件发生副作用前的原始状态。
- 工作区修改和服务自升级使用独立快照作用域。
- WebUI、API 和 scripts/rollback.py 提供在线与离线恢复入口。
- 回滚应覆盖新增、修改、移动和删除。
- 快照存在大小与数量上限，超限文件不得假定可恢复。
- 应定期执行自动恢复演练并记录 RTO、RPO 和失败原因。

## 14. 自升级

自升级由 gantry/tools_self.py 管理并默认关闭。完整流程包括能力开关、安装目录白名单、拒绝路径、修改前快照、服务级语法检查、单元测试、待重启标记和回滚通道。

导入失败、服务无法启动或验证不通过时应优先回滚，避免在损坏状态继续叠加修补。配置、数据、浏览器登录态与 Git 内部目录不应成为自升级目标。

## 15. WebUI

webui/ 采用三文件结构：

- webui/index.html：页面结构。
- webui/style.css：样式。
- webui/app.js：交互、API 和状态展示。

WebUI 展示会话、步骤时间线、审批、工具结果、配置和回滚入口。前端状态必须以 API 与事件流为准。结构或逻辑修改后应使用断 API 演示模式或 Playwright 执行验证。

## 16. 并发与窗口池

网页通道并发受 Edge 窗口池和登录态资源限制，API Provider 并发受外部服务速率和本机资源限制。

gantry/browser.py、gantry/routes_config.py 与 gantry/window_pool.py 对窗口数量上限的定义存在不一致，应统一实现、配置校验、config.example.json 和 docs/MULTI-WINDOW.md，并补充边界测试。

窗口池需明确空闲、占用、异常、回收和超时状态；任务取消时应释放窗口、Provider 流、后台任务和子进程。

## 17. 配置边界

- config.example.json 是公开配置结构基线，不得包含真实凭据。
- 默认监听地址应为 127.0.0.1。
- 非 loopback 绑定且 API Token 为空时必须拒绝启动。
- CSRF 防护默认开启。
- allow_delete、allow_python、allow_outlook_send、allow_self_upgrade 等高风险能力默认关闭。
- 配置 Schema 应拒绝未知字段、错误类型、越界值和危险组合。
- 每项配置应说明默认值、允许范围、安全影响和是否需要重启。

## 18. 异常处理

项目使用隔离失败策略避免非关键错误拖垮服务，但宽泛的 except Exception 和静默 pass 会隐藏根因。异常应分类为可忽略、可重试、告警后继续、步骤失败和服务失败。

所有非预期错误至少记录 session ID、步骤号、工具名、错误类别和 correlation ID。结构化日志必须统一脱敏、轮转和保留策略。

## 19. 测试与验证门

tests/ 覆盖 Agent 解析、审批、API 加固、Provider、窗口池、工具、快照、工作区隔离、验证门、子代理和 UI 状态。

已验证结果：

- Python 语法检查覆盖 255 个文件并通过。
- test_agent_parse.py、test_api_hardening.py、test_validation_gate.py 和 test_workspace_isolation.py 通过。
- 完整 unittest 曾超时，因此不能宣称全量测试通过。
- workspace isolation 测试暴露两个未关闭 SQLite 连接的 ResourceWarning。

建议将测试拆为 fast、integration、browser 和 release 四层，并在 CI 中设置独立超时。代码或配置修改后，只有语法检查与要求的测试均通过，才能宣称治理完成。

## 20. 部署拓扑

推荐单机、本地回环部署。若必须远程访问，应放置在受控反向代理或零信任入口之后，强制认证、TLS、来源限制和审计，不能直接暴露高权限工具 API。

## 21. 已知风险

### P0

- 非 loopback 监听未强制认证，可能远程暴露高权限能力。
- Python 或 PowerShell 进程执行权限接近服务账户权限。

### P1

- SQLite 连接生命周期可能不完整。
- SQLite、内存、SSE、浏览器和 WebUI 多状态源可能产生竞态或状态漂移。
- 宽泛异常捕获可能隐藏根因。
- 完整测试超时，缺少稳定分层的 CI 必过门。
- 依赖未完全锁定，安装和发布结果难以复现。

### P2

- 工具裁剪依赖关键词，必要工具可能未暴露。
- M365 Copilot DOM 变化可能破坏浏览器自动化。
- mixin 对宿主隐式属性的依赖增加静态分析难度。
- 窗口数量上限尚未在代码、配置和文档间统一。

## 22. 演进建议

- P0：非 loopback 强制 Token，并为进程执行、配置写入和自升级拆分权限范围。
- P1：修复 SQLite 连接警告，建立 Agent 状态迁移的单一提交入口。
- P1：测试分层，建立 Pull Request 必过门和 Windows 干净环境安装冒烟。
- P1：锁定依赖，执行漏洞扫描并在发布时生成 SBOM、校验和及来源证明。
- P1：拆分存活与就绪探针，统一结构化日志、脱敏、指标和告警。
- P2：为页面选择器建立契约测试，为工具裁剪增加按需发现兜底。

## 23. 架构基线完成定义

- 核心组件、路由和配置均可映射到唯一代码证据。
- 浏览器通道和 API Provider 通道均有自动化冒烟测试。
- Agent 状态迁移、审批、验证门和回滚均有回归测试。
- 非 loopback 认证硬门、SQLite 生命周期和窗口上限冲突已关闭。
- CI、依赖锁定、SBOM、可信发布和恢复演练形成交付门禁。
- 每次重大架构变更同步更新本文档和相关测试。
