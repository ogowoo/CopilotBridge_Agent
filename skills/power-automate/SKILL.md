---
name: power-automate
description: 生成 Power Automate 工程的标准做法——与 Copilot 同租户的 automate 环境、definition.json 结构、内置连接器引用与 JSON 校验
---

# 生成 Power Automate 工程

## 什么时候用

用户要一个 Power Automate（云端流）工程/自动化方案时。目标环境是与 Copilot **同租户**的
Power Automate（Maker 门户 `https://make.powerautomate.com`），即用户登录 Copilot 所用的
Microsoft 365 租户里默认可用的 automate 环境与内置连接器。

**边界**：本仓库没有 Power Platform 凭据，**不要调用任何真实 Power Automate API**。
只生成工程文件；导入由用户在 make.powerautomate.com 里手动完成（解决方案导入，或
「我的流 → 新建 → 从模板/粘贴定义」）。

## 交付目录

写到任务工作区的 `flows/<flow-name>/` 下，标准结构：

```
flows/<flow-name>/
├── flow/
│   └── definition.json   # 流定义（状态机 JSON，Logic Apps 工作流定义格式）
├── connections.json      # 连接引用（哪些连接器、引用名、连接实例占位）
├── apis.json             # 用到的连接器 API 清单（displayName + apiId）
└── README.md             # 导入步骤 + 所有 <占位> 替换清单
```

## definition.json 的结构

顶层必含 `triggers` 与 `actions`，完整键：

| 键 | 说明 |
|---|---|
| `$schema` | 固定 `https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#` |
| `contentVersion` | `"1.0.0.0"` |
| `parameters` | 两个约定参数：`$connections`（type Object）与 `$authentication`（type SecureObject），动作里用 `@parameters('$authentication')` 引用 |
| `triggers` | 触发器。手动触发 = `type: "Request"` + `kind: "Button"`；计划触发 = `type: "Recurrence"` |
| `actions` | **状态机 JSON**：每个动作是一个节点，含 `type`（连接器动作为 `OpenApiConnection`）、`inputs.host`（`connectionName` / `operationId` / `apiId`）、`inputs.parameters`、`runAfter`（决定 DAG 顺序，`{}` 表示紧跟触发器） |

`connectionReferences`（解决方案包格式里位于 `properties.connectionReferences`）在本工程
里拆到 `connections.json`，导入时按 README 替换。

## 同租户内置连接器引用

同租户下这些内置连接器默认可用，引用名固定，resource 路径写法为
`/providers/Microsoft.PowerApps/apis/<引用名>`：

| 服务 | connectionName / apiId 末段 | 常用 operationId |
|---|---|---|
| Office 365 Outlook | `shared_office365` | `SendEmailV2` |
| SharePoint | `shared_sharepointonline` | `GetItems` / `CreateItem` |
| Microsoft Teams | `shared_teams` | `PostMessageV3` |
| Dataverse | `shared_commondataserviceforapps` | `AddRowUsingExternalId` / `ListRecords` |

动作 `inputs.host` 示例：`apiId` 填 `/providers/Microsoft.PowerApps/apis/shared_teams`，
`connectionName` 填 `shared_teams`。`connections.json` 里每个引用给出 `id`（同上路径）、
`displayName`、`connectionRef` 与 `connectionId` 占位符（后者由用户在同租户环境里选
已有连接或新建连接后填入）。

## 校验（必做）

写完每个 JSON 必须能被 `json.load` 解析。写完文件后用 exec/validate_python 跑：

```python
import json, pathlib
for p in pathlib.Path("flows/<flow-name>").rglob("*.json"):
    json.load(open(p, encoding="utf-8"))
    print("ok", p)
```

解析失败先修再交付；README 里同时列出所有 `<占位：...>` 让用户一目了然。

## 模板

`skills/power-automate/templates/` 下：
- `flow-definition.json`：手动触发 + Teams 发消息的最小可用流；
- `connections.json`：连接引用模板。

复制到 `flows/<flow-name>/` 对应位置后替换 `<占位：...>`，勿留占位符交付。
