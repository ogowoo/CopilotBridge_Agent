let current=null,currentUrl=null,currentModel=null,lastStepCount=-1,lastTotal=-1,lastDetail='',renderedSteps=0,sessCache=[];
let modelCache=[];   // /v1/models 的可用模型（会话页选择器复用）
let budget=0; // 由后端 token_budget 下发，避免与 config.json 脱节
let DEMO=false;

/* ── 演示模式：API 不可达时展示界面（仅预览用，正式环境不会触发） ── */
const DEMO_SESSIONS=[
 {id:'task-demo01',title:'分析 cbp 项目并生成 TODO',state:'running',model:'copilot-think-deeper',total_steps:47,conversation_steps:12,conversation_index:1,estimated_tokens:18320,workspace:'C:\\CopilotBridge\\workspace'},
 {id:'task-demo02',title:'整理 Outlook 未读邮件',state:'paused',total_steps:23,conversation_steps:23,conversation_index:0,estimated_tokens:9640,workspace:'C:\\CopilotBridge\\workspace'},
 {id:'task-demo03',title:'对比两个目录差异',state:'failed',total_steps:101,conversation_steps:4,conversation_index:3,estimated_tokens:23110,workspace:'C:\\CopilotBridge\\workspace'},
 {id:'task-demo04',title:'深圳英文旅游手册',state:'completed',total_steps:15,conversation_steps:15,conversation_index:0,estimated_tokens:7420,workspace:'C:\\CopilotBridge\\workspace'}];
const DEMO_STEPS=[
 {step:44,tool:'read_text',status:'ok',reason:'读取 agent.py 确认当前实现',result:{path:'agent.py',content:'import asyncio\nimport json\n\nclass AgentRunner:\n    async def run(self, task, model):\n        # 主循环：构造提示 → 等待回答 → 解析动作\n        while not done:\n            action = parse(await chat(prompt))\n',truncated:false}},
 {step:45,tool:'write_text_b64',status:'ok',reason:'写入修复后的 agent.py',result:{path:'agent.py',written_chars:7342,file_event:{tool:'write_text_b64',path:'agent.py',before_hash:'9f2c…',after_hash:'41ab…',result:'ok'}}},
 {step:46,status:'service_unavailable',error:'Copilot 服务暂时不可用（第 2 次）：We\'re temporarily unable to respond to this volume of requests. Please try again later.'},
 {step:47,status:'session_rollover',reason:'Token/步数/手动触发自动创建新 Copilot 会话'},
 {step:47,tool:'list_directory',status:'error',error:'目录不存在: CBP/pycache',reason:'检查字节码缓存时间状态'},
 {step:48,tool:'validate_python',status:'ok',reason:'修改后语法检查',result:{passed:true,checked:6,failures:[]}}];
const DEMO_MSG=[
 {role:'user',content:'现在做 todo 里的 P0 阻断项，每完成一个请在 todo 里打上勾'},
 {role:'assistant',content:'已完成 3 个 P0 项：会话切换风暴、暂停状态覆盖、限流识别。剩余 1 项进行中。'}];
function demoApi(u,o={}){
 if(u==='/v1/agent/sessions')return{sessions:DEMO_SESSIONS};
 if(u.endsWith('/runtime'))return{phase:'waiting',detail:'Agent 回合 a1b2c3d4e5f6 已确认，等待 Copilot 回答',session_state:'running',total_steps:47,conversation_steps:12,estimated_tokens:18320,conversation_index:1,current_url:'https://m365.cloud.microsoft/chat/demo',busy:true,updated:Date.now()/1000,answer_preview:'{"type":"tool","tool":"run_unittest","arguments":{"path":"tests"},"reason":"验证修复闭环"}',prompt_preview:'[CBP-AGENT][task-demo01][C001]\n你是本地任务 Agent，可以通过工具处理当前工作目录中的文件。\n…\n当前总步骤 48/1000。只返回下一个 JSON 动作。'};
 let m=u.match(/\/sessions\/([^/?]+)$/);
 if(m)return{session:DEMO_SESSIONS[0],steps:DEMO_STEPS,files:[],messages:DEMO_MSG};
 if(u.includes('/messages'))return{content:'（演示模式）此处为 Copilot 回复',url:null};
 if(o.method==='DELETE'&&u.includes('delete_remote=true'))return{deleted:false,local_deleted:false,message:'部分 Copilot 会话删除失败，本地 Session 已保留。可检查后使用下方按钮重试，或仅清理本地记录。',remote_results:[
  {url:'https://m365.cloud.microsoft/chat?es=SSR',expected_title:'[CBP-AGENT][task-demo01][C000]',deleted:false,error:'URL 对应会话不属于当前 Session，拒绝删除；需要前缀: [CBP-AGENT][task-demo01]',method:'url-plus-session-marker'},
  {url:'https://m365.cloud.microsoft/chat/conversation/8ae00748-77f1-4e5b-979e-8ff7d26d9791?es=SSR',expected_title:'[CBP-AGENT][task-demo01][C001]',deleted:true,error:null,method:'url-plus-session-marker'},
  {url:'https://m365.cloud.microsoft/chat/conversation/f3b21c90-5a4d-4c2e-8b71-1d2e3f4a5b6c?es=SSR',expected_title:'[CBP-AGENT][task-demo01][C002]',deleted:false,error:'点击删除后，目标 URL 仍存在于 Chats 列表',method:'url-plus-session-marker'}]};
 return{deleted:true,local_deleted:true,remote_results:[],session_id:'task-demo01'};
}

function apiToken(){try{return localStorage.getItem('cbp-token')||''}catch(e){return ''}}
/* 这个分支是 **预览版**：功能还没收尾（多窗口并行等）。
   页签标题带 -preview,方便一眼区分手上的页面是稳定版还是预览版。 */
const PREVIEW_BUILD='preview.2';
try{document.addEventListener('DOMContentLoaded',()=>{
 document.title=document.title.replace(/\s*-\s*preview.*$/i,'')+' - '+PREVIEW_BUILD})}catch(e){}
let lastMessages=[];        // 最近一次渲染的消息（对话搜索基于它重渲染；曾漏声明导致搜索抛异常）
let tokenPrompting=null;   // 并发 401 只弹一次：每秒轮询可能同时发起多个请求
let tokenPromptCancelled=false;  // 用户取消过令牌输入 → 不再每秒弹窗，改用常驻横幅
/* 结构化错误：以前抛原始响应体字符串，调用侧只能靠 msg.includes('401') 嗅探状态码——
   而错误体里并没有 "401" 这三个字符，所以那个分支从未命中，401 被当成网络故障进了
   演示模式（界面显示假数据、点"启动任务"不发任何真实请求）。现在带上 status。 */
class ApiError extends Error{
 constructor(status,body,url){super(body||('HTTP '+status));this.name='ApiError';this.status=status;
  this.body=body;this.url=url;
  try{const j=JSON.parse(body);if(j&&j.detail)this.detail=String(j.detail)}catch(e){}}
 get text(){return this.detail||this.body||('HTTP '+this.status)}}
/* 固定浮层分车道：把三个横幅/浮条的实际高度写进 CSS 变量，供 style.css 计算各自位置。
   元素是动态创建且高度随文案变化（窄屏换行），所以必须实测而不是写死。 */
function syncFixedLanes(){
 try{
  const h=id=>{const e=document.getElementById(id);
   if(!e||getComputedStyle(e).display==='none')return 0;
   return Math.round(e.getBoundingClientRect().height)};
  const rs=document.documentElement.style;
  rs.setProperty('--banner-h', h('connBanner')+'px');
  rs.setProperty('--boot-h', h('bootBar')+'px');
  // 限流卡与小标是同一位置的两种形态（展开/收起），取当前可见那个的高度
  // （授权横幅已改为流内元素，不再需要 --approval-h）
  // 限流卡已改为**全屏遮罩**（不占布局），小标 retryChip 仍是浮层但很矮：
 // 这里只按小标高度预留，遮罩不再撑开 live stream 与输入框之间的间距。
 rs.setProperty('--retry-h', h('retryChip')+'px');
  // 提示信息已统一到顶部居中显示：不再占底部空间，故不计算 --toast-h
  rs.setProperty('--toast-h', '0px');
 }catch(e){}}
window.addEventListener('resize',()=>syncFixedLanes());
setInterval(syncFixedLanes,1500);   // 文案变化/换行导致高度变化时靠它跟上（成本极低）

function showConnBanner(msg,clickable){
 let el=document.getElementById('connBanner');
 if(!el){el=document.createElement('div');el.id='connBanner';el.className='connbanner';
  document.body.appendChild(el);
  el.onclick=()=>{if(el.dataset.retry==='1'){tokenPromptCancelled=false;el.style.display='none';location.reload()}}}
 el.textContent=msg;el.dataset.retry=clickable?'1':'0';el.style.display='block';syncFixedLanes();
 el.style.cursor=clickable?'pointer':'default'}
function hideConnBanner(){const el=document.getElementById('connBanner');if(el)el.style.display='none';syncFixedLanes()}
/* ── 破坏性工具授权（一次性审批的 UI 入口）──────────────────────────
   后端在需要授权时把会话置为 waiting 并写入 approval_pending{tool,args_hash}。
   没有这个入口，用户只能手工 curl —— 审批功能等于不可用。 */
let approvalSeen=null;
/* ── 中断提醒的"抢注意力"──
   需要授权/等待回答这类中断没人理，整个任务就停在那里；用户实测"晚了好久才回答"。
   这里做三件事（都不阻塞、不需要额外权限）：
   ① 横幅本身发光/呼吸（CSS .needs-attn）；
   ② 浏览器标签标题闪烁「⚠ 需要授权」,用户切回来/处理完就恢复；
   ③ 页面在后台时用 WebAudio 轻响一声（首次交互后才允许，失败静默）。 */
let _titleTimer=null, _titleBase=null;
function pingInterrupt(text){
 try{
  if(_titleTimer)clearInterval(_titleTimer);
  _titleBase=_titleBase||document.title;
  const hit='⚠ '+String(text||'').slice(0,24);
  let flip=false;
  _titleTimer=setInterval(()=>{document.title=flip?hit:_titleBase;flip=!flip},900);
  // 处理完（横幅消失）由 clearInterruptPing 恢复
 }catch(e){}
 try{ if(document.hidden) beepOnce() }catch(e){}
}
function clearInterruptPing(){
 try{ if(_titleTimer){clearInterval(_titleTimer);_titleTimer=null;document.title=_titleBase||document.title} }catch(e){}
 try{ document.querySelectorAll('.needs-attn').forEach(x=>x.classList.remove('needs-attn')) }catch(e){}
}
let _audioCtx=null;
function beepOnce(){
 try{
  _audioCtx=_audioCtx||new (window.AudioContext||window.webkitAudioContext)();
  const t=_audioCtx.currentTime, o=_audioCtx.createOscillator(), g=_audioCtx.createGain();
  o.type='sine';o.frequency.setValueAtTime(880,t);
  g.gain.setValueAtTime(0.0001,t);g.gain.exponentialRampToValueAtTime(0.08,t+0.02);
  g.gain.exponentialRampToValueAtTime(0.0001,t+0.35);
  o.connect(g);g.connect(_audioCtx.destination);o.start(t);o.stop(t+0.36);
 }catch(e){}
}

let approvalDismissed=null;   // 用户点过「暂不授权」的 key：同一个待授权不再自动弹出
function showApprovalBar(p,scope){
 let el=document.getElementById('approvalBar');
 if(!p||!p.tool){if(el)el.style.display='none';approvalSeen=null;approvalDismissed=null;return}
 if(!el){el=document.createElement('div');el.id='approvalBar';el.className='approvalbar';
  // **流内**：插到会话视图顶部，占位把内容推开，而不是浮在上面盖住发送按钮/时间线。
  // 实测：作为 fixed 浮层时，窄屏会遮住 #injectBtn/#chatFindToggle 43%、#chatlog 34%。
  const host=document.getElementById('sessionView')||document.body;
  host.insertBefore(el,host.firstChild)}
 const key=(current||'')+'|'+p.tool+'|'+(p.args_hash||'');
 // 已拒绝过的同一个待授权保持隐藏：此前「暂不授权」只是本地 display:none，
 // 而 applyRuntime 每秒带着同一个 approval_pending 再调一次 → 横幅自己弹回来。
 if(approvalDismissed===key){if(el)el.style.display='none';return}
 if(approvalSeen!==key){approvalSeen=key;
  el.innerHTML='';
  const span=document.createElement('span');
  const _sess=scope==='session';
  span.textContent=tr('需要授权：')+p.tool+( _sess
    ? ' '+tr('属于不可回滚操作；授权仅对本会话有效（会话结束即失效）')
    : ' '+tr('属于不可回滚操作；授权仅对这一次执行有效（用后即焚）'));
  const btn=document.createElement('button');
  btn.className='btn accent';btn.type='button';btn.textContent=tr('授权执行');
  btn.onclick=async()=>{
   btn.disabled=true;btn.textContent=tr('授权中…');
   try{
    await api(`/v1/agent/sessions/${encodeURIComponent(current)}/approvals`,
              {method:'POST',headers:{'Content-Type':'application/json'},
               body:JSON.stringify({tool:p.tool,args_hash:p.args_hash||'',scope:_sess?'session':'once'})});
    await api(`/v1/agent/sessions/${encodeURIComponent(current)}/actions/resume`,{method:'POST'});
    toast(_sess?tr('已授权并继续执行（本会话内不再逐次询问）')
               :tr('已授权并继续执行（该授权仅对这一次操作有效）'),'ok');
    el.style.display='none';approvalSeen=null;clearInterruptPing();await runtime();
   }catch(e){btn.disabled=false;btn.textContent=tr('授权执行');toast(tr('授权失败: ')+(e.text||e),'err')}};
  const no=document.createElement('button');
  no.className='btn ghost';no.type='button';no.textContent=tr('暂不授权');
  no.onclick=()=>{el.style.display='none';approvalDismissed=key;
    clearInterruptPing()};   // 记住这次拒绝；用户已确认看到，停止闪烁
  el.appendChild(span);el.appendChild(btn);el.appendChild(no)}
 el.style.display='flex';
 // 抢注意力：中断类提醒（需要授权）错过就会卡住整个任务，必须显眼
 try{el.setAttribute('role','alert');el.classList.add('needs-attn');
  el.scrollIntoView({block:'nearest'});pingInterrupt(tr('需要授权：')+p.tool)}catch(e){}}

/* ── Agent 中途提问（ask_user_question 的 UI 入口）────────────────────
   工具阻塞等待 /answer；用户在这里填答案时任务暂停，提交后继续。 */
let questionSeen=null;
// 已答过的提问（key = 会话|问题文本）：即使后端快照里还残留 question 字段，
// 也不再把这条提问画回来（用户实测：回答完它还在，再点只报"没有等待回答的问题"）。
const questionAnswered=new Set();
function questionKey(q){return (current||'')+'|'+((q&&q.text)||'')}
function showQuestionBar(q){
 let el=document.getElementById('questionBar');
 if(!q||!q.text){if(el)el.style.display='none';questionSeen=null;return}
 if(questionAnswered.has(questionKey(q))){if(el)el.style.display='none';return}
 if(!el){el=document.createElement('div');el.id='questionBar';el.className='questionbar';
  const host=document.getElementById('sessionView')||document.body;
  host.insertBefore(el,host.firstChild)}
 const key=(current||'')+'|'+q.text;
 if(questionSeen!==key){questionSeen=key;
  el.innerHTML='';
  const span=document.createElement('span');span.className='qtext';
  span.textContent=tr('Agent 提问：')+q.text;
  const inp=document.createElement('input');inp.type='text';inp.className='qinput';
  inp.placeholder=tr('输入你的回答…');
  (q.options&&q.options.length?q.options:[]).forEach(o=>{
   const chip=document.createElement('button');chip.type='button';chip.className='btn ghost qchip';
   chip.textContent=o;chip.onclick=()=>{inp.value=o;inp.focus()};el.appendChild(chip)});
  const ok=document.createElement('button');ok.type='button';ok.className='btn accent';
  ok.textContent=tr('提交回答');
  const submit=async()=>{
   ok.disabled=true;ok.textContent=tr('提交中…');
   try{
    await api(`/v1/agent/sessions/${encodeURIComponent(current)}/answer`,
              {method:'POST',headers:{'Content-Type':'application/json'},
               body:JSON.stringify({answer:inp.value})});
    questionAnswered.add(questionKey(q));
    toast(tr('已提交回答，任务继续'),'ok');
    el.style.display='none';questionSeen=null;clearInterruptPing();await runtime();
   }catch(e){
    // 404/409 = 已经没有挂起提问（重复提交、或已在别处答过）：不是错误，
    // 直接把提问条收掉并记账，避免它永远挂在界面上。
    const _msg=String((e&&(e.text||e.message))||e||'');
    if((e&&(e.status===404||e.status===409))||/没有等待回答|no pending/i.test(_msg)){
     questionAnswered.add(questionKey(q));
     el.style.display='none';questionSeen=null;clearInterruptPing();
     toast(tr('该提问已回答或已过期，已收起'),'ok');await runtime();return}
    ok.disabled=false;ok.textContent=tr('提交回答');toast(tr('提交失败: ')+_msg,'err')}};
  ok.onclick=submit;
  inp.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();submit()}};
  el.appendChild(span);el.appendChild(inp);el.appendChild(ok)}
 el.style.display='flex';
 try{el.setAttribute('role','alert');el.classList.add('needs-attn');
  el.scrollIntoView({block:'nearest'});pingInterrupt(tr('等待你的回答'))}catch(e){}}

/* ── 子代理面板（看到有哪些子代理、在跑什么、进度到哪）──────────────
   数据来自 GET /v1/agent/subagents；点一行可切到该子会话查看详情。 */
let lastSubagentFetch=0, subagentBusy=false;
let lastSubagentParent=null;
let parentOfCurrent=null;   // 当前会话若是子代理，这里是父会话 id
async function refreshSubagents(force){
 const now=Date.now();
 if(subagentBusy) return;
 // 切了会话必须立刻刷新（否则会短暂显示**上一个会话**的子代理，看起来像"串台"）
 if(current!==lastSubagentParent){force=true;lastSubagentParent=current}
 if(!force && now-lastSubagentFetch<3000) return;
 // **必须按父会话过滤**：不带 parent 会返回全部子代理，面板里就会混进别的任务的
 // 子代理（实测：用户真实会话里冒出 19 个与它无关的子代理）。
 if(!current){renderSubagents([]);return}
 subagentBusy=true;lastSubagentFetch=now;
 try{
  // 当前会话本身可能是子代理：此时按它的**父会话**查询，面板才不会一片空白
  const _scope=parentOfCurrent||current;
  const d=await api('/v1/agent/subagents?parent='+encodeURIComponent(_scope));
  renderSubagents(d.subagents||[]);
 }catch(e){}
 finally{subagentBusy=false}
}
/* 子代理面板折叠：状态记在 localStorage，刷新后保持。 */
/* 实时流面板收起/展开：状态记 localStorage，刷新后保持。 */



/* 面板收起：**点标题行或左侧 ▾** 收起/展开（与 todo 的交互一致）。
   支持两块：data-arg="subagents"（子代理）/ "live"（实时流）。
   状态写 localStorage，刷新后保持；点按钮/标签/下拉不会误触发收起。 */
function cardOf(kind){
 if(kind==='subagents')return document.getElementById('subagentBar');
 if(kind==='live'){
  const chat=document.getElementById('chat');
  return chat?chat.closest('section.card'):null;
 }
 return null;
}
function applyCardCollapsed(){
 [['subagents','cbp.subagentsCollapsed'],['live','cbp.liveCollapsed']].forEach(([kind,key])=>{
  const card=cardOf(kind);if(!card)return;
  let collapsed=false;
  try{collapsed=localStorage.getItem(key)==='1'}catch(e){}
  card.classList.toggle('collapsed',collapsed);
 });
}
function toggleCard(kind){
 const card=cardOf(kind);
 if(!card)return;
 const collapsed=!card.classList.contains('collapsed');
 card.classList.toggle('collapsed',collapsed);
 try{localStorage.setItem(kind==='subagents'?'cbp.subagentsCollapsed':'cbp.liveCollapsed',
                          collapsed?'1':'0')}catch(e){}
}
/* 点标题行 = 切换；但点到标签/按钮/输入框时不动 */
function onCardHeadClick(ev){
 const head=ev.target.closest && ev.target.closest('.chead');
 if(!head)return;
 if(ev.target.closest('button.tab,button.btn,select,input,a,.cardchev'))return;
 // 最近的折叠目标：子代理块（现在在 #teamCard 里，不再是 section.card）
 if(head.closest('#subagentBar')){toggleCard('subagents');return}
 const card=head.closest('section.card');
 if(!card)return;
 if(card.contains(document.getElementById('chat')))toggleCard('live');
}
document.addEventListener('click',(ev)=>{
 const t=ev.target;
 if(!t||!t.closest)return;
 const trow=t.closest('header.topbar>.trow');
 if(!trow)return;
 if(t.closest('button,select,input,textarea,a,#sidChip,[data-act]'))return;
 toggleTopbar();
});

/* ── 任务条（顶栏）折叠：收起后只留标题行（状态行 / 按钮行 / KPI 一起收）──
   展开态 3 行 ≈123px，收起后 ≈57px —— 想多看对话时能把这块收掉。
   状态记 localStorage，跨刷新保留；点箭头或点标题都能切换。 */
function applyTopbarCollapsed(){
 const head=document.querySelector('header.topbar');
 if(!head)return;
 let collapsed=false;
 try{collapsed=localStorage.getItem('cbp.topbarCollapsed')==='1'}catch(e){}
 head.classList.toggle('collapsed',collapsed);
 const btn=document.getElementById('topbarChev');
 if(btn)btn.setAttribute('aria-expanded',collapsed?'false':'true');
}
function toggleTopbar(){
 const head=document.querySelector('header.topbar');
 if(!head)return;
 const collapsed=!head.classList.contains('collapsed');
 head.classList.toggle('collapsed',collapsed);
 const btn=document.getElementById('topbarChev');
 if(btn)btn.setAttribute('aria-expanded',collapsed?'false':'true');
 try{localStorage.setItem('cbp.topbarCollapsed',collapsed?'1':'0')}catch(e){}
}
/* 点标题行的任意空白处也能收起/展开（用户要求"点上面那部分就能折叠"）。
   里面的控件各自有活（模型下拉、刷新、任务 ID 点击复制、箭头、标题的 data-act），
   这些不抢 —— 只处理落在空白/文字上的点击。 */
document.addEventListener('click',(ev)=>{
 const t=ev.target;
 if(!t||!t.closest)return;
 const trow=t.closest('header.topbar>.trow');
 if(!trow)return;
 if(t.closest('button,select,input,textarea,a,#sidChip,[data-act]'))return;
 toggleTopbar();
});
document.addEventListener('click',onCardHeadClick);
/* 调用记录浮层：点别处 / Esc 关闭（点行本身与浮层内部不关） */
document.addEventListener('click',ev=>{
 if(!toolLogOpen)return;
 const t=ev.target;
 if(t.closest&&(t.closest('#toolLogPop')||t.closest('#toolLogLatest')))return;
 toggleToolLogPop(false);
},true);
document.addEventListener('keydown',ev=>{
 if(ev.key==='Escape'&&toolLogOpen)toggleToolLogPop(false);
});

/* 待办条：点横条任意位置都能展开/收起（与状态条一致的交互）。
   内部 `.todolist` 里的条目、以及「待办 x/y」按钮自己会处理点击，别重复触发。 */
document.addEventListener('click',ev=>{
 const bar=ev.target.closest && ev.target.closest('#goalBar');
 if(!bar)return;
 if(ev.target.closest('.todolist'))return;
 if(ev.target.closest('[data-act="toggleTodos"]'))return;   // 按钮自己会切
 try{toggleTodos()}catch(e){}
});

function renderSubagents(list){
 if(!current){list=[]}
 const bar=document.getElementById('subagentBar');
 const box=document.getElementById('subagentList');
 const cnt=document.getElementById('subagentCount');
 if(!bar||!box) return;
 // 正在看子代理时即使它自己没有下级，也要保留面板 + 返回入口（否则用户被困住）
 if(!list.length && !parentOfCurrent){bar.style.display='none';box.innerHTML='';if(cnt)cnt.textContent='';try{syncTeamCard()}catch(e){};return}
 // 用 'block' 而非 ''：'' 会回退到样式表里的 .subagents{display:none}（实测踩到）
 bar.style.display='block';
 const running=list.filter(s=>s.state==='running').length;
 cnt.textContent=tr(' · {n} 个',{n:list.length})+(running?tr('（{r} 个运行中）',{r:running}):'');
 box.innerHTML='';
 // 返回父会话（只有当前会话是子代理时才出现）
 if(parentOfCurrent){
  const back=document.createElement('div');
  back.className='subagentrow subagentback';
  back.dataset.act='openSubagent';back.dataset.arg=parentOfCurrent;
  back.title=tr('返回父会话');
  const bd=document.createElement('span');bd.textContent='←';
  const bt=document.createElement('span');bt.className='sa-desc';
  bt.textContent=tr('返回父会话')+' · '+parentOfCurrent;
  back.appendChild(bd);back.appendChild(bt);box.appendChild(back)}
 list.slice(0,8).forEach(s=>{
  const row=document.createElement('div');
  const _sid=s.agent_id||s.id||'';
  row.className='subagentrow'+(_sid===current?' sel':'');
  row.dataset.act='openSubagent';row.dataset.arg=_sid;
  const dot=document.createElement('span');
  dot.className=s.state==='running'?'dot-on':(s.state==='failed'?'dot-bad':'dot-off');
  const desc=document.createElement('span');desc.className='sa-desc';
  // 子代理描述是模型给的任务文本（数据），不参与 i18n 覆盖率判定
  desc.setAttribute('data-i18n-skip','1');
  desc.textContent=s.description||s.task||_sid;
  const meta=document.createElement('span');meta.className='sa-meta';
  meta.textContent=stateText(s.state)+' · '+s.steps+' '+tr('步')+(s.phase?' · '+s.phase:'');
  row.appendChild(dot);row.appendChild(desc);row.appendChild(meta);
  box.appendChild(row)});
 try{syncTeamCard()}catch(e){}
   // 非空路径以前从不调用 syncTeamCard：#teamCard 在“空列表”那次被 hidden 之后
   // 就再也没被显示回来，于是点进子代理后整块面板消失（用户实测）。
}

/* ── 插件管理（对应 cbp/plugins.py 的贡献点与启停）──────────────────── */
/* ── 插件面板（声明式）：插件给数据，前端按 view 渲染 ──────────────
   view: kv / list / table / json / markdown。插件不注入 HTML，避免 XSS。 */
let panelCache=[];
function _panelUrl(p){ return '/v1/plugins/'+encodeURIComponent(p.plugin)+'/api/'+String(p.path||'').replace(/^\/+/, ''); }
function _panelData(p){
 return api(_panelUrl(p), {method:p.method||'GET'});
}
function _renderKv(obj){
 if(!obj||typeof obj!=='object'||Array.isArray(obj))return _renderJson(obj);
 const rows=Object.entries(obj).map(([k,v])=>{
  const val=(v&&typeof v==='object')?JSON.stringify(v):String(v===null||v===undefined?'':v);
  return '<tr><th>'+esc(k)+'</th><td>'+esc(val)+'</td></tr>';});
 return '<table class="ptable">'+rows.join('')+'</table>';
}
function _renderList(arr){
 if(!Array.isArray(arr))return _renderJson(arr);
 if(!arr.length)return '<div class="meta">'+esc(tr('（空）'))+'</div>';
 return '<ul class="plist">'+arr.map(x=>'<li>'+esc(typeof x==='object'?JSON.stringify(x):String(x))+'</li>').join('')+'</ul>';
}
function _renderTable(arr){
 if(!Array.isArray(arr))return _renderJson(arr);
 if(!arr.length)return '<div class="meta">'+esc(tr('（空）'))+'</div>';
 const cols=[...new Set(arr.flatMap(x=>Object.keys(x||{})))];
 const head='<tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr>';
 const body=arr.map(x=>'<tr>'+cols.map(c=>{
  const v=x&&x[c]; const s=(v&&typeof v==='object')?JSON.stringify(v):(v===null||v===undefined?'':String(v));
  return '<td>'+esc(s)+'</td>';}).join('')+'</tr>').join('');
 return '<table class="ptable">'+head+body+'</table>';
}
function _renderJson(v){
 try{ return '<pre class="pjson">'+esc(JSON.stringify(v,null,2))+'</pre>' }
 catch(e){ return '<pre class="pjson">'+esc(String(v))+'</pre>' }
}
function _renderPanel(p,data){
 const body=document.getElementById('panelBody');
 if(!body)return;
 const box=(data&&typeof data==='object'&&!Array.isArray(data)&&Array.isArray(data[p.id])
            ? data[p.id] : (data&&data.items!==undefined?data.items:data));
 let html='';
 const v=p.view||'kv';
 if(v==='table')html=_renderTable(Array.isArray(box)?box:_firstArray(data));
 else if(v==='list')html=_renderList(Array.isArray(box)?box:_firstArray(data));
 else if(v==='json')html=_renderJson(data);
 else if(v==='markdown')html='<div class="md-body">'+mdToHtml(String((data&&(data.text||data.markdown))||''))+'</div>';
 else html=_renderKv(box);
 body.innerHTML=html;
 if(p.hint)body.insertAdjacentHTML('afterbegin','<div class="meta">'+esc(tr(p.hint))+'</div>');
}
function _firstArray(data){
 if(Array.isArray(data))return data;
 if(data&&typeof data==='object'){
  for(const k of Object.keys(data)) if(Array.isArray(data[k])) return data[k];
 }
 return [];
}
async function openPanels(){
 openMask('panelMask');
 await refreshPanels(true);
}
async function refreshPanels(silent){
 const list=document.getElementById('panelList'), body=document.getElementById('panelBody');
 if(!list||!body)return;
 let panels=[];
 try{ const d=await api('/v1/plugins/panels'); panels=(d&&d.panels)||[] }
 catch(e){ list.innerHTML='<div class="meta">'+esc(tr('读取失败: ')+(e.text||e))+'</div>'; return }
 panelCache=panels;
 if(!panels.length){ list.innerHTML='<div class="meta">'+esc(tr('（没有面板）'))+'</div>'; body.innerHTML=''; return }
 list.innerHTML='';
 panels.forEach((p,i)=>{
  const row=document.createElement('div');
  row.className='panelrow'+(i===0?' on':'');
  row.setAttribute('data-i18n-skip','1');     // 面板标题来自插件（数据）
  row.textContent=tr(p.title||p.id);
  row.title=p.plugin+' · '+p.view;
  row.onclick=()=>{ [...list.children].forEach(c=>c.classList.remove('on')); row.classList.add('on'); loadPanel(p) };
  list.appendChild(row);
 });
 await loadPanel(panels[0]);
}
async function loadPanel(p){
 const body=document.getElementById('panelBody');
 if(!body)return;
 body.innerHTML='<div class="meta">'+esc(tr('加载中…'))+'</div>';
 try{
  const data=await _panelData(p);
  _renderPanel(p,data);
 }catch(e){
  body.innerHTML='<div class="meta">'+esc(tr('读取失败: ')+(e.text||e))+'</div>';
 }
}
function closePanels(){closeMask('panelMask')}

async function openPlugins(){
 openMask('pluginsMask');
 const box=document.getElementById('pluginsBody');
 if(!box) return;
 box.innerHTML='<div class="meta">'+esc(tr('加载中…'))+'</div>';
 let list=[];
 try{ const d=await api('/v1/plugins'); list=d.plugins||[] }catch(e){
  box.innerHTML='<div class="meta">'+esc(tr('读取插件失败: ')+(e.text||e))+'</div>';return}
 if(!list.length){ box.innerHTML='<div class="meta">'+esc(tr('没有安装插件（把插件放进 plugins/ 目录）'))+'</div>';return}
 box.innerHTML='';
 list.forEach(p=>{
  const row=document.createElement('div');row.className='pluginrow';
  const head=document.createElement('div');head.className='phead';
  const name=document.createElement('b');name.textContent=p.name+'  ';
  const ver=document.createElement('span');ver.className='meta';ver.textContent='v'+(p.version||'')+' · ';
  const st=document.createElement('span');st.className='pstat '+(p.status==='loaded'?'on':(p.status==='error'?'bad':''));
  st.textContent=stateText(p.status==='loaded'?'已启用':(p.status==='disabled'?'已禁用':'未加载'));
  head.appendChild(name);head.appendChild(ver);head.appendChild(st);
  const desc=document.createElement('div');desc.className='meta';
  desc.setAttribute('data-i18n-skip','1');   // 插件作者给的描述：数据，不参与覆盖率审计
  desc.textContent=p.description||'';
  const meta=document.createElement('div');meta.className='meta';
  meta.setAttribute('data-i18n-skip','1');   // 贡献点是结构化数据
  meta.textContent=tr('权限: ')+(p.permissions||[]).join(', ')+' · '+tr('贡献: ')+JSON.stringify(p.contributes||{});
  const acts=document.createElement('div');acts.className='pacts';
  const btn=document.createElement('button');btn.type='button';btn.className='btn';
  btn.textContent=p.enabled?tr('禁用'):tr('启用');
  btn.onclick=async()=>{ btn.disabled=true;
    try{ await api('/v1/plugins/'+encodeURIComponent(p.name)+'/'+(p.enabled?'disable':'enable'),{method:'POST'});
      toast(tr('已')+(p.enabled?tr('禁用'):tr('启用'))+': '+p.name,'ok'); await openPlugins();mergePluginI18n(false); }
    catch(e){ btn.disabled=false; toast(tr('操作失败: ')+(e.text||e),'err') } };
  const rb=document.createElement('button');rb.type='button';rb.className='btn ghost';rb.textContent=tr('重载');
  rb.onclick=async()=>{ try{ await api('/v1/plugins/'+encodeURIComponent(p.name)+'/reload',{method:'POST'});
      toast(tr('已重载: ')+p.name,'ok'); await openPlugins(); }catch(e){ toast(tr('重载失败: ')+(e.text||e),'err') } };
  acts.appendChild(btn);acts.appendChild(rb);
  row.appendChild(head);row.appendChild(desc);row.appendChild(meta);row.appendChild(acts);
  if(p.error){ const er=document.createElement('div');er.className='perr';er.textContent=p.error; row.appendChild(er) }
  box.appendChild(row)});
}
function closePlugins(){closeMask('pluginsMask')}

/* ── 斜杠命令（/）：模式与工具的快捷入口 ─────────────────────────
   输入框以 "/" 开头时列出命令；↑↓ 选择、Tab/Enter 补全、Esc 关闭。
   带参数直接回车即执行（/research 问题…），不带参数则补全后等输入。 */
