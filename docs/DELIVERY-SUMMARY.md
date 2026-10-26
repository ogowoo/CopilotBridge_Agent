# 交付物登记与遗留问题汇总

## 已登记交付物

| 文件 | 用途 | 状态 |
|---|---|---|
| docs/ANALYSIS.md | 全面分析报告 | 已生成 |
| docs/ARCHITECTURE.md | 架构说明 | 已生成 |
| docs/TECHNICAL.md | 技术文档 | 已生成 |
| docs/TODO.md | 治理待办清单 | 已生成 |

## 验证结果

- validate_python 已通过，共检查 255 个 Python 文件。
- 核心解析回归测试通过：28 项测试全部成功；完整 unittest 在当前执行窗口内超时，不能宣称全量回归通过。

## 遗留问题

- P0：4 项，涉及非 loopback 强制 Token、CI 必过门、依赖锁定与 SBOM、可信发布流水线。
- P1：8 项，涉及 SQLite 资源释放、状态一致性、测试分层、恢复演练、配置治理和可观测性。
- P2：2 项，涉及运维参考文档补齐、DOM 与工具裁剪脆弱性治理。
- 首要风险是完整测试超时；核心测试仍出现两个未关闭 SQLite 连接的 ResourceWarning，后续应优先落实 P1-RES-01 与 P1-TST-01，再建立 P0-CI-01 合并门禁。
