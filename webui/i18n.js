/* ── i18n：简 / 繁 / 英 ────────────────────────────────────────────────
   设计取舍：
   1. **以中文原文当 key**（t('取消任务')）。代码保持可读，缺翻译时自然回落中文，
      也不会出现"key 对不上导致界面空白"这种 i18n 常见事故。
   2. 英文手写；**繁体由简繁转换自动生成**（短语表优先、字符表兜底），
      另加台湾用语覆盖表（記憶體/檔案/設定/網路…）——比逐条手写省一半工作量，
      质量对界面文案足够；确实需要精修时可往 TW 覆盖表里加。
   3. 只翻译**展示用**文案。判定逻辑里出现的字符串（比较/包含/映射的键）一律不翻，
      否则切语言会把行为改坏。
   4. 后端与 Agent 协议文案（系统提示词、工具描述、步骤 reason/错误原文）保持中文，
      它们与协议和评测绑定；主要 API 的 note 由后端带 note_code，前端本地化。
   ─────────────────────────────────────────────────────────────────── */
const I18N_EN = {
  // ── 侧栏 / 全局 ──
  "收起/展开侧栏": "Collapse / expand sidebar",
  "＋ 新任务": "＋ New task",
  "筛选会话": "Filter sessions",
  "筛选…": "Filter…",
  "使用手册": "Manual",
  "切换自动化浏览器窗口的显示/隐藏": "Show / hide the automation browser window",
  "主题：深色（点击切换）": "Theme: dark (click to cycle)",
  "跨会话搜索对话内容": "Search across sessions",
  "终止服务 / 退出（优雅停机；正在跑的任务会中断，状态已落库，重启后可恢复）":
    "Stop service / quit (graceful shutdown; running tasks are interrupted, state is persisted and can be resumed after restart)",
  "跨会话搜索": "Search all sessions",
  "跨会话搜索对话内容…（Esc 关闭）": "Search all conversations… (Esc to close)",
  "从左侧选择一个 Session，或发起新任务": "Pick a session on the left, or start a new task",
  "提示：时间线中的": "Tip: in the timeline,",
  "查看结果": "view result",
  "可展开结构化 JSON": "expands structured JSON",
  "点击复制会话 ID": "Click to copy session ID",
  "本会话使用的模型": "Model used by this session",
  "当前会话使用的模型（下一回合生效，服务重启后仍沿用）":
    "Model for this session (takes effect next turn, persists across restarts)",
  "刷新模型列表（重新扫描 Copilot 菜单）": "Refresh model list (re-scan the Copilot menu)",
  "暂停": "Pause",
  "恢复": "Resume",
  "压缩上下文": "Compact context",
  "继续执行": "Continue",
  "切换到页面": "Switch to page",
  "打开会话 ↗": "Open chat ↗",
  "取消任务": "Cancel task",
  "更多 ▾": "More ▾",
  "归档": "Archive",
  "回滚任务改动…": "Roll back task changes…",
  "回滚服务自升级…": "Roll back self-upgrade…",
  "仅删除本地记录": "Delete local record only",
  "同时删除 Copilot 会话": "Also delete Copilot conversations",
  "优雅停止本服务进程（正在跑的任务会中断，状态已落库，重启后可恢复）":
    "Gracefully stop the service process (running tasks are interrupted; state is persisted)",
  "⏻ 终止服务 / 退出": "⏻ Stop service / quit",
  // ── KPI / 面板 ──
  "状态": "State",
  "总步骤": "Total steps",
  "当前会话步骤": "Steps this session",
  "会话切换": "Session switches",
  "TOKEN 估算": "Token estimate",
  "当前执行状态": "Activity",
  "当前回答": "Current answer",
  "最近发送内容": "Last prompt",
  "继续对话": "Chat",
  "查看该会话首回合发给模型的完整提示词（含系统提示词、工具清单、项目约定）":
    "Show the full first-turn prompt (system prompt, tool list, project conventions)",
  "📄 初始提示词": "📄 Initial prompt",
  "放大查看当前面板": "Expand panel",
  "⤢ 放大": "⤢ Expand",
  "等待回答…": "Waiting for answer…",
  "继续当前 Session 对话 ·": "Continue this session ·",
  "强行插入指令（运行中即时生效）": "inject an instruction (applies immediately while running)",
  "把当前对话（含附件引用与时间）导出为 Markdown 文件":
    "Export this conversation (with attachment refs and timestamps) as Markdown",
  "⬇ 导出 MD": "⬇ Export MD",
  "在对话里搜索（高亮命中，Enter 跳转下一条）": "Search in this chat (highlight hits, Enter for next)",
  "🔍 搜索": "🔍 Search",
  "运行中插入指令：Agent 下一回合读取并优先执行，不必等这轮跑完（快捷键 Ctrl+J）":
    "Inject an instruction while running: the agent reads it next turn (Ctrl+J)",
  "⚡ 强行插入": "⚡ Inject",
  "Agent 继续执行": "Agent continues",
  "普通对话": "Plain chat",
  "在对话中查找": "Find in chat",
  "搜索对话内容…（Enter 下一条，Shift+Enter 上一条）":
    "Search this conversation… (Enter next, Shift+Enter previous)",
  "上一条": "Previous",
  "下一条": "Next",
  "关闭搜索": "Close search",
  "输入消息，Enter 发送，Shift+Enter 换行；可直接粘贴截图。Ctrl+J = 运行中强行插入指令":
    "Type a message — Enter to send, Shift+Enter for a newline; paste screenshots directly. Ctrl+J = inject while running",
  "发送": "Send",
  "发送中": "Sending",
  // ── 时间线 ──
  "步骤时间线": "Step timeline",
  "全部工具": "All tools",
  "全部状态": "All states",
  "仅成功": "Succeeded only",
  "仅失败/被拦": "Failed / blocked",
  "仅事件": "Events only",
  "导出完整时间线 JSON（含每步参数、结果与错误现场），便于调试":
    "Export the full timeline JSON (per-step arguments, results, errors) for debugging",
  "⬇ 导出": "⬇ Export",
  "渲染": "Rendered",
  "JSON": "JSON",
  "没有符合筛选条件的步骤": "No steps match the filter",
  // ── 代码块 ──
  "复制代码": "Copy code",
  "更多": "More",
  "复制选中 0 行": "Copy 0 lines",
  "点击选中整行": "Click to select the line",
  "点击放大": "Click to zoom",
  "复制代码 / 下载 / 行号 / 换行 / 折叠": "Copy / download / line numbers / wrap / fold",
  "复制这条消息": "Copy this message",
  "编辑这条消息（只改本地历史）": "Edit this message (local history only)",
  "删除这条消息（只改本地历史）": "Delete this message (local history only)",
  "📋 复制代码": "📋 Copy code",
  "⬇ 下载为文件": "⬇ Download as file",
  "显示行号": "Show line numbers",
  "隐藏行号": "Hide line numbers",
  "自动换行": "Wrap lines",
  "关闭换行": "Disable wrapping",
  "展开全部": "Expand all",
  "折叠代码": "Fold code",
  "复制": "Copy",
  "已复制 ✓": "Copied ✓",
  "展开": "Expand",
  "收起": "Collapse",
  "展开全文": "Show full text",
  "代码": "Code",
  "纯文本": "Plain text",
  "图片加载失败": "Image failed to load",
  // ── 状态名 ──
  "运行中": "Running",
  "已暂停": "Paused",
  "等待服务": "Waiting for service",
  "就绪": "Ready",
  "已完成": "Completed",
  "失败": "Failed",
  "已取消": "Cancelled",
  "已过期": "Expired",
  "已归档": "Archived",
  // ── 角色 / 通用 ──
  "你": "You",
  "助手": "Assistant",
  "队列": "Queue",
  "错误": "Error",
  "临时": "temporary",
  "正在生成": "Generating",
  "提示": "Notice",
  "确认": "Confirm",
  "取消": "Cancel",
  "保存": "Save",
  "删除": "Delete",
  "插入": "Insert",
  "确定": "OK",
  "关闭": "Close",
  // ── 对话框 / 提示 ──
  "强行插入": "Inject instruction",
  "插入指令": "Inject instruction",
  "编辑消息": "Edit message",
  "删除消息": "Delete message",
  "会话标签": "Session tag",
  "设置标签": "Set tag",
  "终止服务": "Stop service",
  "需要访问令牌": "Access token required",
  "本服务启用了 api_token，请输入访问令牌（只保存在本机浏览器）":
    "This service requires api_token — enter it (stored only in this browser)",
  "需要授权": "Authorization required",
  "授权执行": "Authorize once",
  "暂不授权": "Not now",
  "立即遵守": "Obey immediately",
  "整轮重排": "Re-plan the turn",
  "马上生效：追加到当前执行中，并抢占批量里剩余的动作":
    "Takes effect now: appended to the running step and preempts the rest of the batch",
  "当前回合作废，按新指令重新规划这一轮": "Discards this turn and re-plans it with the new instruction",
  "插入方式": "Insertion mode",
  // ── 常用 toast / 提示 ──
  "请先选择 Session": "Select a session first",
  "消息不能为空": "Message cannot be empty",
  "操作失败: ": "Operation failed: ",
  "发送失败: ": "Send failed: ",
  "插入失败: ": "Inject failed: ",
  "编辑失败: ": "Edit failed: ",
  "删除失败: ": "Delete failed: ",
  "搜索失败：": "Search failed: ",
  "跳转失败: ": "Jump failed: ",
  "设置标签失败: ": "Setting tag failed: ",
  "加载会话失败：": "Loading session failed: ",
  "标签已更新": "Tag updated",
  "已取消标签": "Tag cleared",
  "已删除": "Deleted",
  "已更新": "Updated",
  "已恢复": "Resumed",
  "暂不授权": "Not now",
  "已请求启动浏览器，稍后自动复查": "Browser start requested; will re-check shortly",
  "健康检查失败": "Health check failed",
  "连接已断开": "Connection lost",
  "服务已终止": "Service stopped",
  "本页可以关闭了；需要时重新运行": "You can close this page; restart with",
  "启动": "start",
  "启动中…": "Starting…",
  "切换中…": "Switching…",
  "切换失败: ": "Switch failed: ",
  "刷新失败: ": "Refresh failed: ",
  "没有匹配的 Session": "No matching session",
  "没有匹配的对话内容": "No matching conversation content",
  "搜索中…": "Searching…",
  "当前没有人工续聊记录。下面发送的消息会发给该 Session 正在使用的模型。":
    "No follow-up messages yet. Messages you send below go to this session's current model.",
  "我": "Me",
  "模型": "Model",
  "导出时间": "Exported at",
  "消息数": "Messages",
  "会话": "Session",
  "标签：": "Tag: ",
  "未分组": "Ungrouped",
  "（没有可渲染的字段）": "(no renderable fields)",
  "未命名": "Untitled",
  "图片": "Image",
  "附件": "Attachment",
  "已排队": "Queued",
  "等待处理…": "waiting…",
  "前置": "ahead: ",
  "条消息": "message(s)",
  "打断当前计划、插入新要求": "interrupt the plan and insert a new requirement",
  // ── 后端 note_code（后端仍回中文原文，这里按 code 本地化）──
  "note.shutdown_pending": "Shutdown already in progress",
  "note.shutdown_soon": "The service is stopping. Running tasks are interrupted (state is persisted and can be resumed after restart).",
  "note.resume_terminal": "Session state is {state}; resume is not supported",
  "note.inject_step": "Queued as next-step: takes effect immediately and the rest of the batch is abandoned",
  "note.inject_turn": "Inserted and the current turn was interrupted: the agent continues with the new instruction",
  "note.inject_idle": "Session is idle, so it was queued as a follow-up (immediate only applies while running)",
  "note.approval_granted": "Authorized once; this authorization is single-use",
};