const SLASH_COMMANDS=[
 {id:'research',label:'research',aliases:['research','深研','dr'],
  desc:'深研：多角度搜索 + 抓正文，生成带来源的报告',
  run:(arg)=>{
   if(!arg){toast(tr('用法：/research 你的问题'),'err');return}
   const t=document.getElementById('task'); if(t)t.value='';
   deepResearchNow(arg)}},
 {id:'fanout',label:'fanout',aliases:['fanout','扇出','并行'],
  desc:'扇出：把多个互不依赖的任务各派一个子代理（用 | 分隔），不经模型判断',
  run:(arg)=>{
   const tasks=String(arg||'').split('|').map(x=>x.trim()).filter(Boolean);
   if(tasks.length<2){toast(tr('用法：/fanout 任务1 | 任务2 | 任务3（至少两个）'),'err');return}
   fanoutNow(tasks)}},
 {id:'audit',label:'audit',aliases:['audit','审计','对抗审计','review'],
  desc:'对抗式审计：多名审计员 + 裁判核验，循环到只剩小问题',
  run:(arg)=>{auditNow(arg)}},
 {id:'agent',label:'agent',aliases:['agent','执行'],
  desc:'切到 Agent 执行模式',run:()=>{setTaskMode('agent');toast(tr('已切到 Agent 执行'),'ok')}},
 {id:'chat',label:'chat',aliases:['chat','对话'],
  desc:'切到普通对话模式',run:()=>{setTaskMode('chat');toast(tr('已切到普通对话'),'ok')}},
 {id:'goal',label:'goal',aliases:['goal','目标'],
  desc:'目标模式：建立目标后多轮推进，未完成不会停',
  run:(arg)=>{
   if(!arg){toast(tr('用法：/goal 你的目标'),'err');return}
   const t=document.getElementById('task');
   if(t)t.value=arg+'\n\n'+'[目标模式] 请把上面这件事当作一个长期**目标**：'
    +'先调用 create_goal 记录下来（max_rounds 取 8-12），必要时用 todo_write 拆出 3-8 个可执行子步骤；'
    +'然后分多轮持续推进，每轮做完一部分就继续做下一部分，目标未达成时不要返回 final；'
    +'只有目标真正达成（或确实被外部条件阻塞）时，先调用 update_goal（complete / blocked）再返回 final。';
   runTask()}},
 {id:'plan',label:'plan',aliases:['plan','计划'],
  desc:'计划模式（只读）：探索后产出方案，exit_plan_mode 提交',
  run:(arg)=>{
   if(!arg){toast(tr('用法：/plan 你的任务'),'err');return}
   const t=document.getElementById('task');
   if(t)t.value=arg;
   planModeOnce=true;        // 由后端强制只读，不只靠提示词
   runTask()}},
 {id:'help',label:'help',aliases:['help','?','帮助'],
  desc:'列出全部可用命令',run:()=>{const t=document.getElementById('task');
   if(t){t.value='/';slashUpdate(true);t.focus()}}},
];
let slashIdx=0;
function slashMatch(q){
 const s=(q||'').toLowerCase();
 if(!s)return SLASH_COMMANDS.slice();
 return SLASH_COMMANDS.filter(c=>c.aliases.some(a=>a.toLowerCase().startsWith(s)));
}
function slashOpen(){
 const box=document.getElementById('slashMenu');
 return !!(box&&!box.hidden);
}
function slashUpdate(force){
 const t=document.getElementById('task'),box=document.getElementById('slashMenu');
 if(!t||!box)return;
 const m=/^\/(\S*)$/.exec(t.value);
 if(!m&&!force){box.hidden=true;box.innerHTML='';return}
 const items=slashMatch(m?m[1]:'');
 if(!items.length){box.hidden=true;box.innerHTML='';return}
 if(slashIdx>=items.length)slashIdx=0;
 box.hidden=false;box.innerHTML='';
 items.forEach((c,i)=>{
  const row=document.createElement('div');
  row.className='slashrow'+(i===slashIdx?' on':'');
  const sd=document.createElement('span');sd.className='sdesc';
  if(c.raw)sd.setAttribute('data-i18n-skip','1');   // MCP 模板描述是数据
  sd.textContent=tr(c.desc);
  row.innerHTML='<code>/'+esc(c.label)+'</code>';
  row.appendChild(sd);
  row.onmousedown=(ev)=>{ev.preventDefault();slashPick(c)};
  box.appendChild(row)});
}
/* MCP prompt 模板 → 动态斜杠命令（/server.模板名 参数1 参数2 …）。
   模板是 server 给的**数据**，描述不参与 i18n 覆盖率判定（raw:true）。 */
let mcpPromptSig='';
async function loadMcpPrompts(){
 try{
  const d=await api('/v1/plugins/mcp/api/prompts');
  const prompts=(d&&d.prompts)||[];
  const sig=JSON.stringify(prompts.map(p=>[p.server,p.name,p.description]));
  if(sig===mcpPromptSig)return;              // 没变化就别重建
  mcpPromptSig=sig;
  for(let i=SLASH_COMMANDS.length-1;i>=0;i--) if(SLASH_COMMANDS[i].mcp) SLASH_COMMANDS.splice(i,1);
  prompts.forEach(p=>{
   const label=p.server+'.'+p.name;
   const argNames=(p.arguments||[]).map(a=>a.name).filter(Boolean);
   SLASH_COMMANDS.push({
    id:'mcp_'+p.server+'_'+p.name, label, aliases:[label], raw:true,
    desc:p.description||('MCP '+p.server+' 的 '+p.name+' 模板'),
    mcp:{server:p.server,name:p.name,argNames},
    run:(arg)=>{
     const parts=String(arg||'').trim()?String(arg).trim().split(/\s+/) : [];
     const args={};
     argNames.forEach((n,i)=>{ if(parts[i]!==undefined) args[n]=parts[i] });
     const missing=argNames.filter(n=>args[n]===undefined);
     if(missing.length){ toast(tr('用法：/')+label+' '+missing.map(n=>'<'+n+'>').join(' '),'err'); return }
     post('/v1/plugins/mcp/api/prompt',{server:p.server,name:p.name,arguments:args})
      .then(r=>{
       if(!r||r.error){ toast(tr('模板渲染失败: ')+(r&&r.error||''),'err'); return }
       const t=document.getElementById('task');
       if(t){ t.value=(r.text||''); t.focus(); t.setSelectionRange(t.value.length,t.value.length) }
       toast(tr('已载入模板: ')+label,'ok');
      })
      .catch(e=>toast(tr('模板渲染失败: ')+(e.text||e),'err'));
    }});
  });
 }catch(e){ /* 没装/没配 MCP 时静默 */ }
}
function post(u,body){
 return api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
}

function slashPick(c){
 const t=document.getElementById('task');if(!t)return;
 t.value='/'+c.label+' ';t.focus();
 const box=document.getElementById('slashMenu');if(box)box.hidden=true;
}
function slashKey(e){
 const box=document.getElementById('slashMenu');
 if(!box||box.hidden)return false;
 const rows=[...box.querySelectorAll('.slashrow')];
 const n=Math.max(rows.length,1);
 if(e.key==='ArrowDown'){slashIdx=(slashIdx+1)%n;slashUpdate(true);e.preventDefault();return true}
 if(e.key==='ArrowUp'){slashIdx=(slashIdx-1+n)%n;slashUpdate(true);e.preventDefault();return true}
 if(e.key==='Enter'){
  // 已是**完整命令**（/chat、/help、/research 问题…）→ 直接执行；
  // 只有还在输入前缀时才补全。否则 /help 会被当成补全、永远执行不了。
  if(slashRun(document.getElementById('task').value)){e.preventDefault();return true}
  const m1=/^\/(\S*)$/.exec(document.getElementById('task').value||'');
  const c1=slashMatch(m1?m1[1]:'')[slashIdx];
  if(c1){slashPick(c1);e.preventDefault();return true}
 }
 if(e.key==='Tab'){
  const m=/^\/(\S*)$/.exec(document.getElementById('task').value||'');
  const c=slashMatch(m?m[1]:'')[slashIdx];
  if(c){slashPick(c);e.preventDefault();return true}
 }
 if(e.key==='Escape'){box.hidden=true;box.innerHTML='';e.preventDefault();return true}
 return false;
}

/* 插件自带的译文：拉一次并合并进字典，让 tr() 能覆盖插件文案。
   平台不知道第三方插件写了哪些中文，由插件清单声明（cbp.i18n）最不失真。 */
async function mergePluginI18n(quiet){
 try{
  const d=await api('/v1/plugins/i18n');
  const en=d.en||{}, tw=d.tw||{};
  let n=0;
  Object.keys(en).forEach(k=>{ if(I18N_EN[k]!==en[k]){I18N_EN[k]=en[k];n++} });
  Object.keys(tw).forEach(k=>{ if(I18N_TW_OVERRIDE[k]!==tw[k]){I18N_TW_OVERRIDE[k]=tw[k];n++} });
  if(n){ applyI18n(document); if(!quiet) toast(tr('已合并 {n} 条插件译文',{n:n}),'ok') }
  return n;
 }catch(e){ return 0 }
}
/* 显式扇出：不经过模型判断，直接把 N 个任务派给 N 个子代理（确定性）。
   与"让模型自己决定要不要用子代理"互补——弱模型经常有捷径就自己串行做完了。 */
async function fanoutNow(tasks){
 closeDlg();
 toast(tr('已派出 {n} 个子代理，正在跑…',{n:tasks.length}),'ok');
 try{
  const d=await api('/v1/agent/fanout',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({tasks,model:currentModel||undefined})});
  const ok=d.succeeded||0,fail=d.failed||0;
  toast(tr('扇出完成：成功 {a} · 失败 {b} · {t} 秒',{a:ok,b:fail,t:d.seconds||0}),
        fail?'err':'ok');
  if(d.session_id) load(d.session_id);
 }catch(e){toast(tr('扇出失败: ')+e,'err')}
}
async function auditNow(arg){
 let objective=String(arg||'').trim();
 if(!objective){
  objective=await dlgPrompt(tr('审计目标：要审什么改动/项目？（范围默认当前工作区）'),'',tr('开始审计'),tr('对抗式审计'));
  if(objective==null)return;
 }
 try{
  const d=await api('/v1/agent/audit',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({objective,model:currentModel||undefined})});
  toast(tr('对抗式审计已启动'),'ok');
  await refreshSessions();
  if(d.session_id)await load(d.session_id);
 }catch(e){toast(tr('审计启动失败: ')+((e&&(e.text||e.detail))||e),'err')}
}
function slashRun(text){
 const m=/^\/(\S+)\s*([\s\S]*)$/.exec(String(text||'').trim());
 if(!m)return false;
 const c=SLASH_COMMANDS.find(x=>x.aliases.some(a=>a.toLowerCase()===m[1].toLowerCase()));
 if(!c)return false;
 const box=document.getElementById('slashMenu');if(box){box.hidden=true;box.innerHTML=''}
 c.run((m[2]||'').trim());
 return true;
}

/* ── 深研一键命令（research 插件 + 模型综合）────────────────────────
   点「深研」→ 输入问题 → 后端收集证据包 → Agent 写成带来源的报告 → 打开新会话。 */
async function deepResearchNow(query){
 // 斜杠命令可以带问题（/research 问题），按钮则弹输入框
 let q=query;
 if(!q){
  q=await dlgPrompt(
   tr('输入研究问题：会用多角度搜索 + 抓取正文，生成带来源的调研报告（约 20-60 秒）'),
   '', tr('开始深研'), tr('深研'));
 }
 if(!q||!String(q).trim())return;
 toast(tr('深研中…（多角度搜索 + 抓正文）'),'ok');
 try{
  const model=currentModel||'';
  const d=await api('/v1/agent/deep-research',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({query:String(q).trim(),model})});
  if(d&&d.session_id){ await load(d.session_id); toast(tr('深研完成：已打开报告会话'),'ok'); }
  else{ toast(tr('深研完成'),'ok') }
 }catch(e){ toast(tr('深研失败: ')+(e.text||e),'err') }
}

/* ── 工作目录选择器（对齐 DSH：native 优先，失败回退 browse）──────────
   native：POST /v1/agent/pick-directory（系统文件夹对话框）
   browse：GET /v1/agent/browse（应用内树，对话框打不开时用） */
let wsCurrent='';
let planModeOnce=false;   // /plan 触发：本轮请求带 plan_mode，后端强制只读
/* 工作区分组折叠状态（localStorage，跨刷新保留） */
function wsCollapsedList(){try{return JSON.parse(localStorage.getItem('cbp-ws-collapsed')||'[]')||[]}catch(e){return []}}
function wsIsCollapsed(k){return wsCollapsedList().indexOf(k)>=0}
function wsToggleCollapse(k){
 let arr=wsCollapsedList();
 const i=arr.indexOf(k);
 if(i>=0)arr.splice(i,1);else arr.push(k);
 try{localStorage.setItem('cbp-ws-collapsed',JSON.stringify(arr))}catch(e){}
 sessSig='';   // 强制重绘
 drawSessions();
}
/* 新建工作区：先切到新会话视图，再打开目录选择（native 优先，可新建目录） */
async function newWorkspace(){
 openTask();
 await pickWorkspace();
}
/* 默认走**应用内浏览**：立刻可用的界面反馈。
   系统对话框（native）会阻塞到用户点选，非交互桌面下等于永不返回（实测：
   点按钮没反应），所以改成弹窗里的可选按钮。 */
async function pickWorkspace(){
 const cur=(document.getElementById('workspace')||{}).value||'';
 await openWsBrowser(cur);
}
async function openWsBrowser(start){
 openMask('wsMask');
 await wsNavigate(start||'');
}
async function wsNavigate(path){
 try{
  const q=path?('?path='+encodeURIComponent(path)):'';
  const d=await api('/v1/agent/browse'+q);
  wsCurrent=d.path||'';
  const p=document.getElementById('wsPath'); if(p)p.textContent=wsCurrent||'—';
  const box=document.getElementById('wsList'); if(!box)return;
  box.innerHTML='';
  if(!(d.entries||[]).length){ box.innerHTML='<div class="meta">'+esc(tr('（没有子目录）'))+'</div>'; return }
  (d.entries||[]).forEach(x=>{
   const row=document.createElement('div');row.className='wsrow';
   row.textContent='📁 '+x.name;
   row.onclick=()=>wsNavigate(x.path);
   box.appendChild(row)});
 }catch(e){ toast(tr('读取目录失败: ')+(e.text||e),'err') }
}
async function wsUp(){
 if(!wsCurrent)return;
 const parent=wsCurrent.replace(/[\\/][^\\/]+$/, '');
 if(parent&&parent!==wsCurrent) await wsNavigate(parent);
}
async function wsDefault(){ await wsNavigate('') }   // 不传 path = 默认工作区
function wsChoose(){
 const w=document.getElementById('workspace');
 if(w&&wsCurrent){ w.value=wsCurrent; toast(tr('已选择工作目录: ')+wsCurrent,'ok') }
 closeMask('wsMask');
}
async function wsMkdir(){
 const name=await dlgPrompt(tr('新目录名（建在当前目录下）'), '', tr('创建'), tr('新建目录'));
 if(!name||!String(name).trim()) return;
 try{
  const d=await api('/v1/agent/browse/mkdir',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({parent:wsCurrent,name:String(name).trim()})});
  toast(tr('已创建目录: ')+(d.path||''),'ok');
  await wsNavigate(d.path||wsCurrent);
 }catch(e){ toast(tr('创建目录失败: ')+(e.text||e),'err') }
}
/* 在指定工作区直接新建会话（DSH 式：组头 ＋ 按钮） */
function newSessionInWorkspace(ws){
 openTask();
 const w=document.getElementById('workspace');
 if(w)w.value=ws||'';
 const t=document.getElementById('task');
 if(t){t.focus()}
}
function closeWs(){closeMask('wsMask')}

async function api(u,o={}){
  if(DEMO)return demoApi(u,o);
  const call=tk=>{const opt=Object.assign({},o);opt.headers=Object.assign({},o.headers||{});
    if(tk)opt.headers['X-CBP-Token']=tk;
    // 语言头：后端据此翻译错误 detail（展示层；协议/日志仍是中文）
    try{opt.headers['X-CBP-Lang']=currentLang()}catch(e){}
    return fetch(u,opt)};
  let r=await call(apiToken());
  if(r.status===401){
    // 服务端配置了 api_token：就地询问一次并记住（仅本机浏览器 localStorage）。
    // 复用同一个 promise，避免轮询期间连弹多次、把用户正在看的确认框顶掉。
    if(tokenPromptCancelled){throw new ApiError(401,'已取消令牌输入','');}
    if(!tokenPrompting){
      tokenPrompting=(async()=>{
        try{
          const tk=await dlgPrompt(tr('本服务启用了 api_token，请输入访问令牌（只保存在本机浏览器）'),apiToken(),'','需要访问令牌');
          if(tk){try{localStorage.setItem('cbp-token',tk);tokenPromptCancelled=false}catch(e){}}
          else{tokenPromptCancelled=true;showConnBanner(tr('未授权：已取消令牌输入（点此重新输入）'),true)}
          return tk;
        }finally{tokenPrompting=null}
      })();
    }
    const tk=await tokenPrompting;
    if(tk)r=await call(tk);
  }
  let t=await r.text();if(!r.ok)throw new ApiError(r.status,t,u);return t?JSON.parse(t):{}}
/* tr() 由 i18n.js 提供（该文件在 app.js 之前加载）。若它加载失败，这里兜底成"原样返回"，
   否则每个用了 tr() 的交互都会抛 ReferenceError。 */
if (typeof tr !== 'function') { window.tr = (k) => String(k == null ? '' : k); }
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
/* ── 通道无关的显示名 ──────────────────────────────────────────────
   服务现在既支持 Copilot 网页，也支持 API 提供商（deepseek/… 等），
   界面文案不该再写死 "Copilot"。 */
const BRAND={deepseek:'DeepSeek',kimi:'Kimi',openai:'OpenAI',grok:'Grok',moonshot:'Kimi',
             anthropic:'Claude',google:'Gemini',qwen:'Qwen',zhipu:'智谱',myapi:'API'};
function isProviderModel(m){return String(m||'').indexOf('/')>=0}
function assistantLabel(model){
 const m=String(model==null?(currentModel||''):model);
 if(!m)return tr('助手');
 if(isProviderModel(m)){
  const p=m.split('/')[0].toLowerCase();
  return BRAND[p]||(p.charAt(0).toUpperCase()+p.slice(1))}
 return 'Copilot'}
/* 消息时间：今天只显示时分秒，跨天补上日期 */
function fmtStamp(ts){
 if(!ts)return '';
 const d=new Date(ts*1000),now=new Date();
 const hm=d.toLocaleTimeString('zh-CN',{hour12:false});
 return d.toDateString()===now.toDateString()?hm
   :`${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} ${hm}`}
function jv(v,key){
 if(v===null||v===undefined)return '<span class="nl">null</span>';
 if(typeof v==='boolean')return '<span class="bl">'+v+'</span>';
 if(typeof v==='number')return '<span class="nm">'+v+'</span>';
 if(typeof v==='string'){
  if(key==='content_b64')return '<span class="nl">'+esc(tr('[Base64 内容，共 {n} 字符]',{n:v.length}))+'</span>';
  let t=v.length>4000?v.slice(0,4000)+'\n'+tr('…[共 {n} 字符，已截断]',{n:v.length}):v;
  return '<span class="st">"'+esc(t)+'"</span>'}
 if(Array.isArray(v)){if(!v.length)return '[]';
  return '[<div class="ind">'+v.map(x=>'<div>'+jv(x)+'</div>').join('')+'</div><span class="nl">]</span>'}
 if(typeof v==='object'){let ks=Object.keys(v);if(!ks.length)return '{}';
  return '{<div class="ind">'+ks.map(k=>'<div><span class="ky">'+esc(k)+'</span>: '+jv(v[k],k)+'</div>').join('')+'</div><span class="nl">}</span>'}
 return esc(String(v))}

/* ── 状态呈现 ── */
const STATE_TEXT={running:'运行中',paused:'已暂停',waiting:'等待服务',ready:'就绪',completed:'已完成',failed:'失败',cancelled:'已取消',expired:'已过期',archived:'已归档'};
function stateText(s){return tr(STATE_TEXT[s]||s||'—')}
/* 会话状态图标：形状 + 颜色双编码，替掉原来占位的文字块（completed 这种太宽）。
   14×14 描边图标，title 里仍给完整文案（悬停可见 / 读屏可读）。 */
const STATE_ICON={
 running:'<circle cx="8" cy="8" r="6.2" stroke-dasharray="26 12" class="spin"/>',
 completed:'<circle cx="8" cy="8" r="6.2"/><path d="M5.1 8.2l2 2 3.8-4"/>',
 failed:'<circle cx="8" cy="8" r="6.2"/><path d="M6 6l4 4M10 6l-4 4"/>',
 paused:'<circle cx="8" cy="8" r="6.2"/><path d="M6.6 5.8v4.4M9.4 5.8v4.4"/>',
 waiting:'<circle cx="8" cy="8" r="6.2"/><path d="M8 4.6V8l2.4 1.6"/>',
 ready:'<circle cx="8" cy="8" r="6.2"/>',
 cancelled:'<circle cx="8" cy="8" r="6.2"/><path d="M4.4 11.6L11.6 4.4"/>',
 expired:'<circle cx="8" cy="8" r="6.2"/><path d="M4.4 11.6L11.6 4.4"/>',
 archived:'<circle cx="8" cy="8" r="6.2"/><path d="M5.4 5.4h5.2v5.2H5.4z"/>'};
