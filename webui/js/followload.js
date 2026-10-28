// followload.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
import { CHANGE_DONE } from './toollog.js';
import { PHASE_LABEL } from './steps.js';
function setFollowupMode(m){followupMode=m;
 document.getElementById('modeAgent').className='btn seg'+(m==='agent'?' on':'');
 document.getElementById('modeChat').className='btn seg'+(m==='chat'?' on':'')}

/* ── 视图重置：发起新任务时清空右侧残留的旧 Session 内容 ── */
function resetView(){
 if(renderedSid)stashView(renderedSid);   // 新任务前把旧会话视图留在缓存里（切回去不用重建）
 renderedSid=null;lastSessionObj=null;
 document.getElementById('title').textContent='—';
 document.getElementById('status').textContent='—';
 clearChanges();               // 新任务：上一任务的完成卡片不能跟过来
 clearGoalSubagents();         // 待办/子代理也一样：新一轮没有就别留在界面上
 let _sc=document.getElementById('sidChip'); if(_sc)_sc.textContent='';
 setPill('');
 setKpis({total_steps:0,conversation_steps:0,conversation_index:0,estimated_tokens:0});
 document.getElementById('steps').innerHTML='';renderedSteps=0;lastStepCount=-1;lastDetail='';
 document.getElementById('answer').textContent=tr('等待回答…');
 document.getElementById('prompt').textContent='—';
 renderMessages([])}

async function load(id){
 // 命中视图缓存（刚从它切走）：直接挂回原来的 DOM，几十毫秒出画面；
 // 随后 runtime() 再做增量刷新。父会话很大时这是"返回不卡"的关键。
 const cut=viewCache.get(id);
 if(cut){
  if(window.current!==id)stashView(renderedSid||window.current);
  viewCache.delete(id);
  window.current=id;
  const cs=cut.session||{};
  restoreView(cut);
  renderedSid=id;lastSessionObj=cs;
  applySessionChrome(cs);
  lastTotal=-1;lastDetail='';        // 让 runtime 对账一次（增量，不会整表重建）
  clearChanges();
  if(CHANGE_DONE.includes(String(cs.state||'')))loadChanges(id,cs.state);
  try{refreshSubagents(true)}catch(e){}
  lastAssistantCount=(cut.lastMessages||[]).filter(m=>m.role==='assistant').length;
  startEvents(id);
  runtime();refreshSessions();refreshQueue();
  // 后台对账一次消息（只在新消息到达时才重建，未变化时仅一次 HTTP + 签名比较）
  refreshChat();
  return;
 }
 if(window.current!==id)stashView(renderedSid||window.current);
 clearGoalSubagents();   // 切会话：上一会话的待办/子代理面板不能闪现在新会话上
 window.current=id;
 let d=await api('/v1/agent/sessions/'+encodeURIComponent(id)),s=d.session;
 // 守卫紧跟 await：原实现写在全部 DOM 写入之后，迟到响应会把旧会话的标题/KPI/时间线
 // 写到用户已切过去的新会话界面上（经典 stale-response）。
 if(window.current!==id)return;
 if(d.token_budget)setKpis({token_budget:d.token_budget,estimated_tokens:s.estimated_tokens??0});
 window.currentModel=s.model||null;window.currentUrl=s.current_url;
 // 记住是不是子代理会话：点进去后要能返回父会话，面板也不能凭空消失
 parentOfCurrent=s.parent_id||null;
 // 刚清空过面板：立刻按正确的父会话刷新一次（否则要等 3 秒节流，"返回父会话"才出现）
 try{refreshSubagents(true)}catch(e){}
 renderStream(d.messages||[],d.steps||[],{scrollEnd:true,force:true});
 // 跟进消息应跟随 Session 自己的模型，而不是左侧新任务表单的选择
 let _ms=document.getElementById('model');
 if(window.currentModel&&[..._ms.options].some(o=>o.value===window.currentModel))_ms.value=window.currentModel;
 refreshSessModelOptions();   // 会话页选择器显示该会话自己的模型
 updateChannelUI();           // 按通道显示/隐藏"网页对话"专用按钮与文案
 document.getElementById('title').textContent=s.title;
 let _sc=document.getElementById('sidChip');
 if(_sc){_sc.textContent=s.id;_sc.onclick=()=>{try{navigator.clipboard.writeText(s.id);toast(tr('已复制会话 ID: ')+s.id,'ok')}catch(e){toast(s.id)}}}
 document.getElementById('status').textContent=stateText(s.state)+' · '+s.workspace;
 setPill(s.state);setKpis(s);
 // 任务完成卡片：换会话先清掉旧的，再按该会话状态决定是否拉改动
 clearChanges();
 if(['completed','failed','cancelled','expired'].includes(String(s.state||''))) loadChanges(id,s.state);
 document.getElementById('steps').innerHTML='';renderedSteps=0;lastDetail='';
 // 普通对话没有 Agent 步骤：把时间线显示为对话消息（比"没有符合筛选条件的步骤"有用）
 if(!d.steps.length && (d.messages||[]).length){
  const asSteps=d.messages.map((m,i)=>({step:i+1,conversation_index:m.conversation_index??0,
   tool:m.role==='user'?'你':'助手',status:'ok',reason:(m.content||'').slice(0,80),
   created:m.created}));
  renderStepsMaybe(asSteps);lastStepCount=asSteps.length;
 }else{
  renderStepsMaybe(d.steps);lastStepCount=d.steps.length;
 }
 document.getElementById('emptyState').style.display='none';
 document.getElementById('sessionView').style.display='';
 // 记住"当前 DOM 属于哪个会话"：切走时 stashView 要靠它把节点存到正确的 sid 名下
 renderedSid=id;lastSessionObj=s;
 // 这份详情刚渲染完，第一次 runtime 不必再拉一遍（长会话 2.8MB + 重渲染）
 skipDetailOnce=id;
 // 守卫已前移到 await 之后（见函数开头）
 // 记住助手消息数（SSE 新消息通知据此决定是否播渐显动画）
 lastAssistantCount=(d.messages||[]).filter(m=>m.role==='assistant').length;
 startEvents(window.current);     // 用 window.current 而非入参：等待期间可能已切到别的会话
 await runtime();await refreshSessions();refreshQueue()}

