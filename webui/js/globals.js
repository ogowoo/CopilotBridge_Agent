/* globals.js — 跨模块共享状态（**经典脚本**，不是 ES 模块）。
 *
 * 为什么单独一个文件：ES 模块顶层 `var/let` 是**模块私有**，既不是 window 属性、也不在
 * 全局词法环境里。别的模块裸名访问会 ReferenceError（模块是严格模式），而这类错误常被
 * 各处 try/catch 吞掉，表现为"会话列表空白 / 工具记录不刷新"，很难从界面看出来。
 * 经典脚本顶层的 `var` 既挂 window 又在全局词法环境 → 模块里可以继续裸名读写，
 * 和拆分前那个 5000 行单体里的语义完全一致。
 *
 * 拆分原则：
 *   · 可变状态（var/let）→ 放这里（规则与校验见 docs/ESM-MIGRATION.md 与 scripts/cross_module_refs.py）；
 *   · 不可变常量（THEMES/SLASH_COMMANDS/MANUAL_HTML…）→ 留在原模块 `export`，用 `import` 引用；
 *   · 函数 → 留在模块里 `export`，由 js/main.js 绑到 window（老调用点不改）。
 *   · 校验：`python scripts/cross_module_refs.py`（跨模块共享合规）+ 浏览器冒烟。
 *
 * 加载顺序（index.html）：boot.js → globals.js → i18n.js → js/main.js(module)。
 * i18n.js 里的 rerenderForLang() 会裸用 lastSteps/lastMessages/sessSig/_lastBoot，
 * 所以必须排在 globals.js 之后。
 */

/* ── 会话与时间线：原 app.js 顶部（拆分时临时留在 app.js 里）── */
var current=null;          // 当前会话 id
var currentUrl=null;       // 当前会话的 Copilot URL
var currentModel=null;     // 当前会话用的模型
var sessCache=[];          // 会话列表缓存
var renderedSteps=0;       // 已渲染的步骤数（增量渲染用）
var lastStepCount=-1, lastTotal=-1, lastDetail='';
var lastRuntime=null;      // 最近一次 runtime 响应（control.js 与 followload.js 共用）
var modelCache=[];         // /v1/models 的可用模型（会话页选择器复用）
var budget=0;              // 由后端 token_budget 下发，避免与 config.json 脱节
var DEMO=false;            // ?demo=1 才允许进演示模式（fetch 失败不再自动进）
var taskMode='agent', liveTabName='chat';   // 运行模式 / 实时流页签

// ── 状态开始（按原 app.js 的分组排列；改动后跑 scripts/cross_module_refs.py）──
/* 原 apistate.js */
var expandedSess=new Set();
var showSubagents=false;
var subagentCount=0;
/* 原 approvals.js */
var lastSubagentFetch=0;
var lastSubagentParent=null;
var parentOfCurrent=null;
var subagentBusy=false;
/* 原 cards.js */
var lastMessages=[];
var toolLogBodySig='';
var toolLogBodyTimer=null;
var toolLogSig='';
var toolLogSteps=[];
var findQuery='';
var tokenPromptCancelled=false;
var tokenPrompting=null;
/* 原 codeux.js */
var _gmResolve=null;
var codeMenuEl=null;
var lastCodeMenuTarget=null;
/* 原 composer.js */
var lastAnsHtml='';
/* 原 control.js */
var retryDismissedAt=null;
/* 原 dialogs.js */
var followupMode='agent';
/* 原 followload.js */
var evtSrc=null;
var evtSid=null;
var evtFails=0;
var lastAssistantCount=0;
var todosOpen=false;
/* 原 panels.js */
var slashIdx=0;
/* 原 sessions.js */
var sessSig='';
/* 原 slash.js */
var planModeOnce=false;
var wsCurrent='';
/* 原 steps.js */
var lastSteps=[];
var stepFilter={tool:'',status:''};
var viewCache=new Map();
/* 原 subagents.js */
var panelCache=[];
/* 原 themeboot.js */
var serviceStopped=false;
var settingsTab='ui';
var settingsSchema=null;
var settingsValues=null;
var _lastBoot=null;

/* 原 steps.js / themeboot.js（被别处用 window.X 误当全局用，2026-10-28 收口） */
var renderedSid=null;
var lastSessionObj=null;
var skipDetailOnce=null;
var sandboxMode='workspace-write';
// ── 状态结束 ──
