# app.js → ES Modules 迁移

`webui/app.js`（5270 行单体）正在按块拆成 ES 模块。**增量迁移**：每步只搬一块，
页面保持全绿、测试照跑，直到 app.js 清空。

## 架构

```
webui/js/
├── state.js    # 共享可变状态（export const S = {}；模块间 `import { S }`，改 S.xxx）
├── main.js     # 入口：import 各块，把老代码仍在调用的函数绑到 window
├── scope.js    # 第一块（示波器浮层，已迁）
└── …           # 后续块按"自包含程度"从小到大迁
```

**关键模式**：拆出去的块把老代码仍在调用的函数绑到 `window`（如
`window.startScope = startScope`），这样 app.js 事件路径的调用点**一行都不用改**
（模块是 deferred，用户交互时早已绑定）。

## 迁移步骤（每块照此做）

1. 从 `app.js` 里把整块**原样**搬进 `webui/js/<名>.js`，顶部 `export { … }`；
2. 私有符号留在模块里（别 export）；跨块引用才走 `import`；
3. `main.js` 里 import 并 `window.<名> = <名>`；
4. 从 `app.js` 删掉原块，跑：
   - `node --check webui/app.js && node --check webui/js/<名>.js`
   - `python -m unittest discover -s tests`
   - `python scripts/ui_smoke.py`（浏览器里看有没有 JS 报错）
5. `tests/test_esm_migration.py` 加该块的钉死断言（块搬走 + main.js 绑定）。

## 路由

`/ui/{asset:path}` 精确名单（style.css/app.js/boot.js/i18n.js）之外，另放行
`js/<名字>.js`（带穿越防护：不许 `..`、不许再嵌套子目录）。

## 迁移顺序建议（自包含 → 有依赖）

1. ✅ scope.js（示波器）
2. @ 菜单（atMenu，事件驱动）
3. toast / 高亮 / mdToHtml / fileCardHtml（纯渲染工具）
4. 步骤渲染（renderSteps/toolLabel/svgIcon）
5. teamCard 四行（todos/subagents/toollog/streamstatus）
6. settings / sandbox / theme
7. sessions 列表与 composer
8. 最后是 init 引导（boot IIFE 迁进 main.js，app.js 正式退役）

## 测试护栏

- `tests/test_esm_migration.py`：模块文件存在、块搬走、main.js 绑定、路由放行；
- `scripts/ui_smoke.py`：无 JS 异常、关键全局齐全；
- 不变量 24 条（含"页面引用资产都在白名单内"）。
