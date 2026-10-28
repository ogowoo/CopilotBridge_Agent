// teamcard.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
function toggleTodos(){
 todosOpen=!todosOpen;
 try{localStorage.setItem('gantry-todos-open',todosOpen?'1':'0')}catch(e){}
 renderGoalBar(window.lastRuntime);
}
const TODO_ICON={completed:'✓',in_progress:'◐',pending:'○'};
const TODO_TXT={completed:'已完成',in_progress:'进行中',pending:'待办'};
let _goalSig='';
/* 实时流顶部的状态条：一行看清"在干什么"。
   原来这块是「当前执行状态」标签页的整页文本，现在压成一行；点一下展开完整详情。 */
/* 聊天栏那句"与 Copilot 继续 · Ctrl+J …"是 JS 拼的（不是静态 HTML），
   语言切换时 applyI18n 管不到它，必须自己重绘——否则切语言后它停在旧语言上。 */
function refreshChatBarMeta(){
 const meta=document.getElementById('chatBarMeta');
 if(!meta)return;
 meta.innerHTML=esc(tr('与'))+' <b>'+esc(assistantLabel())+'</b> '+esc(tr('继续'))+
   ' · <b>Ctrl+J</b> '+esc(tr('强行插入指令（运行中即时生效）'));
 // 这行说明改成 tooltip：面板头部每多一行就吃一行对话区高度（短窗口尤其明显）
 const _bar=document.getElementById('chatBar');
 if(_bar)_bar.title=tr('与')+' '+assistantLabel()+' '+tr('继续')+' · Ctrl+J '+
   tr('强行插入指令（运行中即时生效）');
}

/* 后端 status detail 是中文（gantry 里 28 处 detail=），英文界面会露中文。
   这里做一张"详情翻译表"（正则 → 英文模板 $1..$9），只在英文模式生效。 */
const DETAIL_EN=[
 [/^正在提交 Agent 回合 (\S+)$/,'Submitting agent turn $1'],
 [/^Agent 回合 (\S+) 已确认，等待 Copilot 回答$/,'Agent turn $1 confirmed, waiting for Copilot'],
 [/^任务已进入执行队列$/,'Task queued'],
 [/^已读取文件 (.+)$/,'Loaded file $1'],
 [/^新 Copilot 会话已绑定唯一标识$/,'New Copilot conversation bound'],
 [/^正在执行第 (\d+) 步 · (.+)$/,'Running step $1 · $2'],
 [/^第 (\d+) 步已完成（(.+)）$/,'Step $1 done ($2)'],
 [/^用户暂停，可从检查点恢复$/,'Paused — resumable from checkpoint'],
 [/^⚠ 当前模型不支持图片：已跳过 (\d+) 张附件.*$/,'⚠ This model cannot take images: $1 attachment(s) skipped'],
 [/^正在发送普通对话$/,'Sending chat message'],
 [/^普通对话完成$/,'Chat message done'],
 [/^正在处理跟进消息：(.*)$/,'Processing follow-up: $1'],
 [/^跟进消息处理完成$/,'Follow-up handled'],
 [/^跟进消息已排队：(.*)$/,'Follow-up queued: $1'],
 [/^已插入紧急指令（next-step）：(.*)$/,'Urgent instruction inserted: $1'],
 [/^已插入指令并中断当前回合：(.*)$/,'Instruction inserted; window.current turn interrupted: $1'],
 [/^已切换到该 Session 的 Copilot 页面$/,"Switched to this session's Copilot page"],
 [/^正在调用 (.+) API（(.+)）$/,'Calling $1 API ($2)'],
 [/^(.+) API 正在流式返回…$/,'$1 API streaming…'],
 [/^(.+) API 流式返回完成，正在解析动作$/,'$1 API stream done, parsing action'],
 [/^(.+) API 已返回，正在解析动作$/,'$1 API returned, parsing action'],
 [/^正在调用 (.+) API$/,'Calling $1 API'],
 [/^(.+) API 调用完成$/,'$1 API call done'],
 [/^对抗式审计第 (\d+)\/(\d+) 轮：(.*)$/,'Adversarial audit round $1/$2: $3'],
 [/^第 (\d+) 轮：(\d+) 个原始问题，裁判合并核验…$/,'Round $1: $2 raw issues, judge merging…'],
 [/^第 (\d+) 轮有 p0\/p1，进入修复…$/,'Round $1 has p0/p1 — fixing…'],
 [/^对抗式审计完成$/,'Adversarial audit done'],
];
function trDetail(text){
 const s=String(text==null?'':text);
 if(!s)return s;
 // 语言状态由 i18n.js 的 currentLang() 决定（localStorage: gantry-lang）
 let _lg='zh';
 try{_lg=(typeof currentLang==='function')?currentLang():'zh'}catch(e){_lg='zh'}
 if(String(_lg).toLowerCase()!=='en')return s;
 for(const [re,out] of DETAIL_EN){
  const m=s.match(re);
  if(m)return out.replace(/\$(\d)/g,(_,i)=>m[Number(i)]||'');
 }
 return s;
}