/* 把后端返回的 {note_code, note_vars} 本地化；没有对应翻译时回落后端中文原文 */
function noteText(data, fallback) {
  const code = data && data.note_code;
  if (!code) return (data && data.note) || fallback || "";
  const key = "note." + code;
  const val = t(key, (data && data.note_vars) || {});
  return val === key ? ((data && data.note) || fallback || "") : val;
}

/* 台湾用语覆盖表（在简繁转换之后应用，修正词汇而非仅字形） */
const I18N_TW_OVERRIDE = {
  "文件": "檔案", "文件夹": "資料夾", "保存": "儲存", "设置": "設定", "配置": "設定",
  "内存": "記憶體", "网络": "網路", "加载": "載入", "刷新": "重新整理", "视频": "影片",
  "软件": "軟體", "硬件": "硬體", "打印": "列印", "默认": "預設", "支持": "支援",
  "信息": "資訊", "登录": "登入", "退出": "結束", "撤销": "復原", "回滚": "還原",
  "搜索": "搜尋", "查找": "尋找", "粘贴": "貼上", "复制": "複製", "版本": "版本",
  "菜单": "選單", "选项": "選項", "标签": "標籤", "分组": "分組", "队列": "佇列",
  "授权": "授權", "权限": "權限", "验证": "驗證", "校验": "驗證", "质量": "品質",
  "视频": "影片", "连接": "連線", "断开": "中斷", "重试": "重試", "终止": "終止",
  "排队": "排隊", "处理": "處理", "请求": "請求", "响应": "回應", "进度": "進度",
  "步骤": "步驟", "时间": "時間", "服务": "服務", "错误": "錯誤", "失败": "失敗",
  "删除": "刪除", "插入": "插入", "创建": "建立", "新建": "新增", "打开": "開啟",
  "关闭": "關閉", "显示": "顯示", "隐藏": "隱藏", "运行": "執行", "执行": "執行",
  // 词汇级修正：简繁不是一对一（复→複/復、导→導/匯），按词修才准
  "恢复": "恢復", "重复": "重複", "复制": "複製", "复杂": "複雜", "答复": "答覆",
  "导出": "匯出", "导入": "匯入", "停机": "停機", "强行": "強行", "手册": "手冊",
  "后台": "後台", "进程": "行程", "线程": "執行緒", "缓存": "快取", "屏幕": "螢幕",
  "视频": "影片", "音频": "音訊", "鼠标": "滑鼠", "键盘": "鍵盤", "粘贴": "貼上",
  "搜索": "搜尋", "刷新": "重新整理", "加载": "載入", "卸载": "解除安裝",
};

