// composer.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
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
   // 别只盯 style：有的路径用 class/hidden 切空状态，那时观察器不触发
  .observe(_empty,{attributes:true,attributeFilter:['style','hidden','class']});
}catch(e){}

const LIVE_NAMES={chat:'实时流',timeline:'步骤时间线'};
const LIVE_TAB={chat:'tabChat',timeline:'tabTimeline'};
// liveTabName 在 app.js（var）
let lastAns='';
function liveTab(n){
 if(!LIVE_NAMES[n])n='chat';   // 旧标签名（activity/answer/prompt）已合并进去，静默切到实时流
 window.liveTabName=n;
 for(const k in LIVE_NAMES){
  const pane=document.getElementById(k);if(pane)pane.style.display=k===n?'':'none';
  const btn=document.getElementById(LIVE_TAB[k]);if(btn)btn.className='tab'+(k===n?' on':'')}
 // 输入框的显隐统一走 syncComposer（空状态/页签两种条件）
 try{syncComposer()}catch(e){}
 // 切到时间线页签：把之前为了省时间没建的时间线补上（只建这一次）
 if(n==='timeline'){try{flushStepsIfPending()}catch(e){}}
 if(n==='chat'){try{scrollChatToEnd()}catch(e){}}}  // className 覆盖即清除新内容圆点
function expandLive(){
 document.getElementById('liveTitle').textContent=LIVE_NAMES[window.liveTabName];
 // 对话页只复制聊天记录，避免把输入框（含重复 ID）一并搬进弹窗
 document.getElementById('liveBody').innerHTML=window.liveTabName==='chat'
  ?document.getElementById('chatlog').innerHTML
  :document.getElementById(window.liveTabName).innerHTML;
 openMask('liveMask')}
function closeLive(){closeMask('liveMask')}
document.getElementById('liveCopy').onclick=()=>{try{navigator.clipboard.writeText(document.getElementById('liveBody').textContent||'')}catch(e){}};
/* 查看该会话的初始提示词（首回合发给模型的完整内容）。
   未落库的旧会话由后端按当前工作区近似重建，这里明确标注。 */
async function showPromptView(){
 if(!window.current){toast(tr('请先选择 Session'),'err');return}
 try{
  let d=await api('/v1/agent/sessions/'+encodeURIComponent(window.current)+'/prompt');
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
 if(!window.current){toast(tr('请先选择 Session'),'err');return}
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
 if(!window.current){toast(tr('请先选择 Session'),'err');return}
 let box=document.getElementById('followup'),content=box.value.trim();
 if(!content&&!IMG_STORES.follow.length){toast(tr('消息不能为空'),'err');return}
 // 继续对话 Composer 复用新任务输入框的斜杠命令处理；命令已执行时不再作为普通消息发送。
 if(content.startsWith('/')&&typeof window.slashRun==='function'){
  const handled=await window.slashRun(content,{source:'followup',input:box});
  if(handled!==false)return;
 }
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
 try{let dd=await api('/v1/agent/sessions/'+encodeURIComponent(window.current));baseAssistants=dd.messages.filter(m=>m.role==='assistant').length}catch(e){}
 try{let d=await api(`/v1/agent/sessions/${encodeURIComponent(window.current)}/messages`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content,attachments:atts,model:window.currentModel||document.getElementById('model').value,agent_mode:window.followupMode==='agent'})});
  if(d&&d.queued){
   let note=msgEl('assistant',
     d.position>1?`已排队（前面还有 ${d.position-1} 条）`:'已排队，等待处理…',
     null,{plain:true,who:tr('队列')});
   log.appendChild(note);log.scrollTop=log.scrollHeight;
   refreshQueue();
   awaitFollowupReply(window.current,baseAssistants);
   }else{
    // 后端对 /messages 只返回排队/接收元信息（session_id/queued/id/position/mode），
    // 从不返回 content/url —— 原实现在这里读不存在的字段，会渲染一个空气泡。
    // 正确做法：重载会话，把刚落库的新消息渲染出来。
    await load(window.current)}
 }catch(e){log.appendChild(msgEl('assistant',tr('发送失败: ')+e,null,{plain:true,who:tr('错误')}))}
 finally{btn.disabled=false;btn.removeAttribute('aria-busy')}}