function updateStreamStatus(r){
 const el=document.getElementById('streamStatus');
 if(!el||!r)return;
 const st=String(r.session_state||r.phase||'');
 const phase=String(r.phase||'');
 const parts=[];
 const _stLow=String(st||phase||'').toLowerCase();
 const _stIcon=(_stLow==='completed')?svgIcon('check')
   :(_stLow==='failed'||_stLow==='cancelled')?svgIcon('cross')
   :(_stLow==='paused'||_stLow==='suspended')?svgIcon('lock')
   :svgIcon('pulse');
 parts.push('<span class="urow-ic '+(['completed','failed','cancelled'].includes(_stLow)
   ?(_stLow==='completed'?'ok':'bad'):'live')+'">'+_stIcon+'</span>');
 parts.push('<b class="ss-phase ss-'+esc(st||'running')+'">'+esc(tr(st||phase||'未知'))+'</b>');
 // failed/cancelled 时不要把上一句"已完成"的详情留在状态条上（自相矛盾）
 const _stNow=String(r.session_state||'').toLowerCase();
 const _badNow=(_stNow==='failed'||_stNow==='cancelled');
 const _detailTxt=_badNow
   ? String(r.last_error||'').trim()          // 失败时只说真实原因；没有就整段省略
   : trDetail(String(r.detail||'').trim());
 if(_detailTxt)parts.push('<span class="ss-detail" data-i18n-skip="1">'+esc(_detailTxt.slice(0,160))+'</span>');
 const right=[];
 if(r.total_steps!==undefined&&r.total_steps!==null)
  right.push('<span class="ss-meta">'+esc(tr('步'))+' '+esc(String(r.total_steps))+'</span>');
 if(r.estimated_tokens)right.push('<span class="ss-meta">'+esc(tr('Token'))+' '+esc(String(r.estimated_tokens))+'</span>');
 if(right.length)parts.push('<span class="urow-right">'+right.join('<span class="ss-sep">·</span>')
   +'<span class="urow-chev" id="statusChev">▾</span></span>');
 if(r.last_error)parts.push('<span class="ss-err">⚠ '+esc(String(r.last_error).slice(0,60))+'</span>');
 if(r.model_warning)parts.push('<span class="ss-err">⚠ '+esc(String(r.model_warning).slice(0,60))+'</span>');
 const html=parts.join('<span class="ss-sep">·</span>');
 el.title=tr('点一下展开/收起完整状态');
 if(el.innerHTML!==html)el.innerHTML=html;      // 没变不重写，避免闪烁
}
function toggleStreamDetail(){
 const d=document.getElementById('activity');
 if(!d)return;
 d.hidden=!d.hidden;
 const el=document.getElementById('streamStatus');
 if(el)el.classList.toggle('open',!d.hidden);
 const chev=document.getElementById('statusChev');
 if(chev)chev.textContent=d.hidden?'▾':'▴';
}
/* 「最近发送」：把最近一次真正发给模型的完整内容显示出来（原「最近发送内容」标签页）。 */
async function showLastPrompt(){
 if(!window.current){toast(tr('请先选择 Session'),'err');return}
 try{
  const r=await api('/v1/agent/sessions/'+encodeURIComponent(window.current)+'/runtime');
  const txt=String(r.prompt_preview||'');
  document.getElementById('liveTitle').textContent=tr('最近发送');
  document.getElementById('liveBody').innerHTML=
    '<pre class="termpane" data-i18n-skip="1">'+esc(txt||tr('（还没有发送记录）'))+'</pre>';
  openMask('liveMask');
 }catch(e){toast(tr('读取失败: ')+(e.text||e),'err')}
}


/* 推断"当前在做哪一项"：模型不更新 todo 时，面板也不能干挂着 17 条 pending。
   规则：最近一步的文本里出现 `P0-1` 这类编号 → 命中对应项；否则取第一条 pending。
   标注成「进行中（推断）」，不写库、不假装完成。 */
function inferCurrentTodo(todos){
 const list=todos||[];
 const idx=list.findIndex(t=>String(t.status)==='in_progress');
 if(idx>=0)return idx;
 const recent=(lastSteps||[]).slice(-6).map(s=>{
   try{return (String(s.reason||'')+' '+(s.arguments?JSON.stringify(s.arguments):''))}catch(e){return String(s.reason||'')}
 }).join(' ');
 const m=recent.match(/[A-Z]\d+-\d+/g);
 if(m){
  for(const tok of m.reverse()){
   const hit=list.findIndex(t=>String(t.content||'').includes(tok));
   if(hit>=0)return hit;
  }
 }
 return list.findIndex(t=>String(t.status)==='pending');
}