/* 简→繁字符表（只收界面里会出现的常用字；短语表优先） */
const S2T_CHARS = (() => {
  const pairs = [
    ["设","設"],["计","計"],["会","會"],["话","話"],["题","題"],["标","標"],["签","籤"],
    ["删","刪"],["除","除"],["复","複"],["制","製"],["时","時"],["间","間"],["务","務"],
    ["步","步"],["骤","驟"],["错","錯"],["误","誤"],["败","敗"],["试","試"],["验","驗"],
    ["证","證"],["权","權"],["限","限"],["授","授"],["队","隊"],["列","列"],["排","排"],
    ["进","進"],["度","度"],["请","請"],["求","求"],["响","響"],["应","應"],["连","連"],
    ["断","斷"],["网","網"],["络","絡"],["内","內"],["存","存"],["载","載"],["入","入"],
    ["览","覽"],["图","圖"],["档","檔"],["夹","夾"],["软","軟"],["件","件"],["统","統"],
    ["开","開"],["关","關"],["闭","閉"],["显","顯"],["旧","舊"],["后","後"],["台","台"],
    ["历","歷"],["发","發"],["送","送"],["收","收"],["读","讀"],["写","寫"],["编","編"],
    ["辑","輯"],["输","輸"],["选","選"],["项","項"],["菜","菜"],["单","單"],["体","體"],
    ["验","驗"],["监","監"],["视","視"],["频","頻"],["录","錄"],["频","頻"],["静","靜"],
    ["页","頁"],["面","面"],["栏","欄"],["钮","鈕"],["触","觸"],["点","點"],["击","擊"],
    ["双","雙"],["单","單"],["语","語"],["言","言"],["简","簡"],["繁","繁"],["译","譯"],
    ["翻","翻"],["译","譯"],["词","詞"],["汇","彙"],["义","義"],["类","類"],["型","型"],
    ["层","層"],["级","級"],["队","隊"],["组","組"],["织","織"],["结","結"],["构","構"],
    ["线","線"],["程","程"],["运","運"],["转","轉"],["换","換"],["变","變"],["动","動"],
    ["态","態"],["状","狀"],["归","歸"],["档","檔"],["备","備"],["份","份"],["还","還"],
    ["原","原"],["撤","撤"],["销","銷"],["锁","鎖"],["释","釋"],["缓","緩"],["冲","衝"],
    ["区","區"],["块","塊"],["链","鏈"],["接","接"],["续","續"],["暂","暫"],["停","停"],
    ["预","預"],["测","測"],["估","估"],["算","算"],["耗","耗"],["计","計"],["数","數"],
    ["据","據"],["库","庫"],["表","表"],["项","項"],["值","值"],["键","鍵"],["码","碼"],
    ["编","編"],["号","號"],["长","長"],["短","短"],["宽","寬"],["窄","窄"],["满","滿"],
    ["线","線"],["图","圖"],["表","表"],["格","格"],["式","式"],["样","樣"],["颜","顏"],
    ["色","色"],["深","深"],["浅","淺"],["对","對"],["齐","齊"],["缩","縮"],["放","放"],
    ["滚","滾"],["动","動"],["条","條"],["栏","欄"],["卡","卡"],["片","片"],["模","模"],
    ["块","塊"],["窗","窗"],["口","口"],["弹","彈"],["层","層"],["遮","遮"],["罩","罩"],
    // 常用字补齐（界面词汇覆盖面：命令、导航、状态、操作）
    ["当","當"],["导","導"],["优","優"],["筛","篩"],["询","詢"],["让","讓"],["这","這"],
    ["说","說"],["认","認"],["为","為"],["于","於"],["与","與"],["里","裡"],["并","並"],
    ["从","從"],["个","個"],["们","們"],["来","來"],["该","該"],["无","無"],["见","見"],
    ["观","觀"],["规","規"],["则","則"],["么","麼"],["处","處"],["议","議"],["论","論"],
    ["员","員"],["团","團"],["划","劃"],["择","擇"],["杂","雜"],["顺","順"],["储","儲"],
    ["盘","盤"],["终","終"],["约","約"],["续","續"],["断","斷"],["义","義"],["众","眾"],
    ["产","產"],["场","場"],["长","長"],["门","門"],["问","問"],["间","間"],["闻","聞"],
    ["马","馬"],["鸟","鳥"],["鱼","魚"],["龙","龍"],["风","風"],["云","雲"],["电","電"],
    ["头","頭"],["边","邊"],["过","過"],["进","進"],["远","遠"],["还","還"],["连","連"],
    ["运","運"],["达","達"],["适","適"],["递","遞"],["选","選"],["逻","邏"],["辑","輯"],
    ["钥","鑰"],["锁","鎖"],["错","錯"],["锦","錦"],["钟","鐘"],["银","銀"],["钱","錢"],
    ["额","額"],["题","題"],["类","類"],["页","頁"],["顶","頂"],["项","項"],["须","須"],
    ["预","預"],["领","領"],["频","頻"],["颗","顆"],["题","題"],["风","風"],["飞","飛"],
    ["饰","飾"],["饭","飯"],["饮","飲"],["馆","館"],["验","驗"],["骑","騎"],["驱","驅"],
    ["骤","驟"],["骨","骨"],["高","高"],["鱼","魚"],["鲜","鮮"],["鸣","鳴"],["鸦","鴉"],
    ["龄","齡"],["齐","齊"],["齿","齒"],["龙","龍"],["龟","龜"],["龄","齡"],["龟","龜"],
    ["机","機"],["强","強"],["册","冊"],["复","復"],["汇","匯"],["内","內"],["为","為"],
    ["启","啟"],["确","確"],["谢","謝"],["彻","徹"],["厉","厲"],["严","嚴"],["够","夠"],
  ];
  const map = new Map(pairs);
  return map;
})();