function stateIcon(s){
 const k=STATE_ICON[s]||STATE_ICON.ready;
 return '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor"'
   +' stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+k+'</svg>'}
function stateClass(s){return s==='running'?'run':(s==='paused'||s==='waiting')?'warn':(s==='failed')?'bad':(s==='completed')?'done':''}
/* 父会话已结束、但后代子代理还在跑：状态不能笼统显示"已完成"（用户实测会被误导成
   "任务全做完了"）。这里统一给出"已完成 · 子代理 N 仍在跑"的文案与颜色。 */
function subRunningOf(o){return Number((o&&(o.subagent_running??o.subagentRunning))||0)}
function stateLabel(o){
 const st=String((o&&(o.state||o.session_state))||'ready');
 const n=subRunningOf(o);
 if(n>0&&(st==='completed'||st==='failed'||st==='cancelled'))
  return stateText(st)+' · '+tr('子代理 {n} 仍在跑',{n});
 return stateText(st)}
function stateClassOf(o){
 const st=String((o&&(o.state||o.session_state))||'ready');
 return subRunningOf(o)>0&&(st==='completed'||st==='failed'||st==='cancelled')?'warn':stateClass(st)}
function setPill(s){let e=document.getElementById('statepill');e.textContent=stateText(s);e.className='pill '+stateClass(s)}
function setKpis(o){
 if(o.token_budget){budget=o.token_budget;document.getElementById('kpiBudget').textContent=' / '+Number(budget).toLocaleString()}
 const _st=String(o.state||o.session_state||'ready');
 const _card=document.getElementById('kpiStateCard');
 const _hasSubRun=subRunningOf(o)>0&&(_st==='completed'||_st==='failed'||_st==='cancelled');
 document.getElementById('kpiState').textContent=stateLabel(o);
 // 状态卡片按状态变色；后代仍在跑时也要跟着变黄，别让用户看到绿色"已完成"就走
 if(_card){const _c=_hasSubRun?'waiting':_st; if(_card.dataset.state!==_c)_card.dataset.state=_c}
 // （_card 已在上面取过）
 // （已由上面统一设置，避免把子代理运行态覆盖回完成态）
 // 步数与 token 要把**子代理**算进来：子代理是独立会话、各自记账，
 // 只读主会话字段时"跑了 4 个子代理、几百步"在界面上完全看不出来（用户实测）。
 const _ss=Number(o.subagent_steps||0), _stk=Number(o.subagent_tokens||0),
       _sc=Number(o.subagent_count||0);
 const _ownSteps=Number(o.total_steps??0), _ownTk=Number(o.estimated_tokens??0);
 const stepsAll=_ownSteps+_ss, tkAll=_ownTk+_stk;
 const _marksub=(el,total,own,extra,n)=>{
   if(!el)return;
   el.textContent=Number(total).toLocaleString();
   if(extra>0){
     el.title=tr('本会话 {a} + 子代理 {b}（{n} 个）',{a:Number(own).toLocaleString(),
       b:Number(extra).toLocaleString(),n});
     el.dataset.sub='1';
   }else{el.removeAttribute('title');delete el.dataset.sub}};
 _marksub(document.getElementById('kpiSteps'),stepsAll,_ownSteps,_ss,_sc);
 document.getElementById('kpiConv').textContent=o.conversation_steps??0;
 // 与侧栏行、活动面板的「会话序号」一致：显示"第几个 Copilot 对话"（1 起），
 // 而不是原始下标（0 起）——三处同一个数，避免用户看到两套"会话"数据。
 document.getElementById('kpiRoll').textContent=(o.conversation_index??0)+1;
 const _tkEl=document.getElementById('kpiToken');
 _marksub(_tkEl,tkAll,_ownTk,_stk,_sc);
 const tk=tkAll;
 // 有子代理时在小标里标一下，避免用户以为统计漏了
 try{const _note=document.getElementById('kpiSubNote');
  if(_note)_note.textContent=_sc?tr('含 {n} 个子代理',{n:_sc}):''}catch(e){}
 document.getElementById('tokenbar').style.width=(budget?Math.min(100,tk/budget*100):0)+'%'}

/* ── Session 列表（支持筛选） ── */
/* ── 模型列表：从 /v1/models 动态加载，与后端（含 config.models 扩展）保持一致 ── */
async function loadModels(){
 if(DEMO){modelCache=['copilot-auto','copilot-think-deeper','copilot-gpt-5-6-sol-think-deeper'];
  fillModelSelect(document.getElementById('sessModel'),modelCache.map(id=>({id})),currentModel);return}
 try{let d=await api('/v1/models'),sel=document.getElementById('model');
  if(!d.data||!d.data.length)return;
  // 只显示可用的：未配 API Key 的提供商模型、菜单扫描没见到的 Copilot 模型不显示
  window.__MODELS=d.data || [];   // 挂起卡片按"provider/model"筛提供商模型用
  let avail=d.data.filter(m=>m.available!==false);
  if(!avail.length)avail=d.data;  // 兜底：全不可用时不过滤，防止下拉为空
  modelCache=avail.map(m=>m.id);
  fillModelSelect(document.getElementById('sessModel'), avail, document.getElementById('sessModel').value);
  let prev=sel.value;
  sel.innerHTML='';
  avail.forEach(m=>{let o=document.createElement('option');o.value=m.id;o.textContent=m.id;const _d=modelDesc(m);if(_d)o.title=_d;sel.appendChild(o)});
  sel.value=avail.some(m=>m.id===prev)?prev:avail[0].id;
  refreshSessModelOptions()}catch(e){}
}
/* 填充模型下拉；keep 为当前会话的模型（不在可用列表里也要保留并标注） */
function fillModelSelect(sel, avail, keep){
 if(!sel)return;
 let prev=keep||sel.value||'';
 let seen=avail.map(m=>m.id);
 sel.innerHTML='';
 avail.forEach(m=>{let o=document.createElement('option');o.value=m.id;o.textContent=m.id;const _d=modelDesc(m);if(_d)o.title=_d;sel.appendChild(o)});
 if(prev&&!seen.includes(prev)){
  let o=document.createElement('option');o.value=prev;o.textContent=prev+'（菜单里未找到）';sel.appendChild(o)}}
/* 会话页模型选择器：显示当前会话自己的模型 */
function refreshSessModelOptions(){
 let sel=document.getElementById('sessModel');if(!sel)return;
 let want=currentModel||'';
 if(!want)return;
 let has=[...sel.options].some(o=>o.value===want);
 if(!has){let o=document.createElement('option');o.value=want;o.textContent=want+tr('（菜单里未找到）');sel.appendChild(o)}
 sel.value=want;
 sel.title=tr('当前会话模型：{n}（下一回合生效，服务重启后仍沿用）',{n:want})}	
async function setSessionModel(model){
 if(!current||!model)return;
 try{
  let d=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/model`,
                  {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model})});
  currentModel=d.model||model;
  toast(tr('会话模型已切换为 ')+(d.model||model)+(d.note?'（'+d.note+'）':'')+'，下一回合生效', d.note?'':'ok')
 }catch(e){toast(tr('切换模型失败: ')+(e.text||e),'err');refreshSessModelOptions()}}
/* 模型说明：原生 <option title> 的文案要跟着界面语言走。
   后端 /v1/models 同时给 description（中文）与 description_en（英文）。 */
function modelDesc(m){
 if(!m)return '';
 if(currentLang()==='en'&&m.description_en)return m.description_en;
 if(currentLang()==='tw'&&m.description)return (typeof toTraditional==='function')?toTraditional(m.description):m.description;
 return m.description||'';
}
async function refreshModels(){
 if(DEMO){toast(tr('演示模式（API 未连接）：无法刷新模型列表'),'err');return}
 let btn=document.getElementById('refreshModelsBtn');
 if(btn){btn.disabled=true;btn.textContent='…'}
 try{
  let d=await api('/v1/models/refresh',{method:'POST'});
  await loadModels();
  if(!d.scanned){toast(tr('模型菜单未能扫描：')+(d.reason||'未知原因'),'err');return}
  let n=(d.labels||[]).length;
  if(d.added&&d.added.length)toast(tr('发现新模型: ')+d.added.join(', '),'ok');
  else toast(tr('已重新扫描（')+n+' 个菜单项，无新增）','');
 }catch(e){toast(tr('模型菜单读取失败: ')+e,'err')}
 finally{if(btn){btn.disabled=false;btn.textContent='↻'}}}

/* ── 浏览器窗口显示/隐藏（headless 是启动参数，切换会带登录态重启浏览器上下文） ── */
function drawBrowserBtn(h){let b=document.getElementById('browserBtn');
 // 只更新提示，不动内容——之前 b.textContent='▣' 把 SVG 图标替换成了文字，
 // 按钮里显示"切换"两个字（26px 宽塞不下，竖排成两行）。
 b.title=tr(h?'当前为无头模式（窗口隐藏），点击显示自动化窗口':'当前显示自动化窗口，点击切换为无头模式（隐藏窗口）');
 b.style.color=h?'var(--muted)':'';  // 无头时淡一点，有窗口时正常
}
async function browserState(){if(DEMO)return;try{let d=await api('/v1/agent/browser');drawBrowserBtn(d.headless)}catch(e){}}
async function toggleBrowser(){
 if(DEMO)return;
 let b=document.getElementById('browserBtn');b.disabled=true;b.style.opacity=.5;
 try{let cur=await api('/v1/agent/browser');
  let d=await api('/v1/agent/browser',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({headless:!cur.headless})});
  drawBrowserBtn(d.headless);
  if(d.fallback)toast(tr('无头模式启动失败，已回退为有窗口模式（旧版 Edge 或安全策略不支持无头持久化）'),'err')}catch(e){toast(tr('切换失败: ')+e,'err');browserState()}
 finally{b.disabled=false;b.style.opacity=1}
}

let showSubagents=false;   // 默认折叠子代理会话（它们挂在父会话下）
let subagentCount=0;
/* 子代理会话归集到父会话下：默认折叠，点 ▸ 展开（状态存 localStorage） */
const expandedSess=new Set();
try{JSON.parse(localStorage.getItem('cbp-expanded-sess')||'[]').forEach(x=>expandedSess.add(x))}catch(e){}
function toggleSessExpand(sid){
 if(expandedSess.has(sid))expandedSess.delete(sid);else expandedSess.add(sid);
 try{localStorage.setItem('cbp-expanded-sess',JSON.stringify([...expandedSess]))}catch(e){}
 sessSig='';drawSessions();
}
try{showSubagents=localStorage.getItem('cbp-show-subagents')==='1'}catch(e){}
async function refreshSessions(){
 if(serviceStopped)return;
 try{
  const q='?include_subagents=true';   // 全量：前端把子代理折叠到父会话下
  let d=await api('/v1/agent/sessions'+q);
  sessCache=d.sessions||[];
  subagentCount=d.subagent_count||0;
  drawSessions();
 }catch(e){}
}
function toggleSubagents(){
 // 全部展开 / 全部折叠（子代理会话归集在父会话下）
 const parents=new Set();
 sessCache.forEach(s=>{if(String(s.parent_id||''))parents.add(s.parent_id)});
 const anyCollapsed=[...parents].some(p=>!expandedSess.has(p));
 if(anyCollapsed)parents.forEach(p=>expandedSess.add(p));
 else parents.clear && parents.forEach(p=>expandedSess.delete(p));
 try{localStorage.setItem('cbp-expanded-sess',JSON.stringify([...expandedSess]))}catch(e){}
 sessSig='';drawSessions();
 toast(tr(anyCollapsed?'已展开 {n} 个子代理会话':'已折叠 {n} 个子代理会话',
            {n: parents.size}),'ok');
}
/* 列表签名：内容没变就不重建 DOM，避免每秒滚动回顶/点击错位 */
let sessSig='';
/* 工作区短名：只显示末两段（全路径放 title），避免会话列表被长路径撑开 */
function drawSessions(){
 let q=(document.getElementById('sessFilter').value||'').toLowerCase();
 let box=document.getElementById('sessions');
 let all=sessCache.filter(s=>!q||(s.title||'').toLowerCase().includes(q)||(s.id||'').toLowerCase().includes(q)||(s.state||'').includes(q)||(s.tag||'').toLowerCase().includes(q)||(s.workspace||'').toLowerCase().includes(q));
 // 父子归集：子代理会话挂在父会话下（可展开），不再单占一行
 const byId=new Map(all.map(s=>[s.id,s]));
 const kidsOf=new Map();
 all.forEach(s=>{const p=String(s.parent_id||'');if(p&&byId.has(p)){if(!kidsOf.has(p))kidsOf.set(p,[]);kidsOf.get(p).push(s)}});
 const isKid=s=>{const p=String(s.parent_id||'');return !!p&&byId.has(p)};
 let list=all.filter(s=>!isKid(s));
 let sig=q+'|'+all.map(s=>s.id+s.state+s.total_steps+s.estimated_tokens+(s.subagent_steps||0)+(s.subagent_tokens||0)+(s.subagent_count||0)+(s.subagent_running||0)+(s.conversation_index??'')+(s.title||'')+(s.attachment_count??'')+(s.tag||'')+(s.workspace||'')+(s.parent_id||'')).join('|')+'|'+current+'|'+[...expandedSess].join(',');
 if(sig===sessSig)return;
 sessSig=sig;
 box.innerHTML='';
 if(!list.length){box.innerHTML='<div class="sess-empty">'+esc(tr('没有匹配的 Session'))+'</div>';return}
 // 按**工作区**分组（对齐 DSH：组头是文件夹名，组内按 updated 倒序，组间按最近活跃排序）
 const groups=new Map();
 list.forEach(s=>{const t=(s.workspace||'').trim();if(!groups.has(t))groups.set(t,[]);groups.get(t).push(s)});
 const _maxUpd=(arr)=>Math.max(...arr.map(x=>Number(x.updated)||0));
 const keys=[...groups.keys()].sort((a,b)=>_maxUpd(groups.get(b))-_maxUpd(groups.get(a)));
 const wsName=(p)=>{const parts=String(p||'').replace(/\\/g,'/').split('/').filter(Boolean);
   return parts.length?parts[parts.length-1]:tr('默认工作区')};
 const renderCard=(s,opts)=>{
  opts=opts||{};
  // 子代理不再列进侧栏（用户要求），因此父行也不该再有 ▸ 折叠图标。
  const kids=[];
  let e=document.createElement('div');
  e.className='sess'+(s.id===current?' sel':'')+(opts.child?' child':'');
  e.setAttribute('role','button');e.setAttribute('aria-pressed',s.id===current?'true':'false');
  // 会话标识必须显示：标题可能相似（甚至被 Copilot 改名），删错代价很大
  const _kind=String(s.id||'').startsWith('cron-')?'⏱':String(s.id||'').startsWith('research-')?'🔎':String(s.id||'').startsWith('sub-')?'子':'';
  // 步数/Token 与详情页 KPI 必须同一口径：主会话 + 子代理（含孙）。
  // 以前这里只显示 s.total_steps / s.estimated_tokens，父任务跑了子代理后
  // 侧栏与页面会差出几百步/几千 token（用户实测"列表跟会话页数据不相符"）。
  const _subSteps=Number(s.subagent_steps||0), _subTk=Number(s.subagent_tokens||0),
        _subN=Number(s.subagent_count||0);
  const _ownSteps=Number(s.total_steps??0), _ownTk=Number(s.estimated_tokens??0);
  const _sumTip=_subN?tr('本会话 {a} + 子代理 {b}（{n} 个）',{a:_ownSteps.toLocaleString(),
    b:_subSteps.toLocaleString(),n:_subN}):'';
  const _num=(v,extra)=>extra>0
    ?`<span class="ssub" title="${esc(_sumTip)}">${esc(v)}</span>`
    :esc(v);
  e.innerHTML=`<span class="sstat ${stateClassOf(s)}" title="${esc(stateLabel(s))}" aria-label="${esc(stateLabel(s))}">${stateIcon(s.state)}</span><div class="smeta"><b>${_kind?`<span class="skind">${_kind}</span>`:''}${esc(s.title)}</b><small><code class="sid" title="点击复制会话 ID">${esc(s.id)}</code><br>${tr('步骤')} ${_num(_ownSteps+_subSteps,_subSteps)} · ${tr('会话')} ${esc((s.conversation_index??0)+1)} · Token ${_num(_ownTk+_subTk,_subTk)}</small></div>${(s.attachment_count||0)>0?`<span class="attchip" title="含 ${s.attachment_count} 条带图片附件的消息">📎${s.attachment_count}</span>`:''}<button type="button" class="sess-tag" title="设置标签（用于分组）" data-act="editTag" data-arg="${esc(s.id)}">🏷</button>`;
  // 子代理不再列进侧栏 → 前导位（▸ 的那个位置）也就没用了：
  // 空着那 12px + gap 会把整行往右推，看起来跟工作区分组头"跳格"（用户实测）。
  let chip=e.querySelector('.sid');
  if(chip)chip.onclick=ev=>{ev.stopPropagation();try{navigator.clipboard.writeText(s.id);chip.textContent=tr('已复制 ✓');setTimeout(()=>chip.textContent=s.id,900)}catch(_){}};
  e.onclick=()=>load(s.id);
  // 键盘可达：会话切换是主入口，纯 div+onclick 对键盘用户等于不可用
  e.tabIndex=0;
  e.onkeydown=ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();load(s.id)}};
  box.appendChild(e)};
 keys.forEach(k=>{
  const h=document.createElement('div');h.className='taghead wshead';
  h.title=k||'';
  const ico=document.createElementNS('http://www.w3.org/2000/svg','svg');
  ico.setAttribute('viewBox','0 0 24 24');ico.setAttribute('width','13');ico.setAttribute('height','13');
  ico.setAttribute('fill','none');ico.setAttribute('stroke','currentColor');
  ico.setAttribute('stroke-width','1.8');ico.setAttribute('stroke-linecap','round');
  ico.setAttribute('stroke-linejoin','round');ico.setAttribute('aria-hidden','true');
  ico.innerHTML='<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l1.6 2h9.4A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/>';
  const nm=document.createElement('span');nm.className='wsname';nm.textContent=wsName(k);
  nm.setAttribute('data-i18n-skip','1');   // 文件夹名是数据
  const cnt=document.createElement('span');cnt.className='wscount';cnt.textContent=String(groups.get(k).length);
  const caret=document.createElement('span');caret.className='wscaret';
  caret.textContent=wsIsCollapsed(k)?'▸':'▾';
  const add=document.createElement('button');
  add.type='button';add.className='wsadd';add.title=tr('在此工作区新建会话');
  add.setAttribute('data-i18n-title','在此工作区新建会话');
  add.textContent='＋';
  add.onclick=(ev)=>{ev.stopPropagation();newSessionInWorkspace(k)};
  h.appendChild(caret);h.appendChild(ico);h.appendChild(nm);h.appendChild(cnt);h.appendChild(add);
  h.classList.toggle('collapsed', wsIsCollapsed(k));
  h.onclick=()=>wsToggleCollapse(k);
  box.appendChild(h);
  if(!wsIsCollapsed(k)) groups.get(k).forEach((s)=>{
   renderCard(s);
  })});
 try{translateAttrs(box)}catch(e){}}

/* 会话标签：纯本地组织手段（不参与 Agent 逻辑），列表按标签分组 */
async function editTag(btn){
 const sid=btn.dataset.arg;
 if(!sid)return;
 const cur=((sessCache||[]).find(s=>s.id===sid)||{}).tag||'';
 const tag=await dlgPrompt(tr('设置会话标签（列表按标签分组；留空取消标签）'),cur,tr('保存'),tr('会话标签'));
 if(tag===null)return;
 try{
  await api(`/v1/agent/sessions/${encodeURIComponent(sid)}/tag`,
   {method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({tag:String(tag).trim()})});
  toast(tag.trim()?(tr('标签已设为')+'「'+tag.trim()+'」'):tr('已取消标签'),'ok');
  await refreshSessions();
 }catch(e){toast(tr('设置标签失败: ')+(e.text||e),'err')}}

/* ── 时间线：增量渲染，不重建已有 DOM，展开状态不丢失 ── */
/* 工具结果的语言猜测：优先按参数里的文件扩展名，其次按工具名 */
function guessResultLang(s){
 const byExt={py:'python',pyw:'python',js:'js',mjs:'js',ts:'ts',json:'json',jsonc:'json',
  html:'html',htm:'html',css:'css',md:'markdown',yml:'yaml',yaml:'yaml',sh:'bash',
  ps1:'powershell',sql:'sql',diff:'diff',patch:'diff',txt:'text',log:'text',csv:'text'};
 const args=(s&&s.arguments)||{};
 const p=String(args.path||args.file||args.source||args.destination||'');
 const ext=(p.match(/\.([A-Za-z0-9]+)$/)||[])[1];
 if(ext&&byExt[ext.toLowerCase()])return byExt[ext.toLowerCase()];
 const tool=String((s&&s.tool)||'');
 if(/python|unittest/.test(tool))return 'python';
 if(/excel/.test(tool))return 'text';
 return 'text'}
/* 工具结果的"渲染视图"：短标量排成键值行，长文本/对象用同一套代码块渲染
   （语言标签、高亮、行号、折叠、复制全都一致）；JSON 视图另有一个页签。 */
function stepResultRender(s){
 const r=(s&&s.result)||{};
 if(typeof r!=='object'||r===null)return codeBlockHtml(String(r),guessResultLang(s));
 const scalars=[],blocks=[];
 Object.keys(r).forEach(k=>{
  const v=r[k];
  if(typeof v==='string'){
   const multi=v.includes('\n')||v.length>160;
   if(multi)blocks.push([k,v]);else scalars.push([k,v])}
  else if(v===null||['number','boolean'].includes(typeof v))scalars.push([k,String(v)]);
  else blocks.push([k,JSON.stringify(v,null,1)])});
 let out='';
 if(scalars.length){
  out+='<div class="kvlist">'+scalars.map(([k,v])=>
   `<div class="kvline"><span class="kvk">${esc(k)}</span><span class="kvv">${esc(v)}</span></div>`).join('')+'</div>'}
 const lang=guessResultLang(s);
 blocks.forEach(([k,v])=>{
  const isJson=/^(json|jsonc)$/i.test(lang)||/^[[{]/.test(String(v).trim());
  out+=`<div class="sectlabel">${esc(k)}</div>`+
       codeBlockHtml(v,isJson?'json':lang,{label:langLabel(lang)})});
 if(!out)out='<div class="meta">'+esc(tr('（没有可渲染的字段）'))+'</div>';
 return out}
/* 时间线筛选：按工具名与状态分组过滤。
   默认（无筛选）仍走增量渲染，展开状态不丢；一旦筛选就整表重建——
   筛选态本身是"检查用"的临时视图，重建的代价可以接受。 */
let lastSteps=[],stepFilter={tool:'',status:''},stepFilterKey='';
let stepsPending=false, lastStepsRaw=[];   // 时间线在后台页签时先不建 DOM（长会话省 ~0.9s）
const BAD_STATUS=['error','invalid_action','failed','gate_blocked','noop_guard',
                  'send_failed','service_unavailable','denied'];
function stepMatch(s){
 if(stepFilter.tool&&String(s.tool||'')!==stepFilter.tool)return false;
 if(stepFilter.status==='ok'&&s.status!=='ok')return false;
 if(stepFilter.status==='bad'&&!BAD_STATUS.includes(String(s.status||'')))return false;
 if(stepFilter.status==='event'&&(s.status==='ok'||BAD_STATUS.includes(String(s.status||''))))return false;
 return true}
function fillStepTools(){
 const sel=document.getElementById('stepTool');if(!sel)return;
 const tools=[...new Set((lastSteps||[]).map(s=>String(s.tool||'').trim()).filter(Boolean))].sort();
 const cur=sel.value;
 sel.innerHTML='<option value="">'+esc(tr('全部工具'))+'</option>'+tools.map(t=>`<option value="${esc(t)}">${esc(t)}</option>`).join('');
 if(tools.includes(cur))sel.value=cur}
function stepFilterInput(el){
 const which=el.dataset.arg;
 stepFilter[which==='status'?'status':'tool']=el.value||'';
 stepFilterKey=JSON.stringify(stepFilter);
 renderSteps(lastSteps,true)}
/* 连续的"服务限流/超时"（transient）合成一行：
   pyshot 任务里这类记录有 105 条（占全部事件 21%），逐条铺开会让时间线看着像全在失败，
   而它们既不是模型的错、也不消耗步数预算。 */
function collapseTransient(steps){
 const out=[];
 (steps||[]).forEach(s=>{
  if(s&&s.transient){
   const last=out[out.length-1];
   if(last&&last._mergedTransient){last.times=(last.times||1)+1;
     last.reason=tr('服务限流/超时，自动重试 {n} 次（不计步）',{n:last.times});return}
   out.push({status:'info',tool:'',transient:true,_mergedTransient:true,times:1,
             _time:s._time,reason:tr('服务限流/超时，自动重试 {n} 次（不计步）',{n:1})});
   return;
  }
  out.push(s);
 });
 return out;
}
function renderSteps(steps,force){
 lastSteps=collapseTransient(steps||[]);
 const box=document.getElementById('steps');
 const filtered=!!(stepFilter.tool||stepFilter.status);
 const shown=lastSteps.filter(stepMatch);
 const key=JSON.stringify(stepFilter);
 // 只在"筛选变了 / 被显式要求 / 步骤变少"时整表重建；其余情况继续增量 prepend，
   // 这样默认视图不会失去展开状态，筛选态也不会每秒重建整棵 DOM。
 const rebuild=force||key!==stepFilterKey||shown.length<renderedSteps;
 stepFilterKey=key;
 if(rebuild){box.innerHTML='';renderedSteps=0}
 else if(!shown.length){box.innerHTML=''}
 document.getElementById('stepCount').textContent=
  filtered?`${shown.length}/${lastSteps.length}`+tr(' 条（筛选）')
          :(lastSteps.length?lastSteps.length+tr(' 条'):'');
 fillStepTools();
 // 已被后续成功步骤"解决"的审批拦截：同一个 tool 在本步之后有 ok 步骤，
 // 说明用户后来授权并执行成功了——这条不该继续以"失败"的样子留在时间线里。
 // 注意区分两种解决方式：真的执行了（ok）才敢写"已执行"，只有授权记录时只说"已授权"。
 const resolvedApprovals=new Map();   // index -> 'ok' | 'granted'
 shown.forEach((s,i)=>{
  if(s.status!=='approval_required')return;
  const tool=String(s.tool||'');
  let by=null;
  for(let j=i+1;j<shown.length;j++){
   const t=shown[j];
   if(String(t.tool||'')!==tool)continue;
   if(t.status==='ok'||t.status==='approval_granted'){
    if(t.status==='ok'){by='ok';break}
    by=by||'granted';
   }}
  if(by)resolvedApprovals.set(i,by)});
 shown.slice(renderedSteps).forEach((s,_k)=>{
  const idx=renderedSteps+_k;
  const resolvedBy=resolvedApprovals.get(idx);
  const granted=(s.status==='approval_granted')||!!resolvedBy;
  let cls=s.status==='ok'?'ok':(s.status==='error'||s.status==='invalid_action'?'error':'info');
  if(granted)cls='ok';                     // 已授权：按成功样式呈现，不再误导为失败
  let e=document.createElement('div');e.className='step '+cls;
  let tm=s._time?new Date(s._time*1000).toLocaleTimeString('zh-CN',{hour12:false}):'';
  let detail='';
  if(s.result!==undefined&&s.result!==null){
   detail=`<details><summary>${esc(tr('查看结果'))}</summary>`+
    `<div class="stepviewbar">`+
    `<button type="button" class="btn seg on" data-act="stepView" data-arg="render">${esc(tr('渲染'))}</button>`+
    `<button type="button" class="btn seg" data-act="stepView" data-arg="json">JSON</button></div>`+
    `<div class="stepview" data-mode="render">${stepResultRender(s)}</div>`+
    `<div class="stepview" data-mode="json" style="display:none"><div class="jsonview">${jv(s.result)}</div></div>`+
    `</details>`}
  let chipTxt=(s.status==='approval_granted')?tr('已授权')
    :(resolvedBy==='ok'?tr('已授权，已执行'):(resolvedBy?tr('已授权'):(s.status||'')));
  // approval_granted 这步的 reason 是后端中文原文：这里按语言重写，并保留授权范围信息
  let whyTxt;
  if(s.status==='approval_granted'){
   const _scope=(s.result||{}).scope;
   whyTxt=tr('授权已下达')+(String(_scope)==='session'?tr('（本会话内不再逐次询问）'):tr('（用后即焚）'));
  }else if(resolvedBy){
   whyTxt=(resolvedBy==='ok')?tr('授权已下达，该操作已继续执行'):tr('授权已下达');
  }else{
   whyTxt=s.reason||s.error||'';
  }
  e.innerHTML=`<div class="head"><b>${tr('步骤')} ${esc(s.step??'-')} · <span data-i18n-skip="1">${esc(s.tool||s.status||'event')}</span></b><span class="schip">${esc(chipTxt)}</span>${tm?`<span class="schip" style="border-style:dashed">${tm}</span>`:''}</div><div class="why">${esc(whyTxt)}</div>${detail}`;
  box.prepend(e)});
 renderedSteps=shown.length;
 if(!shown.length&&!box.innerHTML.trim()){
  // 区分两种情况：真的没有步骤（普通对话），还是有步骤但被筛选掉了
  const hasMsgs=(lastMessages||[]).length>0;
  const emptyMsg = lastSteps.length===0 && hasMsgs
    ? tr('普通对话没有 Agent 步骤，见「继续对话」页')
    : (filtered ? tr('没有符合筛选条件的步骤') : tr('该会话还没有 Agent 步骤'));
  box.innerHTML='<div class="sess-empty">'+esc(emptyMsg)+'</div>'}
 applyCodePrefs(box);try{translateAttrs(box)}catch(e){}}

/* 时间线在「步骤时间线」页签里；默认停在「实时流」时它整块是隐藏的。
   长会话（1500 步）为它建 DOM 要 ~0.9s，纯属浪费 —— 先只记住数据，切过去再建。 */
function renderStepsMaybe(steps){
 lastStepsRaw=steps||[];
 if(liveTabName!=='timeline'){
  stepsPending=true;
  lastSteps=collapseTransient(lastStepsRaw);
  const cnt=document.getElementById('stepCount');
  if(cnt)cnt.textContent=lastSteps.length?lastSteps.length+tr(' 条'):'';
  return;
 }
 stepsPending=false;
 renderSteps(lastStepsRaw);
}
function flushStepsIfPending(){
 if(!stepsPending)return;
 stepsPending=false;
 renderSteps(lastStepsRaw);
}
/* 工具结果：渲染 / JSON 双视图切换 */
function stepView(btn){
 const bar=btn.closest('.stepviewbar');if(!bar)return;
 const wrap=bar.parentElement;
 const mode=btn.dataset.arg==='json'?'json':'render';
 [...bar.querySelectorAll('button')].forEach(b=>b.classList.toggle('on',b===btn));
 [...wrap.querySelectorAll(':scope > .stepview')].forEach(v=>{
  v.style.display=(v.dataset.mode===mode)?'':'none'});
 applyCodePrefs(wrap)}

/* ── 实时流：把消息与步骤按时间穿插成一条（DSH 的轨迹） ──
   两个数据源都有 epoch 时间：消息用 created，步骤用 _time，直接归并排序即可。
   消息沿用 msgEl()（气泡、附件、编辑、代码块全都照旧），步骤是新做的紧凑行。 */
let streamSig='';
function stepRowEl(s){
 const st=String(s.status||'');
 const errText=String(s.error||'').trim();
 // 没有工具名的步骤是**系统级记录**（发送失败、回合状态），不是"汇总"：
 // 以前一律显示"汇总"，把这些报错藏了起来（实测 19 条 send_failed 全被显示成"汇总"）。
 const failed=!!errText||['error','send_failed','invalid_action','approval_required',
                          'repeat_guard'].includes(st);
 const cls=failed?'error':(st==='ok'?'ok':'info');
 const el=document.createElement('div');
 el.className='streamstep '+cls;
 const icon=st==='ok'&&!failed?'✓':(failed?(st==='approval_required'?'🔐':'✗'):'•');
 const time=s._time?new Date(Number(s._time)*1000).toLocaleTimeString():'';
 const tool=String(s.tool||'').trim()||(failed?tr('系统'):tr('汇总'));
 const reason=errText||String(s.reason||'').trim();
 el.innerHTML='<span class="ss-ic">'+esc(icon)+'</span>'
   +'<span class="ss-tool">'+esc(tool)+'</span>'
   +'<span class="ss-reason" data-i18n-skip="1" title="'+esc(reason)+'">'+esc(reason)+'</span>'
   +'<span class="ss-clock">'+esc(time)+'</span>';
 // 详情**照抄步骤时间线的渲染**（用户："可以像步骤时间线那样"）：
 // 渲染视图（kvlist / 表格 / jsonview）+ 渲染↔JSON 切换，比裸 JSON 好读得多。
 const hasResult = s.result !== undefined && s.result !== null;
 const hasArgs = s.arguments !== undefined && s.arguments !== null && s.arguments !== '';
 if(hasResult || hasArgs || s.error){
  const det=document.createElement('details');
  det.className='ss-det';
  const sum=document.createElement('summary');
  sum.textContent=tr('查看结果');
  det.appendChild(sum);
  const bar=document.createElement('div');
  bar.className='stepviewbar';
  bar.innerHTML='<button type="button" class="btn seg on" data-act="stepView" data-arg="render">'
    +esc(tr('渲染'))+'</button><button type="button" class="btn seg" data-act="stepView" '
    +'data-arg="json">JSON</button>';
  det.appendChild(bar);
  const rv=document.createElement('div');
  rv.className='stepview'; rv.dataset.mode='render';
  rv.innerHTML=stepResultRender(s);
  det.appendChild(rv);
  const jvv=document.createElement('div');
  jvv.className='stepview'; jvv.dataset.mode='json'; jvv.style.display='none';
  const payload = hasResult ? s.result
    : (hasArgs ? s.arguments : {error: String(s.error||''), status: String(s.status||'')});
  const jbox=document.createElement('div');
  jbox.className='jsonview';
  jbox.innerHTML=jv(payload);
  jvv.appendChild(jbox);
  det.appendChild(jvv);
  el.appendChild(det);
 }
 return el;
}
function renderStream(messages,steps,opts){
 opts=opts||{};
 const box=document.getElementById('chatlog');
 if(!box)return;
 lastMessages=messages||[];
 // 对话流里**只放消息**：工具调用记录搬到独立的「调用记录」区（状态条上方），
 // 长任务里几十条工具行不再把对话顶出去；顺序按用户要求：
 // todo → 子代理 → 调用记录 → 状态信息。
 const items=[];
 (messages||[]).forEach(m=>{
   const prev=items.length?items[items.length-1]:null;
   if(prev&&prev.v.role===m.role
      && String(prev.v.content||'').trim()===String(m.content||'').trim())return;
   items.push({t:Number(m.created)||0,v:m});
 });
 items.sort((x,y)=>x.t-y.t);
 renderToolLog(steps||[],opts);
 const sig=items.length+'|'+(items.length?items[items.length-1].t:'');
 if(sig===streamSig&&!opts.force)return;      // 没变化不重建
 streamSig=sig;
 const atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<60;
 box.innerHTML='';
 if(!items.length){
  box.innerHTML='<div class="meta">'+esc(tr('当前没有对话记录。下面发送的消息会发给该 Session 正在使用的模型。'))+'</div>';
  try{renderLiveBubble()}catch(e){}
  return;
 }
 for(const it of items){
  box.appendChild(msgEl(it.v.role,it.v.content,it.v.created,
    {attachments:it.v.attachments||[],id:it.v.id}));
 }
 applyMsgFold(box);
 try{applyCodePrefs(box)}catch(e){}
 try{translateAttrs(box)}catch(e){}
 if(findQuery)applyFind();
 try{renderLiveBubble()}catch(e){}
 if(atBottom||opts.scrollEnd)pinChatBottom(box);
}
/* 贴底：长会话用了 content-visibility，离屏消息的高度是**估算值**——
   一次性把 scrollTop 设成 scrollHeight 会停在估算位置，真实高度展开后又跑偏
   （实测打开长会话停在最顶部）。所以在 1.5s 内反复贴底，让高度收敛；
   用户一旦滚动（wheel/触摸）就立刻停手，不跟用户抢滚动。 */
let chatPinTimer=null, chatPinUntil=0;
function pinChatBottom(box){
 if(!box)return;
 chatPinUntil=Date.now()+1500;
 if(chatPinTimer)return;
 const tick=()=>{
  chatPinTimer=null;
  if(Date.now()>chatPinUntil)return;
  try{box.scrollTop=box.scrollHeight}catch(e){}
  try{chatPinTimer=setTimeout(tick,60)}catch(e){}
 };
 tick();
}
document.addEventListener('wheel',ev=>{
 const t=ev.target;
 if(t&&t.closest&&t.closest('#chatlog'))chatPinUntil=0;
},{passive:true,capture:true});
document.addEventListener('touchstart',ev=>{
 const t=ev.target;
 if(t&&t.closest&&t.closest('#chatlog'))chatPinUntil=0;
},{passive:true,capture:true});
function scrollChatToEnd(){
 const box=document.getElementById('chatlog');
 if(box)pinChatBottom(box);
}

/* 调用记录：默认只显示**最新一条**（与状态条同款一行），点它浮动展开全部。
   不占布局（浮层定位于该行上方），和状态条的"点开看详情"是同一个交互。 */
let toolLogSig='';
let toolLogOpen=false;
let toolLogSteps=[];        // 调用记录数据：浮层打开时才建 DOM（长会话省 ~1s）
/* ── 视图缓存：从子代理返回父会话时不要整棵重建 ─────────────────────
   用户："从 subagent 返回到 live stream 的时候会很卡。"
   实测（父会话 120 条消息 + 400 步）：返回一次 load() 要 1.5s，其中
   renderStream（Markdown 解析 + 语法高亮）1.0s、renderSteps 0.44s，全同步阻塞。
   这些 DOM 刚刚就在界面上：切走时把节点摘进 DocumentFragment 留着，切回来直接
   append（展开状态 / 代码块 / 滚动位置都保留），只需几十毫秒；随后 runtime 再
   按需增量刷新。只留最近 3 个会话，避免内存膨胀。 */
const viewCache=new Map();
const VIEW_CACHE_MAX=3;
let renderedSid=null, lastSessionObj=null;
let skipDetailOnce=null;   // load() 刚渲染完详情：第一次 runtime 不要再拉一遍并重渲染

function stashView(sid){
 if(!sid)return;
 const chat=document.getElementById('chatlog'),steps=document.getElementById('steps');
 if(!chat||!steps)return;
 // 滚动位置必须在**搬走节点之前**读：清空容器后 scrollHeight 变 0，scrollTop 会被夹到 0
 const scrollTop=chat.scrollTop;
 const chatFrag=document.createDocumentFragment();
 while(chat.firstChild)chatFrag.appendChild(chat.firstChild);
 // 完成卡片按当前会话重新拉，不跟着旧片段走（否则会显示别的会话的改动）
 const oldCard=chatFrag.querySelector&&chatFrag.querySelector('#changesCard');
 if(oldCard)oldCard.remove();
 const stepFrag=document.createDocumentFragment();
 while(steps.firstChild)stepFrag.appendChild(steps.firstChild);
 const one=document.getElementById('toolLogOne'),body=document.getElementById('toolLogBody');
 const oneFrag=document.createDocumentFragment(),bodyFrag=document.createDocumentFragment();
 if(one)while(one.firstChild)oneFrag.appendChild(one.firstChild);
 if(body)while(body.firstChild)bodyFrag.appendChild(body.firstChild);
 const wrap=document.getElementById('toolLog');
 viewCache.delete(sid);                 // 同一会话重复保存：先释放旧片段
 viewCache.set(sid,{chat:chatFrag,steps:stepFrag,one:oneFrag,body:bodyFrag,
  renderedSteps,lastStepCount,streamSig,toolLogSig,
  stepFilter:{tool:stepFilter.tool,status:stepFilter.status},stepFilterKey,
  lastSteps,lastMessages,session:lastSessionObj,scroll:scrollTop,
  atBottom:(chat.scrollHeight-chat.scrollTop-chat.clientHeight)<40,
  stepCountText:(document.getElementById('stepCount')||{}).textContent||'',
  stepsPending,lastStepsRaw,
  toolLogHidden:!!(wrap&&wrap.hidden)});
 while(viewCache.size>VIEW_CACHE_MAX)viewCache.delete(viewCache.keys().next().value);
}
function restoreView(cut){
 const chat=document.getElementById('chatlog'),steps=document.getElementById('steps');
 if(!chat||!steps)return false;
 chat.innerHTML='';steps.innerHTML='';
 chat.appendChild(cut.chat);steps.appendChild(cut.steps);
 const one=document.getElementById('toolLogOne'),body=document.getElementById('toolLogBody');
 if(one){one.innerHTML='';one.appendChild(cut.one)}
 if(body){body.innerHTML='';body.appendChild(cut.body)}
 const wrap=document.getElementById('toolLog');
 if(wrap)wrap.hidden=!!cut.toolLogHidden;
 renderedSteps=cut.renderedSteps;lastStepCount=cut.lastStepCount;
 streamSig=cut.streamSig;toolLogSig=cut.toolLogSig;
 stepFilter={tool:(cut.stepFilter&&cut.stepFilter.tool)||'',status:(cut.stepFilter&&cut.stepFilter.status)||''};
 stepFilterKey=cut.stepFilterKey||JSON.stringify(stepFilter);
 lastSteps=cut.lastSteps||[];lastMessages=cut.lastMessages||[];
 stepsPending=!!cut.stepsPending;lastStepsRaw=cut.lastStepsRaw||[];
 const _cnt=document.getElementById('stepCount');
 if(_cnt)_cnt.textContent=cut.stepCountText||'';
 try{fillStepTools()}catch(e){}
 chat.scrollTop=cut.scroll||0;
 // 切走时停在底部：回来也贴底（content-visibility 下高度是估算值，不能只恢复像素）
 if(cut.atBottom)try{pinChatBottom(chat)}catch(e){}
 return true;
}
/* 会话外壳（标题/状态/KPI/模型/父会话）：恢复缓存时也要走一遍，不能只还原消息区 */
function applySessionChrome(s){
 s=s||{};
 document.getElementById('title').textContent=s.title||current||'—';
 const _sc=document.getElementById('sidChip');
 if(_sc){_sc.textContent=s.id||current||'';
  _sc.onclick=()=>{try{navigator.clipboard.writeText(current);toast(tr('已复制会话 ID: ')+current,'ok')}catch(e){toast(current)}}}
 document.getElementById('status').textContent=stateText(s.state)+' · '+(s.workspace||'');
 setPill(s.state);setKpis(s);
 currentModel=s.model||null;currentUrl=s.current_url||null;
 parentOfCurrent=s.parent_id||null;
 const _ms=document.getElementById('model');
 if(_ms&&currentModel&&[..._ms.options].some(o=>o.value===currentModel))_ms.value=currentModel;
 try{refreshSessModelOptions()}catch(e){}
 try{updateChannelUI()}catch(e){}
 document.getElementById('emptyState').style.display='none';
 document.getElementById('sessionView').style.display='';
}

function renderToolLog(steps,opts){
 opts=opts||{};
 const wrap=document.getElementById('toolLog');
 const one=document.getElementById('toolLogOne');
 const body=document.getElementById('toolLogBody');
 if(!wrap||!one||!body)return;
 const all=collapseTransient(steps||[]);
 const last=all.length?all[all.length-1]:null;
 const sig=all.length+'|'+(last?String(last.step??'')+'|'+String(last._time||''):'');
 const cnt=document.getElementById('toolLogCount');
 if(cnt)cnt.textContent=all.length?`${all.length}`:'';
 const popCnt=document.getElementById('toolLogPopCount');
 if(popCnt){
  const okc=all.filter(s=>s.status==='ok').length;
  popCnt.textContent=all.length?tr('{n} 步 · 成功 {ok}',{n:all.length,ok:okc}):'';
 }
 if(!all.length){wrap.hidden=true;toolLogSig=sig;body.innerHTML='';one.textContent='';return}
 wrap.hidden=false;
 if(sig===toolLogSig&&!opts.force)return;
 toolLogSig=sig;
 // 那一行 = 最新一步（图标 + 工具名 + 理由 + 时间）
 if(last){
  const st=String(last.status||'');
  const errText=String(last.error||'').trim();
  const failed=!!errText||['error','send_failed','invalid_action','approval_required',
                           'repeat_guard'].includes(st);
  const icEl=document.getElementById('toolLogIc');
  if(icEl){
   icEl.textContent=st==='ok'&&!failed?'✓':(failed?(st==='approval_required'?'🔐':'✗'):'•');
   icEl.className='ss-ic '+(failed?'bad':(st==='ok'?'good':''));
  }
  const tool=String(last.tool||'').trim()||(failed?tr('系统'):tr('汇总'));
  one.textContent=tool+' · '+(errText||String(last.reason||'').trim());
  one.title=one.textContent;
  const clock=document.getElementById('toolLogClock');
  if(clock)clock.textContent=last._time
    ? new Date(Number(last._time)*1000).toLocaleTimeString():'';
 }
 // 浮层内容：**打开时才建**（1500 步全量建 DOM 要 ~0.9s，而它默认是收起的）
 toolLogSteps=all;
 body.innerHTML='';
 if(toolLogOpen)fillToolLogBody();
 if(toolLogOpen)positionToolLogPop();
}
function fillToolLogBody(){
 const body=document.getElementById('toolLogBody');
 if(!body)return;
 body.innerHTML='';
 for(const s of toolLogSteps)body.appendChild(stepRowEl(s));
 try{translateAttrs(body)}catch(e){}
 body.scrollTop=body.scrollHeight;
}
function positionToolLogPop(){
 const pop=document.getElementById('toolLogPop');
 const row=document.getElementById('toolLogLatest');
 if(!pop||!row)return;
 const r=row.getBoundingClientRect();
 pop.style.bottom=Math.round(window.innerHeight-r.top+8)+'px';
 pop.style.left=Math.round(r.left)+'px';
 pop.style.width=Math.round(r.width)+'px';
 pop.style.maxHeight=Math.round(Math.min(window.innerHeight*0.55,r.top-24))+'px';
}
function toggleToolLogPop(force){
 const pop=document.getElementById('toolLogPop');
 const row=document.getElementById('toolLogLatest');
 const more=document.getElementById('toolLogMore');
 if(!pop)return;
 toolLogOpen=(typeof force==='boolean')?force:!toolLogOpen;
 pop.hidden=!toolLogOpen;
 if(row)row.classList.toggle('open',toolLogOpen);
 if(more)more.textContent=toolLogOpen?tr('收起'):tr('展开全部');
 if(toolLogOpen){
  fillToolLogBody();               // 首次展开才建全量行（数据在上面的 renderToolLog 里已备好）
  positionToolLogPop();
  const body=document.getElementById('toolLogBody');
  if(body)body.scrollTop=body.scrollHeight;
 }else{
  const body=document.getElementById('toolLogBody');
  if(body)body.innerHTML='';       // 收起时释放长列表 DOM
 }
}


function renderMessages(messages,opts){
 opts=opts||{};
 let box=document.getElementById('chatlog');box.innerHTML='';
 lastMessages=messages||[];
 if(!messages.length){box.innerHTML='<div class="meta">'+esc(tr('当前没有人工续聊记录。下面发送的消息会发给该 Session 正在使用的模型。'))+'</div>';renderChangesCard();return}
 messages.forEach(m=>{box.appendChild(msgEl(m.role,m.content,m.created,{attachments:m.attachments||[],id:m.id}))});
 // 新到达的消息做一次渐显（纯动画，不改内容；尊重 prefers-reduced-motion）
 if(opts.animateLast&&box.lastElementChild)box.lastElementChild.classList.add('reveal');
 applyMsgFold(box);
 applyCodePrefs(box);          // 行号/软换行偏好（可全局开关，记住选择）
 try{translateAttrs(box)}catch(e){}   // 动态 title/placeholder 在 DOM 层翻译（不改源码）
 if(findQuery)applyFind();
 renderLiveBubble();           // 重渲染后恢复"正在生成"临时气泡（若仍在流式）
 renderChangesCard();          // 完成卡片也要跟着消息重建（否则被 innerHTML 清掉）
 pinChatBottom(box)}
/* ── 任务完成卡片：这次改了哪些文件 / 交付物（对齐 DSH 的 Edited N files +X −Y）──
   数据来自后端快照原像 diff（不依赖 git）；任务跑完（completed/failed/cancelled）
   由 runtime 状态触发一次拉取。运行中不显示，避免数字一直跳。 */
let lastChanges=null, changesSid='', changesState='', changesExpanded=false;
const CHANGE_DONE=['completed','failed','cancelled','expired'];
const CHG_LABEL={added:['A','新增'],modified:['M','修改'],deleted:['D','删除'],dir:['DIR','目录']};
function maybeLoadChanges(state){
 if(!current)return;
 const st=String(state||'');
 // 又跑起来了（续聊/恢复）：清掉"已拉取"标记，下次结束时重新统计
 if(!CHANGE_DONE.includes(st)){if(changesSid===current)changesState='';return}
 if(changesSid===current&&changesState===st)return;
 loadChanges(current,st)}
async function loadChanges(sid,state){
 if(!sid)return;
 try{
  const d=await api('/v1/agent/sessions/'+encodeURIComponent(sid)+'/changes');
  if(current!==sid)return;
  lastChanges=d;changesSid=sid;changesState=String(state||'');
  renderChangesCard();
 }catch(e){}}
function clearChanges(){lastChanges=null;changesSid='';changesState='';
 const c=document.getElementById('changesCard');if(c)c.remove()}
function renderChangesCard(){
 const box=document.getElementById('chatlog');if(!box)return;
 let card=document.getElementById('changesCard');
 const d=lastChanges;
 const files=(d&&d.files)||[];
 const deliv=(d&&d.deliverables)||[];
 if(!d||changesSid!==current||(!files.length&&!deliv.length)){if(card)card.remove();return}
 if(!card){card=document.createElement('div');card.id='changesCard';card.className='chgcard'}
 box.appendChild(card);
 const shown=changesExpanded?files:files.slice(0,6);
 const rows=shown.map(f=>{
  const lab=CHG_LABEL[f.status]||['?',f.status||''];
  const num=f.binary?'<i class="chg-bin">'+esc(tr('二进制'))+'</i>'
    :('<i class="chg-add">+'+esc(f.additions||0)+'</i> <i class="chg-del">−'+esc(f.deletions||0)+'</i>');
  const clickable=(f.status!=='dir'&&!f.binary);
  return '<div class="chg-row'+(clickable?' click':'')+'"'+(clickable?' data-diff="'+esc(f.path)+'"':'')
    +' title="'+esc(clickable?tr('查看差异'):tr('二进制文件，无文本差异'))+'">'
    +'<span class="chg-ico '+esc(f.status)+'">'+esc(lab[0])+'</span>'
    +'<span class="chg-path">'+esc(f.path)+'</span>'
    +'<span class="chg-num">'+num+'</span></div>'}).join('');
 const more=files.length>6?'<button type="button" class="btn ghost chg-more">'
   +esc(changesExpanded?tr('收起'):tr('全部 {n} 个文件',{n:files.length}))+'</button>':'';
 const dv=deliv.length?'<div class="chg-deliv"><b>'+esc(tr('交付物'))+'</b>'
   +deliv.map(x=>'<span class="chg-dchip" title="'+esc(x.description||x.path)+'">'+esc(x.path)
     +(x.exists===false?' ⚠':'')+'</span>').join('')+'</div>':'';
 card.innerHTML='<div class="chg-head"><b>'+esc(files.length?tr('本次改动 {n} 个文件',{n:files.length}):tr('本次交付'))
   +'</b><span class="chg-stat"><i class="chg-add">+'+esc(d.additions||0)+'</i>'
   +'<i class="chg-del">−'+esc(d.deletions||0)+'</i></span></div>'
   +'<div class="chg-list">'+rows+'</div>'+more+dv;
 card.querySelectorAll('[data-diff]').forEach(el=>{el.onclick=()=>openChangeDiff(el.dataset.diff)});
 const mb=card.querySelector('.chg-more');
 if(mb)mb.onclick=()=>{changesExpanded=!changesExpanded;renderChangesCard()};
}
async function openChangeDiff(path){
 const sid=current;if(!sid)return;
 document.getElementById('liveTitle').textContent=path+' · '+tr('查看差异');
 const body=document.getElementById('liveBody');
 body.textContent=tr('读取差异…');
 openMask('liveMask');
 try{
  const d=await api('/v1/agent/sessions/'+encodeURIComponent(sid)+'/changes/diff?path='+encodeURIComponent(path));
  if(current!==sid)return;
  if(d.binary){body.innerHTML='<span class="chg-empty">'+esc(tr('二进制文件，无文本差异'))+'</span>';return}
  const lines=String(d.diff||'').split('\n');
  if(!String(d.diff||'').trim()){body.innerHTML='<span class="chg-empty">'+esc(tr('没有差异'))+'</span>';return}
  body.innerHTML=lines.map(l=>{
   let c='';
   if(l.startsWith('+++')||l.startsWith('---'))c=' d-head';
   else if(l.startsWith('@@'))c=' d-hunk';
   else if(l.startsWith('+'))c=' d-add';
   else if(l.startsWith('-'))c=' d-del';
   return '<span class="dline'+c+'">'+esc(l)+'</span>'}).join('');
 }catch(e){body.textContent=String((e&&(e.text||e.message))||e)}}
/* 语言显示名（对齐 Copilot 的写法：PowerShell 而不是 powershell） */
const HL_LABEL={python:'Python',js:'JavaScript',typescript:'TypeScript',json:'JSON',
 bash:'Bash',powershell:'PowerShell',sql:'SQL',yaml:'YAML',html:'HTML',css:'CSS',
 diff:'Diff',code:'代码'};
function langLabel(lang){
 const k=String(lang||'').toLowerCase();
 return HL_LABEL[HL_ALIAS[k]||k]||HL_LABEL[k]||(k?k.charAt(0).toUpperCase()+k.slice(1):'代码')}
/* 把高亮后的 HTML 按行包成 .cl，并维护跨行 span（多行字符串/块注释不会断色）：
   每行开始时重新打开上一行未闭合的 span，行尾先关掉它们（但记着下一行再开）。 */
function wrapCodeLines(html){
 const lines=String(html==null?'':html).split('\n');
 const open=[];
 const out=lines.map((ln,i)=>{
  let prefix='';
  for(const tag of open)prefix+=tag;
  const re=/<span class="[^"]*">|<\/span>/g;let m;
  while((m=re.exec(ln))){if(m[0]==='</span>'){open.pop()}else{open.push(m[0])}}
  const tail='</span>'.repeat(open.length);
  return '<span class="cl"><span class="ln" data-act="codeLineSel" title="'+esc(tr('点击选中整行'))+'">'+(i+1)+'</span>'+prefix+ln+tail+'</span>'});
 return out.join('')}
/* 行号点击选中整行（再点取消；可同时选多行，方便对照） */
function codeLineSel(ln){
 const row=ln.closest('.cl');if(!row)return;
 row.classList.toggle('sel');
 // 同一块里的"复制选中行"按钮随选择数显隐/更新
 const wrap=row.closest('.md-codewrap');if(!wrap)return;
 const n=wrap.querySelectorAll('.cl.sel').length;
 const b=wrap.querySelector('.md-copsel');
 if(b){b.style.display=n?'':'none';b.textContent=`复制选中 ${n} 行`}}
function copySelLines(btn){
 const wrap=btn.closest('.md-codewrap');if(!wrap)return;
 const rows=[...wrap.querySelectorAll('.cl.sel')];
 if(!rows.length)return;
 // 行文本 = 行内容去掉行号列；保持文档顺序
 const text=rows.map(r=>{const ln=r.querySelector('.ln');
  const t=r.textContent||'';return ln?t.slice((ln.textContent||'').length):t}).join('\n');
 copyText(text,btn)}
/* 图片放大查看（消息附件与 Markdown 图片共用）：轻量浮层，点击或 Esc 关闭 */
function imgZoom(el){
 const src=el.getAttribute('src');if(!src)return;
 let ov=document.getElementById('imgZoomOv');
 if(!ov){ov=document.createElement('div');ov.id='imgZoomOv';ov.className='imgzoom';
  // 浮层是程序创建的，直接挂 onclick（CSP 允许 DOM 属性赋值，不必走 ACTIONS 表）
  ov.onclick=imgZoomClose;document.body.appendChild(ov)}
 ov.innerHTML='<img src="'+esc(src)+'" alt="放大查看">';
 ov.style.display='flex';syncFixedLanes()}
function imgZoomClose(){const ov=document.getElementById('imgZoomOv');if(ov)ov.style.display='none'}
/* 图片加载失败的安静降级：外链断链时显示占位提示，而不是浏览器的破图图标。
   error 事件不冒泡但可以捕获（capture=true）。 */
document.addEventListener('error',e=>{
 const t=e.target;
 if(t&&t.tagName==='IMG'&&(t.classList.contains('md-img')||t.classList.contains('msg-att'))
    &&!t.dataset.broken){
  t.dataset.broken='1';
  const chip=document.createElement('span');chip.className='img-broken';
  chip.textContent=tr('图片加载失败');
  const src=t.getAttribute('src')||'';
  if(src)chip.title=src.length>120?src.slice(0,120)+'…':src;
  t.replaceWith(chip)}},true)
/* 代码块"…"菜单：复制 / 下载 / 行号 / 换行 / 折叠。
   CSP 下无内联事件；浮层挂到 body 并固定定位，避免被滚动容器裁掉。
   注意：菜单项的 data-act 必须写成**字面量**（不能 `${变量}` 拼），
   否则 cbp/invariants.py 的"data-act 与 ACTIONS 一一对应"静态核对看不见它，
   会把 mCopy 等当成未引用的死代码。 */
const CODE_EXT={python:'py',js:'js',typescript:'ts',json:'json',bash:'sh',
                powershell:'ps1',sql:'sql',yaml:'yml',html:'html',css:'css'};
let codeMenuEl=null;
let lastCodeMenuTarget=null;
function closeCodeMenu(){if(codeMenuEl){codeMenuEl.remove();codeMenuEl=null}}
function codeMenu(btn){
 closeCodeMenu();
 const wrap=btn.closest('.md-codewrap');if(!wrap)return;
 lastCodeMenuTarget=wrap;
 const code=wrap.querySelector('code');
 const raw=code?code.textContent:'';
 const label=wrap.querySelector('.md-lang');
 const lang=(label?label.textContent:'').toLowerCase();
 const noLines=wrap.classList.contains('no-lines');
 const noWrap=wrap.classList.contains('no-wrap');
 const folded=wrap.classList.contains('collapsed');
 const items=[
  ['📋 复制代码','data-act="mCopy"',false],
  ['⬇ 下载为文件','data-act="mDownload"',false],
  ['# '+(noLines?'显示行号':'隐藏行号'),'data-act="mLines"',noLines],
  ['↩ '+tr(noWrap?'自动换行':'关闭换行'),'data-act="mWrap"',noWrap],
  ['⌃ '+(folded?'展开全部':'折叠代码'),'data-act="mFold"',folded]];
 const el=document.createElement('div');
 el.className='codemenu';
 el.innerHTML=items.map(it=>`<button type="button" class="cmitem${it[2]?' off':''}" ${it[1]}>${esc(it[0])}</button>`).join('');
 el.dataset.code=raw;el.dataset.lang=lang;
 document.body.appendChild(el);
 const r=btn.getBoundingClientRect();
 el.style.top=Math.round(r.bottom+4)+'px';
 el.style.left=Math.round(Math.max(8,Math.min(r.right-el.offsetWidth,window.innerWidth-el.offsetWidth-8)))+'px';
 codeMenuEl=el}
document.addEventListener('click',e=>{
 if(codeMenuEl&&!e.target.closest('.codemenu')&&!e.target.closest('[data-act="codeMenu"]'))closeCodeMenu()});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeCodeMenu();imgZoomClose()}});
/* 行号/换行偏好（记住；默认都开，对齐 Copilot 观感） */
function codePref(key,def){
 try{const v=localStorage.getItem('cbp-code-'+key);return v===null?def:v==='1'}catch(e){return def}}
function setCodePref(key,val){try{localStorage.setItem('cbp-code-'+key,val?'1':'0')}catch(e){}}
function applyCodePrefs(scope){
 const noWrap=!codePref('wrap',true);
 const noLines=!codePref('lines',true);
 (scope||document).querySelectorAll('.md-codewrap').forEach(w=>{
  w.classList.toggle('no-wrap',noWrap);
  w.classList.toggle('no-lines',noLines)})}
/* 复制整条消息 / 复制代码块 */
function copyMsg(btn){let m=btn.closest('.msg');if(!m)return;
 try{navigator.clipboard.writeText(m.dataset.raw||'');btn.textContent=tr('已复制 ✓');setTimeout(()=>btn.textContent='复制',900)}catch(e){}}
function copyCode(btn){let w=btn.closest('.md-codewrap');if(!w)return;let c=w.querySelector('code');
 try{navigator.clipboard.writeText(c?c.textContent:'');btn.textContent=tr('已复制 ✓');setTimeout(()=>btn.textContent=tr('复制'),900)}catch(e){}}
/* 复制任意文本（JSON 代码块等）：按钮文案短暂反馈 */
function copyText(text,btn){
 try{navigator.clipboard.writeText(text||'');
     if(btn){const old=btn.textContent;btn.textContent=tr('已复制 ✓');
      setTimeout(()=>btn.textContent=old,900)}}catch(e){}}
function toggleCodeFold(btn){
 let w=btn.closest('.md-codewrap');if(!w)return;
 let folded=w.classList.toggle('collapsed');
 btn.textContent=tr(folded?'展开':'收起')}
/* ── 长消息折叠 + 消息内搜索 ────────────────────────────────────────
   折叠按**文本长度**判定（不依赖布局高度：会话页可能在未激活的页签里，
   此时 scrollHeight 为 0，测高会失效）。 */
const MSG_FOLD_CHARS=1800;
function applyMsgFold(scope){
 scope.querySelectorAll('.msg-main').forEach(m=>{
  if(m.dataset.foldReady)return;
  m.dataset.foldReady='1';
  if((m.textContent||'').length<=MSG_FOLD_CHARS)return;
  m.classList.add('foldable','collapsed');
  let b=document.createElement('button');b.className='msg-fold';b.textContent=tr('展开全文');
  b.onclick=()=>{let c=m.classList.toggle('collapsed');b.textContent=tr(c?'展开全文':'收起')};
  m.appendChild(b)})}
/* 搜索：输入即高亮命中，Enter/Shift+Enter 或按钮在命中之间跳转 */
let findHits=[],findCur=-1,findQuery='';
function chatFindInput(){
 let q=(document.getElementById('chatFind').value||'').trim();
 findQuery=q;
 // 重渲染拿回干净 DOM（renderMessages 内部会按 findQuery 重新高亮）。
 // 不要再单独调 applyFind()：那会在已标记的 DOM 上再标一遍（嵌套 mark、计数翻倍）。
 renderMessages(lastMessages);
}
function applyFind(){
 let box=document.getElementById('chatlog');findHits=[];findCur=-1;
 let cnt=document.getElementById('findCount');
 if(!findQuery){if(cnt)cnt.textContent='';return}
 // 文本节点快照后逐个替换，避免边遍历边改 DOM；跳过按钮与已有标记
 const nodes=[];
 (function walk(n){for(let c=n.firstChild;c;c=c.nextSibling){
   if(c.nodeType===3){if(c.nodeValue&&c.nodeValue.trim())nodes.push(c)}
   else if(c.nodeType===1&&c.tagName!=='BUTTON'&&c.tagName!=='MARK'
           &&!c.classList.contains('msg-fold')
           &&!c.classList.contains('ln'))walk(c)}})(box);
 const q=findQuery.toLowerCase();
 nodes.forEach(node=>{
  const text=node.nodeValue,low=text.toLowerCase();
  let idx=low.indexOf(q);if(idx<0)return;
  const frag=document.createDocumentFragment();let last=0;
  while(idx>=0){
   frag.appendChild(document.createTextNode(text.slice(last,idx)));
   const mk=document.createElement('mark');mk.className='cbp-hit';mk.textContent=text.substr(idx,findQuery.length);
   frag.appendChild(mk);findHits.push(mk);
   last=idx+findQuery.length;idx=low.indexOf(q,last)}
  frag.appendChild(document.createTextNode(text.slice(last)));
  node.parentNode.replaceChild(frag,node)});
 if(cnt)cnt.textContent=findHits.length?`0/${findHits.length}`:'无匹配';
 if(findHits.length){findCur=0;focusHit()}}
function focusHit(){
 findHits.forEach((h,i)=>h.classList.toggle('cur',i===findCur));
 if(findCur<0)return;
 const h=findHits[findCur];
 h.scrollIntoView({block:'center',behavior:'smooth'});
 let cnt=document.getElementById('findCount');
 if(cnt)cnt.textContent=`${findCur+1}/${findHits.length}`}
function findStep(delta){
 if(!findHits.length)return;
 findCur=(findCur+delta+findHits.length)%findHits.length;focusHit()}
function toggleChatFind(){
 let bar=document.getElementById('chatFindBar');if(!bar)return;
 let show=bar.style.display==='none'||!bar.style.display;
 bar.style.display=show?'flex':'none';
 let inp=document.getElementById('chatFind');
 if(show){inp.focus();inp.select()}
 else{findQuery='';findHits=[];findCur=-1;if(inp)inp.value='';
      let cnt=document.getElementById('findCount');if(cnt)cnt.textContent='';
      renderMessages(lastMessages)}}   // 重渲染：把高亮标记一起清掉
/* ── 代码语法高亮（零依赖，手写）───────────────────────────────────
   输入是**已经 HTML 转义**的代码；做法：先把注释/字符串抽成占位符（占位符只用
   字母 z，绝不含数字，因此不会被后面的数字/关键字正则误匹配），再给关键字、
   数字、属性上色，最后还原占位符。未知语言原样返回（只吃 monospace 样式）。 */
const HL_ALIAS={py:'python',python:'python',python3:'python',
 js:'js',javascript:'js',mjs:'js',cjs:'js',ts:'js',typescript:'js',jsx:'js',tsx:'js',
 json:'json',jsonc:'json',sh:'bash',bash:'bash',shell:'bash',zsh:'bash',console:'bash',
 ps1:'powershell',powershell:'powershell',pwsh:'powershell',
 sql:'sql',yml:'yaml',yaml:'yaml',html:'html',xml:'html',htm:'html',vue:'html',
 css:'css',scss:'css',less:'css',diff:'diff',patch:'diff',udiff:'diff'};
const HL_KW={
 python:'def class return if elif else for while import from as try except finally with lambda yield async await None True False self in not and or is pass raise global nonlocal assert del break continue',
 js:'function const let var return if else for while class new import export from async await try catch finally throw typeof instanceof null undefined true false this extends super of in do switch case break default delete void yield static get set',
 json:'true false null',
 bash:'if then else elif fi for do done while until case esac function return export local readonly echo cd set source unset alias trap',
 powershell:'if elseif else foreach for while do until switch function param return try catch finally throw in begin process end filter',
 sql:'select from where insert into update delete join left right inner outer full on group by order having limit offset create table alter drop index view values set as and or not null distinct count sum avg min max like between asc desc union all case when then end',
 yaml:'true false null',
 html:'DOCTYPE html head body div span script style link meta title table tr td th a img input button form section header footer nav ul li p h1 h2 h3 h4 h5 h6 pre code br hr',
 css:'important media supports import charset'};
function hlKeyword(re){return new RegExp('\\b('+re+')\\b','g')}
function hlWrap(store){return(html)=>{store.push(html);return '\u0000'+'z'.repeat(store.length)+'\u0000'}}
/* 入参是**原始代码**（未转义），返回值是已转义并上色的 HTML。
   顺序很关键：先在原文上抽出注释/字符串（存转义后的 HTML），再转义剩余部分，
   最后上色——若反过来（先转义再上色），引号变成 &quot;，字符串正则永远匹配不到。 */
function highlight(raw,lang){
 const fam=HL_ALIAS[String(lang||'').toLowerCase()];
 const text=String(raw==null?'':raw);
 if(!fam)return esc(text);
 // diff/patch：行首决定颜色（+新增/-删除/@@hunk/--- +++文件头），不做关键字分析
 if(fam==='diff'){
  return text.split('\n').map(l=>{
   let cls='dl-ctx';
   if(/^\+\+\+|^---/.test(l))cls='dl-meta';
   else if(l.startsWith('+'))cls='dl-add';
   else if(l.startsWith('-'))cls='dl-del';
   else if(l.startsWith('@@'))cls='dl-hunk';
   else if(/^(diff|index|new file|deleted file|similarity|rename)/.test(l))cls='dl-meta';
   return '<span class="'+cls+'">'+esc(l)+'</span>'}).join('\n')}
 const store=[],keep=hlWrap(store);
 // HTML 家族反过来：要匹配的是 &lt;tag 这种转义后的形态
 if(fam==='html'){
  let s=esc(text);
  s=s.replace(/(&lt;!--[\s\S]*?--&gt;)/g,m=>keep('<span class="hl-c">'+m+'</span>'));
  s=s.replace(/(&lt;\/?)([A-Za-z][\w:-]*)/g,(m,a,b)=>a+keep('<span class="hl-t">'+b+'</span>'));
  s=s.replace(/([A-Za-z-]+)(=)(&quot;[^&]*?&quot;)/g,(m,a,b,c)=>keep('<span class="hl-p">'+a+'</span>')+b+keep('<span class="hl-s">'+c+'</span>'));
  const kws=HL_KW[fam];
  if(kws)s=s.replace(hlKeyword(kws.replace(/ /g,'|')),'<span class="hl-k">$1</span>');
  return s.replace(/\u0000(z+)\u0000/g,(m,zs)=>store[zs.length-1]||m)}
 let s=text;
 const keepEsc=(m,cls)=>keep('<span class="'+cls+'">'+esc(m)+'</span>');
 if(fam==='python'){
  s=s.replace(/("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')/g,m=>keepEsc(m,'hl-s'));
  s=s.replace(/(#[^\n]*)/g,m=>keepEsc(m,'hl-c'));
 }else if(fam==='js'||fam==='css'){
  s=s.replace(/(\/\*[\s\S]*?\*\/|\/\/[^\n]*)/g,m=>keepEsc(m,'hl-c'));
  s=s.replace(/("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)/g,m=>keepEsc(m,'hl-s'));
 }else if(fam==='json'){
  s=s.replace(/("(?:[^"\\]|\\.)*")(\s*:)?/g,(m,str,colon)=>colon?keepEsc(str,'hl-k')+colon:keepEsc(str,'hl-s'));
 }else if(fam==='sql'){
  s=s.replace(/(--[^\n]*)/g,m=>keepEsc(m,'hl-c'));
  s=s.replace(/("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')/g,m=>keepEsc(m,'hl-s'));
 }else{   // bash / powershell / yaml
  s=s.replace(/(#[^\n]*)/g,m=>keepEsc(m,'hl-c'));
  s=s.replace(/("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')/g,m=>keepEsc(m,'hl-s'));
 }
 s=esc(s);                                  // 剩余部分（关键字/数字所在）统一转义
 const kws=HL_KW[fam];
 if(kws)s=s.replace(hlKeyword(kws.replace(/ /g,'|')),'<span class="hl-k">$1</span>');
 s=s.replace(/\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/g,'<span class="hl-n">$&</span>');
 return s.replace(/\u0000(z+)\u0000/g,(m,zs)=>store[zs.length-1]||m)}
/* ── 助手回复的可视化：JSON 动作 / 最终答复 / Markdown ────────────────
   问题：Agent 模式的回复常常是 JSON 动作原样落进对话（"final" 除外），
   以往按纯文本渲染就是一大坨（还超过折叠阈值被折起来），完全没法读。
   这里按内容形态分流渲染：
     {type:"final",content}      -> content 按 Markdown 渲染 + "最终答复" 标记
     {type:"tool",...}           -> 工具卡：工具名 + 原因 + 参数(JSON 视图)
     {type:"actions",actions:[]} -> 批量动作卡：逐个工具 + 参数
     其它 JSON                    -> JSON 视图（可读、可滚、键值着色）
     非 JSON                      -> Markdown
   CSP 下不能有内联事件，所有按钮走 data-act 事件委托。 */
function tryParseJson(text){
 let t=String(text==null?'':text).trim();
 if(!t)return null;
 let m=/^```(?:json|jsonc)?\s*([\s\S]*?)\s*```$/i.exec(t);
 if(m)t=m[1].trim();
 const looksJson=t.startsWith('{')||t.startsWith('[');
 if(!looksJson)return null;
 try{const v=JSON.parse(t);return (v&&typeof v==='object')?v:null}catch(e){}
 // 容错：模型偶尔在 JSON 后面带尾巴，或前面有解释——取首尾括号之间的部分再试
 const a=t.indexOf('{'),b=t.lastIndexOf('}');
 if(a>=0&&b>a){try{const v=JSON.parse(t.slice(a,b+1));if(v&&typeof v==='object')return v}catch(e){}}
 return null}
function jsonViewBox(obj){
 return '<div class="jsonview">'+jv(obj)+'</div>'}
function actionCardHtml(a){
 const name=(a&&(a.tool||a.name))||'(未知工具)';
 const reason=(a&&(a.reason||a.why))||'';
 const args=(a&&(a.arguments||a.args||a.params))||{};
 return '<div class="act-card">'+
  '<div class="act-head"><span class="act-ico">🔧</span><span class="act-tool">'+esc(String(name))+'</span>'+
  (reason?'<span class="act-why">'+esc(String(reason).slice(0,200))+'</span>':'')+'</div>'+
  jsonViewBox(args)+'</div>'}
function renderJsonReply(obj){
 const t=obj&&obj.type;
 if(t==='final'){
  const body=(obj.content!=null?String(obj.content):'');
  return '<div class="act-badge ok">'+esc(tr('最终答复'))+'</div><div class="md-body">'+mdToHtml(body)+'</div>'}
 if(t==='tool')return actionCardHtml(obj);
 if(t==='actions'){
  const list=Array.isArray(obj.actions)?obj.actions:[];
  return '<div class="act-badge">'+esc(tr('批量动作 · {n} 个',{n:list.length}))+'</div>'+
   list.map(actionCardHtml).join('')}
 return '<div class="act-badge">'+esc(tr('JSON 回复'))+'</div>'+jsonViewBox(obj)}
function renderAssistantBody(content){
 const raw=String(content==null?'':content);
 const obj=tryParseJson(raw);
 if(obj)return renderJsonReply(obj);
 // 看起来像 JSON 但解析不了：明确告诉用户是原文，而不是让人以为界面坏了
 if(/^\s*[{[]/.test(raw))return '<div class="act-badge warn">'+esc(tr('JSON 解析失败（原文）'))+'</div>'+
   '<div class="md-body">'+mdToHtml(raw)+'</div>';
 return '<div class="md-body">'+mdToHtml(raw)+'</div>'}
function msgEl(role,content,ts,opts){
 opts=opts||{};
 let who=opts.who||(role==='user'?tr('你'):assistantLabel());
 let t=fmtStamp(ts||Math.floor(Date.now()/1000));
 let plain=(opts.plain||role==='user');   // 用户/提示类消息不做 Markdown
 let body=plain?`<div class="md-body plain">${esc(content)}</div>`
               :renderAssistantBody(content);
 // 粘贴的图片/截图附件：缩略图行（点击放大）。文件名经 /v1/uploads 提供，
 // 配 api_token 时走 ?token=（img 标签不能带请求头）。
 const atts=(opts.attachments||[]);
 let attsHtml='';
 if(atts.length){
  const tk=apiToken();
  attsHtml='<div class="msg-atts">'+atts.map(n=>
   '<img class="msg-att" src="/v1/uploads/'+encodeURIComponent(n)+(tk?'?token='+encodeURIComponent(tk):'')+
   '" alt="附件" loading="lazy" data-act="imgZoom" data-arg="'+esc(n)+'">').join('')+'</div>'}
 let e=document.createElement('div');e.className='msg '+role;
 if(opts.id!==undefined&&opts.id!==null)e.dataset.mid=opts.id;
 // 编辑/删除：只对库里有 id 的消息显示（乐观显示的临时消息没有 id）
 const mid=(opts.id!==undefined&&opts.id!==null)
   ?`<button class="msg-icon" title="编辑这条消息（只改本地历史）" data-act="editMsg">✎</button>`+
    `<button class="msg-icon del" title="删除这条消息（只改本地历史）" data-act="delMsg">🗑</button>`:'';
 e.innerHTML=`<div class="msg-av">${esc(String(who).charAt(0))}</div>`+
  `<div class="msg-main"><div class="msg-head"><span class="msg-role">${esc(who)}</span>`+
  (t?`<span class="msg-time">${t}</span>`:'')+
  `<button class="msg-copy" title="${esc(tr('复制这条消息'))}" data-act="copyMsg">${esc(tr('复制'))}</button>`+mid+`</div>`+
  body+attsHtml+`</div>`;
 e.dataset.raw=content||'';
 return e}

/* ── 可复用的代码块渲染 ─────────────────────────────────────────────
   Markdown 围栏与时间线的工具结果共用同一套：语言标签、语法高亮、行号、
   软换行、复制/下载/「…」菜单、超长折叠。抽出来是为了两处**行为一致**。 */
function codeBlockHtml(rawCode,lang,opts){
 opts=opts||{};
 const lang2=String(lang||'code');
 const text=String(rawCode==null?'':rawCode);
 // ```json 直接用 JSON 视图（键值着色 + 可滚），比纯文本好读
 if(/^(json|jsonc)$/i.test(lang2)&&opts.jsonView!==false){
  const parsed=tryParseJson(text);
  if(parsed)return '<div class="md-codewrap"><div class="md-codebar">'+
   '<span class="md-lang">JSON</span>'+
   '<span class="md-actions">'+
   `<button type="button" class="md-icon" title="${esc(tr('复制代码'))}" data-act="copyRaw" data-raw="${esc(text)}">⧉</button>`+
   '<button type="button" class="md-icon" title="'+esc(tr('更多'))+'" data-act="codeMenu">…</button></span></div>'+
   jsonViewBox(parsed)+'</div>'}
 const codeHtml=highlight(text,lang2);
 const n=text.split('\n').length;
 const long=opts.fold!==false&&n>22;
 return `<div class="md-codewrap${long?' foldable collapsed':''}"><div class="md-codebar">`+
  `<span class="md-lang">${esc(opts.label||langLabel(lang2))}</span>`+
  (long?`<span class="md-lines">${esc(tr('{n} 行',{n:n}))}</span>`:'')+
  '<span class="md-actions">'+
  `<button type="button" class="md-copsel" data-act="copySelLines" style="display:none">${esc(tr('复制选中 0 行'))}</button>`+
  `<button type="button" class="md-icon" title="${esc(tr('复制代码'))}" data-act="copyCode">⧉</button>`+
  `<button type="button" class="md-icon" title="${esc(tr('更多'))}" data-act="codeMenu">…</button>`+
  (long?`<button type="button" class="md-fold" data-act="toggleCodeFold">${esc(tr('展开'))}</button>`:'')+
  `</span></div><pre class="md-code"><code>${wrapCodeLines(codeHtml)}</code></pre></div>`}

/* ── 轻量 Markdown 渲染（零依赖、离线可用）─────────────────────────
   安全前提：**先整体转义**再插入我们自己的标签；链接只允许 http(s)，
   因此模型输出里的 <script>/javascript: 之类不会变成可执行内容。
   支持：围栏代码块（带语言标签与复制）、标题、粗斜体、行内代码、
   无序/有序列表（含一层缩进嵌套）、引用、表格、分隔线、链接。 */
function mdToHtml(src){
 const lines=String(src==null?'':src).replace(/\r\n?/g,'\n').split('\n');
 const inline=(s)=>esc(s)
  // 图片要在链接之前处理：![alt](url) 里的 [alt](url) 会被链接规则抢先吃掉。
  // 只允许 https? 与 data:image（且排除 svg——svg 可带脚本）；别的协议（javascript:
  // 之类）不匹配、原样保留为文本。
  .replace(/!\[([^\]]*)\]\(((?:https?:\/\/|data:image\/(?:png|jpe?g|gif|webp))[^)\s]*)\)/gi,
           '<img class="md-img" src="$2" alt="$1" loading="lazy" data-act="imgZoom" title="点击放大">')
  .replace(/`([^`]+)`/g,(m,a)=>`<code class="md-ic">${a}</code>`)
  .replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')
  .replace(/(^|[\s（(])\*([^*\n]+)\*/g,'$1<i>$2</i>')
  .replace(/\[([^\]\n]+)\]\((https?:[^)\s]+)\)/g,
           '<a class="md-a" href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
 let out=[],list=[];   // list: 栈，元素为 'ul' | 'ol'
 const closeLists=(depth)=>{while(list.length>depth){out.push(`</${list.pop()}>`);if(list.length)out.push('</li>')}};
 for(let i=0;i<lines.length;i++){
  const line=lines[i];
  // 围栏代码块
  let m=/^\s*```(\S*)\s*$/.exec(line);
  if(m){
   const lang=m[1]||'code',buf=[];
   i++;
   while(i<lines.length&&!/^\s*```\s*$/.test(lines[i])){buf.push(lines[i]);i++}
   closeLists(0);
   out.push(codeBlockHtml(buf.join('\n'),lang));
   continue}
  // 表格：本行含 | 且下一行是分隔行
  if(line.includes('|')&&i+1<lines.length&&/^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i+1])&&/-/.test(lines[i+1])){
   const cells=(r)=>r.replace(/^\s*\|/,'').replace(/\|\s*$/,'').split('|').map(x=>x.trim());
   const head=cells(line);i+=2;const rows=[];
   while(i<lines.length&&lines[i].includes('|')) {rows.push(cells(lines[i]));i++}
   i--;
   closeLists(0);
   out.push('<div class="md-tablewrap"><table class="md-table"><thead><tr>'+
     head.map(h=>`<th>${inline(h)}</th>`).join('')+'</tr></thead><tbody>'+
     rows.map(r=>'<tr>'+head.map((_,k)=>`<td>${inline(r[k]||'')}</td>`).join('')+'</tr>').join('')+
     '</tbody></table></div>');
   continue}
  // 标题
  let h=/^(#{1,6})\s+(.*)$/.exec(line);
  if(h){closeLists(0);out.push(`<div class="md-h md-h${h[1].length}">${inline(h[2])}</div>`);continue}
  // 分隔线
  if(/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)){closeLists(0);out.push('<hr class="md-hr">');continue}
  // 引用
  let q=/^\s*>\s?(.*)$/.exec(line);
  if(q){closeLists(0);out.push(`<div class="md-quote">${inline(q[1])}</div>`);continue}
  // 列表（缩进 0/2/4 空格 -> 最多 3 层）
  let li=/^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line);
  if(li){
   const indent=li[1].replace(/\t/g,'  ').length;
   const depth=Math.min(3,Math.floor(indent/2)+1);
   const kind=/\d/.test(li[2])?'ol':'ul';
   while(list.length<depth){list.push(kind);out.push(`<${kind} class="md-list">`)}
   if(list.length>depth)closeLists(depth);
   out.push('<li>');
   out.push(inline(li[3]));
   continue}
  // 普通段落 / 空行
  closeLists(0);
  if(line.trim()===''){out.push('<div class="md-gap"></div>')}
  else{out.push(`<div class="md-p">${inline(line)}</div>`)}
 }
 closeLists(0);
 return out.join('')}

/* 导出完整时间线：调试/复盘用，含每步原始参数、结果与 invalid_action 的 raw_tail */
async function exportTimeline(){
 if(!current){toast(tr('请先选择 Session'),'err');return}
 try{
  let d=await api('/v1/agent/sessions/'+encodeURIComponent(current));
  // 把**顺序**与**最新状态**写进文件本身：以前导出里 steps 与 messages 方向让人
  // 猜（用户实测："219 那个就是新的，所以我不知道算正序还是倒序"）。现在三个数组
  // 一律正序（旧→新）并在 order 里写清，latest 直接给出最新一步/最新消息时间。
  const _steps=[...(d.steps||[])].sort((a,b)=>(a.step??0)-(b.step??0));
  const _msgs=[...(d.messages||[])].sort((a,b)=>(a.id??0)-(b.id??0));
  const _files=[...(d.files||[])].sort((a,b)=>(a.id??0)-(b.id??0));
  const _last=_steps.length?_steps[_steps.length-1]:null;
  const _lastMsg=_msgs.length?_msgs[_msgs.length-1]:null;
  let payload={exported_at:new Date().toISOString(),app:'CopilotBridge Agent',
               api:'/v1/agent/sessions/'+encodeURIComponent(current),
               order:{steps:'ascending（旧→新，按 step 号）',
                      messages:'ascending（旧→新，按 id/时间）',
                      files:'ascending（旧→新，按 id/时间）',
                      note:'最新的一条在每个数组的**末尾**'},
               counts:{steps:_steps.length,messages:_msgs.length,files:_files.length},
               latest:{step:_last?(_last.step??null):null,
                       step_status:_last?(_last.status??null):null,
                       step_tool:_last?(_last.tool??null):null,
                       message_at:_lastMsg?(_lastMsg.created??null):null,
                       message_role:_lastMsg?(_lastMsg.role??null):null,
                       state:(d.session||{}).state??null},
               session:d.session||{},steps:_steps,messages:_msgs,files:_files};
  let blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'});
  let a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='cbp-'+current+'-timeline-'+new Date().toISOString().slice(0,19).replace(/[:T]/g,'-')+'.json';
  document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),3000);
  toast(tr('时间线已导出（')+((payload.steps||[]).length)+' 步）','ok')}catch(e){toast(tr('导出失败: ')+e,'err')}}

/* ── 页面内通知（toast）与通用模态层（替代原生 alert/confirm/prompt） ── */
function toast(msg,type=''){let box=document.getElementById('toasts'),e=document.createElement('div');
 e.className='toast '+type;e.textContent=msg;box.appendChild(e);
 setTimeout(()=>{e.style.opacity='0';e.style.transition='opacity .3s';setTimeout(()=>e.remove(),320)},type==='err'?5000:2600)}
let _gmResolve=null;
function gmShow(o){if(_gmResolve){const prev=_gmResolve;_gmResolve=null;prev(null)}  // 重入时先了结上一个，防 Promise 悬挂
 return new Promise(res=>{
 _gmResolve=res;
 document.getElementById('gmTitle').textContent=o.title||tr('提示');
 let m=document.getElementById('gmMsg');m.textContent=o.msg||'';m.style.display=o.msg?'':'none';
 let inp=document.getElementById('gmInput');
 if(o.input!==undefined&&o.input!==null){inp.style.display='';inp.value=o.input}else{inp.style.display='none'}
 // 可选：插入方式选择（step 立即遵守 / turn 整轮重排），只有强行插入用得上
 let modes=document.getElementById('gmModes');
 if(modes){
  modes.style.display=o.modes?'':'none';
  if(o.modes){let pick=modes.querySelector('input[value="'+o.modes.default+'"]');
   if(pick)pick.checked=true}}
 let c=document.getElementById('gmCancel');c.style.display=o.cancelText===null?'none':'';c.textContent=o.cancelText||tr('取消');
 let ok=document.getElementById('gmOk');ok.textContent=o.okText||tr('确定');ok.className='btn '+(o.danger?'danger':'accent');
 openMask('gmMask');
 if(inp.style.display!=='none')setTimeout(()=>{inp.focus();inp.select()},60)})}
/* ── 弹窗焦点管理（无障碍）────────────────────────────────────────────
   实测问题：打开任一弹窗后 document.activeElement 仍是 BODY，Tab 会走到背景元素，
   关闭后焦点丢失。这里统一处理：记录打开前焦点 → 聚焦首个可聚焦控件 → Tab 循环 →
   关闭时还焦。 */
let _focusBeforeMask=null;
function _maskFocusables(root){
 return [...root.querySelectorAll('button,input,textarea,select,a[href],[tabindex]:not([tabindex="-1"])')]
  .filter(e=>!e.disabled&&e.getAttribute('aria-hidden')!=='true'&&e.offsetParent!==null)}
let _maskZ=100;
function openMask(id){
 const m=document.getElementById(id);if(!m)return;
 _focusBeforeMask=document.activeElement;
 m.classList.add('open');
 // **后打开的盖住先打开的**：面板从插件管理里打开时必须在上层（实测被挡住）。
 m.style.zIndex=String(++_maskZ);
 const f=_maskFocusables(m);if(f.length)setTimeout(()=>{try{f[0].focus()}catch(e){}},60)}
function closeMask(id){
 const m=document.getElementById(id);if(m)m.classList.remove('open');
 const p=_focusBeforeMask;if(p&&p.focus){try{p.focus()}catch(e){}}
 _focusBeforeMask=null}
document.addEventListener('keydown',e=>{
 if(e.key!=='Tab')return;
 const opens=[...document.querySelectorAll('.mask.open')];
 const m=opens[opens.length-1];if(!m)return;
 const f=_maskFocusables(m);if(!f.length)return;
 const first=f[0],last=f[f.length-1];
 if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}
 else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}});

function gmClose(v){closeMask('gmMask');if(_gmResolve){_gmResolve(v);_gmResolve=null}}
document.getElementById('gmOk').onclick=()=>{let inp=document.getElementById('gmInput');
 let modes=document.getElementById('gmModes');
 let mode=null;
 if(modes&&modes.style.display!=='none'){let sel=modes.querySelector('input[name="gmMode"]:checked');mode=sel?sel.value:'step'}
 if(inp.style.display==='none')gmClose(true);
 else if(mode!==null)gmClose({text:inp.value,mode:mode});
 else gmClose(inp.value)};
document.getElementById('gmCancel').onclick=()=>gmClose(null);
document.getElementById('gmInput').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();document.getElementById('gmOk').click()}});
const dlgAlert=(msg,title='提示')=>gmShow({title,msg,cancelText:null});
const dlgConfirm=(msg,title='确认',danger=false)=>gmShow({title,msg,danger}).then(v=>v===true);
const dlgPrompt=(msg,def='',okText='',title='输入')=>gmShow({title,msg,input:def,okText:okText||undefined});
/* 带"插入方式"选择的输入框（强行插入用）：返回 {text,mode} 或 null */
const dlgInjectPrompt=(msg,def,title,defaultMode)=>{
 const modes=document.getElementById('gmModes');
 const lastMode=(()=>{try{return localStorage.getItem('cbp-inject-mode')||defaultMode||'step'}catch(e){return defaultMode||'step'}})();
 return gmShow({title,msg,input:def,okText:tr('插入'),modes:{default:lastMode}})};

/* ── 跟进消息模式切换（卡片头部按钮，不再占用输入区） ── */
let followupMode='agent';
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
  if(current!==id)stashView(renderedSid||current);
  viewCache.delete(id);
  current=id;
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
 if(current!==id)stashView(renderedSid||current);
 clearGoalSubagents();   // 切会话：上一会话的待办/子代理面板不能闪现在新会话上
 current=id;
 let d=await api('/v1/agent/sessions/'+encodeURIComponent(id)),s=d.session;
 // 守卫紧跟 await：原实现写在全部 DOM 写入之后，迟到响应会把旧会话的标题/KPI/时间线
 // 写到用户已切过去的新会话界面上（经典 stale-response）。
 if(current!==id)return;
 if(d.token_budget)setKpis({token_budget:d.token_budget,estimated_tokens:s.estimated_tokens??0});
 currentModel=s.model||null;currentUrl=s.current_url;
 // 记住是不是子代理会话：点进去后要能返回父会话，面板也不能凭空消失
 parentOfCurrent=s.parent_id||null;
 // 刚清空过面板：立刻按正确的父会话刷新一次（否则要等 3 秒节流，"返回父会话"才出现）
 try{refreshSubagents(true)}catch(e){}
 renderStream(d.messages||[],d.steps||[],{scrollEnd:true,force:true});
 // 跟进消息应跟随 Session 自己的模型，而不是左侧新任务表单的选择
 let _ms=document.getElementById('model');
 if(currentModel&&[..._ms.options].some(o=>o.value===currentModel))_ms.value=currentModel;
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
 startEvents(current);     // 用 current 而非入参：等待期间可能已切到别的会话
 await runtime();await refreshSessions();refreshQueue()}

/* 轮询在飞保护 + 会话切换竞态防护：
   延迟>1s 时不再堆叠请求；await 返回时若已切到别的会话，丢弃迟到数据 */
let runtimeInflight=false;
async function runtime(){
 if(serviceStopped||!current||runtimeInflight)return;
 runtimeInflight=true;
 const myId=current;
 try{let r=await api(`/v1/agent/sessions/${encodeURIComponent(myId)}/runtime`);
  if(current!==myId)return;  // 等待期间用户已切换会话
  await applyRuntime(r);
 }catch(e){}
 finally{runtimeInflight=false}}
/* 渲染 runtime 快照：轮询与 SSE 推送共用（SSE 直接送快照，省一次往返） */
/* 目标 / 计划条：goal 模式显示第几轮；待办清单显示完成度并可展开看条目。
   待办数据结构是 {content, status}（status: pending|in_progress|completed），
   **不是 {done}** —— 以前按 `x.done` 计数，永远算出 0/N（用户看到的"0/5"
   一直不涨就是这个原因）。 */
let todosOpen=false;
try{todosOpen=localStorage.getItem('cbp-todos-open')==='1'}catch(e){}
function toggleTodos(){
 todosOpen=!todosOpen;
 try{localStorage.setItem('cbp-todos-open',todosOpen?'1':'0')}catch(e){}
 renderGoalBar(lastRuntime);
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

function updateStreamStatus(r){
 const el=document.getElementById('streamStatus');
 if(!el||!r)return;
 const st=String(r.session_state||r.phase||'');
 const phase=String(r.phase||'');
 const parts=[];
 parts.push('<b class="ss-phase ss-'+esc(st||'running')+'">'+esc(tr(st||phase||'未知'))+'</b>');
 if(r.detail)parts.push('<span class="ss-detail" data-i18n-skip="1">'+esc(String(r.detail))+'</span>');
 if(r.total_steps!==undefined&&r.total_steps!==null)
  parts.push('<span class="ss-meta">'+esc(tr('步'))+' '+esc(String(r.total_steps))+'</span>');
 if(r.estimated_tokens)parts.push('<span class="ss-meta">'+esc(tr('Token'))+' '+esc(String(r.estimated_tokens))+'</span>');
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
}
/* 「最近发送」：把最近一次真正发给模型的完整内容显示出来（原「最近发送内容」标签页）。 */
async function showLastPrompt(){
 if(!current){toast(tr('请先选择 Session'),'err');return}
 try{
  const r=await api('/v1/agent/sessions/'+encodeURIComponent(current)+'/runtime');
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
 const card=document.getElementById('teamCard');
 const chat=document.getElementById('chat');
 // 顺序（用户要求）：todo/子代理 → 调用记录 → 状态条。
 // 所以插到 #toolLog 之前（没有 toolLog 时再退回状态条之前）。
 const anchor=document.getElementById('toolLog')||document.getElementById('streamStatus');
 if(!card||!chat||card.dataset.mounted)return;
 if(anchor&&anchor.parentElement===chat)chat.insertBefore(card,anchor);
 else chat.appendChild(card);
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
 card.hidden=!(hasGoal||hasSub);
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
  parts.push('<button type="button" class="gb-todo" data-act="toggleTodos" '
    +'title="'+esc(tr('待办清单：Agent 用 todo_write 维护，点一下展开/收起；与 /plan 计划模式无关'))+'">'
    +'☑ '+esc(tr('待办'))+' '+done+'/'+todos.length+' '+(todosOpen?'▾':'▸')+'</button>');
 }
 el.innerHTML=parts.join('<span class="gb-sep">·</span>');
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
  el.appendChild(list);
 }
 try{syncTeamCard()}catch(e){}
}
async function applyRuntime(r){
 if(!r)return;
 lastRuntime=r;              // 供 toggleTodos 重绘待办清单
 const myId=current;
 // SSE 推送与 1s/350ms 轮询可能并发到达同一函数：统一以"当前会话"为准，
 // 迟到的或属于别的会话的快照直接丢弃（否则会把 A 的步骤渲染进 C 的时间线）。
 if(!myId)return;
 if(r.session_id&&r.session_id!==myId)return;
 lastRuntime=r;
 try{renderGoalBar(r)}catch(e){}
 currentUrl=r.current_url||currentUrl;
  document.getElementById('pulse').className=r.busy?'pulse':'dot-off';
  try{
    const _sc=document.getElementById('kpiStateCard');
    const _st2=String(r.session_state||r.state||'');
    if(_sc&&_st2&&_sc.dataset.state!==_st2)_sc.dataset.state=_st2;
  }catch(e){}
  const _dash=(v)=> (v===undefined||v===null||v==='')?'—':v;
   const _actTxt=`${tr('阶段')}: ${_dash(r.phase||r.session_state)}`
    +`\n${r.detail||''}`
    +`\n${tr('总步骤')}: ${_dash(r.total_steps)}`
    +`\n${tr('当前会话步骤')}: ${_dash(r.conversation_steps)}`
    +`\n${tr('Token估算')}: ${_dash(r.estimated_tokens)}`
    +`\n${tr('会话序号')}: ${(r.conversation_index??0)+1}`
    +(r.queue_pending?`${tr('队列')}: ${r.queue_pending} ${tr('条待处理')}\n`:'')
    // 暂停/失败时把原因摆出来：以前只显示一个空的「最后更新:」
    +(r.last_error?`\n${tr('最近错误')}: ${r.last_error}`:'')
    +`\n${tr('最后更新')}: ${r.updated?new Date(r.updated*1000).toLocaleTimeString():'—'}`
    +(r.model_warning?`\n\n⚠ ${r.model_warning}`:'');
  const _actEl=document.getElementById('activity');
  if(_actEl.textContent!==_actTxt)_actEl.textContent=_actTxt;   // 内容没变不重写：否则每秒打断文本选择/重排
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
    if(current!==myId)return;
    renderStepsMaybe(d.steps);lastStepCount=d.steps.length;
    try{renderStream(d.messages||[],d.steps||[],{})}catch(e){}
   }catch(e){console.warn('时间线刷新失败:',(e&&e.text)||e)}}}

/* ── SSE 实时推送（EventSource）────────────────────────────────────
   有 SSE 时不再依赖轮询：runtime 快照直接推送 → 提供商流式输出就是真·逐字。
   EventSource 不能带自定义头，token 走 query 参数；连接失败自动退回轮询
   （保留 350ms 快速轮询），并按退避重连。 */
let evtSrc=null,evtSid=null,evtFails=0,lastAssistantCount=0;
function stopEvents(){
 if(evtSrc){try{evtSrc.close()}catch(e){}}
 evtSrc=null;evtSid=null}
function startEvents(sid){
 stopEvents();
 if(!sid||!window.EventSource)return;
 let url=`/v1/agent/sessions/${encodeURIComponent(sid)}/events`;
 const _tk=apiToken();if(_tk)url+=`?token=${encodeURIComponent(_tk)}`;   // token 在 localStorage(cbp-token)，API_TOKEN 是服务端变量
 try{evtSrc=new EventSource(url)}catch(e){evtSrc=null;return}
 evtSid=sid;
 evtSrc.onopen=()=>{evtFails=0};
 evtSrc.onmessage=(e)=>{
  let ev=null;try{ev=JSON.parse(e.data)}catch(_){return}
  if(current!==sid)return;
  if(ev.type==='runtime')applyRuntime(ev.data);
  else if(ev.type==='queue')refreshQueue();
  else if(ev.type==='messages')refreshChat();
  else if(ev.type==='session')refreshSessions()};
 evtSrc.onerror=()=>{
  let failed=evtSid;
  stopEvents();evtFails++;
  if(current===failed&&evtFails<50)
   setTimeout(()=>{if(current===failed)startEvents(failed)},Math.min(3000*evtFails,15000))}}
/* 只刷新消息列表（SSE 通知有新消息时用，比整页 load 轻） */
async function refreshChat(){
 if(!current)return;
 const myId=current;
 try{let d=await api('/v1/agent/sessions/'+encodeURIComponent(myId));
  if(current!==myId)return;
  let as=(d.messages||[]).filter(m=>m.role==='assistant').length;
  renderStream(d.messages||[],d.steps||[]);
  lastAssistantCount=as}catch(e){}}

/* ── 操作 ── */
async function control(a){if(!current)return;try{let r=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/actions/${a}`,{method:'POST'});if(a==='resume')toast(noteText(r, r.runner_started?tr('已恢复，正在续跑任务'):tr('已恢复')), r.note_kind==='err'?'err':'ok');await load(current)}catch(e){toast(tr('操作失败: ')+e,'err')}}
async function activate(){if(!current)return;try{let d=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/activate`,{method:'POST'});currentUrl=d.url;toast(tr('已将自动化 Edge 切换到该会话'),'ok')}catch(e){toast(tr('操作失败: ')+e,'err')}}
function openCopilot(){if(currentUrl){if(!/^https?:\/\//i.test(currentUrl)){toast(tr('地址不是 http(s)，已拒绝打开'),'err');return}window.open(currentUrl,'_blank','noopener,noreferrer')}
 else toast(isProviderModel(currentModel)?tr('该会话用的是 API 提供商模型，没有可打开的网页对话'):tr('该 Session 暂无网页会话地址'),'err')}
/* 删除会话：**自动判断是否用过 Copilot**，用过就一并删远端记录。
   实测痛点：任务删不掉——一是用户不知道该点"仅删本地"还是"连同 Copilot"，
   二是远端删不掉时整个删除被中止、任务留在列表里。现在合并成一个入口。 */
async function removeSession(auto){
 if(!current)return;
 const sid=current;
 let plan=null;
 try{ plan=await api('/v1/agent/sessions/'+encodeURIComponent(sid)+'/deletion-plan') }catch(e){ plan=null }
 if(plan&&plan.running){toast(tr('该任务正在运行，先停止再删除'),'err');return}
 const remote=plan? !!plan.suggest_delete_remote : !!auto;
 const extra=remote
   ?tr('，并删除该任务关联的 {n} 个 Copilot 网页会话（标题里可能已没有 [CBP-AGENT] 标记，删除时会用会话正文核对归属）',{n:(plan&&plan.remote_count)||1})
   :'';
 const msg=(remote
   ?tr('这个任务用过 Copilot，将同时删除远端对话记录。\n\n  ID: {id}\n  标题: {title}\n\n本地 Session{extra}。此操作不可撤销，确定继续？',
       {id:sid,title:(document.getElementById('title').textContent||'').trim(),extra:extra})
   :tr('即将删除的会话：\n\n  ID: {id}\n  标题: {title}\n\n仅删除本地 Session（这个任务没有用过 Copilot 网页会话）。此操作不可撤销，确定继续？',
       {id:sid,title:(document.getElementById('title').textContent||'').trim()}));
 if(!await dlgConfirm(msg,tr('删除确认'),true))return;
 try{
  let url='/v1/agent/sessions/'+encodeURIComponent(sid);
  const qs=[];
  if(remote)qs.push('delete_remote=true');
  // 用过 Copilot 但远端删不掉时，本地也要清掉（否则任务永远留在列表里）
  if(remote)qs.push('force_local=true');
  if(qs.length)url+='?'+qs.join('&');
  let d=await api(url,{method:'DELETE'});
  const failed=(d.remote_results||[]).filter(x=>!x.deleted);
  if(failed.length&&d.deleted){showDeleteResult(d)}      // 本地已删，展示远端失败明细
  else if(!d.deleted){showDeleteResult(d);return}
  if(d.deleted){
   stopEvents();         // 删掉当前会话必须停 SSE，否则重连定时器会一直敲已删除的会话
   current=null;
   document.getElementById('sessionView').style.display='none';
   document.getElementById('emptyState').style.display='flex';
   await refreshSessions()}
 }catch(e){toast(tr('删除失败: ')+(e.text||e),'err')}}
/* ── 限流倒计时：居中卡片，可收起为右下角小标 ── */
let retryDismissedAt=null;   // 用户已收起的那个终点时间戳（新一轮限流会自动再展开）
let lastRuntime=null;        // 最近一次 runtime 响应，供小标展开时立即重绘
function fmtCountdown(total){
 total=Math.max(0,Math.ceil(total/1000));
 let h=Math.floor(total/3600),m=Math.floor((total%3600)/60),s=total%60;
 return h>0?`${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`
            :`${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`}
function hideRetryUI(){
 let box=document.getElementById('retryCountdown'),chip=document.getElementById('retryChip'),
     mask=document.getElementById('retryMask');
 if(box){box.style.display='none';box.removeAttribute('data-resume')}
 if(mask)mask.style.display='none';
 if(chip){chip.style.display='none';chip.removeAttribute('data-resume')}}
/* 收起遮罩（"稍后再看" / 点遮罩空白处）：保留右下角小标，后台继续倒计时 */
function dismissRetryOverlay(){
 const box=document.getElementById('retryCountdown');
 if(box)retryDismissedAt=parseFloat(box.dataset.resume)||null;
 updateRetryCountdown(lastRuntime)}
/* 服务不可用（短暂退避）时的顶栏小标：不弹遮罩、不打断操作，但一直看得见。
   返回 true 表示"这个状态已经用小标表达了"。 */
function showServiceBadge(){
 const chip=document.getElementById('retryChip');
 if(!chip)return false;
 const err=String((lastRuntime&&lastRuntime.service_error)||"");
 const waiting=!!(lastRuntime&&lastRuntime.service_waiting);
 if(!(waiting||/不可用|rate.?limit|限流|超时|timeout/i.test(err)))return false;
 chip.textContent='⚠ '+tr('服务不可用');
 chip.title=err||tr('最近一次服务错误，点击查看');
 chip.dataset.resume='';
 chip.style.display='inline-flex';
 const mask=document.getElementById('retryMask');
 if(mask)mask.style.display='none';
 const box=document.getElementById('retryCountdown');
 if(box)box.style.display='none';
 return true}
function updateRetryCountdown(r){
 let box=document.getElementById('retryCountdown'),chip=document.getElementById('retryChip'),mask=document.getElementById('retryMask');
 if(!box)return;
 lastRuntime=r||lastRuntime;
 // 局部快照（SSE 只带部分字段）不能当成"没有限流"把遮罩收掉——
 // 以前这里直接读 r.retry_resume_at，SSE 一推就 hideRetryUI()，遮罩闪一下没了。
 const _hasField=!!(r&&Object.prototype.hasOwnProperty.call(r,'retry_resume_at'));
 let resumeAt=(r&&r.retry_resume_at)||null;
 if(!_hasField&&box.dataset.resume)resumeAt=parseFloat(box.dataset.resume)||null;  // 沿用已知
 // ① 没有限流窗口，但服务不可用（短暂退避）：用**顶栏小标**提示，不弹遮罩。
 //    这种情况后端不会写 retry_resume_at（只有 last_error 保留到恢复），
 //    所以必须先判它，否则会被下面"明确清空"直接收掉 —— 用户实测"都没看见弹窗"。
 if(showServiceBadge()){retryDismissedAt=null;return}
 if(_hasField&&!r.retry_resume_at){retryDismissedAt=null;hideRetryUI();return}   // 明确清空
 if(!resumeAt||resumeAt*1000<=Date.now()){
  if(!_hasField&&box.dataset.resume){resumeAt=parseFloat(box.dataset.resume)}
  else{hideRetryUI();return}}
 // 换了新的限流终点（新一轮）→ 重新展开卡片
 if(retryDismissedAt!==null&&retryDismissedAt!==resumeAt)retryDismissedAt=null;
 let showCard=retryDismissedAt!==resumeAt;
 let text=fmtCountdown(resumeAt*1000-Date.now());
 box.dataset.resume=resumeAt;
 if(chip){chip.dataset.resume=resumeAt;chip.textContent='⏳ '+text}
 if(!showCard){
  box.style.display='none';if(mask)mask.style.display='none';if(chip)chip.style.display='inline-flex';
  // 卡片(高) → 小标(矮) 必须重算 --retry-h：否则底部让位带仍按卡片高度预留，
  // 小标会远离输入框、也可能压住内容（布局审计实测 retryChip 遮挡 #followup 6%）
  try{syncFixedLanes()}catch(e){}
  return}
  box.style.display='block';if(mask)mask.style.display='flex';if(chip)chip.style.display='none';
 try{syncFixedLanes()}catch(e){}
 // 标题按实际通道措辞：API 提供商（模型 ID 含 /，如 deepseek/deepseek-chat）
 // 的 5xx/超时也走同一个窗口，写成"Copilot 限流"不准确。
 let isProvider=!!(currentModel&&currentModel.indexOf('/')>=0);
 let title=box.querySelector('.rc-title');
 // 这两条以前是硬编码中文：英文模式下只有副标题是英文、标题仍是中文（用户实测）
 if(title)title.textContent='⏳ '+(isProvider?tr('API 服务限流/不可用，预计恢复')
                                        :tr('Copilot 限流/不可用，预计恢复'));
 box.querySelector('.rc-count').textContent=text;
 let sub=box.querySelector('.rc-sub');
 const suspended=String(lastRuntime&&(lastRuntime.session_state||lastRuntime.state)||'')==='suspended';
 sub.textContent=suspended
  ? tr('Copilot 网页限流，已挂起：到点自动继续，也可以换下面的提供商模型立即继续')
  : tr('第 {n} 次 · 自动重试中，可点「暂停」稍后继续',{n:lastRuntime.retry_attempt||1});
 fillRetrySwap(suspended);
}
/* 挂起态：把"已配置的提供商模型"填进下拉（只列 provider/model，不含 Copilot 网页模型） */
function fillRetrySwap(suspended){
 const box=document.getElementById('retrySwap');
 if(!box)return;
 if(!suspended){box.style.display='none';return}
 let list=[];
 try{list=(window.__MODELS||[]).filter(m=>String(m.id||'').indexOf('/')>=0&&m.available!==false)}catch(e){}
 if(!list.length){
  box.style.display='none';
  const sub=document.querySelector('#retryCountdown .rc-sub');
  if(sub)sub.textContent=tr('Copilot 网页限流，已挂起：到点自动继续（没有配置其它提供商模型）');
  return;
 }
 // 模型直接列成按钮：遮罩上点一下就走，不用先开下拉再点确认
 box.innerHTML=list.map(m=>`<button type="button" class="btn ghost" data-act="swapModelResume"
   data-arg="${esc(m.id)}" title="${esc(tr('用这个模型继续'))}">${esc(m.id)}</button>`).join('');
 box.style.display='flex';
}
async function swapModelResume(model){
 if(!model||!current)return;
 try{
  await api(`/v1/agent/sessions/${current}/model`,{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({model})});
  await api(`/v1/agent/sessions/${current}/actions/resume`,{method:'POST',
   headers:{'Content-Type':'application/json'},body:'{}'});
  toast(tr('已切换到 {m} 并继续',{m:model}),'ok');
 }catch(e){ toast(tr('切换失败: ')+((e&&(e.text||e.detail))||e),'err') }
}
/* 收起（×）与展开（点小标） */
/* "稍后再看"：通过 data-act 走 dismissRetry（见 ACTIONS） */
try{document.getElementById('retryMask').addEventListener('click',e=>{
 // 点遮罩空白处也收起（点卡片内部不触发）
 if(e.target&&e.target.id==='retryMask')dismissRetryOverlay()})}catch(e){}
/* 点小标重新打开遮罩：走 data-act=reopenRetry（见 ACTIONS） */
/* 每秒把倒数刷新一次（与 runtime 轮询解耦，数字平滑跳动） */
setInterval(()=>{
 let box=document.getElementById('retryCountdown'),chip=document.getElementById('retryChip');
 let src=(box&&box.dataset.resume)||(chip&&chip.dataset.resume);
 if(!src)return;
 let ms=parseFloat(src)*1000-Date.now();
 if(ms<=0){retryDismissedAt=null;hideRetryUI();return}
 let text=fmtCountdown(ms);
 if(box&&box.style.display!=='none')box.querySelector('.rc-count').textContent=text;
 if(chip&&chip.style.display!=='none')chip.textContent='⏳ '+text;
},1000);

/* ── 回滚任务改动：按 Copilot 回合的版本回滚工作区 ── */
async function rollbackSession(){
 if(!current){toast(tr('请先选择 Session'),'err');return}
 try{
  let s=await api('/v1/agent/sessions/'+encodeURIComponent(current)+'/snapshots');
  let files=s.files||[];
  if(!files.length){toast(tr('该任务没有可回滚的文件改动'),'err');return}
  let versions=s.versions||[];
  let version=null;
  if(versions.length>1){
   // 版本按 Copilot 回合计：一个版本 = 一个回合。把同一版本的路径合并成一行。
   let byRev={};
   versions.forEach(v=>{(byRev[v.rev]=byRev[v.rev]||[]).push(v)});
   let revs=Object.keys(byRev).map(Number).sort((a,b)=>a-b);
   let lines=revs.map(r=>{
    let vv=byRev[r];
    let t=new Date((vv[0].captured||0)*1000).toLocaleTimeString('zh-CN',{hour12:false});
    let paths=vv.map(x=>x.path).slice(0,5).join('、')+(vv.length>5?`…共 ${vv.length} 个`:'');
        return tr('  · 回合 {r}（{t}）：{paths}',{r:r,t:t,paths:paths});
   }).join('\n');
       let input=await dlgPrompt(
      tr('该任务共 {n} 个回合版本（按时间先后）：\n\n{lines}\n\n输入「0」或留空 = 整任务回滚到开始前；输入版本号 = 撤销该回合之后的改动。',
         {n:revs.length,lines:lines}),'0','',tr('回滚到哪个版本？'));
   if(input===null)return;
   version=parseInt(input,10);
   if(isNaN(version))version=0;
   version=version>0?version:null;
  }
  let preview=files.slice(0,12).map(f=>`· [${f.kind}] ${f.path}`).join('\n');
  let more=files.length>12?`\n… 另有 ${files.length-12} 个`:'';
     let target=version?tr('回滚到版本 {v}（撤销该回合之后的改动）',{v:version}):tr('整任务回滚到开始前');
     let msg=tr('{target}：{n} 个路径。\n修改过的还原、任务新建的删除。\n\n{preview}{more}\n\n此操作不可撤销，确定继续？',
     {target:target,n:files.length,preview:preview,more:more});
     if(!await dlgConfirm(msg,tr('回滚任务改动'),true))return;
  let body=version?{version}:{};
  let d=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/rollback`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let tail=d.failed?`，失败 ${d.failed}`:'';
  toast(tr('回滚完成：还原 {a}、删除 {b}{tail}',{a:d.restored,b:d.deleted,tail:tail}), d.failed?'err':'ok');
  await load(current)
 }catch(e){toast(tr('回滚失败: ')+(e.text||e),'err')}}
/* ── 回滚服务自升级：还原 CopilotBridge 自身代码（需重启服务生效） ── */
async function rollbackSelfUpgrade(){
 try{
  let st=await api('/v1/agent/self/status');
  if(!st.snapshot_scopes||!st.snapshot_scopes.length){toast(tr('没有可回滚的自升级记录'),'err');return}
  let list=[];
  for(const scope of st.snapshot_scopes.slice(0,5)){
   try{let s=await api('/v1/agent/self/snapshots?sid='+encodeURIComponent(scope.replace(/^_self_/,'')));list.push(`${scope}：${(s.files||[]).length} 个文件`)}catch(e){}
  }
     let msg=tr('将把服务自身代码恢复到自升级前的状态（最近一次）：\n\n{list}\n\n回滚后必须重启服务才生效。确定继续？',
     {list:list.join('\n')});
     if(!await dlgConfirm(msg,tr('回滚服务自升级'),true))return;
  let d=await api('/v1/agent/self/rollback?last=true',{method:'POST'});
  toast(tr('自升级已回滚：还原 {a}、删除 {b}{fail} · 请重启服务',{a:d.restored||0,b:d.deleted||0,fail:d.failed?tr('，失败 {n}',{n:d.failed}):''}), d.failed?'err':'ok');
 }catch(e){toast(tr('自升级回滚失败: ')+e,'err')}}
/* ── 删除结果可视化弹窗 ── */
let lastDeleteResult=null;
function showDeleteResult(d){
 lastDeleteResult=d;
 let list=d.remote_results||[],ok=list.filter(x=>x.deleted).length,fail=list.length-ok;
 document.getElementById('dlgHint').textContent=list.length
  ?tr('共 {n} 个 Copilot 会话 · 成功 {a} · 失败 {b}',{n:list.length,a:ok,b:fail}):'';
 let msg=document.getElementById('dlgMsg');msg.style.display=d.message?'':'none';
 msg.textContent=d.message?tr(d.message):'';
 let box=document.getElementById('dlgList');box.innerHTML='';
 if(!list.length)box.innerHTML='<div class="dmsg">'+esc(tr('没有远程删除明细。'))+'</div>';
 list.forEach(r=>{let e=document.createElement('div');e.className='dres '+(r.deleted?'ok':'fail');
  e.innerHTML=`<div class="rhead"><span class="ric">${r.deleted?'✓':'✗'}</span><span class="ttl" title="${esc(r.expected_title||'')}">${esc(r.expected_title||tr('(无标记)'))}</span><span class="mth">${esc(r.method||'')}</span></div>`+
   (r.url?`<div class="url" title="${tr('点击复制完整 URL')}">${esc(r.url)}</div>`:'')+
   (r.error?`<div class="err">${esc(tr(r.error))}</div>`:'');
  if(r.url)e.querySelector('.url').onclick=ev=>{try{navigator.clipboard.writeText(r.url);ev.target.textContent=tr('已复制 ✓  ')+r.url}catch(_){dlgAlert(r.url,tr('链接'))}};
  box.appendChild(e)});
 document.getElementById('dlgForce').style.display=fail?'':'none';
 openMask('dlgMask')}
function closeDlg(){closeMask('dlgMask')}
document.getElementById('dlgCopy').onclick=()=>{if(!lastDeleteResult)return;try{navigator.clipboard.writeText(JSON.stringify(lastDeleteResult,null,2))}catch(e){}};
document.getElementById('dlgForce').onclick=()=>forceDeleteLocal();
async function forceDeleteLocal(){
 if(!current)return;let b=document.getElementById('dlgForce');b.disabled=true;b.textContent=tr('处理中…');
 try{let d=await api('/v1/agent/sessions/'+encodeURIComponent(current)+'?delete_remote=true&force_local=true',{method:'DELETE'});
  if(d.deleted){closeDlg();current=null;document.getElementById('sessionView').style.display='none';document.getElementById('emptyState').style.display='flex';await refreshSessions()}
  else showDeleteResult(d)}catch(e){toast(tr('强制清理失败: ')+e,'err')}
 finally{b.disabled=false;b.textContent=tr('重试远程并强制清理本地记录')}}
/* ── 使用手册 ── */
const MANUAL_HTML=`<h3>快速上手</h3>
<ul>
<li>左侧「新任务」：填任务内容，选 <b>Agent 执行</b>（可读写工作区文件、自动验证）或 <b>普通对话</b>（纯问答），点「启动任务」。</li>
<li>每个任务对应一个 Session，背后是一个独立的 Copilot 网页会话；左侧列表点选查看。</li>
<li>自动化浏览器窗口可用 <b>▣</b> 按钮显示/隐藏（无头切换会重启浏览器但保留登录态）。</li>
</ul>
<h3>浏览器按需启动（无 Copilot 也能用）</h3>
<p>服务启动时<b>不会</b>去拉浏览器：API 与 WebUI 立刻可用，提供商模型（DeepSeek/Kimi/OpenAI 等）
可直接使用。只有真正用到 Copilot（copilot 模型的对话/任务、模型列表刷新）时才会按需启动浏览器。</p>
<ul>
<li>界面顶部会显示状态：未启动 / 启动中 / 等待登录 / 失败，并带<b>重试浏览器</b>按钮（登录完成后点它即可，无需重启服务）。</li>
<li>提供商模型路径<b>永远不触发</b>浏览器；Copilot 未就绪时相关请求会返回明确提示而不是含混报错。</li>
<li>想改成启动即预热：把 <code>config.json</code> 的 <code>browser_startup</code> 设为 <code>eager</code>。</li>
<li>排障：<code>GET /health</code> 里的 <code>copilot_ready</code> / <code>browser_state</code> / <code>browser_error</code>。</li>
</ul>

<h3>会话控制</h3>
<ul>
<li><b>暂停 / 恢复</b>：暂停在当前回合结束后停放；恢复直接从检查点续跑。</li>
<li><b>压缩上下文</b>：手动把历史压缩成摘要（一般不需要，阈值自动触发）。</li>
<li><b>继续执行</b>：追加指令并继续任务。<b>切换到页面</b>：让自动化浏览器跳到该会话。<b>打开会话</b>：在你自己的浏览器里打开 Copilot 页面。</li>
<li><b>取消任务</b>：终止并标记取消。<b>更多 ▾</b>：归档、删除本地记录、连同 Copilot 网页会话一起删除。</li>
</ul>
<h3>任务快照与回滚</h3>
<ul>
<li>任何写操作前自动记录<b>改动前原像</b>（同一路径只记一次，保留最早状态）。</li>
<li>会话详情页点<b>回滚任务改动</b>：改过的文件还原、任务新建的文件/目录删除，回到<b>任务开始前</b>。</li>
<li>上限：单文件 8MB / 单会话 800 条目 / 原像总量 256MB；超限只记入 skipped，<b>不阻断写入</b>。</li>
<li>删除会话会同时回收其工作区快照与自升级快照。</li>
</ul>

<h3>回滚到指定版本</h3>
<p>每个写操作都会记录一个<b>版本点</b>（版本按 Copilot 回合计数：一次 complete 才 +1），
所以回滚不再只有"回到任务开始前"这一种选择。</p>
<ul>
<li>会话详情页的<b>回滚</b>可选择具体版本：回到某一步之前、或某个回合之后的状态。</li>
<li>与"任务级快照"配合：快照负责还原内容，版本点负责选定落点。</li>
<li>离线通道 <code>python scripts/rollback.py</code> 同样支持按版本回滚。</li>
</ul>

<h3>跟进对话</h3>
<ul>
<li>会话卡片下方可继续发消息：<b>Agent 继续执行</b> 会执行工具动作；<b>普通对话</b> 只在 Copilot 会话里问答。输入框 <kbd>Enter</kbd> 发送、<kbd>Shift+Enter</kbd> 换行。</li>
<li>跟进消息跟随 Session 自己的模型，不是左侧新任务表单的模型。</li>
</ul>
<h3>跟进消息队列</h3>
<p>继续对话支持<b>入队即返回</b>：提交的消息先进入队列，后台按序消费，不必等当前回合跑完。</p>
<ul>
<li>排队中/正在处理的消息可在界面上<b>查看、修改、删除</b>，并与消息历史同步。</li>
<li>队列条只显示等待中的消息，正在处理的那条不重复出现。</li>
<li>适合"先把后续几步都交代清楚"的场景，避免打断正在进行的回合。</li>
</ul>

<h3>粘贴图片 / 截图</h3>
<p>继续对话与新任务都支持直接<b>粘贴图片</b>（Ctrl+V 截图或复制的图片），随消息一起发给 Copilot，
用于"看这张报错截图""按这个设计稿改页面"这类任务。</p>

<h3>模型</h3>
<ul>
<li>模型下拉框来自后端 <code>/v1/models</code>：Copilot 网页模型（copilot-*）+ API 提供商模型（如 <code>deepseek/deepseek-chat</code>）。</li>
<li>点 ↻ 重新枚举 Copilot 菜单（新上线的模型自动发现）；子菜单（如 GPT 下的模型）也能识别和选择。</li>
<li>API 提供商需在环境变量配置 Key（如 <code>DEEPSEEK_API_KEY</code>），详见 README。</li>
</ul>
<h3>模型列表刷新</h3>
<p>Copilot 菜单改版或渲染不全时模型会"消失"。会话页顶部模型下拉旁的 <b>↻ 图标</b>可一键重扫
（等价于 <code>POST /v1/models/refresh</code>）；刷新期间按钮禁用，完成后提示并同步会话内模型选项。</p>

<h3>限流倒计时</h3>
<p>Copilot 返回限流时，界面会弹出<b>倒计时浮窗</b>：锚定首次超限时刻、按固定窗口计算剩余等待时间
（默认窗口 <code>service_retry_window_seconds</code> = 3660 秒 = 1 小时 + 1 分钟余量），避免反复手动重试。</p>
<p>倒计时结束后服务会自动继续；你也可以在此期间暂停或取消任务。</p>

<h3>长任务机制</h3>
<ul>
<li>上下文超阈值自动压缩；单会话过长自动换会话接力（检查点摘要带过去）。</li>
<li>Copilot 限流时状态变为「等待服务」并指数退避自动重试；对话被 Copilot 判满时自动换会话重发。</li>
<li>服务重启后自动恢复被中断的任务（auto_resume）。</li>
</ul>
<h3>批量动作（省 Copilot 回合）</h3>
<p>模型可一次返回多个动作：<code>{"type":"actions","actions":[动作1,动作2,...]}</code>，本地按顺序执行，每个动作仍单独记一步。</p>
<ul><li>上限由 <code>max_actions_per_turn</code> 控制；解析层另有硬上限 <code>MAX_BATCH_ACTIONS</code>（当前 4），超出部分会被截断并标记 <code>_batch_truncated</code>。</li>
<li>批内某动作失败不影响后续；批内 final 会被延后，由<b>验证门</b>裁决能否完成。</li></ul>

<h3>长期记忆（跨会话）</h3>
<p>除了 <b>AGENT.md</b>（每回合全文注入的硬约定）与 <b>skills</b>（过程性"怎么做"），
Agent 还有一套<b>长期记忆</b>：把以往任务里验证过的零散事实累积下来，后续相关任务会
自动检索并注入提示词（关键词 + 中文二元组匹配，零依赖、不联网）。</p>
<ul>
<li><b>自动沉淀</b>：任务收尾时 Agent 判断"是否产生了跨任务仍有价值的事实"，值得才写；琐碎过程不写。</li>
<li><b>手动要求</b>：直接说"把 XX 记到长期记忆"即可。</li>
<li><b>作用域</b>：<code>global</code> 对所有任务可见；<code>project</code> 只在当前工作区可见。</li>
<li><b>检索</b>：提示词只注入最相关的少量条目（非全量）；与当前事实冲突时以当前为准，过时的可删除。</li>
<li><b>管理面板</b>：点顶部工具栏的<b>记忆图标</b> → 按作用域筛选、关键词检索、查看统计
（总数 / 置顶 / <b>从未命中</b>，用来发现该清理的条目）、逐条<b>置顶</b>或<b>删除</b>。</li>
<li><b>置顶</b>：置顶的记忆每次检索都注入，只留给极重要的约定；再点一次取消。</li>
<li><b>开关</b>：<code>allow_memory_write</code>（默认 true）可整体关闭记忆写入。</li>
<li><b>接口</b>：<code>GET /v1/agent/memory?query=&amp;scope=</code>、
<code>POST /v1/agent/memory/{id}/pin</code>、<code>DELETE /v1/agent/memory/{id}</code>；
存储为独立库 <code>data/memory.db</code>，上限 500 条/作用域，超出自动淘汰低价值条目。</li>
</ul>

<h3>技能自沉淀（skills）</h3>
<ul>
<li>任务收尾时先 <code>skill_list</code> 查有没有相关技能；没有且做法可复用，用
<code>skill_create</code> 新建（默认写当前工作区 <code>skills/&lt;名字&gt;/SKILL.md</code>）。</li>
<li>写全局 <code>skills/</code>（对所有工作区生效）需开启 <code>allow_self_upgrade</code>。</li>
<li>技能<b>即时可用</b>、无需重启；同名技能项目级覆盖全局。</li>
</ul>

<h3>界面</h3>
<ul>
<li><b>◐</b> 主题轮换：深色/浅色/Nord 蓝/护眼米/Dracula 紫/柔粉/薄荷，刷新保持。</li>
<li>侧栏左缘的细长拉手收起/展开侧栏；实时面板（执行状态/当前回答/发送内容/继续对话）是选项卡，<b>⤢ 放大</b>看全文。</li>
<li>步骤时间线每条带执行时间；「查看结果」展开结构化 JSON。</li>
</ul>
<h3>服务自升级（默认关闭）</h3>
<ol>
<li>在 <code>config.json</code> 的 agent 段设 <code>"allow_self_upgrade": true</code> 并重启；
随后注册 <code>self_*</code> 工具（白名单：<code>cbp/ webui/ scripts/ docs/ tests/ skills/</code> 等；
<code>config.json</code>、<code>data/</code>、<code>EdgeProfile/</code> 永远禁碰）。</li>
<li>让任务"用自升级工具"改代码：self_read_text → self_replace_text →
<b>self_validate</b>（全量语法检查）→ <b>self_unittest</b>（跑服务自身单测），两项通过才能 final。</li>
<li>改动需<b>人工重启</b>才生效；<code>GET /v1/agent/self/status</code> 的 <code>restart_pending</code> 会提示。</li>
<li>回滚：<code>POST /v1/agent/self/rollback</code>（在线）或 <code>python scripts/rollback.py</code>（离线）。</li>
</ol>
<p class="hint">工作区不允许指向服务安装目录（除非已开启自升级）——否则普通文件工具即可改服务代码，绕过权限门。</p>
<h4>开了之后怎么跟它说要改自己</h4>
<p>先说结论：<b>不是"描述你想要什么"就够，而是"指文件 + 说清改什么 + 给验收标准"</b>。
新开一个会话（上下文干净），照下面这个模板说：</p>
<pre>【自升级任务】
文件：cbp/bridge.py（安装目录相对路径，不要写绝对路径）
现状：&lt;你观察到的行为，例如 resume 时提示只拼原任务&gt;
要改：&lt;越具体越好，能贴函数名/字段名/代码片段就贴&gt;
边界：只改上面这个文件或这段逻辑，其它不要动
验收：self_validate + self_unittest 双通过
交付：告诉我改了哪些行、验证结果、需要重启、以及回滚命令</pre>
<p>也可以直接说短句，效果一样：</p>
<ul>
<li>「把 <code>webui/style.css</code> 里侧栏默认宽度 318px 改成 300px，改完跑 self_validate 和 self_unittest」</li>
<li>「看 <code>cbp/api.py</code> 的 <code>/v1/shutdown</code>，把优雅停机超时从 8 秒改成 15 秒，其他别动」</li>
</ul>
<p>先确认开关真的生效：问它一句「你有哪些 self_ 开头的工具」，应该能列出
<code>self_read_text</code> / <code>self_write_text_b64</code> / <code>self_append_text_b64</code> /
<code>self_replace_text</code> / <code>self_validate</code> / <code>self_unittest</code> /
<code>self_rollback</code> 共 7 个；关着的时候是 0 个。</p>
<h4>它会遵守的硬约束</h4>
<ul>
<li><b>只能改白名单</b>：<code>cbp/</code> <code>webui/</code> <code>scripts/</code> <code>docs/</code>
<code>tests/</code> <code>skills/</code> 以及 <code>app.py</code> <code>start.py</code>
<code>install.py</code> <code>requirements.txt</code> <code>AGENT.md</code>
<code>README.md</code> <code>config.example.json</code>。</li>
<li><b>禁写</b>：<code>config.json</code>、<code>data/</code>、<code>EdgeProfile/</code>、
<code>for debug/</code>、<code>.git/</code>、<code>__pycache__/</code>。要调配置<b>你自己改</b>，
它写会被拒。</li>
<li><b>验证门</b>：写完必须 <code>self_validate</code> + <code>self_unittest</code> 都通过，
否则不能宣称完成（系统会拦下 final）。</li>
<li><b>自动快照</b>：每次写/替换都在 <code>data/snapshots/_self_&lt;sid&gt;/</code> 存旧版本，
可按任务整体回滚。</li>
</ul>
<h4>出问题怎么退（三条路，从近到远）</h4>
<ol>
<li>界面：「更多 ▾」→ <b>回滚服务自升级…</b></li>
<li>接口：<code>POST /v1/agent/self/rollback?last=true</code></li>
<li><b>服务起不来时</b>（唯一可用）：<code>python scripts/rollback.py --scope self --last --yes</code></li>
</ol>
<p class="hint">第 3 条是救命通道：把服务改到启动即崩，网页和接口都进不去，只能本地命令行退。</p>
<h4>五个常见坑</h4>
<ul>
<li><b>一次只让它做一件事</b>：回滚粒度是"整个任务"，混进多个不相关改动就只能全退。</li>
<li><b>别只信"验证通过"</b>：<code>self_validate</code> 只查语法、<code>self_unittest</code>
只跑 <code>tests/</code>，都不等于行为正确。正确流程是：它改 → 你 <code>git diff</code> 审 →
重启 → 实际点一遍。</li>
<li><b>改提示词/工具描述算高危</b>：<code>_system_prompt()</code> 与工具 description 会直接改变
Agent 行为，改完必须实测一轮真实任务。</li>
<li><b>没重启 ≠ 没效果</b>：改的是磁盘文件，进程里还是旧代码。觉得"没生效"先确认重启。</li>
<li><b>大文件要分块写</b>：<code>self_write_text_b64</code> 每块 ≤3000 字符，用
<code>self_append_text_b64</code> 续写；没写完就验证必然失败，那不是缺陷。</li>
</ul>
<p class="hint">风险提醒：自升级等于允许 Agent 改自己的代码；若同时开了
<code>allow_shell</code> / <code>allow_python</code> / <code>allow_delete</code>，
实质是给本机完全控制权。建议只在有人盯着时开、改完关回去，且每次改完自己
<code>git diff</code> 审一遍再提交。</p>

<h3>常见问题</h3>
<ul>
<li><b>服务重启后短暂停在登录页</b>：企业 SSO 会经过公司 ADFS 自动回跳，约需 40 秒，一般无需手动登录；若停在账户选择页，手动点一下即可。</li>
<li><b>提示找不到输入框/模型按钮</b>：登录态失效，运行 <code>python scripts/login.py</code> 重新登录（或在有窗口模式下直接登录）。</li>
<li><b>任务显示等待服务</b>：Copilot 在限流，等它自动恢复；持续不可用会安全暂停，恢复后点「恢复」。</li>
</ul>`;
/* 英文手册：整篇对照翻译（章节数与中文版一致，18 节）。
   以前只翻了 h3、正文留中文——用户实测「手册还是夹生的」。 */
const MANUAL_HTML_EN=`<h3>Quick start</h3>
<ul>
<li><b>New task</b> on the left: type the task, choose <b>Agent run</b> (may read/write workspace files, self-verifies) or <b>Plain chat</b> (Q&amp;A only), then press <b>Start</b>.</li>
<li>Each task is a Session backed by its own Copilot web conversation; pick one in the left list to view it.</li>
<li>Show/hide the automation browser with the <b>browser</b> button in the toolbar (switching to headless restarts it but keeps the login).</li>
</ul>
<h3>Browser starts on demand (usable without Copilot)</h3>
<p>The service does <b>not</b> launch a browser at startup: the API and WebUI are available immediately and
provider models (DeepSeek/Kimi/OpenAI, …) work right away. The browser starts only when Copilot is actually
needed (a copilot-model chat/task, or refreshing the model list).</p>
<ul>
<li>The top bar shows the state: not started / starting / waiting for sign-in / failed, plus a <b>Retry browser</b>
button (click it after signing in — no service restart needed).</li>
<li>Provider models <b>never</b> touch the browser; while Copilot is not ready, those requests return an explicit
message instead of a vague error.</li>
<li>To warm the browser up at startup, set <code>browser_startup</code> to <code>eager</code> in <code>config.json</code>.</li>
<li>Troubleshooting: <code>copilot_ready</code> / <code>browser_state</code> / <code>browser_error</code> in <code>GET /health</code>.</li>
</ul>

<h3>Session controls</h3>
<ul>
<li><b>Pause / resume</b>: pause stops after the current turn; resume continues from the checkpoint.</li>
<li><b>Compact context</b>: manually squeeze history into a summary (usually unnecessary — thresholds do it automatically).</li>
<li><b>Continue</b>: append instructions and keep going. <b>Switch to page</b>: point the automation browser at that
conversation. <b>Open conversation</b>: open the Copilot page in your own browser.</li>
<li><b>Cancel task</b>: stop and mark as cancelled. <b>More ▾</b>: archive, delete the local record, or delete it together
with the Copilot web conversation.</li>
</ul>
<h3>Task snapshots and rollback</h3>
<ul>
<li>Before any write the service records a <b>pre-change image</b> (one per path, keeping the earliest state).</li>
<li>On the session page click <b>Roll back task changes</b>: modified files are restored, files/directories the task
created are deleted — back to the state <b>before the task started</b>.</li>
<li>Limits: 8 MB per file / 800 entries per session / 256 MB of images in total; beyond that writes are only noted
in <i>skipped</i> — <b>writing is never blocked</b>.</li>
<li>Deleting a session also reclaims its workspace snapshots and self-upgrade snapshots.</li>
</ul>

<h3>Roll back to a specific version</h3>
<p>Every write records a <b>version point</b> (the version counts Copilot turns: +1 per complete turn), so rollback
is not limited to "back to before the task started".</p>
<ul>
<li><b>Rollback</b> on the session page lets you pick a version: the state before a given step, or after a given turn.</li>
<li>Works together with task snapshots: snapshots restore the content, version points choose the landing spot.</li>
<li>The offline channel <code>python scripts/rollback.py</code> supports rollback by version too.</li>
</ul>

<h3>Follow-up chat</h3>
<ul>
<li>Send more messages under a session card: <b>Agent continue</b> executes tool actions; <b>Plain chat</b> only
talks to the Copilot conversation. <kbd>Enter</kbd> sends, <kbd>Shift+Enter</kbd> inserts a newline.</li>
<li>Follow-ups use the session's own model, not the model picked in the new-task form.</li>
</ul>
<h3>Follow-up message queue</h3>
<p>Follow-ups are <b>queued and return immediately</b>: the message enters a queue, the backend consumes it in order,
so you do not have to wait for the current turn to finish.</p>
<ul>
<li>Queued and in-flight messages can be <b>viewed, edited and deleted</b> in the UI, and stay in sync with the history.</li>
<li>The queue bar lists waiting messages only; the one being processed is not shown twice.</li>
<li>Handy for "lay out the next few steps up front" without interrupting the running turn.</li>
</ul>

<h3>Paste images / screenshots</h3>
<p>Both follow-ups and new tasks accept <b>pasted images</b> (Ctrl+V of a screenshot or copied image); they are sent
to Copilot with the message — for tasks like "look at this error screenshot" or "adjust the page to this design".</p>

<h3>Models</h3>
<ul>
<li>The model dropdown comes from <code>/v1/models</code>: Copilot web models (copilot-*) plus API provider models
(such as <code>deepseek/deepseek-chat</code>).</li>
<li>Click ↻ to re-enumerate the Copilot menu (newly released models are discovered automatically); sub-menus
(e.g. models under GPT) are detected and selectable too.</li>
<li>API providers need their key in the environment (e.g. <code>DEEPSEEK_API_KEY</code>); see the README.</li>
</ul>
<h3>Refreshing the model list</h3>
<p>When the Copilot menu changes or renders incompletely, models "disappear". The <b>↻ icon</b> next to the model
dropdown at the top of the session page rescans (<code>POST /v1/models/refresh</code>); the button is disabled
during the scan, then a toast reports the result and the session's model options are updated.</p>

<h3>Rate-limit countdown</h3>
<p>When Copilot rate-limits, a <b>countdown card</b> appears: it anchors to the first over-limit moment and computes
the remaining wait over a fixed window (default <code>service_retry_window_seconds</code> = 3660 s = 1 h + 1 min of slack),
so you do not retry by hand over and over.</p>
<p>The service resumes automatically when the countdown ends; you may also pause or cancel meanwhile.</p>

<h3>Long-task mechanics</h3>
<ul>
<li>Context is compressed once it exceeds the threshold; an overly long session hands over to a fresh conversation
(carrying a checkpoint summary).</li>
<li>On rate limiting the state becomes "waiting for service" with exponential backoff; when Copilot judges the
conversation full, the turn is resent in a new conversation.</li>
<li>After a service restart, interrupted tasks resume automatically (auto_resume).</li>
</ul>
<h3>Batch actions (fewer Copilot turns)</h3>
<p>The model may return several actions at once: <code>{"type":"actions","actions":[action1,action2,...]}</code>;
they run locally in order and each action is still recorded as its own step.</p>
<ul><li>The count is capped by <code>max_actions_per_turn</code>; the parser also enforces a hard cap
<code>MAX_BATCH_ACTIONS</code> (currently 4), and the excess is truncated and flagged <code>_batch_truncated</code>.</li>
<li>A failing action inside the batch does not affect the rest; a <i>final</i> inside the batch is deferred and the
<b>verification gate</b> decides whether the task may complete.</li></ul>

<h3>Long-term memory (across sessions)</h3>
<p>Besides <b>AGENT.md</b> (hard rules injected in full every turn) and <b>skills</b> (procedural "how to"),
the agent keeps <b>long-term memory</b>: verified facts from past tasks accumulate, and later related tasks retrieve
and inject them into the prompt (keyword + CJK bigram matching — no dependencies, no network).</p>
<ul>
<li><b>Automatic capture</b>: at task wrap-up the agent judges whether a fact is still useful beyond this task;
only then is it written. Trivial process details are not.</li>
<li><b>On request</b>: just say "remember XX in long-term memory".</li>
<li><b>Scope</b>: <code>global</code> is visible to every task; <code>project</code> only within the current workspace.</li>
<li><b>Retrieval</b>: only the few most relevant entries are injected (never everything); on conflict the current
fact wins, and stale entries can be deleted.</li>
<li><b>Panel</b>: click the <b>memory icon</b> in the top toolbar → filter by scope, keyword search, see stats
(total / pinned / <b>never hit</b> — to find entries worth cleaning), and <b>pin</b> or <b>delete</b> them one by one.</li>
<li><b>Pinned</b> entries are injected on every retrieval — reserve them for critical conventions; click again to unpin.</li>
<li><b>Switch</b>: <code>allow_memory_write</code> (default true) turns memory writes off globally.</li>
<li><b>API</b>: <code>GET /v1/agent/memory?query=&amp;scope=</code>,
<code>POST /v1/agent/memory/{id}/pin</code>, <code>DELETE /v1/agent/memory/{id}</code>;
stored in its own database <code>data/memory.db</code>, capped at 500 entries per scope with low-value entries evicted.</li>
</ul>

<h3>Self-accumulating skills</h3>
<ul>
<li>At wrap-up the agent first runs <code>skill_list</code> to look for a matching skill; if none exists and the
approach is reusable, it creates one with <code>skill_create</code> (defaults to
<code>skills/&lt;name&gt;/SKILL.md</code> in the current workspace).</li>
<li>Writing to the global <code>skills/</code> (effective for all workspaces) requires <code>allow_self_upgrade</code>.</li>
<li>Skills are <b>usable immediately</b>, no restart; a project-level skill overrides the global one of the same name.</li>
</ul>

<h3>Interface</h3>
<ul>
<li><b>◐</b> theme cycle: dark / light / Nord blue / eye-friendly beige / Dracula purple / soft pink / mint, kept across reloads.</li>
<li>The thin handle on the rail's left edge collapses/expands the sidebar; the live panel (run state / current answer /
sent content / follow-up) is tabbed — <b>⤢ Expand</b> shows the full text.</li>
<li>Each step in the timeline shows its duration; "View result" expands the structured JSON.</li>
</ul>
<h3>Service self-upgrade (off by default)</h3>
<ol>
<li>Set <code>"allow_self_upgrade": true</code> in the agent section of <code>config.json</code> and restart; the
<code>self_*</code> tools are then registered (allowlist: <code>cbp/ webui/ scripts/ docs/ tests/ skills/</code> etc.;
<code>config.json</code>, <code>data/</code> and <code>EdgeProfile/</code> are always off-limits).</li>
<li>Have a task "use the self-upgrade tools" to change code: self_read_text → self_replace_text →
<b>self_validate</b> (full syntax check) → <b>self_unittest</b> (run the service's own tests); both must pass before
the task may finish.</li>
<li>Changes take effect only after a <b>manual restart</b>; <code>restart_pending</code> in
<code>GET /v1/agent/self/status</code> tells you.</li>
<li>Rollback: <code>POST /v1/agent/self/rollback</code> (online) or <code>python scripts/rollback.py</code> (offline).</li>
</ol>
<p class="hint">A workspace may not point at the service install directory (unless self-upgrade is enabled) —
otherwise ordinary file tools could edit the service code and bypass the permission gate.</p>
<h4>How to ask it to change itself</h4>
<p>Bottom line: <b>"describe what you want" is not enough — name the file, say exactly what to change, and
give the acceptance criteria</b>. Open a fresh session (clean context) and use this template:</p>
<pre>[Self-upgrade task]
File: cbp/bridge.py (install-dir relative path, never an absolute path)
Current: &lt;the behaviour you observed, e.g. resume only re-sends the original task&gt;
Change: &lt;be specific; paste function/field names or a code snippet if you can&gt;
Scope: only the file or logic above — touch nothing else
Accept: self_validate + self_unittest both pass
Deliver: which lines changed, verification result, restart needed, and the rollback command</pre>
<p>Short one-liners work just as well:</p>
<ul>
<li>"Change the default sidebar width in <code>webui/style.css</code> from 318px to 300px, then run
self_validate and self_unittest"</li>
<li>"Look at <code>/v1/shutdown</code> in <code>cbp/api.py</code> and raise the graceful-shutdown timeout
from 8s to 15s; change nothing else"</li>
</ul>
<p>First confirm the switch really took effect: ask "which tools do you have that start with self_?" —
you should get 7: <code>self_read_text</code>, <code>self_write_text_b64</code>,
<code>self_append_text_b64</code>, <code>self_replace_text</code>, <code>self_validate</code>,
<code>self_unittest</code>, <code>self_rollback</code>. With the flag off there are zero.</p>
<h4>Hard limits it must respect</h4>
<ul>
<li><b>Allowlisted paths only</b>: <code>cbp/</code> <code>webui/</code> <code>scripts/</code>
<code>docs/</code> <code>tests/</code> <code>skills/</code> plus <code>app.py</code>
<code>start.py</code> <code>install.py</code> <code>requirements.txt</code>
<code>AGENT.md</code> <code>README.md</code> <code>config.example.json</code>.</li>
<li><b>Never writable</b>: <code>config.json</code>, <code>data/</code>, <code>EdgeProfile/</code>,
<code>for debug/</code>, <code>.git/</code>, <code>__pycache__/</code>. Config changes are <b>yours to
make</b> — its writes are rejected.</li>
<li><b>Verification gate</b>: <code>self_validate</code> + <code>self_unittest</code> must both pass;
otherwise it may not claim completion (the system blocks the final answer).</li>
<li><b>Automatic snapshots</b>: every write/replace stores the previous version under
<code>data/snapshots/_self_&lt;sid&gt;/</code>, so a whole task can be rolled back.</li>
</ul>
<h4>If it goes wrong (three levels, nearest first)</h4>
<ol>
<li>UI: "More ▾" → <b>Roll back self-upgrade…</b></li>
<li>API: <code>POST /v1/agent/self/rollback?last=true</code></li>
<li><b>When the service will not start</b> (the only option left):
<code>python scripts/rollback.py --scope self --last --yes</code></li>
</ol>
<p class="hint">Option 3 is the lifeline: if a change makes the service crash on startup, neither the web UI nor the
API is reachable — only the local command can undo it.</p>
<h4>Five traps</h4>
<ul>
<li><b>One thing at a time</b>: rollback granularity is a whole task, so several unrelated edits can only be
reverted together.</li>
<li><b>Do not trust "verification passed"</b>: <code>self_validate</code> is syntax only and
<code>self_unittest</code> only runs <code>tests/</code>; neither proves correct behaviour. The right flow is:
it edits → <b>you review <code>git diff</code></b> → restart → actually click through it.</li>
<li><b>Prompt and tool-description edits are high risk</b>: <code>_system_prompt()</code> and tool
descriptions change agent behaviour directly — always run one real task after such a change.</li>
<li><b>"No effect" usually means "not restarted"</b>: the file on disk changed, the process still runs the old
code. Confirm the restart before asking for another attempt.</li>
<li><b>Write large files in chunks</b>: <code>self_write_text_b64</code> is ≤3000 chars per chunk; continue
with <code>self_append_text_b64</code>. Validating a half-written file always fails — that is not a defect.</li>
</ul>
<p class="hint">Risk note: self-upgrade lets the agent rewrite its own code; combined with
<code>allow_shell</code> / <code>allow_python</code> / <code>allow_delete</code> that is effectively full control
of the machine. Prefer turning it on only while you watch, turning it off afterwards, and reviewing
<code>git diff</code> yourself before committing.</p>

<h3>FAQ</h3>
<ul>
<li><b>Brief stop on the sign-in page after a restart</b>: enterprise SSO bounces through the company ADFS automatically,
taking about 40 s; usually no manual sign-in is needed. If it stops on the account picker, click your account once.</li>
<li><b>"Input box / model button not found"</b>: the login expired — run <code>python scripts/login.py</code> to sign in
again (or sign in directly in windowed mode).</li>
<li><b>The task shows "waiting for service"</b>: Copilot is rate limiting; wait for it to recover. If it stays
unavailable the task pauses safely — click <b>Resume</b> afterwards.</li>
</ul>`;
/* ── 手册增强：自动目录 + 低频章节折叠（运行时处理，不改模板）── */
const MANUAL_COLLAPSE_ZH=['服务自升级','常见问题'];
const MANUAL_COLLAPSE_EN=['Service self-upgrade','FAQ'];
const MANUAL_COLLAPSE=()=>(currentLang()==='en'?MANUAL_COLLAPSE_EN:MANUAL_COLLAPSE_ZH);
function enhanceManual(){
 const body=document.getElementById('manualBody');
 if(!body)return;
 body.querySelectorAll('.mtoc').forEach(x=>x.remove());
 const hs=[...body.querySelectorAll('h3')];
 hs.forEach((h,i)=>h.id='mh'+i);
 let toc=document.createElement('nav');
 toc.className='mtoc';
 toc.innerHTML='<b>'+esc(tr('目录'))+'</b>'+hs.map((h,i)=>`<a href="#mh${i}">${esc(h.textContent)}</a>`).join('');
 body.insertBefore(toc, body.firstChild);
 toc.querySelectorAll('a').forEach(a=>a.onclick=e=>{
  e.preventDefault();
  const t=document.getElementById(a.getAttribute('href').slice(1));
  if(t)t.scrollIntoView({behavior:'smooth',block:'start'});});
 hs.forEach(h=>{
  if(!MANUAL_COLLAPSE().some(k=>h.textContent.includes(k)))return;
  const d=document.createElement('details');
  d.id=h.id;
  const sum=document.createElement('summary');sum.textContent=h.textContent;
  h.parentNode.insertBefore(d,h);
  d.appendChild(sum);
  let n=h.nextSibling;const move=[];
  while(n&&!(n.nodeType===1&&n.tagName==='H3')){const nx=n.nextSibling;move.push(n);n=nx}
  move.forEach(x=>d.appendChild(x));
  h.remove();
 });
}

function openManual(){const body=document.getElementById('manualBody');
 body.innerHTML=(currentLang()==='en')?MANUAL_HTML_EN:MANUAL_HTML;enhanceManual();
 // 手册是动态注入的：注入后再走一遍翻译。章节标题有英文条目，正文按取舍保持中文
 // （长文档整篇翻译成本高且易失真，该取舍写在 README 的 i18n 说明里）。
 try{if(typeof applyI18n==='function')applyI18n(body)}catch(e){}
 openMask('manualMask')}
function closeManual(){closeMask('manualMask')}
function openTask(){
 try{loadMcpPrompts()}catch(e){}
 // 侧栏收起时编辑器区域虽然还在，但"从左侧选会话"的提示会误导：先展开侧栏
 try{if(document.documentElement.dataset.rail==='closed')toggleRail()}catch(e){}
 // 新会话视图（DSH 式）：空态就是编辑器，没有再套一层弹窗
 document.getElementById('emptyState').style.display='flex';
 document.getElementById('sessionView').style.display='none';
 current=null; try{drawSessions()}catch(e){}
 const t=document.getElementById('task'); if(t){t.value='';t.focus()}
}

document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeDlg();closeLive();gmClose(null);closeManual();closePlugins();closeWs();closePanels();try{closeSettings()}catch(_){}}});

