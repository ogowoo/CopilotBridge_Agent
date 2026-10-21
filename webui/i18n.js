/* ── i18n：简 / 繁 / 英 ────────────────────────────────────────────────
   设计取舍：
   1. **以中文原文当 key**（tr('取消任务')）。代码保持可读，缺翻译时自然回落中文，
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
 "目录": "Contents",
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
  "进行中": "In progress",
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
  // ── 覆盖率审计（scripts/ui_i18n_audit.py）发现的长尾补充 ──
  "重试浏览器":
    "Retry browser",
  "需要在自动化窗口完成 Microsoft 登录":
    "Sign in to Microsoft in the automation window",
  "启动任务":
    "Start task",
  "Agent 执行":
    "Agent run",
  "重新读取 Copilot 模型菜单":
    "Re-read the Copilot model menu",
  "Session ID（可留空）":
    "Session ID (optional)",
  "工作目录":
    "Workspace directory",
  "工作目录（可留空）":
    "Workspace directory (optional)",
  "新任务使用的模型":
    "Model used by the new task",
  "任务内容":
    "Task content",
  "任务内容，例如：分析 workspace 里的项目并生成 TODO；可直接粘贴截图":
    "Task content, e.g. analyse the project under workspace and produce a TODO; you can paste screenshots directly",
  "工作区不允许指向服务安装目录（除非已开启自升级）——否则普通文件工具即可改服务代码，绕过权限门。":
    "The workspace must not point at the service installation directory (unless self-upgrade is enabled) — otherwise ordinary file tools could modify the service code and bypass the permission gate.",
  "按状态筛选":
    "Filter by status",
  "只看某个工具的步骤":
    "Show one tool's steps only",
  "Esc 关闭":
    "Esc to close",
  "未知":
    "unknown",
  " 条":
    " items",
  " 条（筛选）":
    " items (filtered)",
  "语言 / 語言 / Language：":
    "Language: ",
  "（点击切换）":
    " (click to switch)",
  "快速上手":
    "Quick start",
  "浏览器按需启动（无 Copilot 也能用）":
    "On-demand browser (usable without Copilot)",
  "会话控制":
    "Session control",
  "任务快照与回滚":
    "Snapshots and rollback",
  "回滚到指定版本":
    "Roll back to a specific version",
  "跟进对话":
    "Follow-up conversation",
  "跟进消息队列":
    "Follow-up message queue",
  "粘贴图片 / 截图":
    "Pasting images / screenshots",
  "模型列表刷新":
    "Refreshing the model list",
  "限流倒计时":
    "Rate-limit countdown",
  "长任务机制":
    "Long-task mechanics",
  "批量动作（省 Copilot 回合）":
    "Batch actions (fewer Copilot turns)",
  "长期记忆（跨会话）":
    "Long-term memory (across sessions)",
  "技能自沉淀（skills）":
    "Self-accumulating skills",
  "界面":
    "Interface",
  "服务自升级（默认关闭）":
    "Service self-upgrade (off by default)",
  "常见问题":
    "FAQ",

  // ── 空态与列表元信息 ──
  "步骤":
    "Steps",
  " 可展开结构化 JSON":
    " expands the structured JSON",

  // ── 弹窗标题 ──
  "发起新任务":
    "New task",
  "📖 使用手册":
    "📖 Manual",

};

/* 把后端返回的 {note_code, note_vars} 本地化；没有对应翻译时回落后端中文原文 */
function noteText(data, fallback) {
  const code = data && data.note_code;
  if (!code) return (data && data.note) || fallback || "";
  const key = "note." + code;
  const val = tr(key, (data && data.note_vars) || {});
  return val === key ? ((data && data.note) || fallback || "") : val;
}

