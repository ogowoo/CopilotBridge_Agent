# Gantry 项目约定（全局级）

本文件位于**服务安装目录**，属于**全局 AGENT.md**：对所有工作区生效。
工作区根目录若也有 `AGENT.md`，两份会合并注入，**项目级优先**。
技能同理：`<安装目录>/skills/` 是全局技能，`<工作区>/skills/` 是项目技能，同名时项目覆盖全局。

把本目录作为 workspace 交给 Agent 时，本文件同样注入。约定优先级高于模型默认习惯。

## 项目定位

用 Playwright 驱动本机 Edge，把 M365 Copilot 网页版包装成本地 OpenAI 兼容 API（`/v1/chat/completions`），并内置长任务 Agent（JSON 动作协议 + 工具循环）。另支持任意 OpenAI 兼容 API 提供商旁路。

## 目录结构

```
app.py / start.py / install.py   入口(根目录只留入口)
gantry/                             库代码包
                                 api.py / routes_*.py   FastAPI 应用与路由
                                 bridge.py            运行态 facade 与工具装配
                                 agent.py             Agent 核心循环、状态治理、验证门
                                 agent_prompt.py      PromptMixin：系统提示词装配
                                 agent_parse.py       ParseMixin：JSON 动作协议与容错解析
                                 agent_text.py        TextMixin：token 估算与文本压缩
                                 tools.py             工具注册表/分派、路径沙箱与权限门
                                 tools_*.py           文件/执行/Office/Outlook/技能/记忆/自升级 mixin
                                 session_store.py     SQLite 会话持久化
                                 providers.py         API 提供商接入
                                 copilot_delete.py    Copilot 侧栏会话删除
                                 owa.py               Outlook 网页自动化
webui/                           管理台：index.html(结构) style.css app.js
scripts/                         login.py / calibrate.py / outlook_selftest.py
tests/                           unittest(test_agent_parse.py)
docs/                            技术文档
config.json                      运行配置(agent 段约 30 项开关)
data/agent.db                    SQLite 会话库(WAL)
EdgeProfile/                     Edge 登录态(含 Cookie，勿外泄)
```

## 常用命令

```powershell
python -m unittest discover -s tests   # 回归测试(改解析/提示逻辑必跑)
python scripts/login.py                # 登录向导(--reset 重置登录态;服务运行时会拒绝)
python scripts/calibrate.py            # conv_stats 阈值校准分析
python scripts/outlook_selftest.py     # Outlook 分段自检(COM/线程模式对照)
python scripts/rollback.py --list      # 查看可回滚的任务与自升级记录
python start.py                        # 启动服务(默认 127.0.0.1:8765)
```

## 硬性约定(违反会破坏系统)

1. **改代码后必须闭环**：`.py/.json/.html` 被修改后，validate_python + run_unittest 双 passed 前不得宣称完成(系统提示层有同样的门)。
2. **写文件用 b64 分块**：`write_text_b64`/`append_text_b64`，每块 ≤3000 字符；写完再验证，禁止边写边验。
3. **WebUI 是三文件结构**：结构改 `index.html`、样式改 `style.css`、逻辑改 `app.js`；改完用演示模式(断 API 直连 file://)或 Playwright 验证，且 `/ui/{asset}` 白名单只放行这两个文件名。
4. **系统提示词在 `gantry/agent_prompt.py` 的 `PromptMixin._system_prompt()`**：改 Agent 行为优先改这里；其中有对抗 Copilot 输出上限/截断的成熟条款，删除前读注释。
5. **Outlook COM 必须走 `_ComWorker`**(gantry/tools.py)：专用消息泵线程，勿改回 asyncio.to_thread——后台线程无消息泵会导致 0x80004004 E_ABORT。
6. **正则容错解析在 `gantry/agent_parse.py` 的 `ParseMixin._parse()`**：新增容错路径必须同步在 tests/test_agent_parse.py 加回归用例。

## 自我升级与回滚（改这个项目自身时必读）

**自升级模式**（`config.json` 里 `agent.allow_self_upgrade: true` 才开启，默认关闭）让 Agent 用 `self_*` 工具直接修改**服务自身代码**——即使当前工作区是别的项目也能改安装目录：

| 工具 | 用途 |
|---|---|
| `self_read_text` | 读服务自身文件（安装目录相对路径） |
| `self_write_text_b64` / `self_append_text_b64` | 分块写服务代码（Base64，≤3000 字符/块） |
| `self_replace_text` | 精确替换（改代码优先） |
| `self_validate` / `self_unittest` | 全安装目录语法检查 / 服务自身单测（**自升级的硬门**） |
| `self_rollback` | 回滚本次任务对服务代码的改动 |

白名单：`gantry/`、`webui/`、`scripts/`、`docs/`、`tests/`、`app.py`、`start.py`、`install.py`、`requirements.txt`、`AGENT.md`、`README.md`、`config.example.json`。
**禁止触碰**：`config.json`、`data/`、`EdgeProfile/`、`.git/`、`for debug/`。

硬性规则：

1. **改动前有快照**：工作区改动存 `data/snapshots/<sid>/`，自升级改动存 `data/snapshots/_self_<sid>/`（两个作用域可独立回滚）。同一路径保留最早状态。
2. **回滚通道三条**：WebUI「更多 ▾ → 回滚任务改动…／回滚服务自升级…」、`POST /v1/agent/sessions/{sid}/rollback` 与 `POST /v1/agent/self/rollback?last=true`、以及**服务起不来时的离线命令** `python scripts/rollback.py --last --yes`（工作区）/ `python scripts/rollback.py --scope self --last --yes`（服务代码）。
3. **验证门**：自升级写完必须 `self_validate` + `self_unittest` 双通过；提前返回 final 会被 `validation_gate` 拦下。
4. **一次一件事**：回滚按作用域/任务粒度，多个不相关重构混在一次任务里就只能整块回退。
5. **生效需要重启**：自升级写盘后会落下 `data/.self_upgrade_pending` 标记，服务启动时自动归档；返回 final 必须告知"需重启 + 回滚命令"。
6. **改坏了的判定**：导入报错/服务起不来/测试不过 → 优先回滚，不要在坏状态上叠加修补。
7. **AGENT.md 与 skills/ 允许 Agent 自更新**（沉淀验证有效的结论），但禁止删除既有安全约束与验证闭环规则；这类改动会标记 `instruction_update`；AGENT.md 单份 ≤8000 字符。
8. **快照上限**：单文件 8MB、单会话 800 条；超限记入 `skipped` 且不回滚那些路径（大二进制别指望回滚）。

## 安全开关(默认保守)

`allow_python`/`allow_delete`/`allow_outlook_send` 默认全关；`run_python` 一旦开启即等于工作区内任意代码执行。改动这些默认值要在提交说明里写明理由。

## 调试入口

- Agent 任务异常：WebUI 步骤时间线「⬇ 导出」拿完整 JSON(raw_tail 里有 Copilot 原文尾部)。
- 会话阈值不准：`scripts/calibrate.py data/agent.db` 看触顶样本。
- 浏览器层异常：服务目录 `last_page.txt` 是出错时的整页文本快照。