/* 把独立的「步骤时间线」卡片并入实时面板标签页（像 DSH 那样一个面板看全部）。
   直接搬 DOM 节点：筛选下拉、导出按钮、每步的展开/收起全部照旧，不重写渲染逻辑。 */
function mountTimelineIntoTab(){
 const steps=document.getElementById('steps');
 const pane=document.getElementById('timeline');
 if(!steps||!pane||pane.dataset.mounted)return;
 const sec=steps.closest('section.card');
 if(sec){
  const head=sec.querySelector('.chead');
  if(head)pane.appendChild(head);
 }
 pane.appendChild(steps);
 if(sec)sec.remove();
 pane.dataset.mounted='1';
}

/* ── 实时面板选项卡 + 放大查看 ── */
/* ── 底部输入框的显隐统一入口 ─────────────────────────────────────────
   两个来源会打架：空状态(#emptyState)自带一个"发送消息/Start"输入框，而
   composer 是常驻在 .main 底部的；同时可见就会出现**两个输入框**（用户截图：
   672×950 时上下各一个，非常怪）。
   这里统一判断：只有"已选中会话"才显示。
   （**两个页签都显示**：原来切到"步骤时间线"就把它藏了，结果两个页签的卡片底边
   一个在 806、一个拉到 897，高矮不一致很丑 —— 用户要求两页高度协调。） */
