# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本号规则与发布流程见 [docs/VERSIONING.md](docs/VERSIONING.md)。

## [Unreleased]

### Added
- 

### Changed
- 

### Fixed
- 

## [11.0.0-preview.23] - 2026-10-29

### Changed

- **样式也拆了：`webui/style.css`（2340 行 / 136 KB）→ `webui/css/*.css`（11 个文件）**。
  只按**原有章节边界**切段搬家，不重排、不合并选择器：11 个文件的正文按顺序拼接与原文件
  **逐字节相同**（136375 字符 / 2341 行，拆分脚本内部断言 + 独立复核），所以层叠零变化。

  | 文件 | 行 | 内容 |
  | --- | --- | --- |
  | tokens | 28 | 设计令牌（`var(--…)` 的唯一来源） |
  | shell | 267 | 侧栏 / 主区 / 任务条折叠 / KPI + 媒体查询 |
  | chat | 194 | Markdown、时间线、工具结果、diff/JSON、语法高亮 |
  | dialogs | 45 | 遮罩、确认框、删除结果、页面内通知 |
  | themes | 134 | 7 套主题令牌 + 原生控件跟随 |
  | overlays | 148 | 手册、横幅、浮层分车道、子代理面板、插件弹窗 |
  | composer | 363 | 新会话视图、工作区、斜杠、目标/待办条、插件面板 |
  | layout | 219 | 主列 flex、输入框浮动层、窄屏抽屉、会话行、步骤页高度 |
  | settings | 217 | 设置页与相关打磨 |
  | alerts | 76 | 中断提醒、限流遮罩 |
  | refinements | 650 | 累积的收尾打磨（完成卡片、侧栏页脚、toast、四条横条统一） |

- `index.html` 改为 11 个并列 `<link>`（**顺序 = 层叠顺序**），不用 CSS 的 import 指令
  （会串行阻塞首屏）；`/ui/{asset}` 白名单去掉 `style.css`，正则改为
  `(?:js|css)/<名字>.(?:js|css)`。

### Added

- `gantry/ui_src.py` 新增 `ui_css()` / `css_rule()`：测试统一从这里取样式
  （18 个原本直接读 `style.css` 的测试已切换，避免"改在 A 文件、断言在 B 文件"的假覆盖）。
- `tests/test_css_split.py`（8 条）：老文件已删除、11 个文件齐全且顺序与 `CSS_ORDER` 一致、
  `index.html` 全量引用、无 import 指令、每个文件带自己的头注释、令牌/主题/媒体查询仍在。
- `docs/CSS-SPLIT.md`：文件映射表 + 三条规则（顺序即层叠 / 不许 import / 新规则放对文件）。
- 不变量 #15 扩展：认识 `css/` 目录，并**反向**检查"css/ 下有页面未引用的死文件"。

### Fixed

- 使用手册与 skill 里的自助示例还写着 `webui/style.css`（文件已不存在）→ 改为
  `webui/css/tokens.css` 的 `.app`（侧栏宽度 318px 就在那儿）。

### 实测

- 单测 **1234**（skipped=1）OK；不变量 25 条全绿；
  `/ui/css/*.css` 11 个全部 200（2.3–42.3 KB），`/ui/style.css` 404（预期）。
- 浏览器：无 JS 异常、会话可打开；`ui_layout_audit --stress`（7 视口）无溢出/重叠/裁剪，
  `ui_contrast_audit` 全部达标，`ui_i18n_audit` 三语言 446 条无泄漏。

## [11.0.0-preview.22] - 2026-10-28

### Changed

- **app.js 拆完退役（ES Modules 迁移收尾）**：`webui/app.js`（原 5139 行单体）删除，
  业务逻辑全部在 `webui/js/*.js`（30 个 ES 模块），`js/main.js` 是唯一入口。
  `index.html` 现在只加载 `js/globals.js`（经典脚本，共享状态）+ `i18n.js` + 模块入口。
- **新增 `webui/js/globals.js`（跨模块共享状态）**：ES 模块顶层的 `var/let` 是**模块私有**，
  别的模块裸名访问会 ReferenceError（严格模式），而各处 `try/catch` 一吞，界面只表现为
  "空白/不刷新"。经典脚本顶层的 `var` 既挂 window 又在全局词法环境 → 模块可裸名读写，
  与拆分前语义一致。共收口 43 个状态 + 4 个被 `window.X` 误当全局用的私有变量。
- **常量/函数改走真正的模块边界**：分享常量（THEMES/SLASH_COMMANDS/MANUAL_HTML/
  SETTINGS_GROUPS/PHASE_LABEL/HL_LABEL…）改为 owner 模块 `export` + 消费方 `import`；
  函数仍 `export` 并在 `js/main.js` 绑 `window`（老调用点一行不改）。
- **`/ui/{asset}` 白名单**去掉 `app.js`（`js/<名字>.js` 正则已覆盖所有模块与 globals.js）。