/* 轮询在飞保护 + 会话切换竞态防护：
   延迟>1s 时不再堆叠请求；await 返回时若已切到别的会话，丢弃迟到数据 */
let runtimeInflight=false;
async function runtime(){
 if(serviceStopped||!window.current||runtimeInflight)return;
 runtimeInflight=true;
 const myId=window.current;
 try{let r=await api(`/v1/agent/sessions/${encodeURIComponent(myId)}/runtime`);
  if(window.current!==myId)return;  // 等待期间用户已切换会话
  await applyRuntime(r);
 }catch(e){}
 finally{runtimeInflight=false}}
/* 渲染 runtime 快照：轮询与 SSE 推送共用（SSE 直接送快照，省一次往返） */
/* 目标 / 计划条：goal 模式显示第几轮；待办清单显示完成度并可展开看条目。
   待办数据结构是 {content, status}（status: pending|in_progress|completed），
   **不是 {done}** —— 以前按 `x.done` 计数，永远算出 0/N（用户看到的"0/5"
   一直不涨就是这个原因）。 */
try{todosOpen=localStorage.getItem('gantry-todos-open')==='1'}catch(e){}
function updateSendButton(busy){
 const composer=document.querySelector('.main > .composer');
 if(composer)composer.classList.toggle('agent-busy', !!busy);
 const btn=document.getElementById('sendFollowup');
 if(!btn)return;
 btn.classList.toggle('stop', !!busy);
 // 用字面量赋值（不变量检查靠 data-act="stopTask" 这类静态引用找实现，三元表达式会漏）
 if(busy){btn.dataset.act='stopTask'}else{btn.dataset.act='sendFollowup'}
 btn.title=busy?tr('强行停止当前任务'):tr('发送');
 btn.setAttribute('aria-label', btn.title);
}
function stopTask(){
 if(!window.current)return;
 if(!window.lastRuntime||!window.lastRuntime.busy)return;
 // 与服务端的「取消任务」同一通道：actions/cancel（状态落库 + 通知 runner）
 try{api(`/v1/agent/sessions/${encodeURIComponent(window.current)}/actions/cancel`,{method:'POST'})}catch(e){}
 toast(tr('已要求停止当前任务'),'ok');
}