function syncComposer(){
 const comp=document.querySelector('.main > .composer');
 if(!comp)return;
 const empty=document.getElementById('emptyState');
 const emptyOn=!!empty && getComputedStyle(empty).display!=='none';
 const show=!emptyOn;
 comp.style.display=show?'':'none';
 // 输入框藏起来时（空状态）别在底部继续留它的空位
 const main=comp.closest('.main');
 if(main) main.classList.toggle('no-composer',!show);}
try{ // 空状态的显隐有 6 处调用点，用观察器兜住，避免漏掉某一条路径
 const _empty=document.getElementById('emptyState');
 if(_empty && typeof MutationObserver==='function')
  new MutationObserver(()=>{try{syncComposer()}catch(e){}})
   .observe(_empty,{attributes:true,attributeFilter:['style']});
}catch(e){}

const LIVE_NAMES={chat:'实时流',timeline:'步骤时间线'};
const LIVE_TAB={chat:'tabChat',timeline:'tabTimeline'};
let liveTabName='chat';
let lastAns='';
let lastAnsHtml='';
function liveTab(n){
 if(!LIVE_NAMES[n])n='chat';   // 旧标签名（activity/answer/prompt）已合并进去，静默切到实时流
 liveTabName=n;
 for(const k in LIVE_NAMES){
  const pane=document.getElementById(k);if(pane)pane.style.display=k===n?'':'none';
  const btn=document.getElementById(LIVE_TAB[k]);if(btn)btn.className='tab'+(k===n?' on':'')}
 // 输入框的显隐统一走 syncComposer（空状态/页签两种条件）
 try{syncComposer()}catch(e){}
 // 切到时间线页签：把之前为了省时间没建的时间线补上（只建这一次）
 if(n==='timeline'){try{flushStepsIfPending()}catch(e){}}
 if(n==='chat'){try{scrollChatToEnd()}catch(e){}}}  // className 覆盖即清除新内容圆点
