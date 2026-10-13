# CopilotBridge Agent 项目约定

把本目录作为 workspace 交给 Agent 时，本文件会注入系统提示。约定优先级高于模型默认习惯。

## 项目定位

用 Playwright 驱动本机 Edge，把 M365 Copilot 网页版包装成本地 OpenAI 兼容 API（`/v1/chat/completions`），并内置长任务 Agent（JSON 动作协议 + 工具循环）。另支持任意 OpenAI 兼容 API 提供商旁路。

## 目录结构

```
app.py / start.py / install.py   入口(根目录只留入口)
cbp/                             库代码包：api(路由) bridge(浏览器编排)
                                 agent(主循环) tools(工具) session_store
                                 providers(API 网关) copilot_delete owa
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
python scripts/calibrate.py            # conv_stats 阈值校准分析
python scripts/outlook_selftest.py     # Outlook COM 五步自检
python start.py                        # 启动服务(默认 127.0.0.1:8765)
```

## 硬性约定(违反会破坏系统)

1. **改代码后必须闭环**：`.py/.json/.html` 被修改后，validate_python + run_unittest 双 passed 前不得宣称完成(系统提示层有同样的门)。
2. **写文件用 b64 分块**：`write_text_b64`/`append_text_b64`，每块 ≤3000 字符；写完再验证，禁止边写边验。
3. **WebUI 是三文件结构**：结构改 `index.html`、样式改 `style.css`、逻辑改 `app.js`；改完用演示模式(断 API 直连 file://)或 Playwright 验证，且 `/ui/{asset}` 白名单只放行这两个文件名。
4. **系统提示词在 cbp/agent.py `_system_prompt()`**：改 Agent 行为优先改这里；其中有对抗 Copilot 输出上限/截断的成熟条款，删除前读注释。
5. **Outlook COM 必须走 `_ComWorker`**(cbp/tools.py)：专用消息泵线程，勿改回 asyncio.to_thread——后台线程无消息泵会导致 0x80004004 E_ABORT。
6. **正则容错解析在 `_parse`**：新增容错路径必须同步在 tests/test_agent_parse.py 加回归用例。

## 自我升级与回滚（改这个项目自身时必读）

允许 Agent 修改本项目的代码与文档实现"自我升级"，但必须遵守：

1. **改动前有快照**：所有写操作前原像自动存入 `data/snapshots/<sid>/`（同一路径保留最早状态）。整任务回滚入口：WebUI「更多 ▾ → 回滚任务改动…」、`POST /v1/agent/sessions/{sid}/rollback`、或服务起不来时用 `python scripts/rollback.py --last --yes`。
2. **改完必须验证**：`.py/.json/.html` 改动后 validate_python + run_unittest 双通过才算完成；服务重启后能 `python start.py` 起来才算真升级。
3. **升级自己要留下可回退点**：涉及 cbp/ 核心（agent/bridge/tools/api）的改动要一次一件事，别把多个不相关重构混在一次任务里——回滚是按任务的，混在一起就得整块回退。
4. **改坏了的判定**：导入报错/服务起不来/测试不过。此时优先回滚，不要在坏状态上继续叠加修补。
5. **AGENT.md 与 skills/ 可以由 Agent 自己更新**（把验证有效的结论沉淀下来），但：禁止删除既有安全约束与验证闭环规则；系统会把这类改动标记为"自我指令更新"；AGENT.md 控制在 8000 字符内（超出部分不会注入）。
6. **快照有上限**：单文件 8MB、单会话 800 条；超限会记入 skipped 且不回滚那些路径（大二进制文件别指望回滚）。

## 安全开关(默认保守)

`allow_python`/`allow_delete`/`allow_outlook_send` 默认全关；`run_python` 一旦开启即等于工作区内任意代码执行。改动这些默认值要在提交说明里写明理由。

## 调试入口

- Agent 任务异常：WebUI 步骤时间线「⬇ 导出」拿完整 JSON(raw_tail 里有 Copilot 原文尾部)。
- 会话阈值不准：`scripts/calibrate.py data/agent.db` 看触顶样本。
- 浏览器层异常：服务目录 `last_page.txt` 是出错时的整页文本快照。
