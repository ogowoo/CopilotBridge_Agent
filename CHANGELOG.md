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