/* 待办 + 子代理合并成一张卡：两块都空时把卡藏起来（否则界面上挂个空卡片）。 */
/* 待办 + 子代理并进实时流：放到「completed · steps · token」那条状态条**上面**，
   与它同一种行式样式（不再是独立卡片）。 */
function mountTeamCard(){
 // 示波器作为 #teamCard 的背景层，覆盖 Todo/Subagents/Call Log/Stream Status。
 // canvas 留在卡片内部，由绝对定位脱离文档流，高度自然跟随卡片实际高度。
 try{
  const cv=document.getElementById('teamScope');
  const card=document.getElementById('teamCard');
  if(cv&&card&&cv.parentElement!==card)card.appendChild(cv);
 }catch(e){}
 // 结构已在 index.html 里定好：待办/子代理/调用记录/状态四行同属 #teamCard，
 // #teamCard 本身就在 #chat 内。这里只做一次兜底（旧缓存页面可能顺序不对）。
 const card=document.getElementById('teamCard');
 const chat=document.getElementById('chat');
 if(!card||!chat||card.dataset.mounted)return;
 if(card.parentElement!==chat){
  const chatlog=document.getElementById('chatlog');
  if(chatlog&&chatlog.parentElement===chat)chat.insertBefore(card,chatlog.nextSibling);
  else chat.appendChild(card);
 }
 card.dataset.mounted='1';
}

function syncTeamCard(){
 const card=document.getElementById('teamCard');
 if(!card)return;
 const gb=document.getElementById('goalBar');
 const sb=document.getElementById('subagentBar');
 // 必须看**实际有内容**：goalBar hidden 但还留着旧 innerHTML、subagentBar 只是
 // display:''（回落到样式表的 none）都不算"有东西"，否则会留下一个空卡片。
 const hasGoal=!!(gb&&!gb.hidden&&String(gb.innerHTML||'').trim());
 const hasSub=!!(sb&&sb.style.display==='block');
 // 调用记录 / 状态行现在也在这个 section 里，卡片不再整体隐藏：
 // 只在"三样内容都没有"时由行自己隐藏（保留 status 行的最小高度）。
 const tl=document.getElementById('toolLog');
 const hasLog=!!(tl&&!tl.hidden);
 card.hidden=!(hasGoal||hasSub||hasLog||true);   // 状态行常驻 → 卡片常驻
}

/* 清空「待办/目标 + 子代理」两块面板与它们的外层卡片。
   新任务 / 切会话时立刻调用：否则上一轮的待办与子代理会留到新会话的第一份
   runtime 快照到达（浏览器冷启动时可能好几秒），看起来就是"新一轮明明没有却还显示"。 */
function clearGoalSubagents(){
 const gb=document.getElementById('goalBar');
 if(gb){gb.hidden=true;gb.innerHTML=''}
 _goalSig='';
 const sb=document.getElementById('subagentBar');
 if(sb)sb.style.display='none';
 const list=document.getElementById('subagentList');
 if(list)list.innerHTML='';
 const cnt=document.getElementById('subagentCount');
 if(cnt)cnt.textContent='';
 parentOfCurrent=null;          // 别让还在飞的子代理刷新按上一个会话的父 id 查询
 try{syncTeamCard()}catch(e){}
}