### Added

- **跨模块共享检查进不变量（第 25 条）**：`scripts/cross_module_refs.py` 静态分析
  ① 共享状态是否只在 globals.js、② 跨模块函数是否 export+window 绑定、
  ③ 同名函数是否被两个模块各声明一份、④ `window.X` 是否指向模块私有变量；
  发现问题既是独立脚本（退出码 1）也直接进 `invariants.py`。
- 浏览器探针 `scripts/smoke_interact.py`：真的点开一个会话，断言时间线/工具记录/
  状态栏有内容且无 console 错误（只查 window 绑定查不出"列表空白"这类问题）。

### Fixed

- **会话列表整片空白**（迁移中途的真实回归）：`drawSessions` 抛 `ReferenceError`
  被 `refreshSessions` 的 `try/catch` 吞掉 —— `expandedSess/showSubagents/subagentCount`
  等状态在模块里各有一份私有声明。收口到 globals.js 后恢复（实测 21 个会话正常渲染）。
- **任务条点击空白处收起失效**（老 bug，迁移时暴露）：`header.topbar>.trow` 的 click
  处理器被绑了两次（原 app.js 里就有两份），切两次等于没切。删掉重复绑定。
- **视图缓存/权限档位两份状态**：`renderedSid/lastSessionObj/skipDetailOnce`（steps.js 私有，
  followload.js 却写 `window.*`）与 `sandboxMode`（themeboot.js 私有，settings.js 走
  `window.sandboxMode`）→ 收口到 globals.js，切回旧会话不再命中错缓存。
- `gantry/ui_refs.py` 改为扫描**全部**前端 JS（原只看 app.js）；`invariants.py` 里
  读 app.js 的四处（ACTIONS 表、手册中英对照、tr() 调用统计、子代理 parent 检查）
  同步改为读模块目录。
- 测试：`tests/test_esm_migration.py` 重写为**终态**回归（app.js 必须不存在、
  globals 必须在 i18n 之前、共享状态不许在模块里再声明、导出必须绑 window）；
  其余 30 个读 app.js 的测试改用 `gantry/ui_src.py` 的 `ui_js()/fn_body()`
  （按原 app.js 顺序拼接，避免"按函数名切段"切空）。

### 实测

- 单测 1226（skipped=1）OK；不变量 25 条全绿；`ui_smoke.py` /
  `ui_i18n_audit.py`（3 语言 449 条）/ `ui_layout_audit.py --stress`（7 视口）全部通过。
- 浏览器真交互：点开会话 `task-mvs4un2x` → 39 步、4 条消息、调用记录 62 行、状态条
  `completed · 步 39 · Token 19073`，无 console 错误（截图 `data/ui-audit/esm-final.png`）。

## [11.0.0-preview.21] - 2026-10-28

### Added

