// main.js — ES 模块入口（app.js 解耦的入口）。
//
// 迁移策略（增量、每步全绿）：
// 1. 从 app.js 拆出一个自包含块 → webui/js/<块>.js（ES 模块）；
// 2. 这里 import 它，把老代码仍在调用的全局函数绑到 window（window.startScope 等）；
// 3. app.js 删掉原块；事件路径的调用点不用动（模块是 deferred，用户交互时早已绑定）。
//
// 等所有块迁完，app.js 就空了，index.html 只留这一个 <script type="module">。
import { S } from './state.js';
import { startScope, stopScope, scopeFrame } from './scope.js';
import { atUpdate, atPick, atClose, bindAtMenu } from './atmenu.js';
import { toast } from './toast.js';
import { chatFindInput, applyFind, focusHit, findStep, toggleChatFind, highlight } from './find.js';
import { tryParseJson, jsonViewBox, actionCardHtml, renderJsonReply, renderAssistantBody, msgEl, codeBlockHtml, fileCardHtml, mdToHtml, exportTimeline } from './msgrender.js';
import { fillStepTools, stepMatch, collapseTransient, renderSteps, renderStepsMaybe, flushStepsIfPending, stepView, svgIcon, toolLabel, stepRowEl, renderStream, pinChatBottom, scrollChatToEnd, stashView, restoreView, nearestScroller } from './steps.js';
import { toggleToolLog, renderToolLog, loadTurns, summarizeAnswer, turnRow, renderTurnFlow, fillToolLogBody, renderMessages, maybeLoadChanges, loadChanges, clearChanges, renderChangesCard, openChangeDiff } from './toollog.js';
import { toggleTodos, refreshChatBarMeta, trDetail, updateStreamStatus, toggleStreamDetail, showLastPrompt, inferCurrentTodo, mountTeamCard, syncTeamCard, clearGoalSubagents, renderGoalBar } from './teamcard.js';
import { syncFixedLanes, showConnBanner, hideConnBanner, pingInterrupt, clearInterruptPing, beepOnce, showApprovalBar, showQuestionBar } from './approvals.js';
import { refreshSubagents, renderSubagents } from './subagents.js';
import { openPanels, refreshPanels, loadPanel, closePanels, openPlugins, closePlugins } from './panels.js';
import { slashMatch, slashOpen, slashUpdate, loadMcpPrompts, slashPick, slashKey, mergePluginI18n, fanoutNow, auditNow, slashRun, deepResearchNow } from './slash.js';
import { wsCollapsedList, wsIsCollapsed, wsToggleCollapse, newWorkspace, pickWorkspace, openWsBrowser, wsNavigate, wsUp, wsDefault, wsChoose, wsMkdir, newSessionInWorkspace, closeWs } from './workspace.js';
import { api, esc, isProviderModel, assistantLabel, fmtStamp, jv, stateText, stateIcon, stateClass, subRunningOf, stateLabel, stateClassOf, setPill, setKpis, loadModels, fillModelSelect, refreshSessModelOptions, setSessionModel, modelDesc, refreshModels, drawBrowserBtn, browserState, toggleBrowser } from './apistate.js';
import { toggleSessExpand, refreshSessions, toggleSubagents, drawSessions, editTag } from './sessions.js';
import { guessResultLang, stepResultRender, applySessionChrome, langLabel, wrapCodeLines, codeLineSel, copySelLines, imgZoom, imgZoomClose, closeCodeMenu, codeMenu, codePref, setCodePref, applyCodePrefs, copyMsg, copyCode, copyText, toggleCodeFold, applyMsgFold } from './codeux.js';
import { gmShow, openMask, closeMask, gmClose, dlgAlert, dlgConfirm, dlgInjectPrompt, dlgPrompt } from './dialogs.js';
import { setFollowupMode, resetView, load, runtime, stopEvents, stopTask, applyRuntime } from './followload.js';
import { demoApi, apiToken, cardOf, applyCardCollapsed, toggleCard, onCardHeadClick, applyTopbarCollapsed, toggleTopbar } from './cards.js';
import { refreshChat, control, activate, openCopilot, removeSession, fmtCountdown, hideRetryUI, dismissRetryOverlay, showServiceBadge, updateRetryCountdown, fillRetrySwap, swapModelResume, rollbackSession, rollbackSelfUpgrade, showDeleteResult, closeDlg, forceDeleteLocal } from './control.js';
import { enhanceManual, openManual, closeManual, openTask } from './manual.js';
import { mountTimelineIntoTab, syncComposer, liveTab, expandLive, closeLive, showPromptView, continueAgent, autoGrowFollowup, sendFollowup, awaitFollowupReply, updateChannelUI, refreshQueue, editQueueItem, removeQueueItem, thumbsHtml, renderThumbs, removeAttachment, attachPasteHandler, lastSavedAssistant, renderLiveBubble, updateLiveBubble, exportChatMarkdown, editMsg, delMsg, toggleGlobalSearch, gsInput, runGlobalSearch, jumpToMessage, injectMessage, setTaskMode, runTask, waitSessionReady, toggleMore } from './composer.js';
import { themeChoice, resolveTheme, applyTheme, toggleTheme, watchSystemTheme, toggleRail, memInjectCss, memBtn, openMemory, loadMemory, bootBanner, showBoot, shutdownService, restartService, showRestarting, waitServiceBack, showStopped, pollBoot } from './themeboot.js';
import { renderSandbox, loadSandbox, pickSandbox, openSettings, closeSettings, renderSettings, setSettingsTab, collectSettings, saveSettings } from './settings.js';
import { applyTitleVersion, loadVersion } from './version.js';
window.applyTitleVersion = applyTitleVersion;
window.loadVersion = loadVersion;
window.dlgPrompt = dlgPrompt;
window.stopEvents = stopEvents;
window.stopTask = stopTask;
window.applyRuntime = applyRuntime;
window.renderSandbox = renderSandbox;
window.loadSandbox = loadSandbox;
window.pickSandbox = pickSandbox;
window.openSettings = openSettings;
window.closeSettings = closeSettings;
window.renderSettings = renderSettings;
window.setSettingsTab = setSettingsTab;
window.collectSettings = collectSettings;
window.saveSettings = saveSettings;
window.themeChoice = themeChoice;
window.resolveTheme = resolveTheme;
window.applyTheme = applyTheme;
window.toggleTheme = toggleTheme;
window.watchSystemTheme = watchSystemTheme;
window.toggleRail = toggleRail;
window.memInjectCss = memInjectCss;
window.memBtn = memBtn;
window.openMemory = openMemory;
window.loadMemory = loadMemory;
window.bootBanner = bootBanner;
window.showBoot = showBoot;
window.shutdownService = shutdownService;
window.restartService = restartService;
window.showRestarting = showRestarting;
window.waitServiceBack = waitServiceBack;
window.showStopped = showStopped;
window.pollBoot = pollBoot;
window.mountTimelineIntoTab = mountTimelineIntoTab;
window.syncComposer = syncComposer;
window.liveTab = liveTab;
window.expandLive = expandLive;
window.closeLive = closeLive;
window.showPromptView = showPromptView;
window.continueAgent = continueAgent;
window.autoGrowFollowup = autoGrowFollowup;
window.sendFollowup = sendFollowup;
window.awaitFollowupReply = awaitFollowupReply;
window.updateChannelUI = updateChannelUI;
window.refreshQueue = refreshQueue;
window.editQueueItem = editQueueItem;
window.removeQueueItem = removeQueueItem;
window.thumbsHtml = thumbsHtml;
window.renderThumbs = renderThumbs;
window.removeAttachment = removeAttachment;
window.attachPasteHandler = attachPasteHandler;
window.lastSavedAssistant = lastSavedAssistant;
window.renderLiveBubble = renderLiveBubble;
window.updateLiveBubble = updateLiveBubble;
window.exportChatMarkdown = exportChatMarkdown;
window.editMsg = editMsg;
window.delMsg = delMsg;
window.toggleGlobalSearch = toggleGlobalSearch;
window.gsInput = gsInput;
window.runGlobalSearch = runGlobalSearch;
window.jumpToMessage = jumpToMessage;
window.injectMessage = injectMessage;
window.setTaskMode = setTaskMode;
window.runTask = runTask;
window.waitSessionReady = waitSessionReady;
window.toggleMore = toggleMore;
window.enhanceManual = enhanceManual;
window.openManual = openManual;
window.closeManual = closeManual;
window.openTask = openTask;
window.refreshChat = refreshChat;
window.control = control;
window.activate = activate;
window.openCopilot = openCopilot;
window.removeSession = removeSession;
window.fmtCountdown = fmtCountdown;
window.hideRetryUI = hideRetryUI;
window.dismissRetryOverlay = dismissRetryOverlay;
window.showServiceBadge = showServiceBadge;
window.updateRetryCountdown = updateRetryCountdown;
window.fillRetrySwap = fillRetrySwap;
window.swapModelResume = swapModelResume;
window.rollbackSession = rollbackSession;
window.rollbackSelfUpgrade = rollbackSelfUpgrade;
window.showDeleteResult = showDeleteResult;
window.closeDlg = closeDlg;
window.forceDeleteLocal = forceDeleteLocal;
window.demoApi = demoApi;
window.apiToken = apiToken;
window.cardOf = cardOf;
window.applyCardCollapsed = applyCardCollapsed;
window.toggleCard = toggleCard;
window.onCardHeadClick = onCardHeadClick;
window.applyTopbarCollapsed = applyTopbarCollapsed;
window.toggleTopbar = toggleTopbar;
window.setFollowupMode = setFollowupMode;
window.resetView = resetView;
window.load = load;
window.runtime = runtime;
window.gmShow = gmShow;
window.openMask = openMask;
window.closeMask = closeMask;
window.gmClose = gmClose;
window.dlgAlert = dlgAlert;
window.dlgConfirm = dlgConfirm;
window.dlgInjectPrompt = dlgInjectPrompt;
window.guessResultLang = guessResultLang;
window.stepResultRender = stepResultRender;
window.applySessionChrome = applySessionChrome;
window.langLabel = langLabel;
window.wrapCodeLines = wrapCodeLines;
window.codeLineSel = codeLineSel;
window.copySelLines = copySelLines;
window.imgZoom = imgZoom;
window.imgZoomClose = imgZoomClose;
window.closeCodeMenu = closeCodeMenu;
window.codeMenu = codeMenu;
window.codePref = codePref;
window.setCodePref = setCodePref;
window.applyCodePrefs = applyCodePrefs;
window.copyMsg = copyMsg;
window.copyCode = copyCode;
window.copyText = copyText;
window.toggleCodeFold = toggleCodeFold;
window.applyMsgFold = applyMsgFold;
window.toggleSessExpand = toggleSessExpand;
window.refreshSessions = refreshSessions;
window.toggleSubagents = toggleSubagents;
window.drawSessions = drawSessions;
window.editTag = editTag;
window.api = api;
window.esc = esc;
window.isProviderModel = isProviderModel;
window.assistantLabel = assistantLabel;
window.fmtStamp = fmtStamp;
window.jv = jv;
window.stateText = stateText;
window.stateIcon = stateIcon;
window.stateClass = stateClass;
window.subRunningOf = subRunningOf;
window.stateLabel = stateLabel;
window.stateClassOf = stateClassOf;
window.setPill = setPill;
window.setKpis = setKpis;
window.loadModels = loadModels;
window.fillModelSelect = fillModelSelect;
window.refreshSessModelOptions = refreshSessModelOptions;
window.setSessionModel = setSessionModel;
window.modelDesc = modelDesc;
window.refreshModels = refreshModels;
window.drawBrowserBtn = drawBrowserBtn;
window.browserState = browserState;
window.toggleBrowser = toggleBrowser;
window.wsCollapsedList = wsCollapsedList;
window.wsIsCollapsed = wsIsCollapsed;
window.wsToggleCollapse = wsToggleCollapse;
window.newWorkspace = newWorkspace;
window.pickWorkspace = pickWorkspace;
window.openWsBrowser = openWsBrowser;
window.wsNavigate = wsNavigate;
window.wsUp = wsUp;
window.wsDefault = wsDefault;
window.wsChoose = wsChoose;
window.wsMkdir = wsMkdir;
window.newSessionInWorkspace = newSessionInWorkspace;
window.closeWs = closeWs;
window.slashMatch = slashMatch;
window.slashOpen = slashOpen;
window.slashUpdate = slashUpdate;
window.loadMcpPrompts = loadMcpPrompts;
window.slashPick = slashPick;
window.slashKey = slashKey;
window.mergePluginI18n = mergePluginI18n;
window.fanoutNow = fanoutNow;
window.auditNow = auditNow;
window.slashRun = slashRun;
window.deepResearchNow = deepResearchNow;
window.openPanels = openPanels;
window.refreshPanels = refreshPanels;
window.loadPanel = loadPanel;
window.closePanels = closePanels;
window.openPlugins = openPlugins;
window.closePlugins = closePlugins;
window.refreshSubagents = refreshSubagents;
window.renderSubagents = renderSubagents;
window.syncFixedLanes = syncFixedLanes;
window.showConnBanner = showConnBanner;
window.hideConnBanner = hideConnBanner;
window.pingInterrupt = pingInterrupt;
window.clearInterruptPing = clearInterruptPing;
window.beepOnce = beepOnce;
window.showApprovalBar = showApprovalBar;
window.showQuestionBar = showQuestionBar;
window.toggleTodos = toggleTodos;
window.refreshChatBarMeta = refreshChatBarMeta;
window.trDetail = trDetail;
window.updateStreamStatus = updateStreamStatus;
window.toggleStreamDetail = toggleStreamDetail;
window.showLastPrompt = showLastPrompt;
window.inferCurrentTodo = inferCurrentTodo;
window.mountTeamCard = mountTeamCard;
window.syncTeamCard = syncTeamCard;
window.clearGoalSubagents = clearGoalSubagents;
window.renderGoalBar = renderGoalBar;
window.toggleToolLog = toggleToolLog;
window.renderToolLog = renderToolLog;
window.loadTurns = loadTurns;
window.summarizeAnswer = summarizeAnswer;
window.turnRow = turnRow;
window.renderTurnFlow = renderTurnFlow;
window.fillToolLogBody = fillToolLogBody;
window.renderMessages = renderMessages;
window.maybeLoadChanges = maybeLoadChanges;
window.loadChanges = loadChanges;
window.clearChanges = clearChanges;
window.renderChangesCard = renderChangesCard;
window.openChangeDiff = openChangeDiff;
window.fillStepTools = fillStepTools;
window.stepMatch = stepMatch;
window.collapseTransient = collapseTransient;
window.renderSteps = renderSteps;
window.renderStepsMaybe = renderStepsMaybe;
window.flushStepsIfPending = flushStepsIfPending;
window.stepView = stepView;
window.svgIcon = svgIcon;
window.toolLabel = toolLabel;
window.stepRowEl = stepRowEl;
window.renderStream = renderStream;
window.pinChatBottom = pinChatBottom;
window.scrollChatToEnd = scrollChatToEnd;
window.stashView = stashView;
window.restoreView = restoreView;
window.nearestScroller = nearestScroller;
window.tryParseJson = tryParseJson;
window.jsonViewBox = jsonViewBox;
window.actionCardHtml = actionCardHtml;
window.renderJsonReply = renderJsonReply;
window.renderAssistantBody = renderAssistantBody;
window.msgEl = msgEl;
window.codeBlockHtml = codeBlockHtml;
window.fileCardHtml = fileCardHtml;
window.mdToHtml = mdToHtml;
window.exportTimeline = exportTimeline;
window.chatFindInput = chatFindInput;
window.applyFind = applyFind;
window.focusHit = focusHit;
window.findStep = findStep;
window.toggleChatFind = toggleChatFind;
window.highlight = highlight;
window.toast = toast;
window.bindAtMenu = bindAtMenu;
window.atUpdate = atUpdate;
window.atPick = atPick;
window.atClose = atClose;