function toTraditional(text) {
  let s = String(text == null ? "" : text);
  // 先按短语覆盖（词汇级），再做字形转换
  Object.keys(I18N_TW_OVERRIDE).forEach((k) => {
    if (s.includes(k)) s = s.split(k).join("\u0000" + I18N_TW_OVERRIDE[k] + "\u0000");
  });
  let out = "";
  for (const ch of s) out += S2T_CHARS.get(ch) || ch;
  return out.split("\u0000").join("");
}

const I18N = {
  zh: { label: "简", name: "简体中文" },
  tw: { label: "繁", name: "繁體中文" },
  en: { label: "EN", name: "English" },
};
const I18N_ORDER = ["zh", "tw", "en"];

function currentLang() {
  try {
    const v = localStorage.getItem("cbp-lang");
    if (v && I18N[v]) return v;
  } catch (e) {}
  return "zh";
}
function setLang(lang) {
  if (!I18N[lang]) return;
  try { localStorage.setItem("cbp-lang", lang); } catch (e) {}
  applyI18n(document);
  applyLangButton();
  try { rerenderForLang(); } catch (e) {}
}
function cycleLang() {
  const cur = currentLang();
  setLang(I18N_ORDER[(I18N_ORDER.indexOf(cur) + 1) % I18N_ORDER.length]);
}
/* 取词：中文原文当 key；缺翻译回落原文（永不出现空白/占位符） */
function t(key, vars) {
  const lang = currentLang();
  let s = String(key == null ? "" : key);
  if (lang === "en") s = I18N_EN[s] || s;
  else if (lang === "tw") s = toTraditional(s);
  if (vars) Object.keys(vars).forEach((k) => { s = s.split("{" + k + "}").join(String(vars[k])); });
  return s;
}
/* 静态界面：data-i18n / data-i18n-title / data-i18n-ph
   另外做一遍"静态 DOM 直译"：不必给每个静态元素加属性，直接把叶子节点的文本与
   常见属性按字典替换。**排除动态容器**（对话、时间线、回答、日志等）——
   那些内容里可能有模型输出或用户输入，恰好等于某个 key 就会被误翻。 */