function expandLive(){
 document.getElementById('liveTitle').textContent=LIVE_NAMES[liveTabName];
 // 对话页只复制聊天记录，避免把输入框（含重复 ID）一并搬进弹窗
 document.getElementById('liveBody').innerHTML=liveTabName==='chat'
  ?document.getElementById('chatlog').innerHTML
  :document.getElementById(liveTabName).innerHTML;
 openMask('liveMask')}
function closeLive(){closeMask('liveMask')}
document.getElementById('liveCopy').onclick=()=>{try{navigator.clipboard.writeText(document.getElementById('liveBody').textContent||'')}catch(e){}};
/* 查看该会话的初始提示词（首回合发给模型的完整内容）。
   未落库的旧会话由后端按当前工作区近似重建，这里明确标注。 */
async function showPromptView(){
 if(!current){toast(tr('请先选择 Session'),'err');return}
 try{
  let d=await api('/v1/agent/sessions/'+encodeURIComponent(current)+'/prompt');
  let t=d.initial_prompt||'(空：该会话还没有发出过回合)';
  document.getElementById('liveTitle').textContent=(d.rebuilt
    ?'初始提示词（本会话未保存，以下为按当前工作区近似重建）'
    :'初始提示词（首回合实际发送内容）')+` · ${t.length} 字符`;
  let body=document.getElementById('liveBody');
  body.textContent=t;
  if(d.rebuild_error)body.textContent=t+'\n\n(重建失败: '+d.rebuild_error+')';
  document.getElementById('liveMask').classList.add('open')
 }catch(e){toast(tr('读取初始提示词失败: ')+(e.text||e),'err')}}
