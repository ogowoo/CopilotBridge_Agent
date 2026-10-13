# CopilotBridge Agent 技术文档

> 版本：2026-11（第二轮全面审查后）｜配套：使用向说明见 [README.md](../README.md)，
> 本轮审查原始报告见 [REVIEW-2026-11-agent-session.md](REVIEW-2026-11-agent-session.md)

## 1. 文档目的

本文说明项目的架构、运行流程、数据与权限边界、可观测性与运维方式，并记录 2026-11
全面代码审查的修复项与已知限制。项目目标：把 Microsoft 365 Copilot 网页会话封装为
本地 OpenAI 兼容服务，同时提供可调用本地工具、持久化进度、可跨 Copilot 会话接力的
长期任务 Agent。

## 2. 系统边界

### 2.1 核心能力

- **普通对话**：`/v1/chat/completions`，支持流式与非流式。
- **模型管理**：模型枚举/刷新/选择，可见性严格对齐 Copilot 菜单叶子；可接入 OpenAI 兼容外部提供商。
- **Agent 执行**：模型每轮只产生 JSON 动作（或一次批量动作），本地执行工具并回传结果，直至 final。
- **批量动作协议**：一次回答可执行多个工具动作，省 Copilot 回合；批内 final 延后到工具之后由验证门裁决。
- **长任务治理**：步骤、摘要、Token 估算、换会话记录、运行状态全部持久化；支持暂停/恢复/取消/归档。
- **工作区工具**：受根目录约束的文件读写、Base64 写入、Office 文档读写、语法检查与 unittest。
- **Copilot 文件通道**：`copilot_attach`（本地文件上传给 Copilot 分析）、`copilot_file`（让 Copilot 生成文件并落进工作区）。
- **任务级快照**：写操作前记录原像，支持"回滚整个任务的改动"（修改还原、新建删除）。
- **自升级**：白名单 + 双验证门 + 待重启标记 + 在线/离线回滚（默认关闭）。
- **项目上下文**：两级 `AGENT.md` + 两级 `skills/`（渐进式披露），Agent 可用 `skill_create` 自行沉淀技能。
- **M365 集成**：浏览器登录态访问 Copilot；Outlook 默认走 OWA 网页自动化，经典 COM 仅作兜底。
- **管理界面**：零构建单页 WebUI（`index.html + app.js + style.css`）。

### 2.2 明确非目标

- 不是 Microsoft 官方 Copilot API，无服务级别承诺。
- 不绕过租户权限、条件访问、MFA 或网页登录要求。
- 不保证网页选择器长期稳定；Microsoft 前端改版可能导致自动化失效。
- 不应直接暴露公网：默认仅监听回环地址，适合受控单机场景。

## 3. 总体架构

```text
OpenAI 客户端 / WebUI
        │
        ▼
FastAPI 路由层  app.py(shim) → cbp/api.py
        │
        ├─ 普通对话 ─────────► cbp/bridge.py ─► Playwright ─► Edge 持久化 Profile ─► M365 Copilot
        │                          │  ├─ 模型菜单扫描 / 会话内模型锚定
        │                          │  ├─ CBP-TURN 回合标记、CBP-AGENT 会话身份标记
        │                          │  ├─ 任务快照（cbp/snapshots.py）
        │                          │  └─ OWA 后端（cbp/owa.py）
        │
        ├─ Agent 任务 ───────► cbp/agent.py（主循环/动作协议/容错解析/压缩/换会话/验证门）
        │                          ▼
        │                      cbp/tools.py（工作区沙箱、Office、技能、自升级工具）
        │                          ▼
        │                      cbp/session_store.py（SQLite：sessions/steps/messages/file_events/conv_stats）
        │
        ├─ 外部提供商 ───────► cbp/providers.py（OpenAI 兼容 HTTP）
        └─ 会话删除 ─────────► cbp/copilot_delete.py（URL 身份 + 标记同属校验 + 对账 + URL 级终判）
```

### 3.1 模块职责