function renderGoalBar(r){
 const el=document.getElementById('goalBar');
 if(!el)return;
 // 局部快照（SSE 可能只带部分字段）不要当成"没有待办"而清空——那会造成面板
 // 一秒消失再出现的闪烁（用户实测）。只有快照里**确实带了**这两个字段才刷新。
 const hasGoal=r&&Object.prototype.hasOwnProperty.call(r,'goal');
 const hasTodos=r&&Object.prototype.hasOwnProperty.call(r,'todos');
 if(!hasGoal&&!hasTodos)return;
 const goal=(r.goal&&typeof r.goal==='object')?r.goal:null;
 const todos=Array.isArray(r.todos)?r.todos:[];
 if(!goal&&!todos.length){
  if(_goalSig!=='none'){el.hidden=true;el.innerHTML='';_goalSig='none'}
  // 待办条藏掉后外层 #teamCard 也要跟着判定一次：否则空卡片会留在界面上
  try{syncTeamCard()}catch(e){}
  return}
 // 内容没变就不动 DOM：以前每秒重建 innerHTML，展开的待办列表会闪
 // 注意：session_state 也决定渲染（结束态要把未完成项标"未同步"），必须进签名，
   // 否则"运行中 → 已完成"这次变化会被当成"内容没变"而跳过重绘（实测踩过）。
   const _sig=JSON.stringify([r.session_state||'',goal&&[goal.status,goal.rounds,
                            goal.max_rounds,goal.objective],todos,todosOpen]);
 if(_sig===_goalSig)return;
 _goalSig=_sig;
 el.hidden=false;
 const parts=[];
 if(goal){
  const st=String(goal.status||'active');
  const stTxt=tr({active:'进行中',complete:'已完成',blocked:'已阻塞',paused:'已暂停'}[st]||st);
  const obj=String(goal.objective||'').slice(0,90);
  parts.push('<span class="gb-goal" data-i18n-skip="1" title="'+esc(String(goal.objective||''))+'">🎯 '
    +esc(obj)+'</span><span class="gb-meta">'+esc(tr('第'))+' '+esc(String(goal.rounds||0))+'/'
    +esc(String(goal.max_rounds||0))+' '+esc(tr('轮'))+' · '+esc(stTxt)+'</span>');
 }
 if(todos.length){
  // 按 status 计数；兼容旧的 done 字段
  const isDone=x=>!!x&&(x.status==='completed'||x.done===true);
  const done=todos.filter(isDone).length;
  parts.push('<span class="urow-ic">'+svgIcon('todo')+'</span>'
    +'<b class="urow-label" title="'
    +esc(tr('待办清单：Agent 用 todo_write 维护，点一下展开/收起；与 /plan 计划模式无关'))+'">'
    +esc(tr('待办'))+'</b><span class="urow-count">'+done+'/'+todos.length+'</span>'
    +'<span class="urow-right"><span class="urow-chev">'+(todosOpen?'▾':'▸')+'</span></span>');
 }
 // 行与清单分两层：#goalBar 只是容器，hover 只作用在 .goalhead / .todoitem 上
 const headHtml='<div class="goalhead urow">'+parts.join('<span class="gb-sep">·</span>')+'</div>';
 if(todos.length&&todosOpen){
  const list=document.createElement('div');
  list.className='todolist';
  // 会话已经结束（completed/failed/cancelled）却还留着没做完的项：
  // 这时把它们说成「进行中」是假的——标注为「未同步」并给一行提示。
  const _st=String(r.session_state||'');
  const _ended=['completed','failed','cancelled','archived','paused'].includes(_st);
  const _unfinished=todos.some(x=>{const v=String((x&&x.status)||'');return v==='pending'||v==='in_progress'});
  list.innerHTML=todos.map((x,i)=>{
   let st=String((x&&x.status)||(x&&x.done?'completed':'pending'));
   const stale=_ended&&(st==='pending'||st==='in_progress');
   // 没有 in_progress 时，把推断出的当前项**临时**按进行中显示并标注「（推断）」
   const inferred=(!stale&&st==='pending'&&!todos.some(t=>String(t.status)==='in_progress')
                   && i===inferCurrentTodo(todos));
   if(inferred)st='in_progress';
   const ic=TODO_ICON[st]||'○';
   const label=stale?tr('未同步'):(inferred?tr('进行中（推断）'):tr(TODO_TXT[st]||st));
    // 已完成的条目不再挂一颗绿胶囊：全部完成时 9 颗一样的胶囊把列表染成一片，
    // 反而看不出层次（用户反馈"都一个颜色了，有点不协调"）。只留绿勾 + 常态文字。
    const pill=(st==='completed')?'':'<span class="tst">'+esc(label)+'</span>';
    return '<div class="todoitem todo-'+esc(st)+(stale?' todo-stale':'')+'"><span class="tic">'+esc(ic)+'</span>'
     +'<span class="ttxt">'+esc(String((x&&x.content)||''))+'</span>'
     +pill+'</div>';
  }).join('');
  if(_ended&&_unfinished){
   const hint=document.createElement('div');
   hint.className='todohint';
   hint.textContent=tr('任务已结束，但待办状态没有同步（模型收尾时没有更新 todo）。');
   list.appendChild(hint);
  }
  el.innerHTML='';
  el.appendChild(list);
  el.insertAdjacentHTML('beforeend', headHtml);
 } else {
  el.innerHTML=headHtml;
 }
 try{syncTeamCard()}catch(e){}
}
/* ── 顶部示波器（canvas）─────────────────────────────────────────────
   参考用户给的 demo：荧光绿那种"余晖拖尾 + 网格"的示波器。这里改成跟随主题色，
   并且**只在任务进行中**跑 rAF；空闲、页面隐藏、reduced-motion 都会停/只画一帧。 */
/* ── 发送/停止按钮切换（仿 DSH：运行中变"强行停止"） ── */

export { toggleTodos, refreshChatBarMeta, trDetail, updateStreamStatus, toggleStreamDetail, showLastPrompt, inferCurrentTodo, mountTeamCard, syncTeamCard, clearGoalSubagents, renderGoalBar };