async function continueAgent(){
 if(!current){toast(tr('请先选择 Session'),'err');return}
 let instruction=await dlgPrompt(tr('请输入要继续执行的任务'),'继续执行当前任务，从未完成事项开始','继续执行','继续执行');
 if(!instruction)return;
 document.getElementById('followup').value=instruction;
 setFollowupMode('agent');
 await sendFollowup()}
/* ── 输入框随内容自动长高（照 DSH 的 composer）─────────────────────────
   原来 #followup 固定 2 行：内容一多就只在框里滚，框本身不动。
   现在先 `height:auto` 再按 `scrollHeight` 设高（封顶 CSS 的 max-height），
   到顶之后才内部滚动；清空后自动缩回一行。 */
function autoGrowFollowup(){
 const el=document.getElementById('followup');
 if(!el||!el.offsetParent) return;   // 面板隐藏时 scrollHeight 为 0，别把高度写死
 const cs=getComputedStyle(el);
 const min=parseFloat(cs.minHeight)||50, max=parseFloat(cs.maxHeight)||220;
 el.style.height='auto';
 const need=el.scrollHeight;
 el.style.height=Math.max(min,Math.min(need,max))+'px';
 el.style.overflowY=need>max?'auto':'hidden';
 // 常态高度写进 --fu-rest：布局只按"常态"给输入条留位置，长高出来的部分盖住上面的内容。
 // 只在"已经是常态高度"时校准一次，避免输入过程中反复改布局。
 const box=el.closest('.composer');
 if(box){
  // 空输入 = 常态：拿这一刻的真实高度校准，换主题/字体后底部留白照样准
  if(!el.value.trim()) document.documentElement.style.setProperty('--fu-rest',box.offsetHeight+'px');
  document.documentElement.style.setProperty('--fu-h',box.offsetHeight+'px');
 }
}

async function sendFollowup(){
 if(!current){toast(tr('请先选择 Session'),'err');return}
 let box=document.getElementById('followup'),content=box.value.trim();
 if(!content&&!IMG_STORES.follow.length){toast(tr('消息不能为空'),'err');return}
 let atts=IMG_STORES.follow.map(a=>a.dataUrl);
 // 发送按钮是圆形图标按钮：不能再用 textContent 换文案（会把图标冲掉），
 // 用 disabled + aria-busy 表示发送中（图标/标题保持不变，屏幕阅读器也读得到）
 let btn=document.getElementById('sendFollowup');btn.disabled=true;btn.setAttribute('aria-busy','true');
 liveTab('chat');  // 发送后确保停留在对话页看回复
 let log=document.getElementById('chatlog');
 let label=content+(atts.length?`\n（附 ${atts.length} 张图片）`:'');
 log.appendChild(msgEl('user',label));log.scrollTop=log.scrollHeight;box.value='';autoGrowFollowup();
 IMG_STORES.follow=[];renderThumbs();
 let baseAssistants=0;
 try{let dd=await api('/v1/agent/sessions/'+encodeURIComponent(current));baseAssistants=dd.messages.filter(m=>m.role==='assistant').length}catch(e){}
 try{let d=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/messages`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content,attachments:atts,model:currentModel||document.getElementById('model').value,agent_mode:followupMode==='agent'})});
  if(d&&d.queued){
   let note=msgEl('assistant',
     d.position>1?`已排队（前面还有 ${d.position-1} 条）`:'已排队，等待处理…',
     null,{plain:true,who:tr('队列')});
   log.appendChild(note);log.scrollTop=log.scrollHeight;
   refreshQueue();
   awaitFollowupReply(current,baseAssistants);
   }else{
    // 后端对 /messages 只返回排队/接收元信息（session_id/queued/id/position/mode），
    // 从不返回 content/url —— 原实现在这里读不存在的字段，会渲染一个空气泡。
    // 正确做法：重载会话，把刚落库的新消息渲染出来。
    await load(current)}
 }catch(e){log.appendChild(msgEl('assistant',tr('发送失败: ')+e,null,{plain:true,who:tr('错误')}))}
 finally{btn.disabled=false;btn.removeAttribute('aria-busy')}}

/* 队列模式：跟进消息入队后轮询会话消息，助理回复一出现就刷新聊天区 */
async function awaitFollowupReply(sid,baseAssistants){
 for(let i=0;i<300;i++){
  await new Promise(r=>setTimeout(r,1000));
  if(current!==sid)return;                     // 用户切走了就停
  try{
   let d=await api('/v1/agent/sessions/'+encodeURIComponent(sid));
   let as=d.messages.filter(m=>m.role==='assistant');
   if(as.length>baseAssistants){renderMessages(d.messages,{animateLast:true});return}
  }catch(e){}
 }}

/* 按当前会话通道调整界面：API 提供商会话没有"自动化 Edge 页面/网页对话"，
   隐藏"切换到页面""打开会话",避免点了必然失败。 */
function updateChannelUI(){
 let prov=isProviderModel(currentModel);
 ['btnActivate','btnOpenChat'].forEach(id=>{
  let el=document.getElementById(id);if(el)el.style.display=prov?'none':''});
 // 分组框也要跟着隐藏：否则提供商会话下留一个空 div 和一条多余的分隔竖线
 let _wg=document.getElementById('webGrp');if(_wg)_wg.style.display=prov?'none':'';
 let meta=document.getElementById('chatBarMeta');
 // 文案统一由 refreshChatBarMeta 维护（含 tooltip）
 try{refreshChatBarMeta()}catch(e){}}

/* ── 排队消息：查看 / 编辑 / 删除 ── */
async function refreshQueue(){
 if(serviceStopped)return;
 let box=document.getElementById('queueBox');if(!box)return;
 if(!current){box.innerHTML='';return}
 try{
  let d=await api('/v1/agent/sessions/'+encodeURIComponent(current)+'/queue');
  // 正在处理的那条不算"排队"：它已经在跑，进度由运行状态面板显示，而且
  // 后端只允许改删"等待中"的条目（处理中的会 404）。这里只列等待项。
  let items=(d.pending||[]).filter(it=>!it.processing);
  if(!items.length){box.innerHTML='';return}
  box.innerHTML=items.map(it=>`<div class="qitem"><span class="qpos">#${it.position}</span><span class="qtext" title="${esc(it.content)}">${esc(it.content)}</span><span class="qmode">${it.mode==='agent'?'Agent':'对话'}</span><button title="编辑" data-edit="${esc(it.id)}">✎</button><button class="qdel" title="删除" data-del="${esc(it.id)}">×</button></div>`).join('');
   // 不再把 id 拼进内联 onclick（id 含引号即可突破 JS 字符串 → XSS）；与 loadMemory 同一范式
   box.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editQueueItem(b.dataset.edit));
   box.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>removeQueueItem(b.dataset.del));
   try{translateAttrs(box)}catch(e){}     // 队列项的 title 也走 DOM 层翻译
 }catch(e){box.innerHTML=''}}

