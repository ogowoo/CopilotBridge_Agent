---
name: debug-agent-task
description: 排查 Agent 任务异常的标准流程——时间线导出、raw_tail 现场分析、conv_stats 阈值校准
---

# Agent 任务异常排查

## 第一步:拿现场

WebUI 打开问题 Session → 步骤时间线右上角「⬇ 导出」。JSON 里每一步的
`arguments`(完整参数)、`result`、`invalid_action` 的 `raw_tail`(Copilot
返回原文尾部 2000 字符)是核心现场。

## 第二步:按症状分类

| 症状 | 先看 | 常见根因 |
|---|---|---|
| 文件只写了一半 | 最后一个 write/append 步的 raw_tail | Copilot 输出上限截断 → 检查是否用了 b64 分块(≤3000/块) |
| invalid_action 连环 | raw_tail 里的 JSON 断点 | 引号未转义/截断 → 解析容错应已恢复,看恢复标记 |
| 验证失败循环 | validate_python 的 failures | 文件没写完就验证(协议禁止,看 AGENT.md) |
| 等待服务卡很久 | service_unavailable 步 | Copilot 限流,指数退避是预期行为 |
| 会话频繁切换 | session_rollover 步与 conv_stats | 阈值过保守 → 跑校准 |

## 第三步:校准与预防

```powershell
python scripts/calibrate.py data/agent.db   # 触顶样本 vs 主动切换样本
```

建议值取「触顶样本最小 token × 0.85」;无触顶样本时用主动切换样本 P25。
tokens 与 config.json 阈值同口径,直接可填。

## 第四步:浏览器层

出错时服务目录有 `last_page.txt`(整页文本快照, markers 锚点失配时先看它);
`config.json` 的 `markers` 段可追加中文界面锚点。
