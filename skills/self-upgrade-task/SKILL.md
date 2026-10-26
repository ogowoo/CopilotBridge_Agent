---
name: self-upgrade-task
description: 让 Agent 修改服务自身代码（allow_self_upgrade）时的任务写法与验收清单——模板、白名单/禁写、验证门、回滚三条路
---

# 自升级任务（改服务自身代码）

`allow_self_upgrade: true` 时 Agent 多了 7 个 `self_*` 工具，能改**安装目录**里的
服务代码（不是当前工作区）。这个技能规定**任务怎么写**、**怎么验收**、**出问题怎么退**。

## 前提检查（改任何东西之前）

1. `config.json` → `agent.allow_self_upgrade: true`，**重启服务**（配置在导入时读取）。
2. 确认已生效：问 Agent「你有哪些 self_ 开头的工具」，应能列出
   `self_read_text` / `self_write_text_b64` / `self_append_text_b64` /
   `self_replace_text` / `self_validate` / `self_unittest` / `self_rollback`。
   关着时是 0 个。
3. 改之前先 `git status` 看一眼干净不干净——不干净的话出问题时分不清是谁改坏的。

## 任务模板（直接套用）

新开一个会话（上下文干净），把三件事说全：**目标文件 + 具体改什么 + 验收标准**。

```
【自升级任务】
文件：gantry/bridge.py（安装目录相对路径，不要写绝对路径）
现状：<这里写你观察到的行为，例如 resume 时提示只拼原任务>
要改：<越具体越好，能贴字段名/函数名/代码片段就贴>
边界：只改上面这个文件/这段逻辑，其它不要动
验收：self_validate + self_unittest 双通过
交付：告诉我改了哪些行、验证结果、需要重启，以及回滚命令
```

已有的几个短句写法也认（效果一样，只是少了"边界"那行）：

- 「把 `webui/style.css` 里侧栏默认宽度 318px 改成 300px，改完跑 self_validate 和 self_unittest」
- 「看 `gantry/api.py` 的 `/v1/shutdown`，把优雅停机超时从 8 秒改成 15 秒，其他别动」

## 硬约束（Agent 会遵守，你也该知道）

| 项 | 内容 |
|---|---|
| 可改（白名单） | `gantry/` `webui/` `scripts/` `docs/` `tests/` `skills/` `app.py` `start.py` `install.py` `requirements.txt` `AGENT.md` `README.md` `config.example.json` |
| **禁止改** | `config.json`、`data/`、`EdgeProfile/`、`for debug/`、`.git/`、`__pycache__/` → **配置自己改**，它碰不到 |
| 验证门 | 写完必须 `self_validate` + `self_unittest` 都 passed，否则不能宣称完成（系统会拦下 final） |
| 快照 | 每次写/替换在 `data/snapshots/_self_<sid>/` 存旧版本，可按任务整体回滚 |
| 生效 | 改的是磁盘文件，进程里仍是旧代码 → **必须重启** |

## 回滚（三条路，从近到远）

1. UI：「更多 ▾」→ **回滚服务自升级…**
2. API：`POST /v1/agent/self/rollback?last=true`
3. **服务起不来时**（唯一可用）：`python scripts/rollback.py --scope self --last --yes`

第 3 条是救命通道：把服务改到**启动即崩**时网页和 API 都进不去，只能本地命令行退。

## 常见坑

- **一次只做一件事**：回滚粒度是"整个任务"，混进多个不相关改动就只能全退。
- **别让它改 `config.json`**：写会被拒（禁写名单），要调配置你自己动手。
- **别只信"验证通过"**:`self_validate` 只查语法、`self_unittest` 只跑 `tests/`，
  行为正确性还得你自己 `git diff` 看一遍再重启实测。
- **提示词/工具描述也算代码**：改 `_system_prompt()` 或工具 description 会改变
  Agent 行为，属于高危改动，改完必须实测一轮任务。
- **重启才生效**:改完没重启却"看起来没效果"，先确认是不是没重启，别急着让它再改一版。
- **写大文件要分块**：`self_write_text_b64` 每块 ≤3000 字符，多块用
  `self_append_text_b64` 续写；没写完就 `self_validate` 必然失败，那不是缺陷。

## 风险提示

`allow_self_upgrade: true` 等于**允许 Agent 改自己的代码**；若同时开了
`allow_shell` / `allow_python` / `allow_delete`，实质是给本机完全控制权。建议：

- 只在有人盯着时开，改完关回去；
- 需要长期开着时，至少保持 `allow_delete: false`；
- 每次改完自己 `git diff` 审一遍，再提交。