/* ── 长尾补充（面板/记忆/技能/回滚/删除明细/演示数据/HTML 片段）────────── */
Object.assign(I18N_EN, {
  // 审批：授权后时间线要给出正面记录，不再停在"未获授权"
  "已授权": "Authorized",
  // 重启服务
  // 强行插入弹窗（gmShow 默认按钮 + 说明）
  "插入": "Inject",
  "取消": "Cancel",
  "确定": "OK",
  "提示": "Notice",
  "插入指令（运行中即时生效，会立即中断当前回合后按新指令继续）。插入方式决定语义：":
    "Inject an instruction (takes effect immediately while running and interrupts the current turn). The mode decides the semantics:",
  // 对话面板的 ⋯ 菜单（面板附加操作 / 对话区更多操作）
  "面板附加操作（初始提示词 / 最近发送 / 放大）":
    "Panel extras (initial prompt / last prompt / expand)",
  "对话区更多操作（导出 / 搜索 / 强行插入）":
    "Chat actions (export / search / force-inject)",
  // 窄屏会话列表开关
  "会话列表（展开/收起）": "Sessions (expand / collapse)",
  // 窄屏抽屉的遮罩（点一下关闭）
  "关闭会话列表": "Close session list",
  // 授权横幅（一次性 / 本会话）与终止服务确认框
  "属于不可回滚操作；授权仅对本会话有效（会话结束即失效）":
    "is not reversible; the approval lasts for this session only (cleared when the session ends)",
  "属于不可回滚操作；授权仅对这一次执行有效（用后即焚）":
    "is not reversible; the approval covers this single run only (burned after use)",
  "已授权并继续执行（本会话内不再逐次询问）":
    "Approved, resuming (no further prompts within this session)",
  "即将终止本服务（进程退出）：": "About to stop this service (the process will exit):",
  "正在运行的任务会中断（状态已落库，重启后可恢复）":
    "Running tasks will be interrupted (state is saved and can be resumed after restart)",
  "自动化浏览器会被关闭，本页面随即断开":
    "The automation browser will close and this page will disconnect",
  "需要时重新运行 start.py 启动服务": "Run start.py again when you need the service back",
  // 中断类提醒（需要授权 / 等待回答 / 需要人工处理）
  "需要授权：": "Needs approval: ",
  "等待你的回答": "Waiting for your answer",
  "任务已暂停，等待你处理": "Task paused, waiting for you",
  "重启服务": "Restart service",
  // 待办（todo_write 维护的清单，区别于 /plan 计划模式）
  "待办": "Todo",
  "待办清单：Agent 用 todo_write 维护，完成一个勾一个；与 /plan 计划模式无关":
    "Todo list: maintained by the agent via todo_write, one item ticked per completion; unrelated to /plan mode",
  "就地重启本服务（更新代码后用它；正在跑的任务会中断，状态已落库，开着 auto_resume 会自动续跑）":
    "Restart the service in place (use this after changing code; running tasks are interrupted, state is persisted, and auto_resume continues them)",
  "↻ 重启服务": "↻ Restart service",
  "服务正在重启": "Service restarting",
  "正在等待服务恢复（通常几秒），恢复后本页会自动刷新…":
    "Waiting for the service to come back (usually a few seconds); this page refreshes itself when it does…",
  "服务在 90 秒内未恢复，请检查控制台或重新运行 start.py":
    "The service did not come back within 90 s — check the console or run start.py again",
  "note.restart_pending": "Stop/restart already in progress",
  "note.restart_soon": "The service is restarting. Running tasks are interrupted (state is persisted); this page refreshes once the service is back.",
  "已授权，已执行": "Authorized and executed",
  "授权已下达": "Authorization granted",
  "授权已下达，该操作已继续执行": "Authorization granted; the operation continued",
  "（用后即焚）": " (single-use)",
  "（本会话内不再逐次询问）": " (not asked again in this session)",
  "note.approval_session_granted": "Session-wide authorization granted; this tool will not ask again in this session",
  " 个</div>": " item(s)</div>",
  " 个菜单项，无新增）": " menu entries, nothing new)",
  " 字符]</span>": " chars]</span>",
  " 属于不可回滚操作；授权仅对这一次执行有效（用后即焚）": " is irreversible; this authorization applies to this execution only (single-use)",
  " 条消息）": " messages)",
  " 步）": " steps)",
  "(无标记)": "(no marker)",
  "(未知工具)": "(unknown tool)",
  "(空：该会话还没有发出过回合)": "(empty: this session has not sent a turn yet)",
  "<b>目录</b>": "<b>Contents</b>",
  "<div class=\"act-badge ok\">最终答复</div><div class=\"md-body\">": "<div class=\"act-badge ok\">Final answer</div><div class=\"md-body\">",
  "<div class=\"act-badge warn\">JSON 解析失败（原文）</div>": "<div class=\"act-badge warn\">JSON parse failed (raw text)</div>",
  "<div class=\"act-badge\">JSON 回复</div>": "<div class=\"act-badge\">JSON reply</div>",
  "<div class=\"act-badge\">批量动作 · ": "<div class=\"act-badge\">Batch actions · ",
  "<div class=\"dmsg\">没有远程删除明细。</div>": "<div class=\"dmsg\">No remote deletion details.</div>",
  "<div class=\"mem-empty\">没有匹配的记忆。Agent 完成任务时会自动沉淀，也可以直接让它「记到长期记忆」。</div>":
    "<div class=\"mem-empty\">No matching memories. The agent persists them automatically when finishing tasks — or just ask it to remember something.</div>",
  "<span class=\"mem-pin\">★ 置顶</span>": "<span class=\"mem-pin\">★ Pinned</span>",
  "<span class=\"nl\">[Base64 内容，共 ": "<span class=\"nl\">[Base64 content, ",
  "Agent 回合 a1b2c3d4e5f6 已确认，等待 Copilot 回答": "Agent turn a1b2c3d4e5f6 confirmed, waiting for Copilot's answer",
  "Copilot 未就绪：": "Copilot not ready: ",
 "（{e}）": " ({e})",
  "HTTP 响应为空": "Empty HTTP response",
  "Token/步数/手动触发自动创建新 Copilot 会话": "Token/step count/manual trigger created a new Copilot conversation",
  "URL 对应会话不属于当前 Session，拒绝删除；需要前缀: [CBP-AGENT][task-demo01]":
    "The conversation for this URL does not belong to the current session; deletion refused. Expected prefix: [CBP-AGENT][task-demo01]",
  "⏳ API 服务限流/不可用，预计恢复": "⏳ API service rate-limited/unavailable, recovery expected",
  "⏳ Copilot 限流/不可用，预计恢复": "⏳ Copilot rate-limited/unavailable, recovery expected",
  "　提供商模型不受影响，可直接使用。": " Provider models are unaffected and can be used directly.",
  "主题：": "Theme: ",
 "当前": "current",
 "点击切换": "click to switch",
 "跟随系统": "Follow system",
 "深色": "Dark",
 "浅色": "Light",
 "琥珀": "Amber",
 "墨玉": "Ink jade",
 "护眼米": "Solarized",
 "薄荷": "Mint",
 "主题：{n}（当前 {c}，点击切换）": "Theme: {n} (current: {c}, click to switch)",
 "主题：{n}（点击切换）": "Theme: {n} (click to switch)",
  "任务启动失败: ": "Task start failed: ",
  "任务已建立": "Task created",
  "会话模型已切换为 ": "Session model switched to ",
  "修改后语法检查": "Syntax check after the change",
  "修改排队消息": "Edit queued message",
  "关键词检索…（回车）": "Keyword search… (Enter)",
  "写入修复后的 agent.py": "Write the fixed agent.py",
  "分析 cbp 项目并生成 TODO": "Analyze the cbp project and generate a TODO",
  "切换模型失败: ": "Switching model failed: ",
  "初始提示词（本会话未保存，以下为按当前工作区近似重建）":
    "Initial prompt (not stored for this session; approximately rebuilt from the current workspace)",
  "初始提示词（首回合实际发送内容）": "Initial prompt (what was actually sent on the first turn)",
  "删除排队消息": "Delete queued message",
  "删除确认": "Confirm deletion",
  "删除记忆": "Delete memory",
  "删除这条排队中的消息？此操作不可撤销。": "Delete this queued message? This cannot be undone.",
  "删除这条长期记忆？删除后不再参与检索。": "Delete this long-term memory? It will no longer be retrieved.",
  "发现新模型: ": "New models found: ",
  "取消置顶": "Unpin",
  "回滚到哪个版本？": "Roll back to which version?",
  "回滚失败: ": "Rollback failed: ",
  "回滚服务自升级": "Roll back self-upgrade",
  "图片超过 8MB，已忽略": "Image exceeds 8MB — ignored",
  "地址不是 http(s)，已拒绝打开": "Not an http(s) address — refused to open",
  "处理中…": "Working…",
  "复制选中行": "Copy selected lines",
  "宽限期后硬退出": "force-exit after the grace period",
  "对比两个目录差异": "Compare two directories",
  "导出失败: ": "Export failed: ",
  "已删除排队消息": "Queued message deleted",
  "已删除记忆 #": "Memory deleted #",
  "已取消令牌输入": "Token entry cancelled",
  "已取消置顶": "Unpinned",
  "已复制 ✓  ": "Copied ✓  ",
  "已复制会话 ID: ": "Session ID copied: ",
  "已完成 3 个 P0 项：会话切换风暴、暂停状态覆盖、限流识别。剩余 1 项进行中。":
    "3 P0 items done: session-switch storm, pause-state override, rate-limit detection; 1 remaining in progress.",
  "已导出 Markdown（": "Markdown exported (",
  "已将自动化 Edge 切换到该会话": "Automation Edge switched to this session",
  "已恢复，正在续跑任务": "Resumed — continuing the task",
  "已授权并继续执行（该授权仅对这一次操作有效）": "Authorized and continuing (this authorization is single-use)",
  "已排队，等待处理…": "Queued, waiting…",
  "已插入": "Injected",
  "已更新排队消息": "Queued message updated",
  "已置顶（检索时恒定注入）": "Pinned (always injected during retrieval)",
  "已重新扫描（": "Re-scanned (",
  "强制清理失败: ": "Force cleanup failed: ",
  "当前为无头模式（窗口隐藏），点击显示自动化窗口":
    "Headless mode (window hidden) — click to show the automation window",
  "当前会话": "Current session",
  "当前会话模型：": "Current session model: ",
  "当前会话模型：{n}（下一回合生效，服务重启后仍沿用）":
    "Current session model: {n} (applies to the next turn, kept after restart)",
  "当前显示自动化窗口，点击切换为无头模式（隐藏窗口）":
    "Automation window visible — click to switch to headless (hidden)",
  "当前没有对话记录": "No conversation yet",
  "授权中…": "Authorizing…",
 "Agent 提问：": "Agent asks: ",
 "选择工作目录": "Choose working directory",
 "新建工作区": "New workspace",
 "新建目录": "New folder",
 "已取消": "Cancelled",
 "新目录名（建在当前目录下）": "New folder name (created in the current directory)",
 "创建": "Create",
 "已创建目录: ": "Folder created: ",
 "创建目录失败: ": "Failed to create folder: ",
 "工作目录（默认 workspace）": "Working directory (default: workspace)",
 "已选择工作目录: ": "Working directory selected: ",
 "系统对话框不可用，改用应用内浏览":
   "System dialog unavailable, using in-app browser",
 "（没有子目录）": "(no subdirectories)",
 "读取目录失败: ": "Failed to read directory: ",
 "新会话": "New session",
 "显示子代理会话": "Show subagent sessions",
 "展开/折叠子代理会话": "Expand/collapse subagent sessions",
 "全部展开/折叠子代理会话": "Expand/collapse all subagent sessions",
 "已展开": "Expanded",
 "已折叠": "Collapsed",
 "已展开 {n} 个子代理会话": "Expanded {n} subagent sessions",
 "已折叠 {n} 个子代理会话": "Collapsed {n} subagent sessions",
 "导出失败: ": "Export failed: ",
 "已复制会话 ID: ": "Session ID copied: ",
 "该会话用的是 API 提供商模型，没有可打开的网页对话":
   "This session uses an API provider model; there is no web conversation to open",
 "该 Session 暂无网页会话地址": "This session has no web conversation URL yet",
 "回滚完成：还原 {a}、删除 {b}{tail}": "Rollback done: restored {a}, deleted {b}{tail}",
 "自升级已回滚：还原 {a}、删除 {b}{fail} · 请重启服务":
   "Self-upgrade rolled back: restored {a}, deleted {b}{fail} · restart the service",
 "，失败 {n}": ", failed {n}",
 "删除失败: ": "Delete failed: ",
 "已更新": "Updated",
 "已插入": "Inserted",
 "已置顶（检索时恒定注入）": "Pinned (always injected on retrieval)",
 "已取消置顶": "Unpinned",
 "个子代理归属会话": "subagent sessions",
 "已折叠子代理会话": "Subagent sessions hidden",
 "在此工作区新建会话": "New session in this workspace",
 "从左侧选择会话，或在这里开始新的对话": "Pick a session on the left, or start a new chat here",
 "发消息或创建任务，例如：分析 workspace 里的项目并生成 TODO；可直接粘贴截图":
   "Send a message or create a task, e.g. analyse the project under workspace and produce a TODO; screenshots can be pasted directly",
 "开始": "Start",
 "Enter 发送，Shift+Enter 换行": "Enter to send, Shift+Enter for a new line",
 "输入 / 查看命令": "Type / for commands",
 "深研：多角度搜索 + 抓正文，生成带来源的报告":
   "Deep research: multi-angle search + page fetching, produces a sourced report",
 "切到 Agent 执行模式": "Switch to Agent mode",
 "切到普通对话模式": "Switch to plain chat mode",
 "列出全部可用命令": "List all available commands",
 "目标模式：建立目标后多轮推进，未完成不会停":
   "Goal mode: creates a goal and keeps pushing across rounds until done",
 "计划模式（只读）：探索后产出方案，exit_plan_mode 提交":
   "Plan mode (read-only): explore, then submit a plan via exit_plan_mode",
 "用法：/goal 你的目标": "Usage: /goal your objective",
 "用法：/plan 你的任务": "Usage: /plan your task",
 "已阻塞": "blocked",
 "已暂停": "paused",
 "轮": "round",
 "计划": "Plan",
 "第": "round",
 "用法：/research 你的问题": "Usage: /research your question",
 "扇出：多角度并行调研（用 | 分隔任务）": "Fan-out: parallel research (separate tasks with |)",
 "扇出：把多个互不依赖的任务各派一个子代理（用 | 分隔），不经模型判断":
   "Fan-out: give each independent task its own subagent (separate with |), without asking the model",
 "用法：/fanout 任务1 | 任务2 | 任务3（至少两个）":
   "Usage: /fanout task1 | task2 | task3 (at least two)",
 "已派出 {n} 个子代理，正在跑…": "Dispatched {n} subagents, running…",
 "扇出完成：成功 {a} · 失败 {b} · {t} 秒": "Fan-out done: {a} ok · {b} failed · {t} s",
 "扇出失败: ": "Fan-out failed: ",
 "已切到 Agent 执行": "Switched to Agent mode",
 "已切到普通对话": "Switched to plain chat",
 "时间线中的": "In the timeline, ",
 "深研": "Deep research",
 "多角度搜索 + 抓正文，生成带来源的调研报告（新会话）":
   "Multi-angle search + page fetching, produces a sourced research report (new session)",
 "开始深研": "Start research",
 "输入研究问题：会用多角度搜索 + 抓取正文，生成带来源的调研报告（约 20-60 秒）":
   "Enter a research question: multi-angle search + page fetching, then a sourced report (~20-60s)",
 "深研中…（多角度搜索 + 抓正文）": "Researching… (multi-angle search + fetching)",
 "深研完成：已打开报告会话": "Research done: report session opened",
 "深研完成": "Research done",
 "深研失败: ": "Research failed: ",
 "插件管理": "Plugins",
 "插件面板": "Plugin panels",
 "（没有面板）": "(no panels)",
 "（空）": "(empty)",
 "读取失败: ": "Failed to read: ",
 "插件管理…": "Plugins…",
 "已启用": "Enabled",
 "已禁用": "Disabled",
 "未加载": "Not loaded",
 "启用": "Enable",
 "禁用": "Disable",
 "重载": "Reload",
 "权限: ": "Permissions: ",
 "贡献: ": "Contributes: ",
 "没有安装插件（把插件放进 plugins/ 目录）": "No plugins installed (put them under plugins/)",
 "加载中…": "Loading…",
 "读取插件失败: ": "Failed to load plugins: ",
 "操作失败: ": "Action failed: ",
 "重载失败: ": "Reload failed: ",
 "已重载: ": "Reloaded: ",
 "已": "Done: ",
 "子代理": "Subagents",
 "刷新": "Refresh",
 " · {n} 个": " · {n}",
 "（{r} 个运行中）": " ({r} running)",
 "步": "steps",
 "输入你的回答…": "Type your answer…",
 "提交回答": "Submit answer",
 "提交中…": "Submitting…",
 "已提交回答，任务继续": "Answer submitted; the task continues",
 "提交失败: ": "Submit failed: ",
  "授权失败: ": "Authorization failed: ",
  "排队跟进": "Queued follow-up",
  "插入指令（运行中即时生效，会立即中断当前回合后按新指令继续）。插入方式决定语义：":
    "Inject an instruction (applies immediately while running and interrupts the current turn). The mode decides the semantics:",
  "操作失败": "Operation failed",
  "整理 Outlook 未读邮件": "Tidy up unread Outlook mail",
  "无匹配": "No match",
  "无头模式启动失败，已回退为有窗口模式（旧版 Edge 或安全策略不支持无头持久化）":
    "Headless start failed; fell back to windowed mode (old Edge or security policy lacks headless persistence support)",
  "无法连接本地服务（127.0.0.1:8765 是否在运行？）": "Cannot reach the local service (is 127.0.0.1:8765 running?)",
  "时间线刷新失败:": "Timeline refresh failed:",
  "时间线已导出（": "Timeline exported (",
  "智谱": "Zhipu",
  "更新失败: ": "Update failed: ",
  "最多附加 4 张图片": "Up to 4 images",
  "最终答复": "Final answer",
  "最近发送的内容": "Last prompt",
  "服务已终止（连接已断开）": "Service stopped (connection lost)",
  "未初始化": "not initialized",
  "未授权：已取消令牌输入（点击重新输入）": "Unauthorized: token entry cancelled (click to re-enter)",
  "未知": "unknown",
  "未知原因": "unknown reason",
  "标签已设为": "Tag set to ",
  "检查字节码缓存时间状态": "Check the bytecode cache timestamp",
  "检查用": "for inspection",
  "模型列表已刷新": "Model list refreshed",
  "模型菜单未能扫描：": "Could not scan the model menu: ",
  "模型菜单读取失败: ": "Reading the model menu failed: ",
  "正在发送/流式返回/等待回答": "sending / streaming / waiting for answer",
  "正在发送普通对话 ": "Sending plain chat ",
  "正在建立 Agent 任务 ": "Creating agent task ",
  "没有可回滚的自升级记录": "No self-upgrade record to roll back",
  "浏览器启动中…": "Browser starting…",
  "浏览器启动失败": "Browser start failed",
  "浏览器已启动，等待 Copilot 就绪（可能需要在自动化窗口登录）":
    "Browser started; waiting for Copilot to be ready (you may need to sign in in the automation window)",
  "浏览器未启动（首次使用 Copilot 时自动启动）": "Browser not started (starts automatically on first Copilot use)",
  "深圳英文旅游手册": "Shenzhen English travel guide",
  "渲染视图": "Rendered view",
  "演示模式（?demo=1）——数据为本地假数据": "Demo mode (?demo=1) — local fake data",
  "演示模式（API 未连接）：无法刷新模型列表": "Demo mode (API not connected): cannot refresh the model list",
  "点击删除后，目标 URL 仍存在于 Chats 列表": "After deletion the target URL is still in the Chats list",
  "点击复制完整 URL": "Click to copy the full URL",
  "现在做 todo 里的 P0 阻断项，每完成一个请在 todo 里打上勾":
    "Now do the P0 blocking items in the todo; tick each one off as it completes",
  "确定终止？": "Stop the service?",
  "移除": "Remove",
  "等待模型回答…": "Waiting for the model…",
  "继续执行当前任务，从未完成事项开始": "Continue the current task, starting from unfinished items",
  "编辑": "Edit",
  "编辑这条消息的内容（只改本地历史；已发给网页 Copilot 的内容无法撤回）":
    "Edit this message (local history only; content already sent to web Copilot cannot be recalled)",
  "网页对话": "web conversation",
  "自升级回滚失败: ": "Self-upgrade rollback failed: ",
  "设置会话标签（列表按标签分组；留空取消标签）":
    "Set a session tag (the list groups by tag; leave empty to clear)",
  "设置标签（用于分组）": "Set tag (used for grouping)",
  "该 Session 暂无网页会话地址": "This session has no web conversation URL",
  "该任务没有可回滚的文件改动": "This task has no file changes to roll back",
  "该会话用的是 API 提供商模型，没有可打开的网页对话":
    "This session uses an API provider model — there is no web conversation to open",
  "请求失败: ": "Request failed: ",
  "请输入要继续执行的任务": "Enter the task to continue with",
  "请输入访问令牌": "Enter the access token",
  "读取 agent.py 确认当前实现": "Read agent.py to confirm the current implementation",
  "读取初始提示词失败: ": "Reading the initial prompt failed: ",
  "还在生成": "still generating",
  "这条消息不在最近加载的记录里（可能超出 500 条上限）":
    "This message is not in the recently loaded records (may exceed the 500-message limit)",
  "这条消息还没有落库，无法编辑": "This message is not stored yet and cannot be edited",
  "部分 Copilot 会话删除失败，本地 Session 已保留。可检查后使用下方按钮重试，或仅清理本地记录。":
    "Some Copilot conversations could not be deleted; the local session was kept. Check and retry with the button below, or clean up the local record only.",
  "重试远程并强制清理本地记录": "Retry remote deletion and force-clear the local record",
  "链接": "Link",
  "长期记忆：查看 / 检索 / 置顶 / 删除（点击打开面板）":
    "Long-term memory: view / search / pin / delete (click to open the panel)",
  "需要 api_token：": "api_token required: ",
  "需要在自动化窗口完成 Microsoft 登录": "Microsoft sign-in is required in the automation window",
  "需要授权：": "Authorization required: ",
  "默认工作区": "Default workspace",
  "（下一回合生效，服务重启后仍沿用）": " (takes effect next turn, persists across restarts)",
  "（演示模式）此处为 Copilot 回复": "(demo mode) Copilot's reply appears here",
  "（点击切换）": " (click to cycle)",
  "（菜单里未找到）": " (not found in the menu)",
 "长期记忆": "Long-term memory",
 "上一级": "Go up one level",
 "默认": "Default",
 "执行": "Execution",
  "数据": "Data",
  "设置": "Settings",
  "外观": "Appearance",
 "仅可查看": "View only",
 "工作区内修改": "Modify workspace",
 "完全权限": "Full access",
 "只读：能读文件和搜索，不能改任何东西": "Read-only: can read and search, cannot change anything",
 "只能改工作区里的文件；命令在沙箱里执行": "Can only change files in the workspace; commands run sandboxed",
 "可读写工作区之外，命令不受沙箱限制（谨慎）": "Can read/write outside the workspace; commands are unsandboxed (careful)",
 "权限档位：工具能改什么（点开选择）": "Permission level: what tools may change (click to pick)",
 "只读：所有写操作与命令执行会被拒绝": "Read-only: all writes and command execution are refused",
 "工作区内修改：只能改工作区文件；命令在受限沙箱里跑": "Workspace write: only workspace files; commands run in a restricted sandbox",
 "完全权限：可读写工作区之外，命令不受沙箱限制": "Full access: read/write outside the workspace; commands are not sandboxed",
 "权限已切换为 {m}": "Permission switched to {m}",
 "权限档位（工具能改什么）": "Permission level (what tools can change)",
  "换用其它提供商模型继续": "Continue with another provider model",
 "Copilot 网页限流，已挂起：到点自动继续，也可以换下面的提供商模型立即继续": "Copilot web rate-limited. Suspended: it will resume automatically at the window end, or pick a provider model below to continue now",
 "Copilot 网页限流，已挂起：到点自动继续（没有配置其它提供商模型）": "Copilot web rate-limited. Suspended: it will resume automatically at the window end (no provider models configured)",
 "换模型继续": "Switch model & continue",
 "已切换到 {m} 并继续": "Switched to {m} and resumed",
 "第 {n} 次 · 自动重试中，可点「暂停」稍后继续": "Attempt {n} · auto-retrying; you can pause and resume later",
 "Copilot 限流时挂起（到点自动继续，可换提供商模型）": "Suspend on Copilot rate limit (auto-resume at window end, or switch provider)",
  "服务限流/超时，自动重试 {n} 次（不计步）": "Rate limited / timed out; auto-retried {n} times (not counted)",
  "设置分组": "Settings sections",
 "每次操作都询问": "Ask every time",
 "本会话内不再逐次询问": "Ask once per session",
 "首次使用时启动": "Start on first use",
 "服务启动时预热": "Warm up at startup",
 "浏览器": "Browser",
 "入口": "Shortcuts",
 "危险区": "Danger zone",
 "主题": "Theme",
 "语言": "Language",
 "护眼": "Solarized",
 "工作区浏览": "Workspace browser",
 "记忆面板": "Memory",
 "终止服务 / 退出": "Stop service / quit",
 "重启/终止都会中断正在跑的任务；状态已落库，重启后可恢复。": "Restarting or stopping interrupts running tasks; state is persisted and can be resumed.",
 "改完点保存；外观类即时生效": "Click Save when done; appearance applies immediately",
 "设置已保存（{n} 项）": "Settings saved ({n})",
 "保存失败: ": "Save failed: ",
 "浏览器启动方式": "Browser startup",
 "无头模式（不显示浏览器窗口）": "Headless (no browser window)",
 "服务重启后自动续跑任务": "Auto-resume tasks after restart",
 "允许 copilot_file 直读文件": "Allow copilot_file to read files",
 "保留诊断信息": "Keep diagnostics",
 "记录工具耗时区间": "Record tool spans",
 "默认模型": "Default model",
 "自动派发子代理（待办≥2 且互不重叠时）": "Auto-dispatch subagents (2+ non-overlapping todos)",
 "每 N 步提醒模型更新待办（0=关闭）": "Remind model to update todos every N steps (0=off)",
 "子代理并发数": "Subagent concurrency",
 "子代理最多步数": "Max subagent steps",
 "审批范围": "Approval scope",
 "上下文预算（token）": "Context budget (tokens)",
 "最多保留会话数": "Max retained sessions",
 "文件快照保留份数": "File snapshot retention",
 "已完成会话保留天数": "Completed session retention (days)",
 "lazy": "Lazy",
 "eager": "Eager",
 "each": "Each action",
 "session": "Whole session",
 "{n} 行": "{n} lines",
 "复制选中 {n} 行": "Copy {n} selected lines",
 "批量动作 · {n} 个": "Batch actions · {n}",
 "JSON 回复": "JSON reply",
 "[Base64 内容，共 {n} 字符]": "[Base64 content, {n} chars]",
 "…[共 {n} 字符，已截断]": "…[{n} chars total, truncated]",
 "JSON 解析失败（原文）": "JSON parse failed (raw text)",
 "收起/展开子代理（点标题也行）": "Collapse or expand subagents (clicking the title works too)",
 "收起/展开这块面板（点标题也行）": "Collapse or expand this panel (clicking the title works too)",
 "收起/展开": "Collapse / expand",
 "收起/展开这块面板（只留标题行）": "Collapse or expand this panel (title row only)",

 "收起/展开任务条（点标题也行）": "Collapse or expand the task bar (clicking the title works too)",
  "对话区更多操作（导出 / 搜索 / 强行插入 / 初始提示词 / 最近发送 / 放大）": "More chat actions (export / search / inject / initial prompt / last prompt / expand)",

 "点一下收起/展开任务条（也可以点右边的箭头）": "Click to collapse or expand the task bar (or use the arrow on the right)",
 "折叠": "Collapse",
 "系统": "system",
 "与": "with",
 "继续": "continue",
 "📨 最近发送": "📨 Last prompt",
 "查看最近一次实际发送给 Copilot 的完整内容": "Show the full content most recently sent to Copilot",
 "点一下展开/收起完整状态": "Click to expand or collapse full status",
 "继续当前 Session 对话": "Continue this session",
 "实时流": "Live stream",
 "最近发送": "Last prompt",
 "该面板已合并进「实时流」": "That panel is now merged into Live stream",
 "Token": "Token",
 "（还没有发送记录）": "(nothing sent yet)",
 "未同步": "Not synced",
 "任务已结束，但待办状态没有同步（模型收尾时没有更新 todo）。": "The task has ended, but the todo list was not synced (the model did not update it while finishing).",
 "删除任务（自动含 Copilot 记录）": "Delete task (includes its Copilot records)",
 "，并删除该任务关联的 {n} 个 Copilot 网页会话（标题里可能已没有 [CBP-AGENT] 标记，删除时会用会话正文核对归属）": ", plus the {n} Copilot conversation(s) this task created (their titles may no longer carry the [CBP-AGENT] marker; ownership is verified against the conversation body)",
 "这个任务用过 Copilot，将同时删除远端对话记录。\n\n  ID: {id}\n  标题: {title}\n\n本地 Session{extra}。此操作不可撤销，确定继续？": "This task used Copilot, so its remote conversation records will be deleted too.\n\n  ID: {id}\n  Title: {title}\n\nLocal session{extra}. This cannot be undone — continue?",
 "即将删除的会话：\n\n  ID: {id}\n  标题: {title}\n\n仅删除本地 Session（这个任务没有用过 Copilot 网页会话）。此操作不可撤销，确定继续？": "About to delete this session:\n\n  ID: {id}\n  Title: {title}\n\nLocal session only (this task never used a Copilot web conversation). This cannot be undone — continue?",
 "该任务正在运行，先停止再删除": "This task is running; stop it before deleting",
 "部分 Copilot 会话删除失败，本地 Session 已保留。可检查后使用 force_local=true 仅清理本地记录。": "Some Copilot conversations could not be deleted; the local session is kept. Inspect the list, then use force_local=true to delete only the local record.",
 "左侧 Chats 中找不到与保存 URL 对应的会话；侧栏未能读出会话列表，无法确认是否已删除": "Could not find the conversation matching the saved URL in the left Chats list, and the sidebar list was unreadable, so deletion cannot be confirmed",
 "未记录任何远端会话 URL，跳过远端删除": "No remote conversation URLs recorded; skipped remote deletion",
 "选择 Session 后，可以在这里继续对话。": "Pick a session on the left, then continue the conversation here.",
 "演示模式 · API 未连接": "Demo mode · API not connected",
 "⏳ 服务限流/不可用，预计恢复": "⏳ Rate limited / unavailable, expected recovery",
 "第 1 次 · 自动重试中，可点「暂停」稍后继续": "Attempt 1 · retrying automatically; you can pause and continue later",
 "查看": "View",
 "复制内容": "Copy content",
 "共 {n} 个 Copilot 会话 · 成功 {a} · 失败 {b}": "{n} Copilot conversations · {a} deleted · {b} failed",
 "没有远程删除明细。": "No remote deletion details.",
 "删除结果": "Deletion result",
 "复制结果 JSON": "Copy result JSON",
 "部分 Copilot 会话删除失败：本地 Session 已保留，可稍后用「强制清理本地记录」只删本地。": "Some Copilot conversations could not be deleted; the local session is kept. You can later use \"force-clear local record\" to delete only the local copy.",
 "阶段": "Phase",
 "Token估算": "Token estimate",
 "会话序号": "Session index",
 "条待处理": "pending",
 "最近错误": "Last error",
 "最后更新": "Last updated",
 "待办清单：Agent 用 todo_write 维护，点一下展开/收起；与 /plan 计划模式无关": "Todo list: maintained by the agent via todo_write; click to expand/collapse (unrelated to /plan mode)",
 "未授权：已取消令牌输入（点此重新输入）": "Unauthorized: token prompt cancelled (click to enter it again)",
 "即将删除的会话：\n\n  ID: {id}\n  标题: {title}\n\n仅删除本地 Session{extra}。此操作不可撤销，确定继续？": "About to delete this session:\n\n  ID: {id}\n  Title: {title}\n\nDeleting the local session only{extra}. This cannot be undone — continue?",
 "  · 回合 {r}（{t}）：{paths}": "  · turn {r} ({t}): {paths}",
 "该任务共 {n} 个回合版本（按时间先后）：\n\n{lines}\n\n输入「0」或留空 = 整任务回滚到开始前；输入版本号 = 撤销该回合之后的改动。": "This task has {n} turn versions (chronological):\n\n{lines}\n\nEnter \"0\" or leave empty = roll the whole task back to before it started; enter a version number = undo the changes made after that turn.",
 "回滚到版本 {v}（撤销该回合之后的改动）": "Roll back to version {v} (undo changes after that turn)",
 "整任务回滚到开始前": "Roll the whole task back to before it started",
 "{target}：{n} 个路径。\n修改过的还原、任务新建的删除。\n\n{preview}{more}\n\n此操作不可撤销，确定继续？": "{target}: {n} paths.\nModified files are restored, files the task created are deleted.\n\n{preview}{more}\n\nThis cannot be undone — continue?",
 "回滚任务改动": "Roll back task changes",
 "将把服务自身代码恢复到自升级前的状态（最近一次）：\n\n{list}\n\n回滚后必须重启服务才生效。确定继续？": "Restore the service's own code to the state before its last self-upgrade:\n\n{list}\n\nA restart is required afterwards. Continue?",
 "即将重启本服务（用于让改过的代码生效）：\n\n· 服务会先优雅退出，然后**就地重启**（同一个进程，端口不变）\n· 正在运行的任务会中断（状态已落库；开着 auto_resume 会自动续跑）\n· 自动化浏览器会重新连接\n· 本页会在服务恢复后**自动刷新**，无需手动操作\n\n确定重启？": "Restart this service so changed code takes effect:\n\n· The service exits gracefully, then **restarts in place** (same process, same port)\n· Running tasks are interrupted (state is persisted; auto_resume continues them)\n· The automation browser reconnects\n· This page **refreshes itself** once the service is back — no action needed\n\nRestart now?",
 "主题：跟随系统（点击切换）": "Theme: follow system (click to switch)",
 "语言 / 語言 / Language（点击切换）": "Language / 語言 / Language (click to switch)",
 "跟进消息": "Follow-up message",
 "输入": "Input",
 "收起（右下角小标可再展开）": "Collapse (expand again from the badge at the bottom right)",
 "限流中：点击展开倒计时": "Rate limited: click to expand the countdown",
 "定时任务": "Scheduled tasks",
 "MCP 服务器": "MCP servers",
 "按间隔/每天/一次性时间点执行的任务": "Tasks that run on an interval, daily, or once at a set time",
 "连接状态、工具与资源": "Connection state, tools and resources",
 "回到默认工作区": "Back to the default workspace",
 "在此新建目录": "Create a folder here",
 "选择此目录": "Use this folder",
 "检索": "Search",
 "置顶": "Pin",
 "取消置顶": "Unpin",
 "命中 {n}": "{n} hits",
 "全部作用域": "All scopes",
 "仅全局": "Global only",
 "仅当前项目": "Current project only",
 "关键词检索…（回车）": "Search keywords… (Enter)",
 "共 {t} 条 · 置顶 {p} · 从未命中 {u} · {kinds} · {scopes}":
   "{t} entries · {p} pinned · {u} never hit · {kinds} · {scopes}",
 "没有匹配的记忆。Agent 完成任务时会自动沉淀，也可以直接让它「记到长期记忆」。":
   "No matching memories. The agent records them as tasks finish; you can also just ask it to \"remember this\".",

  "，下一回合生效": ", takes effect next turn",
  "，以及该任务关联的所有 Copilot 网页会话（标题里可能已没有 [CBP-AGENT] 标记，删除时会用会话正文核对归属）":
    ", plus all Copilot web conversations tied to this task (titles may no longer carry the [CBP-AGENT] marker; ownership is verified against the conversation body)",
});