| 文件 | 行数* | 职责 |
|---|---|---|
| `app.py` | 21 | 组装 shim：`from cbp.api import app`（uvicorn 加载点） |
| `cbp/api.py` | ~700 | HTTP 路由、鉴权中间件、SSE、会话控制、自升级/快照端点、静态资源 |
| `cbp/bridge.py` | ~2100 | 页面自动化、模型识别与锚定、回合标记、回答收集、会话编排、快照接线、自升级状态 |
| `cbp/agent.py` | ~1090 | 动作协议与容错解析、批量动作、主循环、压缩/换会话、验证闭环（旗持久化） |
| `cbp/tools.py` | ~1240 | 工作区沙箱与文件工具、Office、Outlook 路由、技能 list/read/create、self_* 与权限门 |
| `cbp/session_store.py` | ~240 | SQLite 持久化、列迁移、状态机、统计与清理 |
| `cbp/snapshots.py` | ~250 | 任务级快照：捕获原像、按根维度回滚、原子还原、保留策略 |
| `cbp/copilot_delete.py` | ~600 | 侧栏会话删除：定位、同属校验、确认、对账补删、URL 级复核 |
| `cbp/owa.py` | ~200 | Outlook 网页自动化（搜索/阅读/发送） |
| `cbp/providers.py` | ~160 | 外部 OpenAI 兼容提供商与模型路由 |
| `scripts/login.py` | 61 | 登录向导（`--reset` 重置登录态） |
| `scripts/calibrate.py` | 92 | 用 `conv_stats` 实测样本反推压缩/换会话阈值 |
| `scripts/rollback.py` | 120 | 离线自升级回滚（服务起不来时使用） |
| `scripts/outlook_selftest.py` | 142 | COM 自检（pywin32→CoInitialize→Dispatch→MAPI→收件箱） |
| `webui/*` | 138+490+325 | 单页管理台（HTML/CSS/JS 三文件，无构建步骤） |
| `tests/*` | 12 文件 / 92 用例 | 单元与集成测试 |

\* 行数随迭代变化，此处为 2026-11 审查时的规模。

## 4. 关键运行流程

### 4.1 启动与首次登录

1. `start.py` 读 `config.json`，以 `uvicorn app:app` 启动（默认 `127.0.0.1:8765`）。
2. lifespan 启动 Playwright 持久化上下文 → 等待 Copilot 就绪（企业租户 SSO/ADFS 回跳可达数十秒）。
3. **归档"待重启"标记**：`mark_self_upgrade_applied()` 只在服务真正启动时调用（不在模块导入时，避免测试/脚本误清标记）。
4. 就绪后做模型菜单扫描，把叶子标签写入可见性集合。
5. 首次使用前跑 `python scripts/login.py`；登录态存于 `EdgeProfile/`。
6. 非回环地址且未配置 `api_token` 时拒绝启动。

### 4.2 普通对话

1. `/v1/chat/completions` → 按模型 ID 路由到 Copilot 网页或外部提供商。
2. Copilot 路径复用持久化上下文：选中模型 → 注入唯一 `CBP-TURN` 标记 → 发送 → 等回答稳定。
3. 标记确认送达（输入框清空 + 页面出现标记），缺失时回退到 user_turn 计数。
4. 流式返回增量事件，非流式返回整段回答。

### 4.3 Agent 长任务

1. 创建/复用持久化 Session；`AgentRunner` 组装规则、目标、摘要、最近步骤与项目上下文。
2. 模型每轮返回一个 JSON 动作或 `{"type":"actions","actions":[...]}` 批量。
3. `ToolRegistry` 校验工具名、参数、权限开关、路径沙箱后执行；结果写入 SQLite 并回传模型。
4. 代码类改动（`.py/.json/.html`，含 b64 写入族与 `self_*`）置**验证旗**；只有
   `validate_python`+`run_unittest`（自升级为 `self_validate`+`self_unittest`）均通过才允许 final。
5. 达到压缩阈值 → 生成本地摘要；达到换会话阈值 → 新建 Copilot 会话并带检查点接力。
6. 暂停/取消/中断可从检查点恢复；验证旗随会话持久化（防止恢复后绕过验证门）。

### 4.4 会话治理与恢复