/* ── @ 文件弹层（DSH 的 @ 体验：输入 @ 唤起工作区文件选择） ── */
async function applyRuntime(r){
 if(!r)return;
 window.lastRuntime=r;              // 供 toggleTodos 重绘待办清单
 // 任务进行中：底部那条分隔线动起来（纯视觉）。注意用模块作用域的 lastRuntime，
 // 不是 window.lastRuntime（后者是 undefined，类永远挂不上）。
 try{
  const _tc=document.getElementById('teamCard');
  // 示波器浮层贴在卡片上沿：把卡片高度写进 CSS 变量
  try{
   const _chat=document.getElementById('chat');
   if(_chat&&_tc)_chat.style.setProperty('--teamcard-h', _tc.offsetHeight+'px');
  }catch(e){}
  updateSendButton(!!r.busy);
  if(_tc){
   const was=_tc.classList.contains('busy');
   const now=!!r.busy;
   if(was!==now){
    _tc.classList.toggle('busy', now);
    if(now){try{startScope()}catch(e){}} else {try{stopScope()}catch(e){}}
   }
  }
 }catch(e){}
 const myId=window.current;
 // SSE 推送与 1s/350ms 轮询可能并发到达同一函数：统一以"当前会话"为准，
 // 迟到的或属于别的会话的快照直接丢弃（否则会把 A 的步骤渲染进 C 的时间线）。
 if(!myId)return;
 if(r.session_id&&r.session_id!==myId)return;
 window.lastRuntime=r;
 try{renderGoalBar(r)}catch(e){}
 window.currentUrl=r.current_url||window.currentUrl;
  document.getElementById('pulse').className=r.busy?'pulse':'dot-off';
  try{
    const _sc=document.getElementById('kpiStateCard');
    const _st2=String(r.session_state||r.state||'');
    if(_sc&&_st2&&_sc.dataset.state!==_st2)_sc.dataset.state=_st2;
  }catch(e){}
  const _dash=(v)=> (v===undefined||v===null||v==='')?'—':v;
   // 展开的完整状态：与待办/子代理同一套「键值行」，不再是一坨 <pre> 纯文本
   const _rows=[
     [tr('阶段'), _dash(tr(PHASE_LABEL[r.phase]||'')||r.phase||r.session_state)],
     r.detail?[tr('正在做什么'), trDetail(String(r.detail))]:null,
     [tr('总步骤'), _dash(r.total_steps)],
     [tr('当前会话步骤'), _dash(r.conversation_steps)],
     [tr('Token估算'), _dash(r.estimated_tokens)],
     [tr('会话序号'), (r.conversation_index??0)+1],
     r.queue_pending?[tr('队列'), r.queue_pending+' '+tr('条待处理')]:null,
     r.last_error?[tr('最近错误'), String(r.last_error)]:null,
     r.model_warning?[tr('模型提示'), String(r.model_warning)]:null,
     [tr('最后更新'), r.updated?new Date(r.updated*1000).toLocaleTimeString():'—'],
   ].filter(Boolean);
   const _actHtml=_rows.map(([k,v])=>
     '<div class="kvrow"><span class="kvk">'+esc(String(k))+'</span>'
     +'<span class="kvv">'+esc(String(v))+'</span></div>').join('');
   const _actEl=document.getElementById('activity');
   if(_actEl&&_actEl.dataset.sig!==_actHtml){   // 内容没变不重写：否则每秒打断文本选择
     _actEl.dataset.sig=_actHtml;
     _actEl.innerHTML=_actHtml;
   }
  updateStreamStatus(r);
  let _ans=r.answer_preview||'等待模型回答…',_ae=document.getElementById('answer'),_pj=null;
  try{_pj=JSON.parse(_ans)}catch(e){}
  // 结构化动作走 JSON 视图；普通回答按 Markdown 渲染（`.term` 面板里 code 块仍可读）
  if(_ans!==lastAnsHtml){   // 仅内容变化才重建：保住代码块展开状态与滚动位置
   if(_pj&&typeof _pj==='object'){_ae.innerHTML='<div class="jsonview">'+jv(_pj)+'</div>'}
   else{_ae.innerHTML='<div class="md-body">'+mdToHtml(_ans)+'</div>'}
   applyCodePrefs(_ae);     // 回答面板里的代码块同样吃行号/换行偏好
   try{translateAttrs(_ae)}catch(e){}
   lastAnsHtml=_ans}
  // 对话区的"正在生成"气泡：只在真的在流式/等待**且会话仍在跑**时显示；
  // 完成阶段或空闲态一律清掉（否则会和已入库的消息重复显示）
  const _livePhase=String(r.phase||'');
  const _liveState=String(r.session_state||'');
  const _inFlight=['api_sending','api_streaming','waiting','sending'].includes(_livePhase)
                  && ['running','waiting','paused'].includes(_liveState);
  updateLiveBubble(_inFlight?(r.answer_preview||''):'',_inFlight);
  document.getElementById('prompt').textContent=r.prompt_preview||'';
  setPill(r.session_state);setKpis(r);
  // 任务刚跑完：拉一次"改了哪些文件"卡片（运行中不拉，数字会一直跳）
  try{maybeLoadChanges(r.session_state)}catch(e){}
  // 只在真的等待授权时显示（后端已按 waiting 过滤，这里再兜一层）
  try{showApprovalBar(r.session_state==='waiting'?r.approval_pending:null,r.approval_scope)}catch(e){}
  try{showQuestionBar(r.question||null)}catch(e){}
  try{refreshSubagents(r.session_state==='running')}catch(e){}
  updateRetryCountdown(r);
  // 刚 load() 过：那次已经把详情渲染完了，第一次 runtime 不要再拉一遍 2.8MB 并重渲染
  if(skipDetailOnce===myId){
   skipDetailOnce=null;
   lastDetail=r.detail||'';
   lastTotal=r.total_steps??lastTotal;
  }else if(r.total_steps!==lastTotal||(r.detail||'')!==lastDetail){
   lastDetail=r.detail||'';
   lastTotal=r.total_steps??lastTotal;
   // 详情请求失败不能变成 unhandled rejection（此前会让时间线永久停在旧数据且无提示）
   try{
    let d=await api('/v1/agent/sessions/'+encodeURIComponent(myId));
    if(window.current!==myId)return;
    renderStepsMaybe(d.steps);lastStepCount=d.steps.length;
    try{renderStream(d.messages||[],d.steps||[],{})}catch(e){}
   }catch(e){console.warn('时间线刷新失败:',(e&&e.text)||e)}}}