- **app.js 拆成 ES 模块（第 1 步）**（用户选了真模块化）：脚手架 webui/js/ —
  state.js（共享可变状态的家）、main.js（入口，把拆出去的全局函数绑到 window）、
  第一块 **scope.js**（示波器浮层，约 131 行、零跨块依赖，整体搬出）。
  - 增量策略：拆出去的块把老代码仍在调用的函数绑到 window，事件路径的调用点不用动；
  - 路由 /ui/{asset:path} 放行 js/<名字>.js（带穿越防护）；
  - 顺手修了两个积压：oot.js（localStorage 迁移）**从没被加载**（死代码）→ 现在
    index.html 里第一个加载；白名单不变量适配新路由形态（精确名单 + js/*.js 正则）。
  - 实测：浏览器里 startScope/stopScope/scopeFrame 都是 function、无 JS 错误、
    boot.js 迁移真的跑了；单测 1226（skipped=1）OK。

## [11.0.0-preview.20] - 2026-10-28

### Added

- **前端 id 引用完整性检查**（用户："加上"）：`gantry/ui_refs.py` —— 扫
  `webui/index.html` 与 `webui/app.js`，JS 里 `getElementById/querySelector` 引用的
  元素 id 必须在 index.html 的 `id=…` 或 JS 动态生成（innerHTML 模板/`el.id=…`）里真实存在。
  - 进了 `self_validate`（自升级验收硬门之一，结果带 `ui_refs` 字段）；
  - 也进了不变量（`invariants.py`，现在 24 条）；
  - 白名单 `ALLOWED_MISSING` 只有 `sid`（旧字段的防御性兜底），不许静默膨胀。
  专门拦"改了 JS 逻辑却忘了加元素"这类假完成（task-mvs4un2x 实测的坑）。

### Added（测试）

- `tests/test_ui_id_refs.py`（7 条）：缺漏检出、动态生成 id 认可、行号回报、
  现网零缺漏、白名单不膨胀、`self_validate` 包含本检查。

## [11.0.0-preview.19] - 2026-10-28

### Added

- **心跳/示波器动画的设置开关**（用户用自升级命令做的功能，收尾补全）：设置 → 外观
  → "任务进行时的心跳动画（卡片上沿的示波器）"，默认开；关掉后 `startScope` 立即返回，
  不再产生动画帧（实测关后 rAF=false、开后 rAF=true）。

### Fixed

- **自升级任务"完成得太早"**：自升级代理（task-mvs4un2x）加了 `startScope` 的开关判断
  和 `saveSettings` 的持久化，却**忘了把复选框元素加进设置页**就宣布完成
  （它的验证只有 py_compile + unittest，覆盖不了"元素存在"）。我补上了外观区的复选框
  与 i18n，现在设置页真能看见了。

## [11.0.0-preview.18] - 2026-10-28

### Fixed

- **自升级回滚列不出快照**（用户：`python scripts/rollback.py --list --scope self`
  只得到"没有可回滚的服务自身代码快照记录"）。两个原因：
  1. **从来没写过**：agent.db 里 `self_replace_text` / `self_write_text_b64` 的历史
     调用是 **0 次**（只有 self_read_text / self_search_text），没有写操作自然没有原像；
  2. **真 bug**：`snapshots._dir()` 为防 `.`/`..` 把"非字母数字开头"的名字加了 `s`
     前缀，连带把自升级作用域 `_self_<sid>` 改成 **`s_self_<sid>`** —— 而
     `rollback.py --scope self` 与 `rollback_self_upgrade(use_last)` 都按 `_self_`
     前缀找 → 永远匹配不到（只有 `--sid` 能恢复，因为它走 `_dir` 同一条路）。
     修：`_dir()` 只拦危险开头（`.`/空），`_` 开头原样保留；前缀匹配处**两套都认**
     （`_self_` 与旧 `s_self_`）；CLI 的空提示补一句"只读排查不会留快照"。

- **单测卡死（真机实测）**：上游"修重启"把 `exec_restart` 从 `os.execv` 改成
  `Popen + os._exit(0)`，但 `tests/test_restart_service.py` 仍按旧契约（patch execv、
  期待 SystemExit）写 → 测试里**真的 spawn 了** `python start.py -s -v`，
  父进程 `os._exit(0)` 立刻退出、spawn 的服务还活着 → 外部看起来"跑不完/卡住"。
  修：测试改成新契约（patch Popen + os._exit）；`_entry_argv()` 不再透传任何参数
  （start.py 不解析 argv，透传只会把 `-s -v`/`discover`/`tests` 带进去）。

### Verified

受控实验（只在 `docs/` 下建临时文件，不碰真实代码）：
`self_replace_text` → 快照目录 `_self_probe-self-snap2` → CLI `--list --scope self`
列出 → CLI `--sid … --yes` 回滚成功（还原 1、失败 0）→ 文件回到原内容。

### Added（测试）

- `tests/test_self_snapshot_scope.py`（5 条）：`_self_` 不加前缀、`scopes("_self_")`
  能列出、`.`/`..` 仍被挡、旧 `s_self_` 仍被认、CLI 两套前缀。

## [11.0.0-preview.17] - 2026-10-27

### Added

- **发送按钮运行中变"强行停止"**（仿 DSH）：任务在跑时发送键变成红色方块，点一下
  走 `actions/cancel` 停掉当前任务；停止态/发送态由 runtime.busy 实时切换。
- **聊天框 `@` 唤起工作区文件选择**（DSH 的 @ 体验）：输入 `@` 弹出文件列表
  （`/v1/agent/workspace/files?q=`，工作区守卫、跳过 .git/__pycache__/data），
  ↑↓/Enter 选择插入 `@路径`；发消息时后端把 `@路径` 展开成文件内容随消息一起发
  （`gantry/atrefs.py`：围栏内不展开、越界不认、单文件 ≤20k、合计 ≤60k、最多 4 个），
  新任务和跟进消息都支持。

## [11.0.0-preview.16] - 2026-10-27

### Fixed

- **长期记忆"消失了"**（用户实测好久没更新）。根因不是坏了，是**模型从不调
  `memory_write`**：全库历史步骤里它只被调用过 1 次（其余记忆是评测基线/手动塞的）。
  提示词那句"任务收尾判断要不要记"是软提示，模型忙起来永远忽略。

### Added

- **记忆提醒（memory_nudge）**：与 noop_guard 同一模式——任务**实质产出 ≥3 个动作**
  且本任务一条 `memory_write` 都没记过时，第一次返回 final 拦一次，提醒
  "若有跨任务价值的事实请记一条；没有就明说没有"；**只拦一次**（fail-open），
  之后放行。写 1~2 个文件的小任务不打扰（阈值 ≥3，实测批量写两文件不被拦）。
- 设置 → 执行 → "收尾提醒写记忆" 开关（默认开，`agent.memory_nudge`）。

### Added（测试）

- `tests/test_memory_nudge.py`（4 条）：拦截块存在、fail-open 标记、设置页开关、
  产出工具清单。

## [11.0.0-preview.15] - 2026-10-27

### Added

- **Telegram 插件**（用户想要"手机端操作 Gantry"，类似 Hermes 的 bot 交互）：
  `plugins/telegram/`（manifest + plugin.py + README + 15 条测试）。
  - 长轮询 `getUpdates`，不需要公网地址（@BotFather 建 bot 拿 token 即可）；
  - **白名单 chat_id 才受理**（bot 是公开的，必须限制）；token/白名单从 `.env`
    （`TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_CHAT_IDS`）或插件配置读；
  - 一个 chat 映射一个会话 sid（`/new` 强制开新），普通文字继续当前会话（agent 跑），
    完成把回答整段发回，进行中按节流发进度；
  - 命令：`/new /status /sessions /todo /cancel /pause /resume /model [id] /answer /help`；
  - `GET /v1/plugins/telegram/api/status` 面板看健康状态；
  - 无 token 时插件待命不报错。

## [11.0.0-preview.14] - 2026-10-27

### Changed

- **代码明文单次写入上限 1500 → 6000**（用户："1500 是不是太少了"）。
  1500 是"防 Copilot 网页输出截断"的保守值；实测它的回答能装下远不止 1500，
  真正踩坑的是 10KB+ 的大块。6000 仍在安全区，小文件不再被逼着走 Base64 分块。
  - 上限仍可在 config.json 里改（`plain_code_limit`），现在也进了设置页
    （设置 → 执行 → 代码明文单次写入上限）；
  - `AGENT_PROTOCOL`（给模型看的规则）里的数字同步；
  - 超限错误信息不变（仍指路到 `write_text_b64`/`append_text_b64` 分块）。
- 测试里的"超限用例"载荷从 2400/3000/2000 字符调到 8400/7000/7000
  （>6000 才真超限），断言数字同步。

## [11.0.0-preview.13] - 2026-10-27

### Added

- **`.env` 密钥文件**（用户提议）：密钥放 `.env`（已入 `.gitignore`），
  `config.json` 里用 `"${VAR}"` 引用。
  - 读取优先级：**系统环境变量 > .env > config.json 字面量**（用户明确要求：
    系统已有的环境变量**不会**被 .env 覆盖——`load_env_file` 只补缺）；
  - 支持 `#` 注释、`export` 前缀、单/双引号、行内注释；
  - 引用了但没设置的 `${VAR}` 保留原样并记 warning（不静默置空，避免把 token 抹成空串）；
  - config.json 里仍有明文敏感字段（key/token/secret/password）时启动日志提醒。
  - 模板：`.env.example`；`config.example.json` 的 api_token 示例改为 `${GANTRY_TOKEN}`。
- **config.json 从 git 脱钩**（`git rm --cached`）：它历史上被跟踪过，这是密钥泄漏的
  唯一通道（`.gitignore` 忽略了它但已在跟踪里照样入库）。文件保留在盘上，只是不再入库。

### Added（测试）

- `tests/test_envfile.py`（6 条）：系统优先、注释/引号/export 解析、占位符解析与
  未设置保留、明文敏感字段点名、加载顺序（.env 先于 config.json）。

## [11.0.0-preview.12] - 2026-10-27

### Fixed

- **危险区开关拨了没用**（全链路实测）：`allow_self_upgrade` 历史位置是 config.json 的
  **agent 段**（`tools.py` 从 `AGENT_CFG` 读、提示文案也写 `agent.allow_self_upgrade`），
  而 schema 键被我误写成顶层 → UI 写顶层、注册表读 agent 段 → 开关永远不生效。
  修法：schema 键改为 `agent.allow_self_upgrade`（danger 组），live-apply 写回 `AGENT_TOOLS`；
  撤掉此前加的"顶层并入 agent 段"（它把用户配置漏进了测试环境，炸了 13 个测试）。
- **`self_find_files`/`self_search_text` 调不动**：只注册了描述，没进分发表
  （`self._tools`）和工具分组（`TOOL_GROUPS["self"]`）→ 模型一调就报错，误判成
  "没提供自升级工具"，退回工作区乱翻。三处（描述/分组/分发）现在由测试钉住一致性。
- **自升级任务跑去工作区**（用户实测"你去到我的 wgime 项目里去了"）：
  协议默认"路径相对于当前工作目录"，模型据此把工作区当目标。现在任务文本含
  `[自升级任务]`/`自升级` 时额外注入一段强提示：目标代码在安装目录、不要碰工作区、
  搜不到就停下来问。英文模式有译本（`prompt_lang.EN.self_upgrade_lead`）。
- **英文模式下自升级段仍是中文**：`_self_upgrade_rules` 没走 `localize`，
  实测把英文模式的中文占比顶到 6.00% 压线。补了 `prompt_lang.EN["self_upgrade"]` 译本。
- **self_* 工具缺英文描述**：开关开着后它们进了工具清单，`tool_i18n` 没条目
  → 英文界面露中文工具说明。补齐 9 条描述 + 16 条参数。

### Verified

- 模糊任务实测（不提文件名）：`self_search_text` 直接命中 `gantry/agent.py`，
  随后 `self_read_text` 读源码，**零工作区工具**、零乱猜路径，13 秒完成。
  回答正确定位到限流处理分支、`retry_resume_at`、suspended 状态。

### Added（测试）

- `tests/test_self_upgrade_wiring.py`（3 条）：键必须在 agent 段、只有一个 agent.* 分支、
  开关开着时 self_* 工具必须可调用。
- `test_self_search.py` 新增注册一致性（描述/分组/分发表三处必须齐全）。

## [11.0.0-preview.11] - 2026-10-27

### Fixed

- **改名后自升级"按文件夹名猜路径"**：模型去找 `gantry-main` 这类不存在的目录
  （用户实测）。`global_root` 本就由 `__file__` 反推、与文件夹名无关，但提示词没告诉
  模型——现在自升级规则里**注入真实安装目录**（"文件夹名可能还是旧的，不要按名字猜路径"），
  `/self-upgrade` 模板加"不记得文件名就先 self_search_text 定位、报给你确认后再改"。

## [11.0.0-preview.10] - 2026-10-27

### Changed

- **四行展开高度调大**（待办/子代理/调用记录/状态）：级联底部一路压小的旧规则
  （最小到 11vh/132px）由末尾的 ROWS-TALLER 覆写块统一抬到 min(38vh, 400px)、
  调用记录 min(38vh, 420px)，卡片总高 min(74vh, 860px)。
  950px 视口实测：三行各 361px、调用记录 361px、卡片 703px；布局审计无溢出。

## [11.0.0-preview.9] - 2026-10-27

### Added
- 

### Changed
- 

### Fixed
-

## [11.0.0-preview.8] - 2026-10-27

用户："我不记得具体的文件和路径，模糊的说法（让 Agent 自升级）不成功吗？"

诚实答案：之前**不行**——自升级工具只有 read/write/replace/validate/unittest/rollback，
**没有搜索/列举**，agent 拿到模糊描述连"代码在哪个文件"都不知道。本版补齐。

### Added

- **`self_find_files(pattern)`**：glob 列举服务目录（如 `gantry/*.py`）——"先定位"第一步；
- **`self_search_text(query, path, max_results)`**：在服务目录里搜文本/正则，返回
  file:line + 内容——"那个按钮/那个报错"先搜出来再改；
- `/self-upgrade` 模板加"**不记得文件名就先调 self_search_text 搜**，把定位报给你确认后再改"。

### Fixed

- **白名单在 `allow_self_upgrade=False` 时构造为空**（`tools.py:265`）：危险区开关即时生效，
  但运行时把开关打开会遇到"允许了却什么文件都不让碰"。现在白名单**始终构造**，
  与开关解耦（`_self_path` 本就先查开关）。

### Added（测试）

- `tests/test_self_search.py`（5 条）：搜索/列举在开关打开时可用、关闭时被拒、
  黑名单（config.json）搜不到、新工具已注册进工具列表。

## [11.0.0-preview.7] - 2026-10-27

### Added

- **设置 → 危险区：「允许 Agent 修改服务自身代码（自升级）」开关**（用户要求）。
  原有 `allow_self_upgrade` 只能手改 config.json；现在进了设置页（默认关），
  且**即时生效**（`/v1/config` 保存时直接写回 `AGENT_TOOLS`，不用重启）。
- **斜杠命令 `/self-upgrade`**（别名：自升级/改服务）：一键把中文自升级任务模板
  填进输入框并切到 Agent 模式（手册里原有的英文模板做成了"方便直接填"的命令）。
- **维护端点 `POST /v1/agent/copilot/cleanup-marked`**：本地记录删了之后，也能按
  会话标记清理远端 Copilot 会话（`scan_only` 先列后删；`delete_matching` 逐行删）。
  侧栏扫描支持**滚动**（虚拟列表只渲染可视行，不滚会漏——实测删一轮还剩残留）。

### Fixed

- **浏览器启动失败（`ERR_ABORTED`，页面停在 about:blank）**：首次启动的
  `page.goto(..., wait_until="domcontentloaded")` 没有容错——企业租户的 SSO 会在
  domcontentloaded 之前就重定向走，Playwright 报 `ERR_ABORTED`。复用窗口/新窗口两处
  goto 都有兜底，唯独首次启动漏了。现在 ERR_ABORTED 视为正常（交给就绪检测接手）。
  `tests/test_browser_boot_aborted.py`（2 条）钉住三处 goto 都必须容错。

## [11.0.0-preview.6] - 2026-10-26

处理 GitHub issue #2 / #3 / #4：

### Added

- **issue #2 `power-automate` 技能**：新目录 `skills/power-automate/`
  （`SKILL.md` + `templates/flow-definition.json` + `templates/connections.json`）。
  教 Agent 生成 Power Automate 工程：同租户 automate 环境（make.powerautomate.com）、
  `definition.json` 结构（triggers/actions/parameters/runAfter 状态机）、
  同租户内置连接器引用（Office 365 Outlook / SharePoint / Teams / Dataverse）、
  交付目录 `flows/<flow-name>/`，并要求生成后跑 `json.load` 校验。
  明确禁止真实 API 调用（没有凭据）——技能只负责"产出正确工程"。
- **issue #3 README 三语版**：默认 `README.md` 改为**英文**；新增 `README.zh-CN.md`
  （简体）与 `README.zh-TW.md`（繁体，台湾惯用字），每份顶部有语言切换行。
  翻译由 4 个子代理分段完成，规则：代码块/命令/API 路由/config 键名/路径/URL/表格
  结构原样保留，只翻译散文与表格单元格。

### Verified

- **issue #4 Copilot 多任务并发检验**：临时把 `agent.copilot_windows` 开到 2，
  并发发两个任务（"只回一个词：ALPHA/BETA"）：两条都成功、耗时几乎相同
  （138.0s vs 138.8s，串行的话后者应 ~2×）、回答与各自提示词一致、`windows=2`，
  收工后把窗口数收回 1 并清理诊断会话。结论：**并发正常**。

### Added（测试）

- `tests/test_readme_langs.py`（5 条）：三份 README 存在、互指、标题统一、围栏成对、
  默认版前 30 行全英文。
- `tests/test_skill_power_automate.py`（4 条）：技能可发现/可读、模板 JSON 可解析、
  SKILL.md 覆盖同租户环境与交付目录。

## [11.0.0-preview.5] - 2026-10-26

### Fixed

- **「重启服务」因路径问题失败**（用户报障）。三个坑：
  1. **入口解析用 `sys.argv[0]`**：用 `python -m gantry.cli serve` 或 `uvicorn app:app`
     启动时，`sys.argv[0]` 是**包内的 `gantry/cli.py`**；`os.execv` 把它当脚本重跑 →
     `from .brand import env` 立刻 `ImportError: attempted relative import with no known
     parent package`，重启后的进程直接死掉，WebUI 等 90 秒报"未恢复"。
     现在入口**固定**为项目根的 `start.py`（由包位置反推绝对路径），只在没有 `start.py`
     （装成纯包）时退回 `-m gantry.cli serve`；`serve` 这类子命令不再被当参数带过去。
  2. **cwd 依赖**：`exec` 不改变工作目录，而 `start.py` 的 `uvicorn.Config("app:app")`、
     `config.json`、`data/` 都相对 cwd。现在 `exec_restart()` 先 `os.chdir(项目根)`，
     派生兜底也用 `cwd=项目根`；`start.py` 还额外把项目根插入 `sys.path`。
  3. **`gantry serve --port N` 静默无效**：只写了环境变量，`start.py` 从不读它。
     现在端口优先级 = `GANTRY_PORT`（兼容 `CBP_PORT`）> `config.json` > 8765。

### Added

- `tests/test_restart_paths.py`（8 条）：入口必须是项目根的绝对 `start.py`、绝不能是包内
  `.py`、模拟 `-m gantry.cli serve` 启动、无 `start.py` 时退回模块启动、
  `exec_restart()` 必须 `chdir`、端口必须优先取环境变量。

实测（临时端口 8799，不动在跑的 8765）：`POST /v1/restart` → 200 → 90 秒内
`/health` 恢复、版本一致；`gantry serve --port 8798` 确认绑定 8798。

## [11.0.0-preview.4] - 2026-10-26

### Fixed

- **issue #1 `Locator.press: Timeout 30000ms exceeded`**（用户中午提的 issue）。
  日志显示 locator 已解析到 Lexical 编辑器
  `<span data-lexical-editor="true" id="m365-chat-editor-target-element">`，
  但 `elementHandle.press("Enter")` 等满 30 秒超时。

  根因：`send()` 在 `fill()` 之后继续用**同一个句柄**按键，而 Copilot 的 Lexical
  编辑器在输入后会重渲染，旧 `<span>` 脱离 DOM —— Playwright 的可操作性检查
  永远等不到元素，30 秒后抛错（表现为任务卡半分钟后失败）。

  修法（三级兜底，且每级都缩短超时，失败要快而不是干等 30s）：
  1. 当前句柄 `press(keys, timeout=6s)`；
  2. 失败 → 重新 `input_box()` 取新句柄再按；
  3. 再失败 → `page.keyboard.press(keys)`（走当前焦点元素，不依赖旧句柄）。

  `Control+A` / `Enter` / `Control+Enter` 全部改走 `_press_box()`；提示词写入改走
  `_type_prompt()`（`fill` → 重新取句柄逐字 → `keyboard.type`）。

### Added

- `tests/test_press_fallback.py`（5 条）：句柄失效时必须在**新句柄**或
  `page.keyboard` 上重试，且 `send()` 不得再用可能失效的句柄直接 `press`。

## [11.0.0-preview.3] - 2026-10-26

审核遗留项收尾（用户确认"都改完"并给 token 加了 `workflow` scope）：

### Changed

- **安装名也改成 Gantry**：`pyproject.toml` 的 `name` 从 `copilotbridge-agent` 改为
  `gantry`（`import gantry` 与 CLI 入口 `gantry` 本来就已经是 gantry）；
  补 `[project.urls]`（Homepage / Repository / Changelog）。
  迁移：`pip uninstall copilotbridge-agent` → `pip install -e .`（或 `pip install gantry`）。
- **CI 发布产物改名**：`.github/workflows/release.yml` 的暂存目录、压缩包、artifact、
  Release 标题全部 `CopilotBridge_*` → `Gantry-*`。
- **发布前加 fail-closed 校验**：`tag == gantry/version.py 的 VERSION`，且
  `CHANGELOG.md` 必须有同名小节（`docs/VERSIONING.md §6` 要求 1:1）。
  位置在"解析版本"之后、构建之前；用 `pwsh` 写（整个 job 是 `windows-latest`，
  最初的 bash 写法在 Windows runner 上会直接失败——这是审核指出的另一处）。

### Fixed

- `release.yml` 的校验 step 最初被追加到文件末尾（构建之后）且语法是 bash，已改正。

## [11.0.0-preview.2] - 2026-10-26

改名收尾（多 agent 交叉审核发现的问题，逐条修）：

### Fixed

- **搜索高亮丢样式**（真实回归）：CSS 只定义 `mark.cbp-hit`，而 app.js 生成
  `class="gantry-hit"` —— 改名时只扫了 `.py/.js/.html/.md`，漏了 `.css`。
  现在类名/动画名统一为 `gantry-*`。
- **旧 Copilot 会话删不掉**：删除时用新标记 `[GANTRY-AGENT][sid]` 做归属校验，
  改名前的旧会话只有 `[CBP-AGENT][sid]` → 被判"不属于本会话"，必现误拒。
  现在 `brand.marker_prefixes()` 同时给出两套前缀，归属校验与对账终验都认两套。
- **旧请求头被覆盖**：`routes_sessions` / `routes_core` 只读新头 `X-Gantry-Lang`，
  旧客户端带 `X-CBP-Lang` 时会被重新设成 zh；改用 `a._lang_of()`（两套都认）。
- **`GANTRY_*` 环境变量此前是死代码**：`brand.env()` 无人调用。现已接入
  `cli.py`（URL/TOKEN/PORT）、`agentrun.py`（EVENT_SOURCE）、插件与脚本；
  `GANTRY_*` 优先、`CBP_*` 回落。`gantry serve --port` 现在同时写两个变量名。
- **升级后丢 token / 语言 / 主题**：前端只读 `gantry-*`，没有旧键回落。
  `boot.js` 增加一次性迁移（`cbp-*` → `gantry-*`，带 `gantry-migrated` 标记），
  `apiToken()` 也回落 `cbp-token`。
- **英文界面仍有中文**：19 个 `tr()` key 没有英文条目（`版本`/`调用`/`回答`/`已复制`…），
  `等待模型回答…` 完全没走 `tr()`；已补齐。
- **繁体漏字**：S2T 表缺 `实→實`、`东→東`（"实時流"、"不能改任何东西"），已补。
- 界面残留旧名：页签标题兜底、导出 JSON 的 `app` 字段、`--help` 描述、包 docstring、
  自升级提示词、出站 `User-Agent`、首页徽标（`CB` → `G`）。

### Changed

- 启动日志现在带品牌与版本：`Gantry v11.0.0-preview.2 已启动（…）`。
- 文档/CI 元数据：README 版本行与克隆地址、`docs/TECHNICAL.md` 标题与标记名、
  `PROVIDER-deepseek-web.md` 项目名、`MULTI-WINDOW.md` 过期版本头、
  `release.yml` 产物/artifact/Release 名称；`requires-python` 与 CI 统一到 3.11。
- `bump_version.py` 现在也能同步 README 里 `**x.y.z**` 形态的版本行
  （此前只匹配全角括号，README 从没被同步过）。

### Added

- `tests/test_rename.py`（14 条）钉住上述回归：类名一致性、旧标记双认、
  `brand.env` 优先/回落、localStorage 迁移、首页徽标、英文覆盖率、启动横幅。

## [11.0.0-preview.1] - 2026-10-26

### Added

- **项目更名为 Gantry**（原 CopilotBridge Agent）：包名 `cbp/` → `gantry/`，
  界面/文档/启动日志统一品牌；`GET /` 返回 `name=Gantry`。
- 兼容层 `gantry/brand.py`：新名字优先、旧名字仍可用——
  - Copilot 会话标记新写 `[GANTRY-AGENT][sid][Cnnn]` / `[GANTRY-TURN:…]`，
    **读**时同时认旧的 `[CBP-AGENT]` / `[CBP-TURN]`（否则旧会话删不掉、对不上账）；
  - 环境变量 `GANTRY_*` 优先，回落 `CBP_*`；
  - 请求头接受 `X-Gantry-Lang/Token`，同时兼容 `X-CBP-Lang/Token`；
  - 前端 localStorage 用 `gantry-*`，读不到时回落旧键 `cbp-*`。

### Changed

- **BREAKING**：Python 包名从 `cbp` 改为 `gantry`。外部脚本若 `import cbp`、
  或写死了 `cbp/api.py` 之类的路径，需要改成 `gantry`。
  由环境变量/请求头/标记驱动的调用不受影响（上面的兼容层）。
- `pyproject.toml` 的包名仍为 `copilotbridge-agent`（安装名），CLI 入口 `gantry`。

## [10.3.0-preview.3] - 2026-10-26

### Added

- **文件直读 + 文件卡片**：说「给我看下 docs/x.md」时由本地读出并渲染成卡片
  （文本带行号可折叠、图片直显、二进制给下载），支持简中/繁中/英文动词，
  也支持「原文 / 这个文件 / 刚才那个」指代上次展示过的文件。
- **回合流**：调用记录按回合展示「发送 → 回答 → 工具调用」，提示词正文落库
  （`turn_results.prompt`），新增 `GET /v1/agent/sessions/{sid}/turns`。
- **版本管理**：版本号单一来源 `gantry/version.py`、`scripts/bump_version.py`、
  本 CHANGELOG、`docs/VERSIONING.md`、`.gitattributes`（统一 LF）。

### Changed

- 待办 / 子代理 / 调用记录 / 状态四行合并进同一个 `#teamCard`；去掉分隔线，
  任务进行中改由**示波器浮层**（canvas、鼠标穿透、不占布局、跟随主题色）呈现。
- 调用记录改为行内展开（原浮层弹窗），默认只画最近 20 回合，可「显示更早的」。
- 会话记忆：Agent 提示词附带本会话最近 6 条对话。
- 对话区保底高度；`#teamCard` 不再强制最小高度（消除收起时的空白）。
- 模型下拉可用性改为**并集**：菜单扫描只做"新增可用"，不再把没扫到的模型判成不存在。

### Fixed

- **跟进消息从未进入提示词**：`pending.append(task)` 曾被误缩进到 `except` 里，
  导致追加指令不生效、模型一直在旧任务上打转（会话 task-mvn6jjrt 实录）。
- **把上一次的最终答复当成新回答**：去掉 `generation_seen` 后门，并用
  `[CBP-TURN:…]` 标记切分，只取本回合文本。
- **`gantry.bridge` 缺 `_focus_box`** 导致任务起不来；输入框聚焦加三级兜底
  （短超时点击 → 关浮层再点 → `el.focus()`）。
- **「重试远程并强制清理本地记录」按钮无反应**：本地已删后 `current` 为空，
  按钮静默返回；现在记住 `_sid/_urls`，并支持本地不存在时仅重试远端删除。
- `noop_guard` 改为按**最新一条指令**判断，纯分析请求不再被逼着产出文件。
- 状态条 `failed` 时不再显示上一句"已完成"；空回答不再渲染「回答（空）」。
- 英文模式下的状态详情不再露中文（前端 `DETAIL_EN` 覆盖 28 条后端模板）。
- 清理诊断遗留消息、修正会话 `last_view_file` 字段的存储白名单。

## [10.3.0-preview.2] - 2025-10-24

### Added

- 多窗口并行：`agent.copilot_windows` 1~4，`WindowPool` / `_PageProxy` / 窗口池复用。
- 提供商模型接入（DeepSeek / Kimi / Grok / OpenAI 兼容），`plugins/deepseek-web`。
- 托盘/自动化浏览器稳定性改进（新窗口就绪等待、SSO 未完成不误判）。

### Changed

- 底部四条横条统一为 `.urow` 行样式（36px 等高、图标 + 标签 + 计数 + 折叠箭头）。
- 设置面板按执行/模型/界面重排；i18n 覆盖扩展到所有设置页。

## [10.2.1] - 2025-10-20

- 归档版本：tag `v10.2.1-archive`、分支 `archive/10.2.1-stable`。
  多窗口合并前的最后一条稳定线。