/* 队列模式：跟进消息入队后轮询会话消息，助理回复一出现就刷新聊天区 */
async function awaitFollowupReply(sid,baseAssistants){
 for(let i=0;i<300;i++){
  await new Promise(r=>setTimeout(r,1000));
  if(window.current!==sid)return;                     // 用户切走了就停
  try{
   let d=await api('/v1/agent/sessions/'+encodeURIComponent(sid));
   let as=d.messages.filter(m=>m.role==='assistant');
   if(as.length>baseAssistants){renderMessages(d.messages,{animateLast:true});return}
  }catch(e){}
 }}

/* 按当前会话通道调整界面：API 提供商会话没有"自动化 Edge 页面/网页对话"，
   隐藏"切换到页面""打开会话",避免点了必然失败。 */
function updateChannelUI(){
 let prov=isProviderModel(window.currentModel);
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
 if(!window.current){box.innerHTML='';return}
 try{
  let d=await api('/v1/agent/sessions/'+encodeURIComponent(window.current)+'/queue');
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
 if(!window.current)return;
 let d=await api('/v1/agent/sessions/'+encodeURIComponent(window.current)+'/queue');
 let it=(d.pending||[]).find(x=>x.id===itemId);if(!it)return;
 let content=await dlgPrompt(tr('修改排队消息'),it.content,'保存');
 if(content===null)return;
 try{
  await api(`/v1/agent/sessions/${encodeURIComponent(window.current)}/queue/${itemId}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content})});
  toast(tr('已更新排队消息'),'ok');refreshQueue()}catch(e){toast(tr('更新失败: ')+(e.text||e),'err')}}

async function removeQueueItem(itemId){
 if(!window.current)return;
 if(!await dlgConfirm(tr('删除这条排队中的消息？此操作不可撤销。'),'删除排队消息',true))return;
 try{
  await api(`/v1/agent/sessions/${encodeURIComponent(window.current)}/queue/${itemId}`,{method:'DELETE'});
  toast(tr('已删除排队消息'),'ok');refreshQueue();load(window.current)}catch(e){toast(tr('删除失败: ')+e,'err')}}
/* ── 粘贴图片/截图附件 ── */
var IMG_STORES={task:[],follow:[]};

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
 if(!window.current){toast(tr('请先选择 Session'),'err');return}
 const msgs=lastMessages||[];
 if(!msgs.length){toast(tr('当前没有对话记录'),'err');return}
 const title=(document.getElementById('title').textContent||'').trim()||window.current;
 let out=`# ${title}\n\n`;
 out+=`- Session: \`${window.current}\`\n`;
 out+=`- 模型: \`${window.currentModel||'-'}\`\n`;
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
  a.download='gantry-'+window.current+'-chat-'+new Date().toISOString().slice(0,19).replace(/[:T]/g,'-')+'.md';
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
  const d=await api(`/v1/agent/sessions/${encodeURIComponent(window.current)}/messages/${id}`,
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
  await api(`/v1/agent/sessions/${encodeURIComponent(window.current)}/messages/${id}`,{method:'DELETE'});
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
 if(!window.current){toast(tr('请先选择 Session'),'err');return}
 let pick=await dlgInjectPrompt(
  tr('插入指令（运行中即时生效，会立即中断当前回合后按新指令继续）。插入方式决定语义：'),
  '', tr('强行插入'),'step');
 if(pick===null)return;
 let content=String(pick.text||'').trim();
 if(!content)return;
 let queue=(pick.mode==='turn')?'turn':'step';
 try{localStorage.setItem('gantry-inject-mode',queue)}catch(e){}
 try{
  let d=await api(`/v1/agent/sessions/${encodeURIComponent(window.current)}/inject`,
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
// taskMode 在 app.js（var）
function setTaskMode(m){window.taskMode=m;
 document.getElementById('ntAgent').className='btn seg'+(m==='agent'?' on':'');
 document.getElementById('ntChat').className='btn seg'+(m==='chat'?' on':'')}

async function runTask(){
 let task=document.getElementById('task').value.trim();if(!task)return;
 let atts=IMG_STORES.task.map(a=>a.dataUrl);
 let _plan=window.planModeOnce; window.planModeOnce=false;
 let b={model:document.getElementById('model').value,plan_mode:_plan,messages:[{role:'user',content:task}],stream:false,session_id:((document.getElementById('sid')||{}).value||'').trim()||('task-'+Date.now().toString(36)),workspace:document.getElementById('workspace').value.trim()||null,reuse_session:true,agent_mode:window.taskMode==='agent',attachments:atts};
 window.current=b.session_id;
 window.currentModel=b.model;      // 会话页模型选择器随即显示本次任务用的模型
 IMG_STORES.task=[];renderThumbs();
 document.getElementById('emptyState').style.display='none';
 document.getElementById('sessionView').style.display='';
 resetView();
 // 先用本地已知信息填标题/状态：/v1/agent/completions 会一直挂到整轮任务
 // 结束才返回，等它的回调会让整个任务期间标题都显示"—"
 let title=(task.split('\n')[0]||'').trim().slice(0,120)||window.current;
 document.getElementById('title').textContent=title;
 let _sc=document.getElementById('sidChip');
 if(_sc){_sc.textContent=window.current;_sc.onclick=()=>{try{navigator.clipboard.writeText(window.current);toast(tr('已复制会话 ID: ')+window.current,'ok')}catch(e){toast(window.current)}}}
 fillModelSelect(document.getElementById('sessModel'), modelCache.map(id=>({id})), window.currentModel);
 refreshSessModelOptions();
 document.getElementById('status').textContent=(window.taskMode==='agent'?'Agent 执行':'普通对话')
  +' · '+(b.workspace||'默认工作区');
 setPill('running');
 document.getElementById('activity').textContent=(window.taskMode==='agent'?'正在建立 Agent 任务 ':'正在发送普通对话 ')+window.current+'...';
 if(window.DEMO){await refreshSessions();return}
  // 走 api()：裸 fetch 不带 X-Gantry-Token，配置 api_token 时「启动任务」必然 401
  api('/v1/agent/completions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})
   .then(async d=>{
    if(!d)throw Error('HTTP 响应为空');
    window.current=d.session_id;await load(window.current)})
   .catch(e=>{window.current=null;document.getElementById('sessionView').style.display='none';
    document.getElementById('emptyState').style.display='flex';toast(tr('任务启动失败: ')+(e.text||e),'err')});
 waitSessionReady(window.current);
 setTimeout(refreshSessions,700)}

/* 新任务启动后：轮询会话是否已在后端建立，一旦出现就用真实数据填充
   标题/工作区/步骤并进入每秒运行状态刷新（排队等锁时可能几十秒才有会话）。 */
async function waitSessionReady(sid){
 for(let i=0;i<200;i++){
  if(window.current!==sid)return;                  // 用户已切换到别的会话
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

export { mountTimelineIntoTab, syncComposer, liveTab, expandLive, closeLive, showPromptView, continueAgent, autoGrowFollowup, sendFollowup, awaitFollowupReply, updateChannelUI, refreshQueue, editQueueItem, removeQueueItem, thumbsHtml, renderThumbs, removeAttachment, attachPasteHandler, lastSavedAssistant, renderLiveBubble, updateLiveBubble, exportChatMarkdown, editMsg, delMsg, toggleGlobalSearch, gsInput, runGlobalSearch, jumpToMessage, injectMessage, setTaskMode, runTask, waitSessionReady, toggleMore, THEMES, THEME_IDS };