- `conv_tokens/conv_chars` **只在换会话时清零**（反映远端对话真实压力）；
  `estimated_tokens/estimated_chars` 在本地压缩时重置。
- 本地压缩不会缩短远端 Copilot 对话，因此必须结合主动换会话与检查点接力。
- `auto_resume` 只恢复"服务中断"的会话；用户主动暂停不会被自动拉起。
- 本地 Session 删除与远端 Copilot 对话删除相互独立；远端失败时保留本地记录与逐条 `remote_results`。
- `scripts/calibrate.py` 读取 `conv_stats` 按模型/结束原因分析容量，辅助校准阈值。

## 5. HTTP 接口

鉴权：配置 `api_token` 后，**所有 `/v1/*` 请求**需带 `Authorization: Bearer <token>`、
`X-CBP-Token: <token>` 或 `?token=`（便捷用法，注意会进入浏览器历史/Referer）。
token 比较使用 `hmac.compare_digest`；访问日志对 `token=` 做脱敏。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` `/health` | 健康检查（含页面状态与 busy） |
| GET/POST | `/v1/agent/browser` | 浏览器可见性（有窗口/无窗口）切换 |
| GET | `/v1/models` | 模型清单（含 `available` 标记） |
| POST | `/v1/models/refresh` | 重新扫描 Copilot 菜单 |
| GET/POST | `/v1/chat/completions` | OpenAI 兼容对话（GET 便于浏览器直测） |
| GET | `/ui` `/ui/` `/ui/{asset}` | 管理台与静态资源（白名单） |
| GET | `/v1/agent/sessions` | 会话列表 |
| GET | `/v1/agent/sessions/{sid}` | 会话详情（含 steps/messages/file_events/conv_stats/snapshots） |
| GET | `/v1/agent/sessions/{sid}/runtime` | 实时阶段、预览、busy、模型告警 |
| POST | `/v1/agent/sessions/{sid}/actions/{action}` | pause/resume/cancel/archive/compact/rollover… |
| POST | `/v1/agent/sessions/{sid}/messages` | 追加消息（普通对话或 Agent 续跑） |
| POST | `/v1/agent/sessions/{sid}/activate` | 切换激活会话（含 503/409 语义） |
| POST | `/v1/agent/sessions/{sid}/model` | 设定/更正会话模型（失效模型自动回退并给出说明） |
| GET | `/v1/agent/sessions/{sid}/snapshots` | 任务改动快照摘要（可回滚清单） |
| POST | `/v1/agent/sessions/{sid}/rollback` | 回滚该会话的工作区改动 |
| DELETE | `/v1/agent/sessions/{sid}` | 删除会话（`delete_remote` / `force_local`） |
| GET | `/v1/agent/self/status` | 自升级状态：开关、白名单、待重启、快照作用域 |
| GET | `/v1/agent/self/snapshots` | 自升级快照作用域清单 |
| POST | `/v1/agent/self/rollback` | 回滚自升级（`sid` 或 `last=true`） |
| GET/POST | `/v1/agent/workspace` | 查看/切换工作区（运行中被拒） |
| GET | `/v1/agent/tools` | 当前注册的工具清单 |
| POST | `/v1/agent/completions` | Agent 任务入口（OpenAI 兼容响应 + agent_steps） |

## 6. 工具与权限

### 6.1 工具清单（按开关分组）

| 组 | 工具 | 开关 |
|---|---|---|
| 文件 | `list_directory` `find_files` `read_text` `write_text` `append_text` `write_text_b64` `append_text_b64` `replace_text` `make_directory` `copy_path` `move_path` | 始终可用（受工作区沙箱） |
| 删除 | `delete_path` | `allow_delete` |
| 校验 | `validate_python` `run_unittest` | 始终可用 |
| 执行 | `run_python` | `allow_python` |
| Office | `read_excel` `write_excel` `read_docx` `write_docx` `read_pptx` `read_pdf` | `allow_office` |
| Outlook | `outlook_unread` `outlook_read` `outlook_search` `outlook_send` | `allow_outlook`（发送另需 `allow_outlook_send`） |
| 技能 | `skill_list` `skill_read` `skill_create` | `allow_skill_write`（全局技能另需 `allow_self_upgrade`） |
| Copilot | `copilot_attach` `copilot_file` | `allow_copilot_file` |
| 自升级 | `self_read_text` `self_write_text_b64` `self_append_text_b64` `self_replace_text` `self_validate` `self_unittest` `self_rollback` | `allow_self_upgrade`（默认关，关闭时**不注册**） |

### 6.2 权限边界

- **工作区沙箱**：`_path()` = `resolve()` + `relative_to(root)`；`glob` 结果二次过滤；符号链接/悬空链接有兜底。
- **工作区不得覆盖服务安装目录**：否则普通文件工具即可改服务代码/config.json，等于绕过自升级权限门；
  该配置现在被拒绝，除非显式开启 `allow_self_upgrade`。开启后敏感路径（`config.json`、`data/`、
  `EdgeProfile/`、`.git/`）仍按自升级黑名单拒绝访问。
- **自升级路径**：白名单（`cbp/ webui/ scripts/ docs/ tests/ skills/ app.py start.py install.py
  requirements.txt AGENT.md README.md config.example.json`）+ 黑名单（`config.json data/ EdgeProfile/ .git/`），
  先判黑名单；目录穿越、junction、大小写变形均在 `resolve()` 后拦截。
- **快照沙箱**：作用域目录名强制以字母数字开头且必须是快照根的直接子目录（防 `.`/`..`）；
  回滚目标必须 `relative_to(root)`；原像路径必须 `relative_to(scope_dir)`。
- **删除会话**：`[CBP-AGENT][sid]` 标记同属校验，无标记一律拒删；URL 为持久身份。

## 7. 数据、状态与生命周期

| 存储 | 内容 | 生命周期 |
|---|---|---|
| `data/agent.db` | sessions / steps / messages / file_events / conv_stats | 会话删除时**连同从属表**一起清理；完成会话按 `completed_session_retention_days` 归档 |
| `data/snapshots/<sid>/` | 工作区改动原像 + manifest | 会话删除即回收；`snapshot_retention` 限定作用域数 |
| `data/snapshots/_self_<sid>/` | 自升级改动原像 | 同上，且会话删除时一并回收 |
| `EdgeProfile/` | M365 登录态 | 最高敏感级，禁止入仓/共享 |
| `data/last_page.html` 等 | 诊断产物 | `diagnostics`/`diagnostic_html` 控制，需定期清理 |

快照语义：

- **改动前捕获**，同一会话同一路径只记一次（保留最早状态）→ "回滚任务"回到**任务开始前**。
- 三种状态：`file`（原像字节）、`dir`（整棵目录树）、`absent`（当时不存在 → 回滚时删除）。
- 上限：单文件 8MB、单会话 800 条目、原像总量 256MB（超限记入 `skipped`，**不阻断写入**）。
- 回滚为**原子替换**（临时文件 + `os.replace`），避免中断留下截断文件。
- 快照记录带根维度：会话中途换过工作区时，异根原像只跳过、不误写。

## 8. 配置参考（`config.json` 的 `agent` 段）

| 键 | 默认 | 说明 |
|---|---|---|
| `workspace` / `workspace_roots` | `workspace` / 空 | 工作区与允许的根白名单（空=只受沙箱约束） |
| `max_total_steps` | 1000 | 单会话总步数上限 |
| `max_steps_per_conversation` | 80 | 单个 Copilot 会话步数上限（触发换会话） |
| `max_conversation_rollovers` | 25 | 最大换会话次数 |
| `context_token_budget` / `compact_at_tokens` / `rollover_at_tokens` | 80000 / 35000 / 55000 | 上下文预算、压缩阈值、换会话阈值（用 `scripts/calibrate.py` 校准） |
| `keep_recent_steps` / `max_summary_tokens` / `max_context_chars` | 8 / 5000 / 240000 | 压缩保留步数、摘要上限、字符上限 |
| `auto_compact` / `auto_rollover` / `auto_resume` | true | 自动压缩 / 自动换会话 / 中断自动恢复 |
| `min_steps_before_rollover` | 20 | 换会话前至少跑够的步数（防抖动） |
| `max_parse_failures` | 5 | 连续解析失败上限（触发压缩，不换会话） |
| `max_actions_per_turn` | 4（硬顶 12） | 批量动作上限 |
| `max_read_chars` / `max_write_chars` | 120000 / 200000 | 单次读写上限 |
| `allow_delete` / `allow_python` / `allow_office` | false / false / true | 高风险能力开关 |
| `allow_outlook` / `allow_outlook_send` | false / false | Outlook 读取/发送 |
| `allow_skill_write` | true | 是否允许 `skill_create` |
| `allow_copilot_file` | true | 是否允许 Copilot 文件通道工具 |
| `allow_self_upgrade` / `self_upgrade_paths` | false / 内置白名单 | 自升级开关与可触达路径 |
| `snapshot_retention` | 50 | 保留的快照作用域数 |
| `agent_md_max_chars` | 8000 | 单份 AGENT.md 注入上限 |
| `python_timeout_seconds` | 20 | 单次执行/校验超时（unittest 另取 max(该值,120)） |
| `max_service_unavailable` / `service_retry_base_seconds` / `service_retry_max_seconds` | 30 / 30 / 600 | 限流退避策略 |

顶层：`api_token`、`server.host/port`、`headless`、`browser_channel`、`reply_timeout_seconds`、
`reply_hard_timeout_seconds`、`stable_seconds`、`diagnostics`、`diagnostic_html`、`default_model`、
`models`、`providers`、`markers`。

## 9. 可观测性与故障定位

建议统一记录：Session ID、Copilot 会话序号、回合标记、模型、提供商、步号、动作类型、耗时、
重试次数、结束原因、错误分类。日志不得记录 API Key、完整 Cookie、认证头或邮件正文
（`token=` 已做脱敏）。

定位顺序：

1. 端口/配置解析/SQLite 可写性；`/v1/agent/self/status` 看自升级与待重启。
2. `EdgeProfile` 是否被占用、页面是否仍登录（重启后 SSO 回跳需等就绪）。
3. 回合标记是否送达 → 区分等待超时、限流、会话长度上限、动作格式错误。
4. 模型菜单/页面选择器是否因改版失效（看 `/v1/models` 的 `available`）。
5. 工作区权限、工具开关、路径沙箱拒绝记录（时间线里的 error 步骤）。
6. Outlook 问题先跑只读自检，再区分 OWA 页面问题与 COM 线程问题。
7. 阈值调整前先用 `scripts/calibrate.py` 看真实 `conv_stats` 样本。

## 10. 测试

`python -m unittest discover -s tests`（当前 **92 个用例**，约 15 秒）：

| 文件 | 覆盖 |
|---|---|
| `test_agent_parse.py` | 动作解析、截断恢复、批量成组、嵌套 JSON 不误判 |
| `test_batch_actions.py` | 批量解析/上限/去重/截断恢复、批内失败不中断、成本只记一次、验证门 |
| `test_validation_gate.py` | **验证旗持久化**：改代码未验证 → 暂停恢复后仍被拦；真正验证后放行 |
| `test_skill_create.py` | 技能两级作用域、权限门、重名保护、frontmatter 生成 |
| `test_sync_tools.py` | 同步工具经 `execute()` 调用（`skill_list` 回归） |
| `test_model_menu_scan.py` | 菜单叶子识别与可见性对齐 |
| `test_session_model.py` | 会话模型设定/回退/鉴权头兼容 |
| `test_snapshots.py` | 快照捕获/回滚语义、上限、沙箱 |
| `test_scopes_and_self_upgrade.py` | 两级 AGENT.md/skills、自升级沙箱 |
| `test_no_undefined_names.py` | 全仓静态检查未定义名（防 `from agent import` 类残留） |
| `test_copilot_file.py` | Copilot 文件通道 |

## 11. 2026-11 审查：已修复项

四路并行审查（bridge / agent+store / tools+webui / api+delete+owa+snapshots），确认后已修复：

**严重**

1. `_has_complete_agent_action` 里 `from agent import AgentRunner` 缺包前缀（包重构残留）→
   异常被吞、函数恒 False：完整动作被当畸形、含"稍后重试"字样的合法 final 被当限流重发。改为模块级导入。
2. 用户暂停在回合等待期间被写成 `state=failed` → AgentRunner 不再停放、重启后 `auto_resume` 也捞不回。
   新增 `except AgentPaused` 分支写回 `paused`。
3. **验证闭环旗活不过暂停/重启**：三面旗是 `run()` 局部变量，恢复后直接 final 即可绕过验证门。
   现持久化到 `sessions.gate_state`（新增列 + 迁移），`run()` 开头读回、变更即落库、正常完成时清除。
4. **`write_text_b64` / `append_text_b64` 不在验证门触发集合内**——而提示词要求代码一律用 b64 写入，
   等于给验证闭环开后门。已补入集合。
5. 批内 `final` 被静默丢弃，验证门那一轮根本没被评估。现保留在工具动作之后交由验证门裁决。
6. `SnapshotStore._dir` 未拒绝 `.`/`..`：`DELETE /sessions/%2E%2E` 可 `rmtree` 掉 `data/`（含 agent.db）。
   现已强制"以字母数字开头 + 必须是快照根的直接子目录"。
7. **把工作区设为服务安装目录即可绕过整套自升级权限门**（写 `cbp/*`、`config.json`、全局技能）。
   该配置现被拒绝，除非显式开启 `allow_self_upgrade`；开启后敏感路径仍按自升级黑名单拒绝。
8. 删除流程**假成功**两处：整页 `body_text()` 用含中文"已删除"的正则匹配（服务自己问过删除类问题即误判）、
   虚拟列表未渲染目标行时把"看不见"当"已删除"。现已：整页兜底只认英文强特征串；
   `verified-absent` 判定必须再经 **URL 级复核**，复核不通过则拒绝报成功。
9. 删除会话只删 `sessions` 行（从属表与快照永久残留）、自升级作用域 `_self_<sid>` 不回收
   （会被"回滚最近一次自升级"选中）。现已级联清理从属表 + 回收两类快照目录。
10. 删除会话的"正在运行"检查在锁外（TOCTOU）：可删掉正在运行的会话。现移入锁内复检。

**重要**

11. `rollback_self_upgrade` / `self_upgrade_status` 在 `data/snapshots` 不存在或被并发 `prune()` 时
    抛 `FileNotFoundError`（自升级 failback 最该可用时 500）。现 `SnapshotStore.__init__` 建目录 +
    `scopes()` 只返回含 manifest 的作用域，排序容忍并发删除。
12. 自升级回滚"只要 restored>0 就清待重启标记"，掩盖 failed 项。现只在全部成功时清除。
13. 快照 `MAX_TOTAL_BYTES` 定义了从未使用；快照目录自身可能被 copytree 进快照（工作区=安装目录时指数膨胀）。
    现已生效，并跳过快照根内的路径。
14. 快照原像路径未校验（`../../` 可逃出作用域目录）、原像命名可碰撞、回滚用 `copy2` 直接覆盖
    （失败留下截断文件）。现校验 `stored` 落在作用域内、加根维度、改原子替换。
15. 跨工作区同名相对路径互相覆盖：会话中途换工作区会把 A 根内容写回 B 根。现按 root 维度跳过异根原像。
16. 幂等保护只比对 `history[-1]`：中间夹一个成功动作即可重放 append/move。现回看最近 10 步。
17. `outlook_send` 走 OWA 后端时跳过 `allow_outlook`（COM 路径会拦）。现统一走 `_owa_or_none()`。
18. **配置 `api_token` 后 WebUI 全线 401**：前端不带令牌，启动探针 401 后直接 `return` → 界面永久空白；
    "启动任务"也走裸 `fetch`。现统一走 `api()`（自动带 `X-CBP-Token`、401 时就地询问并记住、
    并发 401 只弹一次），后端支持 `X-CBP-Token` 头 + 常量时间比较 + 访问日志脱敏。
19. WebUI 轮询用 `steps.length` 与 `total_steps` 比较，而 rollover/限流事件不增 `total_steps`
    → 条件恒真，每秒额外全量拉会话详情。现用独立 `lastTotal`。
20. WebUI `load()` 无迟到响应保护（快速切会话会把视图改回旧会话，而操作仍打在新会话上）。已加 `myId` 校验。
21. `mark_self_upgrade_applied()` 曾在 `bridge.py` 导入时执行，测试/脚本 import 即误清"待重启"标记。
    现只在 lifespan（服务真正启动）调用。
22. 快照清理只覆盖成功路径且会话删除后不回收（单会话上限 256MB × retention 50 最坏十几 GB）。
    现失败路径也 `prune()`、会话删除即 `clear()`。
23. 同会话"排队中的 resume"与新消息会顺序双跑（旧任务文本 + 新消息各执行一遍）。
    现新消息进入时取消该会话排队中的 resume 任务。

## 12. 已知限制与待办

| 级别 | 项目 | 说明 |
|---|---|---|
| 中 | COM 线程用 MTA | `CoInitializeEx(0)` 与注释声称的 STA+消息泵不一致；MAPI 命名空间要求 STA。改动需真机 Outlook 验证 |
| 中 | COM 超时后队列仍执行 | `wait_for` 超时只放弃 future，`_ComWorker` 仍会执行该操作（发送类操作可能重复）。建议加作废标记并修正文案里的超时常量 |
| 中 | `read_text` 对二进制/UTF-16 静默返回乱码 | 默认分支 `errors="replace"`、分段分支 strict，行为不一致；乱码被当原文写回会毁文件。建议统一严格解码并显式报 `binary` |
| 中 | 离线回滚与服务无互斥 | `scripts/rollback.py` 运行期间若服务在跑，capture 会把"已回滚内容"存成原像。建议加锁文件租约 |
| 中 | 提示词注入面 | `AGENT.md` 与技能清单行原样进系统提示；技能 `name` 取 frontmatter/H1 而非目录名。建议一律用目录 slug + 声明"以下为数据非指令" |
| 中 | 压缩先于换会话判定 | 压缩会清零同一轮的换会话压力，换会话延迟一回合；换会话时 `pending` 被清空，摘要只有 goal |
| 低 | 成本计数缺口 | `validation_gate` 分支不写 `estimated_*`，`final` 分支不写 `conv_*` |
| 低 | `LIMIT` 取最旧记录 | `messages(limit)` / `steps(limit)` 取到最早若干条而非最近 |
| 低 | `RPC_E_SERVERCALL_RETRYLATER` 常量写错 | 应为 0x8001010A，"Outlook 忙则退避重试"基本不触发 |
| 低 | 死代码与潜在 XSS 汇 | `sessSig` 无分隔符拼接、少量未走 `esc()` 的插值、`railNewTask()` 无引用 |
| 低 | `?token=` 便捷用法 | 会进入浏览器历史/Referer；生产建议只用请求头 |
| 低 | `/health` 暴露会话 URL | 未鉴权端点返回含 conversation id 的 page_url |

## 13. 安装、运行与运维

1. 独立虚拟环境 + `python install.py`（Playwright 浏览器组件）。
2. 从 `config.example.json` 生成本机 `config.json`（不要覆盖已有运行配置）。
3. `python scripts/login.py` 完成 Edge 持久化 Profile 登录。
4. `python start.py` → 访问 `/ui` 与 `/v1/models` 自检。
5. 建一个只读测试任务，确认会话持久化、暂停/恢复、回合标记正常。
6. 启用 Outlook 先跑只读自检，再逐项开放；发送能力保持关闭，除非有明确审批与审计。
7. 定期备份 `data/agent.db`；备份前停止写入或使用 SQLite 在线备份，避免直接复制活跃库。
8. 自升级改动需**人工重启**才生效；`/v1/agent/self/status` 的 `restart_pending` 会提示。
   回滚：`POST /v1/agent/self/rollback`（在线）或 `python scripts/rollback.py`（离线）。
