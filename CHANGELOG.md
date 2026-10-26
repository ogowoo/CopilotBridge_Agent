# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本号规则与发布流程见 [docs/VERSIONING.md](docs/VERSIONING.md)。

## [Unreleased]

### Added

### Changed

### Fixed

## [10.3.0-preview.3] - 2026-10-26

### Added

- **文件直读 + 文件卡片**：说「给我看下 docs/x.md」时由本地读出并渲染成卡片
  （文本带行号可折叠、图片直显、二进制给下载），支持简中/繁中/英文动词，
  也支持「原文 / 这个文件 / 刚才那个」指代上次展示过的文件。
- **回合流**：调用记录按回合展示「发送 → 回答 → 工具调用」，提示词正文落库
  （`turn_results.prompt`），新增 `GET /v1/agent/sessions/{sid}/turns`。
- **版本管理**：版本号单一来源 `cbp/version.py`、`scripts/bump_version.py`、
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
- **`cbp.bridge` 缺 `_focus_box`** 导致任务起不来；输入框聚焦加三级兜底
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
