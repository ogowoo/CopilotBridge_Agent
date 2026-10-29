---
name: gantry-tool-plugin
description: 为 Gantry 通过标准插件扩充工具库的实现与验收流程
---

# Gantry 工具插件开发

## 什么时候用

- 需要在不继续膨胀 gantry/tools_extra.py 的前提下新增工具。
- 需要工具支持插件发现、启停、异常隔离和卸载清理。

## 关键步骤

1. 在服务根级 plugins/<plugin-name>/ 创建 plugin.json 与 plugin.py。
2. plugin.json 声明 permissions: ["tools"]，并在 contributes.tools 列出工具名。
3. plugin.py 的 setup(ctx) 使用插件上下文注册工具，并保存注册句柄供卸载清理。
4. 文件路径解析复用 ToolRegistry._path，不要调用不存在的 _resolve。
5. 先运行 self_validate，再运行 self_unittest pattern=test_plugins.py；两者通过后重启 Gantry。
6. 重启后检查 /v1/plugins 中插件 status=loaded，并用 tool_list 确认声明的工具已进入运行时工具库。

## 常见坑

- 只通过静态测试不等于运行态加载成功，插件代码改动必须重启后验收。
- 插件目录未加入自升级白名单时，self_write_text_b64 与 self_replace_text 会拒绝操作。
- 注册函数引用 ToolRegistry 私有成员前必须先核对实际实现；当前路径沙箱入口是 _path。
- 插件加载失败时优先读取 /v1/plugins 的 error 字段，不要反复运行完整回归。

## 验证标准

- self_validate 返回 passed=true。
- self_unittest pattern=test_plugins.py 返回 passed=true。
- /v1/plugins 显示目标插件为 loaded 且 registered.tools 完整。
- tool_list 能列出插件声明的全部工具。