/* 动态渲染出来的**属性**在 DOM 层翻译（title / placeholder / aria-label）。
   为什么不改源码：把 JS 生成的 HTML 属性写成 tr(...) / ${esc(tr(...))} 需要区分
   模板串与拼接串上下文，实测极易写坏（我自己就写坏过三次：转义引号截断、模板里
   把 JS 源码当属性值、拼接结构错乱）。改在 DOM 层就没有这个风险：
   只翻译"属性值恰好等于字典词条"的，命中不了就原样不动。 */
const I18N_ATTR_ORIG = new WeakMap();
function translateAttrs(scope) {
  const root = scope || document;
  root.querySelectorAll("[title],[placeholder],[aria-label]").forEach((el) => {
    ["title", "placeholder", "aria-label"].forEach((a) => {
      if (!el.hasAttribute(a)) return;
      let store = I18N_ATTR_ORIG.get(el);
      if (!store) { store = {}; I18N_ATTR_ORIG.set(el, store); }
      if (store[a] === undefined) store[a] = el.getAttribute(a) || "";
      const src = String(store[a] || "");
      const key = src.trim();
      if (!key) return;
      const val = tr(key);
      if (val !== key && el.getAttribute(a) !== val) el.setAttribute(a, val);
    });
  });
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
  // 二〇二五补：技术术语按词修正（简繁不是一对一，字级转换会把"数据"变"數據"）
  "数据": "資料", "数据库": "資料庫", "字段": "欄位", "会话": "對話", "消息": "訊息",
  "服务器": "伺服器", "客户端": "用戶端", "令牌": "權杖", "密码": "密碼", "账户": "帳戶",
  "脚本": "指令碼", "代码": "程式碼", "项目": "專案", "插件": "外掛", "接口": "介面",
  "日志": "日誌", "状态": "狀態", "内容": "內容", "计划": "計畫", "目标": "目標",
  "子代理": "子代理", "子任务": "子任務", "会话列表": "對話列表", "工作区": "工作區",
  "邮件": "郵件", "审批": "核准", "快照": "快照", "重启": "重新啟動", "升级": "升級",
  "安装": "安裝", "目录": "目錄", "路径": "路徑", "文件名": "檔名", "文件夹名": "資料夾名稱",
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
    // 覆盖率审计发现的缺口（例如「览」：用户实测「重試浏覽器」）
    ["业","業"],
    ["记","記"],
    ["忆","憶"],
    ["国","國"],
    ["报","報"],
    ["挂","掛"],
    ["拦","攔"],
    ["检","檢"],
    ["极","極"],
    ["环","環"],
    ["现","現"],
    ["画","畫"],
    ["离","離"],
    ["种","種"],
    ["积","積"],
    ["竖","豎"],
    ["竞","競"],
    ["紧","緊"],
    ["纯","純"],
    ["细","細"],
    ["经","經"],
    ["绑","綁"],
    ["绕","繞"],
    ["绘","繪"],
    ["给","給"],
    ["绝","絕"],
    ["继","繼"],
    ["绪","緒"],
    ["维","維"],
    ["缀","綴"],
    ["缘","緣"],
    ["联","聯"],
    ["节","節"],
    ["获","獲"],
    ["蓝","藍"],
    ["虚","虛"],
    ["补","補"],
    ["装","裝"],
    ["许","許"],
    ["访","訪"],
    ["评","評"],
    ["识","識"],
    ["诉","訴"],
    ["诚","誠"],
    ["详","詳"],
    ["调","調"],
    ["谱","譜"],
    ["负","負"],
    ["责","責"],
    ["账","賬"],
    ["质","質"],
    ["贴","貼"],
    ["费","費"],
    ["赋","賦"],
    ["赖","賴"],
    ["车","車"],
    ["轮","輪"],
    ["轻","輕"],
    ["较","較"],
    ["辞","辭"],
    ["迟","遲"],
    ["邮","郵"],
    ["术","術"],
    ["专","專"],
    ["资","資"],
    ["浏","瀏"],
  ];
  const map = new Map(pairs);
  return map;
})();