const I18N_SKIP_IDS = new Set([
  "chatlog", "steps", "answer", "prompt", "activity", "queueBox", "gsResults",
  "toasts", "liveBody", "bootText", "sessions",
]);
function i18nSkipped(el) {
  for (let p = el; p; p = p.parentElement) {
    if (p.id && I18N_SKIP_IDS.has(p.id)) return true;
    if (p.dataset && p.dataset.i18nSkip === "1") return true;
  }
  return false;
}
function i18nKnown(key) {
  const lang = currentLang();
  if (lang === "en") return Object.prototype.hasOwnProperty.call(I18N_EN, key);
  return true;   // 繁体的字形转换对任意中文都成立
}
/* 原文存 WeakMap：不能用 dataset（"i18nOrigaria-label" 不是合法的 dataset 属性名） */
const I18N_ORIG = new WeakMap();
function i18nSwapText(el, attr) {
  let store = I18N_ORIG.get(el);
  if (!store) { store = {}; I18N_ORIG.set(el, store); }
  if (store[attr] === undefined) {
    store[attr] = attr === "text" ? el.textContent : (el.getAttribute(attr) || "");
  }
  const src = store[attr];
  const key = String(src || "").trim();
  if (!key || !i18nKnown(key)) return;
  const val = t(key);
  if (attr === "text") {
    const next = src.replace(key, val);
    if (el.textContent !== next) el.textContent = next;
  } else if (el.getAttribute(attr) !== val) {
    el.setAttribute(attr, val);
  }
}
function applyI18n(root) {
  const scope = root || document;
  scope.querySelectorAll("[data-i18n]").forEach((el) => {
    const v = t(el.dataset.i18n);
    if (el.textContent !== v) el.textContent = v;
  });
  scope.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
  scope.querySelectorAll("[data-i18n-ph]").forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
  scope.querySelectorAll("[data-i18n-aria]").forEach((el) => {
    el.setAttribute("aria-label", t(el.dataset.i18nAria));
  });
  // 静态 DOM 直译
  const all = scope.querySelectorAll(
    "[title],[placeholder],[aria-label],button,summary,option,label,th,span,div,b,small,code,kbd");
  all.forEach((el) => {
    if (i18nSkipped(el)) return;
    ["title", "placeholder", "aria-label"].forEach((a) => {
      if (el.hasAttribute(a)) i18nSwapText(el, a);
    });
    if (el.children.length === 0 && el.textContent && el.textContent.trim()) i18nSwapText(el, "text");
  });
}
function applyLangButton() {
  const b = document.getElementById("langBtn");
  if (b) {
    b.textContent = I18N[currentLang()].label;
    b.title = "语言 / 語言 / Language：" + I18N[currentLang()].name + "（点击切换）";
  }
}
/* 切语言后重画动态区域（这些区域的内容是 JS 生成的，不在 data-i18n 覆盖内） */
function rerenderForLang() {
  try { if (typeof drawSessions === "function") { sessSig = ""; drawSessions(); } } catch (e) {}
  try { if (typeof renderMessages === "function" && lastMessages.length) renderMessages(lastMessages); } catch (e) {}
  try { if (typeof renderSteps === "function" && lastSteps.length) renderSteps(lastSteps, true); } catch (e) {}
  try { if (typeof refreshQueue === "function") refreshQueue(); } catch (e) {}
  try { if (lastRuntime && typeof applyRuntime === "function") applyRuntime(lastRuntime); } catch (e) {}
  try { if (typeof fillStepTools === "function") fillStepTools(); } catch (e) {}
}