/* ── SSE 实时推送（EventSource）────────────────────────────────────
   有 SSE 时不再依赖轮询：runtime 快照直接推送 → 提供商流式输出就是真·逐字。
   EventSource 不能带自定义头，token 走 query 参数；连接失败自动退回轮询
   （保留 350ms 快速轮询），并按退避重连。 */
function stopEvents(){
 if(evtSrc){try{evtSrc.close()}catch(e){}}
 evtSrc=null;evtSid=null}
function startEvents(sid){
 stopEvents();
 if(!sid||!window.EventSource)return;
 let url=`/v1/agent/sessions/${encodeURIComponent(sid)}/events`;
 const _tk=apiToken();if(_tk)url+=`?token=${encodeURIComponent(_tk)}`;   // token 在 localStorage(gantry-token)，API_TOKEN 是服务端变量
 try{evtSrc=new EventSource(url)}catch(e){evtSrc=null;return}
 evtSid=sid;
 evtSrc.onopen=()=>{evtFails=0};
 evtSrc.onmessage=(e)=>{
  let ev=null;try{ev=JSON.parse(e.data)}catch(_){return}
  if(window.current!==sid)return;
  if(ev.type==='runtime')applyRuntime(ev.data);
  else if(ev.type==='queue')refreshQueue();
  else if(ev.type==='messages')refreshChat();
  else if(ev.type==='session')refreshSessions()};
 evtSrc.onerror=()=>{
  let failed=evtSid;
  stopEvents();evtFails++;
  if(window.current===failed&&evtFails<50)
   setTimeout(()=>{if(window.current===failed)startEvents(failed)},Math.min(3000*evtFails,15000))}}
/* 只刷新消息列表（SSE 通知有新消息时用，比整页 load 轻） */

export { setFollowupMode, resetView, load, runtime, stopEvents, stopTask, applyRuntime };