function toTraditional(text) {
  let s = String(text == null ? "" : text);
  // 先按短语覆盖（词汇级），再做字形转换
  // **按词长降序**应用：否则"服务"会先吃掉"服务器"的前两个字，
  // 让"服务器→伺服器"这类长词永远匹配不到（实测 服務器）。
  Object.keys(I18N_TW_OVERRIDE).sort((a, b) => b.length - a.length).forEach((k) => {
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
  // JS 拼出来的文案（聊天栏那句）applyI18n 管不到，切语言后要主动重绘
  try { if (typeof refreshChatBarMeta === 'function') refreshChatBarMeta(); } catch (e) {}
  applyLangButton();
  try { rerenderForLang(); } catch (e) {}
}
function cycleLang() {
  const cur = currentLang();
  setLang(I18N_ORDER[(I18N_ORDER.indexOf(cur) + 1) % I18N_ORDER.length]);
}
/* 取词：中文原文当 key；缺翻译回落原文（永不出现空白/占位符） */
function tr(key, vars) {
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
  const val = tr(key);
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
    const v = tr(el.dataset.i18n);
    if (el.textContent !== v) el.textContent = v;
  });
  scope.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = tr(el.dataset.i18nTitle); });
  scope.querySelectorAll("[data-i18n-ph]").forEach((el) => { el.placeholder = tr(el.dataset.i18nPh); });
  scope.querySelectorAll("[data-i18n-aria]").forEach((el) => {
    el.setAttribute("aria-label", tr(el.dataset.i18nAria));
  });
  // 静态 DOM 直译
  const all = scope.querySelectorAll(
    "[title],[placeholder],[aria-label],button,summary,option,label,th,span,div,b,small,code,kbd,h1,h2,h3,h4,p,li");
  all.forEach((el) => {
    if (i18nSkipped(el)) return;
    // **显式 key 优先**：带 data-i18n* 的元素由上面的处理决定文案，DOM 直译不得覆盖它。
    // 真事故：以英文重载页面时，DOM 直译把"已经翻成英文"的文本cache 成了"原文"，
    // 于是切回中文时它又拿英文原文覆盖掉 data-i18n 设好的中文 → 界面中英混杂。
    ["title", "placeholder", "aria-label"].forEach((a) => {
      if (!el.hasAttribute(a)) return;
      if (a === "title" && el.hasAttribute("data-i18n-title")) return;
      if (a === "placeholder" && el.hasAttribute("data-i18n-ph")) return;
      if (a === "aria-label" && el.hasAttribute("data-i18n-aria")) return;
      i18nSwapText(el, a);
    });
    if (el.hasAttribute("data-i18n")) return;
    if (el.children.length === 0 && el.textContent && el.textContent.trim()) i18nSwapText(el, "text");
  });
}
function applyLangButton() {
  const b = document.getElementById("langBtn");
  if (b) {
    b.textContent = I18N[currentLang()].label;
    // 提示语也要走 tr()：原来直接拼中文，英文模式下这条 title 一直是中文
  b.title = tr("语言 / 語言 / Language：") + I18N[currentLang()].name + tr("（点击切换）");
  }
}
/* 切语言后重画动态区域（这些区域的内容是 JS 生成的，不在 data-i18n 覆盖内） */
function rerenderForLang() {
  try { if (typeof drawSessions === "function") { sessSig = ""; drawSessions(); } } catch (e) {}
  try { if (typeof renderMessages === "function" && lastMessages.length) renderMessages(lastMessages); } catch (e) {}
  try { if (typeof renderSteps === "function" && lastSteps.length) renderSteps(lastSteps, true); } catch (e) {}
  try { if (typeof refreshQueue === "function") refreshQueue(); } catch (e) {}
  try { if (lastRuntime && typeof applyRuntime === "function") applyRuntime(lastRuntime); } catch (e) {}
  // #bootText 在跳过名单里（内容是动态拼的），所以切语言必须主动重画一次，
  // 否则要等下一次 pollBoot（最多 8 秒）才会变成新语言。
  try { if (typeof showBoot === "function" && typeof _lastBoot !== "undefined" && _lastBoot)
          showBoot(_lastBoot[0], _lastBoot[1], _lastBoot[2]); } catch (e) {}
  try { if (typeof fillStepTools === "function") fillStepTools(); } catch (e) {}
  // 模型下拉的 <option title> 来自后端说明：切语言要重建，否则原生提示还是旧语言
  try { if (typeof loadModels === "function") loadModels(); } catch (e) {}
  // 主题按钮标题是拼串（"主题：X（当前 Y…）"），必须在切语言后重设一次
  try { if (typeof applyTheme === "function") applyTheme(themeChoice(), false); } catch (e) {}
  // 手册是整块注入的 HTML：切语言后必须整篇重画（中英是两套完整模板）
  try {
    if (typeof openManual === "function"
        && document.getElementById("manualMask")?.classList.contains("open")) {
      openManual();
    }
  } catch (e) {}
}