async function editQueueItem(itemId){
 if(!current)return;
 let d=await api('/v1/agent/sessions/'+encodeURIComponent(current)+'/queue');
 let it=(d.pending||[]).find(x=>x.id===itemId);if(!it)return;
 let content=await dlgPrompt(tr('修改排队消息'),it.content,'保存');
 if(content===null)return;
 try{
  await api(`/v1/agent/sessions/${encodeURIComponent(current)}/queue/${itemId}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content})});
  toast(tr('已更新排队消息'),'ok');refreshQueue()}catch(e){toast(tr('更新失败: ')+(e.text||e),'err')}}

async function removeQueueItem(itemId){
 if(!current)return;
 if(!await dlgConfirm(tr('删除这条排队中的消息？此操作不可撤销。'),'删除排队消息',true))return;
 try{
  await api(`/v1/agent/sessions/${encodeURIComponent(current)}/queue/${itemId}`,{method:'DELETE'});
  toast(tr('已删除排队消息'),'ok');refreshQueue();load(current)}catch(e){toast(tr('删除失败: ')+e,'err')}}
/* ── 粘贴图片/截图附件 ── */
const IMG_STORES={task:[],follow:[]};

function thumbsHtml(arr,store){
 return arr.map((a,i)=>`<span class="thumb"><img src="${a.dataUrl}" alt=""><button class="rm" title="移除" data-act="removeAttachment" data-store="${store}" data-arg="${i}">×</button></span>`).join('')}
function renderThumbs(){
 let tb=document.getElementById('taskThumbs'),fb=document.getElementById('followThumbs');
 if(tb)tb.innerHTML=thumbsHtml(IMG_STORES.task,'task');
 if(fb)fb.innerHTML=thumbsHtml(IMG_STORES.follow,'follow')}
function removeAttachment(store,i){IMG_STORES[store].splice(i,1);renderThumbs()}

function attachPasteHandler(taId,store){
 let ta=document.getElementById(taId);if(!ta)return;
 ta.addEventListener('paste',e=>{
  let items=(e.clipboardData&&e.clipboardData.items)||[];
  for(let i=0;i<items.length;i++){
   let it=items[i];
   if(it.kind==='file'&&it.type&&it.type.startsWith('image/')){
    e.preventDefault();
    let f=it.getAsFile();if(!f)continue;
    if(f.size>8*1024*1024){toast(tr('图片超过 8MB，已忽略'),'err');continue}
    if(IMG_STORES[store].length>=4){toast(tr('最多附加 4 张图片'),'err');continue}
    let r=new FileReader();
    r.onload=()=>{IMG_STORES[store].push({dataUrl:r.result});renderThumbs()};
    r.readAsDataURL(f);
   }
  }
 })}

/* ── 对话区的"正在生成"气泡（流式逐字）──────────────────────────────
   来源就是 runtime.answer_preview（SSE 推送/轮询都会更新）。它是**临时**气泡：
   正式的助手消息落库后会经 SSE messages 事件触发重渲染，把它冲掉。
   样式刻意与正式消息区分（虚线边框 + "临时"标签），避免把流到一半的
   JSON 动作误当成最终答复。 */
let liveStreamText='';
let liveStreamAt=0;          // 文本最后一次变化的时刻（用于判"还在生成"）
let liveStreamVisible=false; // 由 updateLiveBubble 决定；renderLiveBubble 只负责画
/** 已入库的最后一条助手消息内容（用于去重） */
function lastSavedAssistant(){
 const as=(lastMessages||[]).filter(m=>m.role==='assistant');
 return as.length?String(as[as.length-1].content||'').trim():''}
function renderLiveBubble(){
 const box=document.getElementById('chatlog');if(!box)return;
 let el=document.getElementById('liveBubble');
 if(!liveStreamVisible||!liveStreamText){
  if(el)el.remove();
  return}
 if(!el){
  el=document.createElement('div');el.id='liveBubble';el.className='msg assistant live';
  el.innerHTML='<div class="msg-av">…</div><div class="msg-main">'+
   '<div class="msg-head"><span class="msg-role">'+esc(tr('正在生成'))+'</span>'+
   '<span class="msg-time">'+esc(tr('临时'))+'</span></div><div class="md-body" id="liveBubbleBody"></div></div>';
  box.appendChild(el)}
 const body=document.getElementById('liveBubbleBody');
 if(body)body.innerHTML=mdToHtml(liveStreamText);
 box.appendChild(el);          // 保持在末尾
 box.scrollTop=box.scrollHeight}
/* 更新临时气泡。三种情况必须**不显示**，否则会出现"两条一样的信息"：
   1) 内容已作为消息入库——Copilot 网页通道的 answer_preview 会长期保留同一段
      文字，而它同时又是已保存的助手消息，于是气泡与真实消息重复（实测就是这个）；
   2) 内容已停止增长（超过阈值没变 = 不是在生成，而是残留的 runtime 快照）；
   3) 会话已不在运行/等待中（由调用方传入 inFlight=false）。 */
function updateLiveBubble(text,inFlight){
 const raw=inFlight?String(text==null?'':text).trim():'';
 const now=Date.now();
 if(raw!==liveStreamText){liveStreamText=raw;liveStreamAt=raw?now:0}
 if(!liveStreamText)liveStreamAt=0;
 const saved=lastSavedAssistant();
 const alreadySaved=!!liveStreamText&&!!saved&&(saved===liveStreamText||saved.startsWith(liveStreamText));
 const stale=!!liveStreamText&&(now-liveStreamAt>3500);
 liveStreamVisible=!!liveStreamText&&!alreadySaved&&!stale;
 renderLiveBubble()}

/* ── 导出对话为 Markdown ──────────────────────────────────────────── */
function exportChatMarkdown(){
 if(!current){toast(tr('请先选择 Session'),'err');return}
 const msgs=lastMessages||[];
 if(!msgs.length){toast(tr('当前没有对话记录'),'err');return}
 const title=(document.getElementById('title').textContent||'').trim()||current;
 let out=`# ${title}\n\n`;
 out+=`- Session: \`${current}\`\n`;
 out+=`- 模型: \`${currentModel||'-'}\`\n`;
 out+=`- 导出时间: ${new Date().toLocaleString('zh-CN')}\n`;
 out+=`- 消息数: ${msgs.length}\n\n---\n\n`;
 msgs.forEach(m=>{
  const who=m.role==='user'?tr('我'):assistantLabel();
  const ts=m.created?new Date(m.created*1000).toLocaleString('zh-CN'):'';
  out+=`## ${who}${ts?` · ${ts}`:''}\n\n`;
  out+=String(m.content||'')+'\n';
  const atts=m.attachments||[];
  if(atts.length)out+='\n'+atts.map(n=>`![附件 ${n}](data/uploads/${n})`).join('\n')+'\n';
  out+='\n'});
 try{
  const blob=new Blob([out],{type:'text/markdown;charset=utf-8'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='cbp-'+current+'-chat-'+new Date().toISOString().slice(0,19).replace(/[:T]/g,'-')+'.md';
  document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),2000);
  toast(tr('已导出 Markdown（')+msgs.length+' 条消息）','ok');
 }catch(e){toast(tr('导出失败: ')+e,'err')}}

/* ── 消息编辑 / 删除（只改本地历史）────────────────────────────────
   诚实语义：已发给网页 Copilot 的内容无法撤回；提供商会话会同步内存上下文
   （后端 sync_provider_history_message），否则模型下一回合仍看到旧文本。 */
async function editMsg(btn){
 const wrap=btn.closest('.msg');const id=wrap&&wrap.dataset.mid;
 if(!id){toast(tr('这条消息还没有落库，无法编辑'),'err');return}
 const old=wrap.dataset.raw||'';
 const text=await dlgPrompt(
  tr('编辑这条消息的内容（只改本地历史；已发给网页 Copilot 的内容无法撤回）'),
  old,tr('保存'),tr('编辑消息'));
 if(text===null)return;
 const v=String(text).trim();
 if(!v||v===old)return;
 try{
  const d=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/messages/${id}`,
   {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:v})});
  toast(noteText(d,tr('已更新')),'ok');
  await refreshChat();
 }catch(e){toast(tr('编辑失败: ')+(e.text||e),'err')}}
async function delMsg(btn){
 const wrap=btn.closest('.msg');const id=wrap&&wrap.dataset.mid;
 if(!id)return;
 if(!await dlgConfirm('删除这条消息？\n\n只影响本地历史，不会撤回已发给网页 Copilot 的内容。',
                      tr('删除消息'),true))return;
 try{
  await api(`/v1/agent/sessions/${encodeURIComponent(current)}/messages/${id}`,{method:'DELETE'});
  toast(tr('已删除'),'ok');
  await refreshChat();
 }catch(e){toast(tr('删除失败: ')+(e.text||e),'err')}}

/* ── 跨会话搜索 ─────────────────────────────────────────────────────
   后端 /v1/search 搜 messages 正文；点结果即跳转到该会话，并把那条消息
   滚到视野中间高亮一下。 */
let gsTimer=null;
function toggleGlobalSearch(){
 const box=document.getElementById('gSearch');
 if(!box)return;
 // 打开时要写 'flex'（不能写空串：空串会被下一次调用当成"未初始化"而永远关不上）
 const show=(box.style.display==='none'||box.style.display==='');
 box.style.display=show?'flex':'none';
 if(show){const i=document.getElementById('gsInput');i.focus();i.select()}
 else{const r=document.getElementById('gsResults');if(r)r.innerHTML=''}}
function gsInput(){
 clearTimeout(gsTimer);
 gsTimer=setTimeout(runGlobalSearch,320)}
async function runGlobalSearch(){
 const box=document.getElementById('gsResults');if(!box)return;
 const q=(document.getElementById('gsInput').value||'').trim();
 if(!q){box.innerHTML='';return}
 box.innerHTML='<div class="meta">'+esc(tr('搜索中…'))+'</div>';
 try{
  const d=await api('/v1/search?q='+encodeURIComponent(q));
  const list=d.results||[];
  if(!list.length){box.innerHTML='<div class="meta">'+esc(tr('没有匹配的对话内容'))+'</div>';return}
  box.innerHTML='';
  list.forEach(r=>{
   const e=document.createElement('div');e.className='gsrow';
   const when=fmtStamp(r.created);
   e.innerHTML=`<div class="gstop"><b>${esc(r.title||r.session_id)}</b>`+
    `<span class="gsmeta">${r.role==='user'?'我':'模型'} · ${when}</span></div>`+
    `<div class="gssnip">${esc(r.snippet||'')}</div>`;
   e.onclick=()=>jumpToMessage(r.session_id,r.message_id);
   box.appendChild(e)});
 }catch(e){box.innerHTML='<div class="meta">'+esc(tr('搜索失败：'))+esc(e.text||e)+'</div>'}}
async function jumpToMessage(sid,mid){
 try{
  await load(sid);
  liveTab('chat');
  const el=document.querySelector(`#chatlog .msg[data-mid="${mid}"]`);
  if(el){
   el.scrollIntoView({block:'center',behavior:'smooth'});
   el.classList.add('flash');
   setTimeout(()=>el.classList.remove('flash'),2200);
   toggleGlobalSearch();   // 跳过去后收起搜索面板
  }else toast(tr('这条消息不在最近加载的记录里（可能超出 500 条上限）'),'err');
 }catch(e){toast(tr('跳转失败: ')+(e.text||e),'err')}}

document.addEventListener('keydown',e=>{if(e.target&&e.target.id==='followup'&&e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();sendFollowup()}});
/* 强行插入指令（像 DSH 的 Ctrl+J）：运行中立即生效，Agent 下一回合优先执行；
   与"排队跟进"不同——后者要等当前这轮跑完才起新一轮。 */
async function injectMessage(){
 if(!current){toast(tr('请先选择 Session'),'err');return}
 let pick=await dlgInjectPrompt(
  tr('插入指令（运行中即时生效，会立即中断当前回合后按新指令继续）。插入方式决定语义：'),
  '', tr('强行插入'),'step');
 if(pick===null)return;
 let content=String(pick.text||'').trim();
 if(!content)return;
 let queue=(pick.mode==='turn')?'turn':'step';
 try{localStorage.setItem('cbp-inject-mode',queue)}catch(e){}
 try{
  let d=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/inject`,
                  {method:'POST',headers:{'Content-Type':'application/json'},
                   body:JSON.stringify({content,queue})});
  toast(noteText(d,tr('已插入')), d.running?'ok':'');
  liveTab('chat');
  // 让聊天区尽快看到这条指令（标"插入",与普通发送区分）
  let log=document.getElementById('chatlog');
  log.appendChild(msgEl('user',content,null,
    {who:tr('你')+'（'+tr(queue==='step'?'立即遵守':'整轮重排')+'）'}));
  log.scrollTop=log.scrollHeight;
  refreshSessions();
 }catch(e){toast(tr('插入失败: ')+(e.text||e),'err')}}
document.addEventListener('keydown',e=>{
 if((e.ctrlKey||e.metaKey)&&!e.shiftKey&&(e.key==='j'||e.key==='J')){e.preventDefault();injectMessage()}});
/* ── 新任务模式：Agent 执行 / 普通对话 ── */
let taskMode='agent';
function setTaskMode(m){taskMode=m;
 document.getElementById('ntAgent').className='btn seg'+(m==='agent'?' on':'');
 document.getElementById('ntChat').className='btn seg'+(m==='chat'?' on':'')}

async function runTask(){
 let task=document.getElementById('task').value.trim();if(!task)return;
 let atts=IMG_STORES.task.map(a=>a.dataUrl);
 let _plan=planModeOnce; planModeOnce=false;
 let b={model:document.getElementById('model').value,plan_mode:_plan,messages:[{role:'user',content:task}],stream:false,session_id:((document.getElementById('sid')||{}).value||'').trim()||('task-'+Date.now().toString(36)),workspace:document.getElementById('workspace').value.trim()||null,reuse_session:true,agent_mode:taskMode==='agent',attachments:atts};
 current=b.session_id;
 currentModel=b.model;      // 会话页模型选择器随即显示本次任务用的模型
 IMG_STORES.task=[];renderThumbs();
 document.getElementById('emptyState').style.display='none';
 document.getElementById('sessionView').style.display='';
 resetView();
 // 先用本地已知信息填标题/状态：/v1/agent/completions 会一直挂到整轮任务
 // 结束才返回，等它的回调会让整个任务期间标题都显示"—"
 let title=(task.split('\n')[0]||'').trim().slice(0,120)||current;
 document.getElementById('title').textContent=title;
 let _sc=document.getElementById('sidChip');
 if(_sc){_sc.textContent=current;_sc.onclick=()=>{try{navigator.clipboard.writeText(current);toast(tr('已复制会话 ID: ')+current,'ok')}catch(e){toast(current)}}}
 fillModelSelect(document.getElementById('sessModel'), modelCache.map(id=>({id})), currentModel);
 refreshSessModelOptions();
 document.getElementById('status').textContent=(taskMode==='agent'?'Agent 执行':'普通对话')
  +' · '+(b.workspace||'默认工作区');
 setPill('running');
 document.getElementById('activity').textContent=(taskMode==='agent'?'正在建立 Agent 任务 ':'正在发送普通对话 ')+current+'...';
 if(DEMO){await refreshSessions();return}
  // 走 api()：裸 fetch 不带 X-CBP-Token，配置 api_token 时「启动任务」必然 401
  api('/v1/agent/completions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})
   .then(async d=>{
    if(!d)throw Error('HTTP 响应为空');
    current=d.session_id;await load(current)})
   .catch(e=>{current=null;document.getElementById('sessionView').style.display='none';
    document.getElementById('emptyState').style.display='flex';toast(tr('任务启动失败: ')+(e.text||e),'err')});
 waitSessionReady(current);
 setTimeout(refreshSessions,700)}

/* 新任务启动后：轮询会话是否已在后端建立，一旦出现就用真实数据填充
   标题/工作区/步骤并进入每秒运行状态刷新（排队等锁时可能几十秒才有会话）。 */
async function waitSessionReady(sid){
 for(let i=0;i<200;i++){
  if(current!==sid)return;                  // 用户已切换到别的会话
  try{let d=await api('/v1/agent/sessions/'+encodeURIComponent(sid));
   if(d&&d.session){await load(sid);return}}catch(e){}
  await new Promise(r=>setTimeout(r,1000));
 }}

function toggleMore(el, e){
 // 事件委托下 e.currentTarget 是 document，`closest('.menu')` 永远为 null——
 // 之前菜单弹不出来就是这个原因（顶栏「更多」没给 data-arg）。这里以**按钮元素**为准。
 if(e&&e.stopPropagation)e.stopPropagation();
 let menu=null;
 if(el&&el.dataset&&el.dataset.arg)menu=document.getElementById(el.dataset.arg);
 if(!menu&&el&&el.closest){const host=el.closest('.menu');menu=host?host.querySelector('.menuList'):null}
 if(!menu)return;
 // 同一时间只开一个：否则顶栏「更多」和面板「⋯」会同时展开、互相遮挡
 document.querySelectorAll('.menuList.open').forEach(m=>{if(m!==menu)m.classList.remove('open')});
 menu.classList.toggle('open')}
document.addEventListener('click',e=>{let m=document.getElementById('moreMenu');if(m&&m.classList.contains('open')&&!e.target.closest('#moreBtn'))m.classList.remove('open')});

/* 主题：选择（choice）与解析（resolved）分开——
   choice='auto' 表示**跟随系统**（默认），resolved 才落到具体主题 id。
   之前只有 toggleTheme 写 localStorage，加载靠 boot.js；现在两边同一套语义。 */
const THEMES=[["auto","跟随系统"],["","深色"],["amber","琥珀"],["jade","墨玉"],["light","浅色"],["solarized","护眼米"],["mint","薄荷"]];
const THEME_IDS=THEMES.map(x=>x[0]);
function themeChoice(){
 try{
  const t=localStorage.getItem('cbp-theme');
  if(t===null||t===''||t==='auto')return 'auto';
  if(t==='dark')return '';
  return THEME_IDS.indexOf(t)>=0?t:'auto';
 }catch(e){return 'auto'}
}
function resolveTheme(c){
 if(c!=='auto')return c;
 try{return (window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches)?'light':''}catch(e){return ''}
}
function applyTheme(c,persist){
 const r=resolveTheme(c);
 if(r){document.documentElement.dataset.theme=r}else{document.documentElement.removeAttribute('data-theme')}
 document.documentElement.dataset.themeChoice=c;
 if(persist){try{localStorage.setItem('cbp-theme',c===''?'dark':c)}catch(e){}}
 const btn=document.getElementById('themeBtn');
 if(btn){
  // 名称与括号都必须过 tr()：标题是**拼出来的**，i18n 的 DOM 直译只会按整串精确匹配，
  // 拼串里夹着中文就会在英文界面漏出来（实测：Theme: 跟随系统（current 浅色…））
  const nm=tr((THEMES.find(x=>x[0]===c)||THEMES[0])[1]);
  const cur=tr((THEMES.find(x=>x[0]===r)||{1:'深色'})[1]);
  btn.title=c==='auto'
   ?tr('主题：{n}（当前 {c}，点击切换）',{n:nm,c:cur})
   :tr('主题：{n}（点击切换）',{n:nm});
 }
}
function toggleTheme(){
 const cur=themeChoice();
 const i=Math.max(0,THEME_IDS.indexOf(cur));
 applyTheme(THEME_IDS[(i+1)%THEME_IDS.length],true);
}
/* 系统主题变化时实时跟随（仅当 choice==='auto'） */
function watchSystemTheme(){
 try{
  const mq=window.matchMedia('(prefers-color-scheme: light)');
  const on=()=>{if(themeChoice()==='auto')applyTheme('auto',false)};
  if(mq.addEventListener)mq.addEventListener('change',on);
  else if(mq.addListener)mq.addListener(on);
 }catch(e){}
}
/* ── 左侧栏折叠（归零只剩边缘拉手） ── */
function toggleRail(){let r=document.documentElement;
 // 窄屏(≤860)把侧栏做成**抽屉浮层**（DSH 式）：默认收起（无属性=收起），
 // 打开写 'open'；宽屏沿用原语义（无属性=展开、'closed'=收起成拉手）。
 const narrow=matchMedia('(max-width:860px)').matches;
 const open=narrow ? (r.dataset.rail==='open') : (r.dataset.rail!=='closed');
 const c=!open;                                  // 取反后的状态
 if(narrow){r.dataset.rail=c?'open':'closed'}
 else if(c){delete r.dataset.rail}else{r.dataset.rail='closed'}
 // 图标：窄屏收起态用 ☰（"打开会话列表"比一个小尖括号好认），展开态用 ‹ 收起
 const btn=document.getElementById('railBtn');
 if(btn)btn.textContent = narrow ? (c?'‹':'☰') : (c?'‹':'›');
 try{localStorage.setItem('cbp-rail',narrow?(c?'open':'closed'):(c?'':'closed'))}catch(e){}}
try{const _r=document.documentElement,_b=document.getElementById("railBtn");
 if(_b){const _narrow=matchMedia("(max-width:860px)").matches;
  const _open=_narrow?(_r.dataset.rail==="open"):(_r.dataset.rail!=="closed");
  _b.textContent=_narrow?(_open?"‹":"☰"):(_open?"›":"‹")}}catch(e){}
/* ── 长期记忆管理面板 ───────────────────────────────────────────────
   外观复用项目既有的 .mask/.dlg 弹窗类（与确认框同款），图标用内联 SVG
   而不是 emoji：emoji 在 Windows 上是彩色位图，和界面风格不搭。 */
let memScope='all',memQuery='';
const MEM_ICON='<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" '
 +'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
 +'<path d="M12 5a3 3 0 0 0-3 3v1a2.5 2.5 0 0 0 0 5v1a3 3 0 0 0 6 0v-1a2.5 2.5 0 0 0 0-5V8a3 3 0 0 0-3-3z"/>'
 +'<path d="M9 9H7.5a2 2 0 0 0 0 4H9M15 9h1.5a2 2 0 0 1 0 4H15"/></svg>';
const MEM_CSS=`
#memBtn svg{width:16px;height:16px;display:block}
#memStats{color:var(--muted);font-size:12px;margin:6px 0;font-variant-numeric:tabular-nums}
#memList{overflow:auto;flex:1;min-height:120px}
.mem-row{border-top:1px solid var(--line);padding:9px 2px}
.mem-row:first-child{border-top:0}
.mem-meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.mem-meta .sp{margin-left:auto}
.mem-kind{color:var(--accent)}
.mem-pin{color:var(--amber)}
.mem-text{white-space:pre-wrap;margin-top:4px;line-height:1.5;font-size:13px}
.mem-empty{color:var(--muted);padding:14px 2px}
`;
function memInjectCss(){
 if(document.getElementById('memCss'))return;
 const st=document.createElement('style');st.id='memCss';st.textContent=MEM_CSS;document.head.appendChild(st);
}
function memBtn(){
 memInjectCss();
 let b=document.getElementById('memBtn');
 if(b)return b;
 b=document.createElement('button');
 b.id='memBtn';b.type='button';b.className='iconbtn';b.innerHTML=MEM_ICON;
 b.title=tr('长期记忆：查看 / 检索 / 置顶 / 删除（点击打开面板）');
 // 放到顶部工具栏：优先贴着手册按钮，其次常见工具栏容器，最后才退回 body
 // 与「使用手册 / 主题」按钮同一行：左侧栏 .listhead 内（排在手册按钮后面）
 // 图标容器可能是 .listacts（两行布局后的图标行），退回 .listhead
 const bar=document.querySelector('.listacts')||document.querySelector('.listhead');
 if(bar){bar.appendChild(b)}
 else{const a=document.getElementById('btnRefreshSess')||document.querySelector('.trow');
  if(a&&a.parentElement){a.parentElement.insertBefore(b,a.nextSibling)}else{document.body.appendChild(b)}}
 b.onclick=openMemory;
 return b;
}
async function openMemory(){
 let mask=document.getElementById('memMask');
 if(!mask){
  mask=document.createElement('div');
  mask.id='memMask';mask.className='mask';
  mask.innerHTML=`<div class="dlg" style="width:min(720px,94vw)">
   <div class="chead" style="border-bottom:1px solid var(--line)">${tr('长期记忆')}
    <span class="hint"><button class="btn ghost" id="memClose" style="padding:2px 10px;font-size:12px">${tr('关闭')}</button></span></div>
   <div style="padding:12px 15px;display:flex;flex-direction:column;gap:8px;min-height:0">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
     <select id="memScopeSel" class="btn ghost" style="padding:4px 8px">
      <option value="all">${tr('全部作用域')}</option><option value="global">${tr('仅全局')}</option><option value="project">${tr('仅当前项目')}</option>
     </select>
     <input id="memSearch" class="btn ghost" style="flex:1;min-width:150px;padding:4px 10px;text-align:left" placeholder="${tr('关键词检索…（回车）')}">
     <button class="btn ghost" id="memReload" style="padding:4px 12px">${tr('检索')}</button>
    </div>
    <div id="memStats"></div>
    <div id="memList"></div>
   </div>
  </div>`;
  document.body.appendChild(mask);
  mask.querySelector('#memClose').onclick=()=>mask.style.display='none';
  mask.querySelector('#memReload').onclick=loadMemory;
  mask.querySelector('#memScopeSel').onchange=e=>{memScope=e.target.value;loadMemory()};
  mask.querySelector('#memSearch').onkeydown=e=>{if(e.key==='Enter')loadMemory()};
  mask.onclick=e=>{if(e.target===mask)mask.style.display='none'};
 }
 mask.style.display='flex';
 await loadMemory();
}
async function loadMemory(){
 const q=document.getElementById('memSearch'),sel=document.getElementById('memScopeSel');
 if(q)memQuery=q.value.trim();
 if(sel)memScope=sel.value;
 const d=await api('/v1/agent/memory?scope='+encodeURIComponent(memScope)+'&query='+encodeURIComponent(memQuery));
 const st=d.stats||{};
 document.getElementById('memStats').textContent=
  tr('共 {t} 条 · 置顶 {p} · 从未命中 {u} · {kinds} · {scopes}',{t: st.total||0, p: st.pinned||0, u: st.unused||0, kinds: JSON.stringify(st.by_kind||{}), scopes: (d.scopes||[]).join(', ')});
 const box=document.getElementById('memList');
 if(!(d.items||[]).length){
  box.innerHTML=('<div class="mem-empty">'+esc(tr('没有匹配的记忆。Agent 完成任务时会自动沉淀，也可以直接让它「记到长期记忆」。'))+'</div>');return}
 box.innerHTML=d.items.map(m=>`<div class="mem-row">
   <div class="mem-meta">
    <span>#${esc(m.id)}</span><span class="mem-kind">${esc(m.kind)}</span><span>${esc(m.scope)}</span>
    <span>${tr('命中 {n}',{n:m.use_count||0})}</span>${m.pinned?'<span class="mem-pin">★ '+tr('置顶')+'</span>':''}
    ${(m.tags||[]).map(t=>`<span>#${esc(t)}</span>`).join('')}
    <span class="sp"></span>
    <button class="btn ghost" data-pin="${m.id}" data-v="${m.pinned?1:0}" style="padding:2px 10px;font-size:12px">${m.pinned?tr('取消置顶'):tr('置顶')}</button>
    <button class="btn ghost" data-del="${m.id}" style="padding:2px 10px;font-size:12px;color:var(--danger-text);border-color:var(--danger-border)">${tr('删除')}</button>
   </div>
   <div class="mem-text">${esc(m.text)}</div>
  </div>`).join('');
  box.querySelectorAll('[data-del]').forEach(b=>b.onclick=async()=>{
  if(!await dlgConfirm(tr('删除这条长期记忆？删除后不再参与检索。'),'删除记忆',true))return;
  try{await api('/v1/agent/memory/'+b.dataset.del+'?scope='+encodeURIComponent(memScope),{method:'DELETE'});
   toast(tr('已删除记忆 #')+b.dataset.del,'ok');loadMemory()}
  catch(e){toast(tr('删除失败: ')+e,'err')}});
 box.querySelectorAll('[data-pin]').forEach(b=>b.onclick=async()=>{
  const want=b.dataset.v!=='1';
  try{await api('/v1/agent/memory/'+b.dataset.pin+'/pin?pinned='+want+'&scope='+encodeURIComponent(memScope),{method:'POST'});
   toast(want?tr('已置顶（检索时恒定注入）'):tr('已取消置顶'),'ok');loadMemory()}
  catch(e){toast(tr('操作失败: ')+e,'err')}});
}
try{memBtn()}catch(e){}
/* ── Copilot 未就绪提示条（按需启动后：没有 Copilot 也能用提供商模型）── */
let bootStateSeen=null;
function bootBanner(){
 let el=document.getElementById('bootBar');
 if(!el){
  el=document.createElement('div');el.id='bootBar';
  el.innerHTML='<span id="bootIcon" aria-hidden="true">⚠</span>'
   +'<span id="bootText"></span>'
   +'<button class="btn ghost" id="bootRetry">重试浏览器</button>';
  document.body.appendChild(el);
  // 这个浮层是**按需创建**的（第一次 pollBoot 才建），而 applyI18n 在页面加载时就跑完了
  // ——所以它的静态中文永远不会被扫到。建好立刻自己翻一次。
  try{if(typeof applyI18n==='function')applyI18n(el)}catch(e){}
  el.querySelector('#bootRetry').onclick=async()=>{
   const b=el.querySelector('#bootRetry');b.disabled=true;b.textContent=tr('启动中…');
   try{await api('/v1/agent/browser/retry',{method:'POST'});toast(tr('已请求启动浏览器，稍后自动复查'),'ok')}
   catch(e){toast(tr('请求失败: ')+e,'err')}
   finally{b.disabled=false;b.textContent=tr('重试浏览器')}};
 }
 return el;
}
let _lastBoot=null;   // 记住最近一次的未就绪状态：切语言后要立刻重画这条提示
function showBoot(state,ready,err){
 _lastBoot=[state,ready,err];
 const el=bootBanner();
 if(ready){
  el.style.display='none';
  document.body.classList.remove('bootbar-on');
  syncFixedLanes();return}
 document.body.classList.add('bootbar-on');
 el.style.display='flex';   // 先写文字再 syncFixedLanes：否则量到的是空框高度，
                            // 内容会在 1.5s 心跳重算时跳一下（截图抓到过空框）
 // 这条提示在 i18n 的动态容器跳过名单里（#bootText），所以必须自己查表
 const map={idle:tr('浏览器未启动（首次使用 Copilot 时自动启动）'),
            starting:tr('浏览器启动中…'),
            browser_up:tr('浏览器已启动，等待 Copilot 就绪（可能需要在自动化窗口登录）'),
            needs_login:tr('需要在自动化窗口完成 Microsoft 登录'),
            failed:tr('浏览器启动失败')};
 // 错误详情也要过 tr()：它是后端/本地给的中文字符串（如"健康检查失败"），
 // 直接拼会在一句英文里夹中文（实测评测抓到的语言覆盖缺口）。
 const errPart=err?tr('（{e}）',{e:tr(String(err)).slice(0,120)}):'';
 const txt=tr('Copilot 未就绪：')+(map[state]||state||tr('未知'))+errPart+tr('　提供商模型不受影响，可直接使用。');
 document.getElementById('bootText').textContent=txt;
 syncFixedLanes();
}
/* ── 终止服务 / 退出 ────────────────────────────────────────────────
   优雅停机：后端先回响应、再延迟触发 uvicorn 停机（拿不到 server 对象就退回
   信号，并始终有"宽限期后硬退出"兜底）。停机后本页停止一切轮询，
   否则满屏失败请求 + 健康检查横幅乱跳。 */
let serviceStopped=false;
async function shutdownService(){
 if(!await dlgConfirm(
  tr('即将终止本服务（进程退出）：')+'\n\n'
  +'· '+tr('正在运行的任务会中断（状态已落库，重启后可恢复）')+'\n'
  +'· '+tr('自动化浏览器会被关闭，本页面随即断开')+'\n'
  +'· '+tr('需要时重新运行 start.py 启动服务')+'\n\n'
  +tr('确定终止？'),tr('终止服务'),true))return;
 try{
  const d=await api('/v1/shutdown',{method:'POST'});
  showStopped(d?noteText(d,''):'');
 }catch(e){
  // 连接可能比响应先断：同样视为已停止，不要报"操作失败"
    showStopped(tr('服务已终止（连接已断开）'));
 }
}
/* ── 重启服务：优雅退出 → 就地 exec 重启；本页轮询 /health，恢复后自动刷新 ── */
let restarting=false, restartStartedAt=0;
async function restartService(){
 if(restarting)return;
  if(!await dlgConfirm(
    tr('即将重启本服务（用于让改过的代码生效）：\n\n' +
       '· 服务会先优雅退出，然后**就地重启**（同一个进程，端口不变）\n' +
       '· 正在运行的任务会中断（状态已落库；开着 auto_resume 会自动续跑）\n' +
       '· 自动化浏览器会重新连接\n' +
       '· 本页会在服务恢复后**自动刷新**，无需手动操作\n\n' +
       '确定重启？'),tr('重启服务'),false))return;
 restarting=true; restartStartedAt=Date.now();
 try{ await api('/v1/restart',{method:'POST'}) }catch(e){}
 showRestarting();
 const ok=await waitServiceBack(90000);
 if(ok){ location.reload(); return }
 restarting=false;
 toast(tr('服务在 90 秒内未恢复，请检查控制台或重新运行 start.py'),'err');
}
/* 挂一条重启提示条（复用停止横幅的位置与视觉） */
function showRestarting(extra){
 let el=document.getElementById('stoppedBanner');
 if(!el){el=document.createElement('div');el.id='stoppedBanner';el.className='stoppedbanner';
  document.body.insertBefore(el,document.body.firstChild)}
 const secs=Math.max(0,Math.round((Date.now()-restartStartedAt)/1000));
 el.innerHTML='<b>↻ '+esc(tr('服务正在重启'))+'</b><span>'+esc(extra||tr('正在等待服务恢复（通常几秒），恢复后本页会自动刷新…'))+'</span>'
  +'<span class="meta" id="restartElapsed">'+esc(String(secs))+'s</span>';
 el.style.display='flex';
 try{hideRetryUI()}catch(e){}; try{hideConnBanner()}catch(e){}; try{stopEvents()}catch(e){}
 syncFixedLanes();
}
/* 轮询 /health：任一 2xx 且能解析即认为服务回来了 */
async function waitServiceBack(timeoutMs){
 const t0=Date.now();
 for(;;){
  if(Date.now()-t0>timeoutMs) return false;
  const secs=Math.round((Date.now()-restartStartedAt)/1000);
  const el=document.getElementById('restartElapsed'); if(el) el.textContent=secs+'s';
  try{
   const r=await fetch('/health',{cache:'no-store'});
   if(r.ok){ await r.json(); return true }
  }catch(e){}
  await new Promise(r=>setTimeout(r,700));
 }
}
function showStopped(note){
 serviceStopped=true;
 let el=document.getElementById('stoppedBanner');
 if(!el){el=document.createElement('div');el.id='stoppedBanner';el.className='stoppedbanner';
  document.body.insertBefore(el,document.body.firstChild)}
 el.innerHTML='<b>⏻ '+esc(tr('服务已终止'))+'</b><span>'+esc(note||'')+'</span>'
  +'<span class="meta">'+esc(tr('本页可以关闭了；需要时重新运行'))+' <code>python start.py</code> '+esc(tr('启动'))+'。</span>';
 el.style.display='flex';
 try{hideRetryUI()}catch(e){}
 try{hideConnBanner()}catch(e){}
 try{stopEvents()}catch(e){}
 syncFixedLanes();
}
async function pollBoot(){
 if(serviceStopped)return;
 try{const h=await api('/health');showBoot(h.browser_state,h.copilot_ready,h.browser_error)}
 catch(e){if(!serviceStopped)showBoot('failed',false,tr('健康检查失败'))}
}
try{setInterval(pollBoot,8000);setTimeout(pollBoot,1200)}catch(e){}

try{const _rs=document.getElementById('btnRefreshSess');if(_rs)_rs.onclick=async()=>{
 if(_rs.disabled)return;_rs.disabled=true;_rs.style.opacity=.5;
 try{await refreshModels();refreshSessModelOptions();toast(tr('模型列表已刷新'),'ok')}
 catch(e){toast(tr('刷新失败: ')+e,'err')}
 finally{_rs.disabled=false;_rs.style.opacity=1}};}catch(e){}



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
})();
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
 loadVersion();mountTimelineIntoTab();mountTeamCard();applyCardCollapsed();applyTopbarCollapsed();
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


/* ── 事件委托（CSP 收紧后的统一入口）─────────────────────────────────
   原先页面有 49 处内联 onclick/oninput/onkeydown，加上动态生成的 4 处。
   内联处理器有两个问题：① 迫使 CSP 保留 script-src 'unsafe-inline'，
   而内联脚本正是 XSS 的主要放大器；② 动态 HTML 每次重建都要重新绑定。
   现在统一走 data-act / data-act-input / data-act-key + 一个分发器：
   处理器集中在此，可审计；动态内容天然可用；CSP 可以只留 'self'。 */
/* ── 设置页 ───────────────────────────────────────────────────────────
   齿轮入口：外观 / 执行 / 浏览器 / 数据 / 入口 / 危险区。
   服务端配置走 /v1/config（白名单，见 api.CONFIG_SCHEMA），外观与语言即时生效。 */
const SETTINGS_GROUPS=[
 ['ui', '外观'], ['exec', '执行'], ['browser', '浏览器'],
 ['data', '数据'], ['entries', '入口'], ['danger', '危险区']];
let settingsSchema=null, settingsValues=null, settingsTab='ui';
/* 枚举值的显示名：直接显示 "session"/"each"/"lazy" 太像代码（用户看到的是生词）。 */
const ENUM_LABEL={each:'每次操作都询问',session:'本会话内不再逐次询问',
  lazy:'首次使用时启动',eager:'服务启动时预热'};

/* ── 权限档位（仿 DSH）─────────────────────────────
   后端早有 SANDBOX_MODES 与 /v1/agent/sandbox（含 persist），这里只是把它做成
   输入框上方一眼可见的 chip：点开三档、选中即生效并落库。 */
const SANDBOX_UI={
 'read-only':{ico:'👁', label:'仅可查看'},
 'workspace-write':{ico:'🛠', label:'工作区内修改'},
 'danger-full-access':{ico:'⚠', label:'完全权限'},
};
let sandboxMode='workspace-write';
function renderSandbox(){
 const mode=SANDBOX_UI[sandboxMode]?sandboxMode:'workspace-write';
 const meta=SANDBOX_UI[mode];
 // 两处实例（会话内 + 新建任务）都要同步，所以按 class 遍历而不是按 id
 document.querySelectorAll('.sandboxchip').forEach(chip=>{
  const ico=chip.querySelector('.sb-ico');
  const label=chip.querySelector('[data-sandbox-label],#sandboxLabel');
  if(ico)ico.textContent=meta.ico;
  if(label)label.textContent=tr(meta.label);
  chip.dataset.mode=mode;
  chip.classList.toggle('warn',mode==='danger-full-access');
  const host=chip.closest('.menu');
  const menu=host?host.querySelector('.menuList'):null;
  chip.setAttribute('aria-expanded',menu&&menu.classList.contains('open')?'true':'false');
 });
 document.querySelectorAll('.sandboxmenu .sbopt').forEach(b=>{
  const on=b.dataset.arg===mode;
  b.classList.toggle('on',on);
  const c=b.querySelector('.sb-check');
  if(c)c.textContent=on?'✓':'';
 });
}
async function loadSandbox(){
 try{
  const r=await api('/v1/agent/sandbox');
  sandboxMode=String(r.mode||'workspace-write');
 }catch(e){}
 renderSandbox();
}
async function pickSandbox(mode){
 if(!SANDBOX_UI[mode])return;
 try{
  const r=await api('/v1/agent/sandbox',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode,persist:true})});
  sandboxMode=String(r.mode||mode);
  renderSandbox();
  document.querySelectorAll('.sandboxmenu.open').forEach(m=>m.classList.remove('open'));
  toast(tr('权限已切换为 {m}',{m:tr(SANDBOX_UI[sandboxMode].label)}),'ok');
 }catch(e){ toast(tr('切换失败: ')+((e&&(e.text||e.detail))||e),'err') }
}

async function openSettings(){
 const mask=document.getElementById('settingsMask');
 if(!mask)return;
 mask.classList.add('open');
 await renderSettings();
}
function closeSettings(){
 const mask=document.getElementById('settingsMask');
 if(mask)mask.classList.remove('open');
}
function _cfgInput(key,spec,val){
 const id='set_'+key.replace(/\./g,'_');
 if(spec.kind==='bool'){
  return `<label class="setrow" for="${id}"><input type="checkbox" id="${id}" data-cfg="${esc(key)}" ${val?'checked':''}>
    <span>${esc(tr(spec.label))}</span></label>`;
 }
 if(spec.kind==='enum'){
  const opts=spec.values.map(v=>{
   const label=ENUM_LABEL[v]||v;
   return `<option value="${esc(v)}" ${String(val)===v?'selected':''}>${esc(tr(label))}</option>`;
  }).join('');
  return `<label class="setrow" for="${id}"><span>${esc(tr(spec.label))}</span>
    <select id="${id}" data-cfg="${esc(key)}">${opts}</select></label>`;
 }
 const t=spec.kind==='int'?'number':'text';
 // 没配过的键显示"默认"占位：空输入框会让人以为坏了/以为必须填
 const ph=(val===undefined||val===null||val==='')?`placeholder="${esc(tr('默认'))}"`:'';
 return `<label class="setrow" for="${id}"><span>${esc(tr(spec.label))}</span>
   <input type="${t}" id="${id}" data-cfg="${esc(key)}" value="${esc(val??'')}" ${ph}
     ${spec.min!==undefined?`min="${spec.min}"`:''} ${spec.max!==undefined?`max="${spec.max}"`:''}></label>`;
}
async function renderSettings(){
 const box=document.getElementById('settingsBody');
 const nav=document.getElementById('settingsNav');
 if(!box||!nav)return;
 if(!settingsValues){
  try{
   const r=await api('/v1/config');
   settingsValues=r.values||{}; settingsSchema=r.schema||{};
  }catch(e){ settingsValues={}; settingsSchema={}; }
 }
 const groups={};
 Object.entries(settingsSchema).forEach(([k,spec])=>{
  const g=spec.group||'exec';
  (groups[g]=groups[g]||[]).push([k,spec]);
 });
 const has=(gid)=>gid==='ui'||gid==='entries'||gid==='danger'||(groups[gid]||[]).length;
 const visible=SETTINGS_GROUPS.filter(([gid])=>has(gid));
 if(!visible.some(([gid])=>gid===settingsTab))settingsTab=visible[0][0];

 // 左导航
 nav.innerHTML=visible.map(([gid,label])=>{
  const on=gid===settingsTab;
  return `<button type="button" class="setnavbtn${on?' on':''}${gid==='danger'?' danger':''}"
     role="tab" aria-selected="${on?'true':'false'}" data-act="settingsTab" data-arg="${gid}">
     ${esc(tr(label))}</button>`;
 }).join('');

 // 右内容：所有分组都渲染，非当前的隐藏（值仍留在 DOM，保存不会丢）
 const pane=(gid,html)=>`<section class="setgroup${gid==='danger'?' danger':''}" data-pane="${gid}"
    style="${gid===settingsTab?'':'display:none'}">${html}</section>`;

 const uiHtml=`<h4>${esc(tr('外观'))}</h4>
   <label class="setrow"><span>${esc(tr('主题'))}</span>
     <select id="setTheme" data-ui="theme">${[['auto','跟随系统'],['light','浅色'],['dark','深色'],['solarized','护眼'],['mint','薄荷'],['amber','琥珀']]
       .map(([v,t])=>`<option value="${v}" ${_settingsTheme()===v?'selected':''}>${esc(tr(t))}</option>`).join('')}</select></label>
   <label class="setrow"><span>${esc(tr('语言'))}</span>
     <select id="setLang" data-ui="lang">${[['zh','中文'],['en','English'],['tw','繁體中文']]
       .map(([v,t])=>`<option value="${v}" data-i18n-skip="1" ${currentLang()===v?'selected':''}>${esc(t)}</option>`).join('')}</select></label>`;

 const entriesHtml=`<h4>${esc(tr('入口'))}</h4>
   <div class="setbtns">
    <button type="button" class="btn ghost" data-act="openManual">📖 ${esc(tr('使用手册'))}</button>
    <button type="button" class="btn ghost" data-act="openPlugins">🧩 ${esc(tr('插件管理'))}</button>
    <button type="button" class="btn ghost" data-act="openPanels">🗂 ${esc(tr('插件面板'))}</button>
    <button type="button" class="btn ghost" data-act="openWsBrowser">📁 ${esc(tr('工作区浏览'))}</button>
    <button type="button" class="btn ghost" data-act="closeSettings" data-arg="memory">💬 ${esc(tr('记忆面板'))}</button>
   </div>`;

 const dangerHtml=`<h4>${esc(tr('危险区'))}</h4>
   <div class="setbtns">
    <button type="button" class="btn ghost" data-act="restartService">↻ ${esc(tr('重启服务'))}</button>
    <button type="button" class="btn ghost danger" data-act="shutdownService">⏻ ${esc(tr('终止服务 / 退出'))}</button>
   </div>
   <p class="sethint">${esc(tr('重启/终止都会中断正在跑的任务；状态已落库，重启后可恢复。'))}</p>`;

 const cfgPane=(gid,label)=>{
  const rows=(groups[gid]||[]).map(([k,spec])=>_cfgInput(k,spec,settingsValues[k])).join('');
  return rows?`<h4>${esc(tr(label))}</h4>${rows}`:'';
 };

 box.innerHTML=visible.map(([gid,label])=>{
  if(gid==='ui')return pane('ui',uiHtml);
  if(gid==='entries')return pane('entries',entriesHtml);
  if(gid==='danger')return pane('danger',dangerHtml);
  return pane(gid,cfgPane(gid,label));
 }).join('');
 try{translateAttrs(box);translateAttrs(nav)}catch(e){}
}
function _settingsTheme(){
 try{const t=localStorage.getItem('cbp-theme');return (!t||t==='auto')?'auto':t}catch(e){return 'auto'}
}
function setSettingsTab(id){
 const known=SETTINGS_GROUPS.map(g=>g[0]);
 if(!known.includes(id))return;
 settingsTab=id;
 document.querySelectorAll('#settingsNav .setnavbtn').forEach(b=>{
  const on=b.dataset.arg===id;
  b.classList.toggle('on',on);b.setAttribute('aria-selected',on?'true':'false');
 });
 document.querySelectorAll('#settingsBody .setgroup[data-pane]').forEach(p=>{
  p.style.display=(p.dataset.pane===id)?'':'none';
 });
}

function collectSettings(){
 const out={};
 const box=document.getElementById('settingsBody');
 if(!box)return out;
 box.querySelectorAll('[data-cfg]').forEach(el=>{
  const key=el.dataset.cfg;
  if(!key)return;
  out[key]=el.type==='checkbox'?el.checked:el.value;
 });
 return out;
}

async function saveSettings(){
 const themeSel=document.getElementById('setTheme');
 const langSel=document.getElementById('setLang');
 if(themeSel&&themeSel.value!==themeChoice()){try{localStorage.setItem('cbp-theme',themeSel.value)}catch(e){};applyTheme(themeSel.value,false);applyLangButton()}
 if(langSel&&langSel.value!==currentLang()){setLang(langSel.value)}
 // 只提交**改动过**的字段：整表提交时，空的文本/枚举控件会被后端校验拒掉
 // （实测 ``default_model: ""`` 让整批保存 400），也会把没动过的键来回写文件。
 const raw=collectSettings();
 const cur=settingsValues||{};
 const values={};
 Object.keys(raw).forEach(k=>{
  const before=(cur[k]===undefined||cur[k]===null)?'':String(cur[k]);
  const now=String(raw[k]);
  if(now!==before)values[k]=raw[k];
 });
 if(!Object.keys(values).length){toast(tr('没有改动'),'ok');return}
 try{
  const r=await api('/v1/config',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({values,persist:true})});
  settingsValues=Object.assign({},settingsValues,r.applied||{});
  toast(tr('设置已保存（{n} 项）',{n:Object.keys(r.applied||{}).length}),'ok');
 }catch(e){ toast(tr('保存失败: ')+((e&&(e.text||e.detail||e.message))||String(e)),'err') }
}

const ACTIONS={
 toggleRail:()=>toggleRail(),
 // 窄屏抽屉的遮罩：点一下收起（只有窄屏才有意义，宽屏没有遮罩）
 closeRail:()=>{const r=document.documentElement;
  if(matchMedia('(max-width:860px)').matches){r.dataset.rail='closed';
   try{localStorage.setItem('cbp-rail','closed')}catch(e){}}},
 openTask:()=>openTask(),
 openManual:()=>openManual(),
 toggleBrowser:()=>toggleBrowser(),
 toggleTheme:()=>toggleTheme(),
 cycleLang:()=>cycleLang(),
 drawSessions:()=>drawSessions(),
 chatFindInput:()=>chatFindInput(),
 gsInput:()=>gsInput(),
 findStepKey:(el,e)=>{if(e.key==='Enter'){e.preventDefault();findStep(e.shiftKey?-1:1)}},
 gsKey:(el,e)=>{if(e.key==='Escape'){e.preventDefault();toggleGlobalSearch()}},
 findStep:(el)=>findStep(Number(el.dataset.arg||1)),
 control:(el)=>control(el.dataset.arg),
 continueAgent:()=>continueAgent(),
 activate:()=>activate(),
 openCopilot:()=>openCopilot(),
 toggleMore:(el,e)=>toggleMore(el, e),
 toggleToolLog:()=>toggleToolLogPop(),
 toolLogAll:(el,e)=>{if(e&&e.stopPropagation)e.stopPropagation();toggleToolLogPop();},
 openSettings:()=>openSettings(),
 swapModelResume:(el)=>swapModelResume(el&&el.dataset&&el.dataset.arg),
 dismissRetry:()=>dismissRetryOverlay(),
 reopenRetry:()=>{retryDismissedAt=null;updateRetryCountdown(lastRuntime)},
 pickSandbox:(el)=>pickSandbox(el&&el.dataset?el.dataset.arg:''),
 settingsTab:(el)=>{setSettingsTab(el&&el.dataset?el.dataset.arg:'');},
 openWsBrowser:()=>openWsBrowser(''),
 closeSettings:(el)=>{closeSettings();if(el&&el.dataset&&el.dataset.arg==='memory'){try{openMemory()}catch(e){}}},
 saveSettings:()=>saveSettings(),
 rollbackSession:()=>rollbackSession(),
 rollbackSelfUpgrade:()=>rollbackSelfUpgrade(),
 removeSession:(el)=>removeSession(el.dataset.arg==='true'),
 liveTab:(el)=>liveTab(el.dataset.arg),
 toggleStreamDetail:()=>toggleStreamDetail(),
 cardCollapse:(el)=>toggleCard(el.dataset.arg||'live'),
 topbarCollapse:()=>toggleTopbar(),
 showLastPrompt:()=>showLastPrompt(),
 openSubagent:(el)=>load(el.dataset.arg),
 openPlugins:()=>openPlugins(),
 openPanels:()=>openPanels(),
 refreshPanels:()=>refreshPanels(),
 closePanels:()=>closePanels(),
 deepResearch:()=>deepResearchNow(),
 pickWorkspace:()=>pickWorkspace(),
 newWorkspace:()=>newWorkspace(),
 wsMkdir:()=>wsMkdir(),
 wsUp:()=>wsUp(),
 wsDefault:()=>wsDefault(),
 wsChoose:()=>wsChoose(),
 closeWs:()=>closeWs(),
 closePlugins:()=>closePlugins(),
 toggleSubagents:()=>toggleSubagents(),
 showPromptView:()=>showPromptView(),
 expandLive:()=>expandLive(),
 toggleChatFind:()=>toggleChatFind(),
 injectMessage:()=>injectMessage(),
 setFollowupMode:(el)=>setFollowupMode(el.dataset.arg),
 setTaskMode:(el)=>setTaskMode(el.dataset.arg),
 sendFollowup:()=>sendFollowup(),
 exportTimeline:()=>exportTimeline(),
 refreshModels:()=>refreshModels(),
 runTask:()=>runTask(),
 taskKey:(el,e)=>{if(slashKey(e))return;if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(slashRun(el.value))return;runTask()}},
 taskInput:()=>slashUpdate(),
 closeDlg:()=>closeDlg(),
 closeManual:()=>closeManual(),
 closeLive:()=>closeLive(),

 // 点击遮罩**背景**才关闭（点弹窗内部不关）：原来写在内联里的 if(event.target===this)
 mask:(el,e)=>{if(e.target!==el)return;if(el&&el.id==='settingsMask'){try{closeSettings()}catch(_){};return}
  ({dlg:closeDlg,manual:closeManual,live:closeLive,plugins:closePlugins,ws:closeWs,panel:closePanels}[el.dataset.arg]||(()=>{}))()},
 maskGm:(el,e)=>{if(e.target===el)gmClose(null)},
 // 动态生成内容里的处理器（消息复制/代码折叠/附件移除）
 copyMsg:(el)=>copyMsg(el),
 copyCode:(el)=>copyCode(el),
 copyRaw:(el)=>copyText(el.dataset.raw||'',el),
 codeMenu:(el)=>codeMenu(el),
 codeLineSel:(el)=>codeLineSel(el),
 copySelLines:(el)=>copySelLines(el),
 stepView:(el)=>stepView(el),
 editTag:(el)=>editTag(el),
 stepFilter:(el)=>stepFilterInput(el),
 exportChatMarkdown:()=>exportChatMarkdown(),
 editMsg:(el)=>editMsg(el),
 delMsg:(el)=>delMsg(el),
 toggleGlobalSearch:()=>toggleGlobalSearch(),
 shutdownService:()=>shutdownService(),
 restartService:()=>restartService(),
 toggleTodos:()=>toggleTodos(),
 imgZoom:(el)=>imgZoom(el),
 mCopy:(el)=>{const m=codeMenuEl;if(m)copyText(m.dataset.code||'',el);closeCodeMenu()},
 mDownload:(el)=>{
  const m=codeMenuEl;if(!m){return}
  const text=m.dataset.code||'';
  const ext=CODE_EXT[m.dataset.lang]||'txt';
  try{
   const blob=new Blob([text],{type:'text/plain;charset=utf-8'});
   const a=document.createElement('a');
   a.href=URL.createObjectURL(blob);a.download='code.'+ext;
   document.body.appendChild(a);a.click();a.remove();
   setTimeout(()=>URL.revokeObjectURL(a.href),2000);
  }catch(e){}
  closeCodeMenu()},
 mLines:()=>{
  // 偏好作用于所有代码块，不能只改当前块
  setCodePref('lines',!codePref('lines',true));
  applyCodePrefs();
  closeCodeMenu()},
 mWrap:()=>{
  setCodePref('wrap',!codePref('wrap',true));
  applyCodePrefs();
  closeCodeMenu()},
 mFold:()=>{
  const wrap=lastCodeMenuTarget;
  closeCodeMenu();
  if(!wrap)return;
  const folded=wrap.classList.toggle('collapsed');
  const b=wrap.querySelector('.md-fold');
  if(b)b.textContent=tr(folded?'展开':'收起')},
 toggleCodeFold:(el)=>toggleCodeFold(el),
 removeAttachment:(el)=>removeAttachment(el.dataset.store,Number(el.dataset.arg)),
};
document.addEventListener('click',e=>{
 const el=e.target.closest?e.target.closest('[data-act]'):null;
 if(!el)return;const fn=ACTIONS[el.dataset.act];if(fn)fn(el,e)});
document.addEventListener('input',e=>{
 const el=e.target.closest?e.target.closest('[data-act-input]'):null;
 if(!el)return;const fn=ACTIONS[el.dataset.actInput];if(fn)fn(el,e)});
document.addEventListener('keydown',e=>{
 const el=e.target.closest?e.target.closest('[data-act-key]'):null;
 if(!el)return;const fn=ACTIONS[el.dataset.actKey];if(fn)fn(el,e)});

/* 启动后同步一次输入框显隐：空状态默认就可见（没有 style 变更事件），
   观察器不会触发，必须在这里主动算一次，否则空状态与常驻输入框会同时出现。 */
try{syncComposer()}catch(e){}

/* 版本信息：侧栏底部那颗淡淡的小标签。
   数据来自 GET /（后端 app 的 version），拿不到就保持隐藏——不喧哗。 */
async function loadVersion(){
 const el=document.getElementById('appVersion');
 if(!el)return;
 try{
  const r=await fetch('/',{cache:'no-store'});
  if(!r.ok)return;
  const d=await r.json();
  const v=String(d.version||'').trim();
  if(!v)return;
  el.textContent='v'+v;
  el.title=tr('版本')+'：'+v+'（'+(d.name||'Copilot Local Bridge Agent')+'）';
  el.hidden=false;
 }catch(e){/* 拿不到就不显示 */}
}
