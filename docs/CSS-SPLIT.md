# 样式结构：`webui/css/*.css`（12 个文件）

`webui/style.css`（原 2340 行 / 136 KB）先按"原有章节边界"切成 11 段（逐字节等价，视觉零变化），
随后把历史累积的 `refinements.css`（650 行）按**组件族**摊进各文件，得到现在 12 个文件。

## 文件与顺序（= `index.html` 里 `<link>` 的顺序 = 层叠顺序）

| # | 文件 | 行 | 内容 |
| --- | --- | --- | --- |
| 1 | `tokens.css` | 31 | 设计令牌：颜色/字号/圆角/阴影/关键尺寸（`var(--…)` 的来源）、`.app` 网格 |
| 2 | `shell.css` | 321 | 左侧栏（含页脚 ⚙/版本号、收起态、窄屏抽屉）、主区域、任务条折叠、KPI + 外壳媒体查询 |
| 3 | `chat.css` | 241 | Markdown 消息体（可读性/限宽）、时间线步骤、工具结果双视图、diff/JSON 卡、语法高亮、文件卡片 |
| 4 | `dialogs.css` | 67 | 遮罩、确认框、删除结果、页面内通知（toast 置顶） |
| 5 | `themes.css` | 134 | 7 套主题令牌 + 原生控件跟随 |
| 6 | `overlays.css` | 148 | 使用手册、未就绪/连接/授权横幅、浮层分车道、子代理面板、插件弹窗 |
| 7 | `composer.css` | 386 | 新会话视图、工作目录、工作区分组、斜杠命令、插件面板、输入框（含发送/停止按钮、@ 弹层） |
| 8 | `layout.css` | 243 | 主列 flex、对话区两栏滚动定稿、输入框浮动层、窄屏抽屉、会话行、步骤页高度、离屏跳过绘制、卡片示波器 |
| 9 | `settings.css` | 217 | 设置页与相关打磨 |
| 10 | `alerts.css` | 76 | 中断类提醒（授权/等待回答）、全页面限流遮罩 |
| 11 | `rows.css` | 473 | **底部四条横条**（待办/子代理/调用记录/状态）：行高、内边距、hover、展开区、底部块滚动布局 |
| 12 | `changes.css` | 46 | 任务完成卡片（本次改动/交付物）与 diff 浮层的行着色 |

## 三条规则

1. **顺序就是层叠**：`<link>` 的顺序不能改，也不能漏——少一个文件只会"少一片样式"，
   不会报错（这类问题最难查）。不变量 #15 与 `tests/test_css_split.py` 都在核这件事。
2. **不许用 CSS 的 import 指令**：`@import` 会让浏览器**串行**下载，首屏变慢；
   一律用 `index.html` 里的并列 `<link>`。
3. **新规则放语义对应的文件**，不要靠 `!important` 抢层级。`rows.css` 例外：它内部确实
   存在"后写覆盖先写"，所以新规则追加到文件末尾，别插到中间。

## 改样式后必须做的事

```bash
python scripts/css_style_diff.py --record data/css-diff    # 改之前：录一份计算样式基线
# …改 css…
python scripts/css_style_diff.py --compare data/css-diff   # 改之后：逐元素比对，必须 0 差异
python scripts/invariants.py && python -m unittest discover -s tests
python scripts/ui_layout_audit.py --stress && python scripts/ui_contrast_audit.py
```

`css_style_diff.py` 是"改 CSS 不改行为"的硬证据：它把 `/v1/**` 的响应**录下来**，
用同一份数据驱动同一个页面，在 12 个场景（默认 / 会话 / 展开四行 / 设置 / 手册 / 插件 /
新任务 × 暗色 / 浅色 × 1400 / 900 / 480 宽）里逐元素比对约 85 个布局与颜色属性，
并顺带比对 DOM 结构。**实测：拆分与重排两步都是 0 差异。**

> 反面教材（这轮重排真的踩到）：第一版用正则 `[^{}]*\{[^{}]*\}` 切块，撑不住嵌套的
> `@media`——媒体查询外壳被吃掉，窄屏"浮动齿轮"规则跑到宽屏生效；探针立刻报出 540 处差异。
> 现在改成按大括号深度切"顶层语句"，`@media` / `@keyframes` 整体搬运。

## 测试怎么取样式

```python
from gantry.ui_src import ui_css, css_rule

css = ui_css()                  # 12 个文件按层叠顺序拼接（等价于原来的整份 style.css）
self.assertTrue(css_rule(".todolist"))    # 精确取某选择器的规则块
```

不要再用 `(BASE / "webui" / "style.css").read_text(...)`——文件已经不存在了。
