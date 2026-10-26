# Gantry 代码与工程分析报告

## 1. 管理层摘要

- 项目已从网页代理演进为本地 Agent 自动化平台，集成 FastAPI、Playwright、M365 Copilot、外部 API Provider、工具执行、SQLite、审批、快照回滚和 WebUI。
- 工程优势是安全开关、验证门、工作区隔离和回滚能力；主要短板是远程暴露保护、资源生命周期、状态一致性、完整测试超时、供应链和发布治理。
- 建议实施顺序：安全边界 → SQLite 与状态治理 → 测试与 CI → 供应链与发布 → 可观测性与文档。

## 2. 事实与判断

- 已验证事实：Python 语法检查覆盖 255 个文件并通过。
- 已验证事实：test_agent_parse.py、test_api_hardening.py、test_validation_gate.py、test_workspace_isolation.py 均通过。
- 已验证事实：完整 unittest 曾超时，因此不能宣称全量测试通过。
- 已验证事实：workspace isolation 测试出现两个未关闭 SQLite 连接的 ResourceWarning。
- 风险判断：未在真实生产负载下验证的内容均作为风险或建议，不写成已发生故障。

## 3. 项目定位与目录

| 路径 | 职责 |
|---|---|
| app.py、start.py、install.py | 应用入口、启动和安装 |
| gantry/api.py、gantry/routes_*.py | FastAPI 与 OpenAI 兼容接口 |
| gantry/bridge.py、gantry/lifecycle.py | 运行态装配和资源生命周期 |
| gantry/browser.py、gantry/page.py、gantry/provider.py | Edge 与 M365 Copilot 网页通道 |
| gantry/providers.py | OpenAI 兼容 API Provider |
| gantry/agent*.py、gantry/agentrun.py | Agent 提示词、解析、文本治理和执行循环 |
| gantry/tools.py、gantry/tools_*.py | 工具注册、权限、审批和执行 |
| gantry/session_store.py | SQLite 会话、消息、步骤和状态 |
| gantry/snapshots.py、gantry/tools_self.py | 快照、回滚和自升级 |
| webui/、scripts/、tests/ | 管理台、运维脚本和测试 |

## 4. 核心调用链

- 启动：start.py → gantry/api.py → gantry/lifecycle.py → Bridge、数据库、浏览器和后台任务。
- 普通请求：路由 → gantry/bridge.py → 浏览器通道或 gantry/providers.py → 标准响应或 SSE。
- Agent：gantry/routes_agents.py → gantry/agentrun.py → 提示词 → 模型 → gantry/agent_parse.py → ToolRegistry → SQLite。

## 5. 架构优点

- 安全采用纵深防御：本机监听、CSRF、能力开关、审批、路径约束、快照和验证门。
- 模型输出治理完整：JSON 容错、截断恢复、上下文压缩、步骤限制和重复动作保护。
- 副作用可恢复：工作区修改和服务自升级分别保存快照，并提供在线与离线回滚。
- 测试覆盖解析、审批、API 加固、验证门、工作区隔离、Provider、窗口池和工具等关键域。

## 6. 安全与可靠性

### P0

- 非 loopback 绑定若未强制 Token，可能远程暴露高权限 API。应在 start.py、gantry/api.py 和配置校验中 fail closed。
- Python 或 PowerShell 进程权限接近服务账户，路径沙箱无法隔离进程。应使用低权限 worker、资源限制和独立审批。

### P1

- 宽泛异常捕获可能把根因表现为超时或状态漂移，重点涉及 gantry/agent.py、gantry/agentrun.py、gantry/api.py、gantry/bridge.py。
- 浏览器通道依赖外部 DOM，应增加选择器冒烟测试、诊断快照和降级路径。

## 7. 状态与资源生命周期

- 系统同时存在 SQLite、内存、SSE、浏览器和 WebUI 状态，建议建立唯一状态提交入口和明确迁移表。
- 审批、暂停、恢复和终止应保持幂等，并带稳定状态版本。
- 两个 SQLite ResourceWarning 表明克隆或测试清理路径可能未可靠关闭连接，应明确连接所有权并增加 teardown 检查。

## 8. 测试与 CI/CD

- 将测试拆为 fast、integration、browser、release 四层，并设置独立超时。
- Pull Request 必须执行语法检查、fast、integration、安装冒烟和安全扫描，失败禁止合并。
- Windows 干净环境应验证安装、启动、健康检查和 WebUI。
- 完整测试超时解决前，专项测试通过不能替代全量回归结论。

## 9. 依赖、供应链与发布

- 锁定直接和传递依赖，固定 Python、Playwright 和浏览器版本。
- CI 执行漏洞及许可证扫描，发布生成 SBOM。
- Tag 触发干净构建、全量验证、校验和、签名或来源证明。
- 自动演练新增、修改、移动、删除和 revision 回滚，并记录 RTO/RPO。

## 10. 可观测性

- 分离 /health/live 与 /health/ready。
- 统一 JSON 日志、correlation ID、脱敏、轮转和保留策略。
- 输出延迟、错误率、队列、任务成功率、重试、窗口和 SQLite 指标。
- 对资源耗尽、连续失败、审批积压和浏览器不可用建立告警。

## 11. 文档成熟度

- 已有 README、快速入门、技术说明、多窗口、Provider 和版本文档。
- 仍需完善 API、配置、安全、故障排查和运维手册。
- gantry/browser.py、gantry/routes_config.py、gantry/window_pool.py 的窗口上限定义需要统一后再同步文档。
- 规范、教程、设计、评审和历史研究应分类管理。

## 12. 建议实施顺序

- P0：非 loopback 强制 Token；隔离高风险进程执行；建立 CI 必过门。
- P1：修复 SQLite 连接警告；统一状态机；测试分层；锁依赖和可信发布。
- P2：完善指标告警、工具发现兜底、DOM 契约测试和文档分类。