// 共享可变状态（现在是空的，随块迁过来再填）
window.__S = S;

// 老代码在用的全局入口（app.js 里 startScope()/stopScope() 的调用点照旧工作）
window.startScope = startScope;
window.stopScope = stopScope;
window.scopeFrame = scopeFrame;   // 调试/审计脚本有用（量波形用）

// ── 初始化期的 DOM 事件绑定（模块执行时 DOM 已就绪）────────────────────
// ⚠ 加载顺序教训（2026-10-28 实测）：模块是 deferred，**晚于**经典 app.js 执行。
// 所以**初始化期的 DOM 事件绑定必须在这里做**（本模块执行时 DOM 已就绪），
// 不能留在 app.js 的启动 IIFE 里调模块函数（那时 window 上还没有）。
try {
  bindAtMenu(document.getElementById('followup'));
try { mountTeamCard(); } catch (e) { console.warn('mountTeamCard 失败', e); }
} catch (e) {
  console.warn('bindAtMenu 失败', e);
}

// ── 启动（原 app.js 末尾的启动 IIFE；模块 deferred 完才跑，window 绑定已就位）──
function bootInit(){
try{applyTheme(themeChoice(),false);watchSystemTheme()}catch(e){}
  try{loadMcpPrompts()}catch(e){}
  /* ── 启动：API 不可达时进入演示模式（仅用于界面预览） ── */
  document.getElementById('sessModel').onchange=e=>setSessionModel(e.target.value);
  attachPasteHandler('task','task');
  attachPasteHandler('followup','follow');
  /* 输入框自动长高：input 覆盖键入/粘贴/删除，切回会话时再校一次 */
  (function bindFollowupAutoGrow(){
    const el=document.getElementById('followup');
    if(!el) return;
    el.addEventListener('input',autoGrowFollowup);
    document.addEventListener('visibilitychange',autoGrowFollowup);
    // @ 文件弹层：状态与绑定都在 js/main.js + js/atmenu.js（模块 deferred 后才可用）
  })();
  /* 旧实现保留在下方（不删）：原 bindFollowupAutoGrow 只绑了 input/visibilitychange */
  function bindFollowupAutoGrowLegacy(){
    const el=document.getElementById('followup');
    if(!el) return;
    el.addEventListener('input',autoGrowFollowup);
    document.addEventListener('visibilitychange',autoGrowFollowup);
  }
  
  (async()=>{try{await api('/v1/agent/sessions');hideConnBanner()}
   catch(e){
    // 以前 fetch 网络失败与 401 落进同一个 catch，一律 DEMO=true → 界面显示硬编码假数据，
    // 用户点"启动任务"其实不发任何请求却看到"任务已建立"（最严重的误导性成功）。
    // 现在按类型分流，且**只有显式 ?demo=1 才允许进演示模式**。
    const isAuth=(e&&e.status===401);
    const isNet=(e instanceof TypeError)||!/\b\d{3}\b/.test(String(e&&e.message||e));
    if(isAuth){showConnBanner(tr('需要 api_token：')+(e.detail||tr('请输入访问令牌')),false)}
    else if(isNet){showConnBanner(tr('无法连接本地服务（127.0.0.1:8765 是否在运行？）'),false)}
    else{showConnBanner(tr('加载会话失败：')+(e.text||e),false)}
    if(location.search.includes('demo=1')){DEMO=true;
     document.getElementById('demoBadge').style.display='block';
     document.getElementById('demoBadge').textContent=tr('演示模式（?demo=1）——数据为本地假数据')}
    }
   applyI18n(document);   // 静态界面按当前语言渲染（data-i18n + 静态 DOM 直译）
   applyLangButton();
   loadVersion();mountTimelineIntoTab();applyCardCollapsed();applyTopbarCollapsed();  // mountTeamCard 改由 js/main.js 在模块加载后调用
   loadModels();loadSandbox();browserState();mergePluginI18n(true);
   refreshSessions().then(()=>{if(DEMO&&sessCache.length&&!current)load(sessCache[0].id)});
   setInterval(()=>{refreshSessions();runtime();refreshQueue()},1000)})();
  /* 流式期间加快刷新：常规 1s 太慢，看起来是一秒一跳；"正在发送/流式返回/等待回答"
     时用 350ms，让内容真的像在逐字增长（非流式阶段仍走上面的 1s）。 */
  const FAST_PHASES=['api_sending','api_streaming','waiting','sending'];
  (function fastTick(){
   setTimeout(()=>{
    try{
     // 有 SSE 长连接时不必快速轮询（推送已即时到达）；断开后才退回 350ms
     if(!evtSrc&&lastRuntime&&FAST_PHASES.indexOf(String(lastRuntime.phase||''))>=0){
      runtime();refreshQueue()}}catch(e){}
    fastTick()},350)})();
  
}
// 原 app.js 的启动调用点：DOM 就绪后统一跑
if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',bootInit)}else{bootInit()}
