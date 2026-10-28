// approvals.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
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
var questionAnswered=new Set();
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

export { syncFixedLanes, showConnBanner, hideConnBanner, pingInterrupt, clearInterruptPing, beepOnce, showApprovalBar, showQuestionBar };
