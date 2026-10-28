---
name: webui-change
description: 修改 WebUI 时的结构与验证规范——三文件职责划分、演示模式与 Playwright 验证清单
---

# WebUI 修改规范

管理台在 `webui/`，三文件职责严格分离：

- `index.html`：纯 DOM 结构 + head 里 2 行防闪烁启动脚本(主题/侧栏 localStorage 读取，勿外置)
- `style.css`：全部样式(7 套主题令牌在 `:root` 与各 `data-theme` 段)
- `js/*.js`：全部业务逻辑（ES 模块，一块一个文件；共享可变状态放 `js/globals.js`）

## 服务端配套

`gantry/routes_core.py` 里 `/ui` 307 到 `/ui/`，`/ui/{asset}` 白名单放行 `style.css`/`boot.js`/`i18n.js` 与 `js/<名字>.js`。
新增静态资源要同时改白名单集合。

## 验证清单(改完必做)

1. 演示模式(file:// 直接打开 webui/index.html)能完整渲染——相对引用对静态打开必须可用
2. 无 JS 错误(pageerror 监听为空)
3. Playwright 过一遍核心流程：选项卡切换、新任务弹窗开合、发送
4. 桌面 1400px + 移动 700px 两种视口截图检查布局
5. 响应式断点：1100px(内容区单栏化)、860px(移动端回退)两侧各验一次

## 常见坑

- 选项卡内容由 `LIVE_NAMES`/`LIVE_TAB` 两个字典驱动，新增页签两处都要加
- 弹窗复制内容用 `expandLive()`，对话页只复制 `#chatlog`(避免重复 ID)
- 相对引用依赖 `/ui/` 结尾的基地址，所以 `/ui` 必须保持重定向
