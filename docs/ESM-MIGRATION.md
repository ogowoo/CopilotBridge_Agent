# app.js → ES Modules 迁移（已完成）

`webui/app.js`（原 5139 行单体）**已退役删除**。现在的结构：

```
webui/
├── index.html      结构
├── css/*.css       样式（11 个文件，加载顺序 = 层叠顺序，见 docs/CSS-SPLIT.md）
├── boot.js         经典脚本：首屏主题 / localStorage 键迁移（cbp-* → gantry-*）
├── i18n.js         经典脚本：三语言（zh / tw / en）+ applyI18n/tr
└── js/
    ├── globals.js  ★经典脚本★ 跨模块共享的可变状态（唯一允许的"全局变量"）
    ├── main.js     ES 模块入口：import 所有模块、把函数绑到 window、跑启动流程
    └── *.js        ES 模块（30 个，按原 app.js 的块一一对应）
```

`index.html` 末尾只有三段：

```html
<script src="js/globals.js"></script><script src="i18n.js"></script>
<script type="module" src="js/main.js"></script>
```

## 三条边界（踩过的坑，别再犯）

### 1. 可变状态只能放 `js/globals.js`

ES 模块顶层的 `var/let` 是**模块私有**：既不是 `window` 属性，也不在全局词法环境里。
别的模块裸名访问会 `ReferenceError`（模块是严格模式），而前端到处是 `try/catch`，
于是错误被吞掉，界面只表现为"会话列表空白 / 工具记录不刷新"——单测全绿也发现不了。

**真事**：迁移中途 `drawSessions` 抛 `ReferenceError`，被 `refreshSessions` 的
`try { … } catch(e){}` 吞掉，侧栏整片空白；`python -m unittest`（1226 条）和不变量
都是绿的，只有真的打开浏览器点一下才看见。

`globals.js` 是**经典脚本**，顶层 `var` 既挂 `window` 又在全局词法环境里，
模块里可裸名读写，和拆分前的单体语义完全一致。判断标准：

| 东西 | 放哪 |
| --- | --- |
| 会被多个模块读/写的数据（`current`、`lastSteps`、`toolLogSig`、`settingsTab`…） | `js/globals.js`（`var`） |
| 不可变常量（`THEMES`、`SLASH_COMMANDS`、`MANUAL_HTML`、`SETTINGS_GROUPS`…） | owner 模块 `export`，消费方 `import` |
| 函数 | 模块里 `export`，由 `js/main.js` 绑 `window`（老调用点一行不改） |

### 2. 模块晚于经典脚本执行

`<script type="module">` 是 deferred：**它在所有经典脚本之后才跑**。
所以初始化期的 DOM 绑定与启动流程必须在 `main.js` 里（`bindAtMenu()`、
`mountTeamCard()`、`bootInit()`），不能留在经典脚本的启动 IIFE 里调模块函数
（那时 `window` 上还没有这些函数）。曾因此报 `mountTeamCard is not defined`。

### 3. 一个名字只能有一份实现

从同一段源码里"重叠切块"会造出两份同名函数（`cardOf/toggleCard/applyTopbarCollapsed`…
曾同时存在于 `cards.js` 和 `subagents.js`），两份会各改各的；事件监听也会重复绑定
（顶栏点击的处理器原 app.js 里就有两份，切两次等于没切，收起功能等于失效）。

## 护栏

```bash
node --check webui/js/*.js                      # 语法
python scripts/cross_module_refs.py             # 跨模块共享合规（也可 --check）
python scripts/invariants.py                    # 25 条不变量，含上面这条
python -m unittest discover -s tests            # 1226 条
python scripts/ui_smoke.py                      # 浏览器：关键全局 + 无 JS 异常
python scripts/smoke_interact.py                # 浏览器：真点开一个会话看有没有内容
```

`scripts/cross_module_refs.py`（也进了不变量 #25）静态检查四件事：

1. 跨模块裸引用的状态是否都在 `globals.js`；
2. 跨模块调用的函数是否 `export` + 绑了 `window`（或消费方 `import`）；
3. 同一个名字是否被两个模块各声明一份；
4. `window.X` 是否指向某个模块的**私有**变量（两份状态）。

测试侧统一用 `gantry/ui_src.py`：

- `ui_js()` 按**原 app.js 的顺序**拼接 `webui/*.js` + `webui/js/*.js`
  （老测试爱"按函数名切一段再看"，顺序变了段就切空）；
- `fn_body("renderGoalBar")` 按大括号配平取单个函数体，比 `index(A)..index(B)` 稳。

## 迁移顺序（留档）

state → scope → atmenu → toast → find → msgrender → steps → toollog → teamcard →
approvals → subagents → panels → slash → workspace → apistate → sessions → codeux →
dialogs → followload → cards → control → manual → composer → themeboot → settings →
version →（收尾）globals.js + 删除 app.js。

每步都跑一遍 `node --check` + 浏览器冒烟 + 单测，保证"停在任何一步都是可用的"。
