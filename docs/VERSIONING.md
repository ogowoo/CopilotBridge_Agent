# 版本规则与协作流程

## 1. 版本号格式

```
MAJOR.MINOR.PATCH[-channel.N]        # SemVer 2.0 子集
例：10.3.0-preview.3 / 10.3.1-preview.1 / 10.4.0 / 11.0.0-beta.1
```

| 位 | 什么时候 +1 | 例子 |
|---|---|---|
| **MAJOR** | 不兼容变更：HTTP 接口/OpenAI 兼容字段、工具契约（名称/参数/返回）、数据库表结构、插件 API、配置键语义 | 工具参数改名、`turn_results` 拆表 |
| **MINOR** | 向后兼容的功能新增 | 新增「文件卡片」、新增 provider、新增设置项 |
| **PATCH** | 向后兼容的修复（含纯 UI / 文档 / 测试 / 性能） | 布局修复、i18n 补词、错误文案 |
| **channel** | 预发布通道：`alpha`（内部）< `beta`（可测）< `preview`（准发布）；无后缀 = **stable** | `-preview.3` |

约定：

- **预发布序号只增不减**；切换通道（preview → beta）序号从 1 重新开始。
- 发布到正式版：去掉后缀（`10.3.0-preview.3` → `10.3.0`），此后只允许补 PATCH。
- 版本号**只在 `cbp/version.py` 里写一次**，其它位置一律引用（`pyproject.toml`、README
  由 `scripts/bump_version.py` 同步；`GET /` 与 FastAPI 的 version 也读它）。

## 2. 每次改动的落地清单

1. 代码 + 测试（`python -m unittest discover -s tests`）
2. 不变量与 UI 体检：`scripts/invariants.py`、`scripts/ui_smoke.py`、
   `scripts/ui_i18n_audit.py`、`scripts/ui_layout_audit.py --stress`、`scripts/ui_contrast_audit.py`
3. 在 `CHANGELOG.md` 的 `## [Unreleased]` 下补条目（Added / Changed / Fixed）
4. 需要发版时：

```bash
python scripts/bump_version.py preview            # 或 patch / minor / major / stable
# 检查 git diff（version.py / pyproject.toml / CHANGELOG.md）
python scripts/bump_version.py show               # 确认当前号与下一号
git add -A && git commit -m "chore(release): vX.Y.Z-..."
git tag -a vX.Y.Z-... -m "release X.Y.Z-..."
git push && git push --tags
```

或者一条命令（会自动改号、搬 CHANGELOG、提交、打标签、推送）：

```bash
python scripts/bump_version.py preview --commit --tag --push
```

## 3. 提交信息约定

```
<type>(<scope>): <摘要>
```

- `type`：`feat` / `fix` / `perf` / `refactor` / `docs` / `test` / `style` / `chore`
- `scope`：`webui` / `agent` / `browser` / `provider` / `tools` / `plugins` / `api` / `删除` …
- 摘要用一句话说明**改了什么、为什么**；细节放正文。一个提交只做一件事。

## 4. 多机器协作

仓库里同时可能有 Windows / macOS / Linux 上的工作副本，为此：

- **行尾统一 LF**（见 `.gitattributes`）。Windows 上由 git 自动转换；
  若发现整文件"全变红"，先 `git add --renormalize .` 再看 diff。
- **每次开始前先同步**：`git pull --rebase`（避免 merge 提交污染历史）。
- **同一个功能不要在两台机器并行改**；跨机器切分支前先 commit 或 stash。
- 版本号冲突时**以 `cbp/version.py` 为准**，冲突解决后跑一次
  `python scripts/bump_version.py --to <最新号> --set` 让 `pyproject.toml` 对齐。
- 发布前必须有一台机器跑完整测试套件；标签只在干净工作区打。
- `.venv/`、`data/`、`workspace/`、`node_modules/` 不入库（见 `.gitignore`）；
  它们在不同机器上各自独立。

## 5. 分支与标签

| 名称 | 用途 |
|---|---|
| `main` | 主线，随时可发布 preview / stable |
| `preview/<主题>` | 大改动的预览线（如 `preview/multi-window`），合入前不保证稳定 |
| `archive/<版本>` | 归档旧稳定线（如 `archive/10.2.1-stable`） |
| tag `vX.Y.Z[-ch.N]` | 每个发布点一个标签，与 CHANGELOG 小节一一对应 |
| tag `vX.Y.Z-archive` | 历史归档点（仅标记，不参与发布线） |

## 6. 版本号与 CHANGELOG 的对应

- 每个 `## [X.Y.Z-...]` 小节 = 一个 tag = 一次发布。
- `## [Unreleased]` 永远在最上面，记录"已经合入但还没发版"的改动。
- 发布时由脚本把 Unreleased 的内容整体搬到新版本小节并打上日期。
