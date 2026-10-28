// toollog.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
function toggleToolLog(){
 const body=document.getElementById('toolLogBody');
 const chev=document.getElementById('toolLogChev');
 const wrap=document.getElementById('toolLog');
 const row=document.getElementById('toolLogLatest');
 if(!body)return;
 const open=body.hidden;                 // 当前隐藏 → 展开
 body.hidden=!open;
 if(wrap)wrap.classList.toggle('open',open);
 if(row)row.classList.toggle('open',open);
 if(chev)chev.textContent=open?'▾':'▸';
 try{localStorage.setItem('gantry.toolLogOpen',open?'1':'0')}catch(e){}
 if(open){
  try{renderToolLog(toolLogSteps,{force:true})}catch(e){}
  body.scrollTop=body.scrollHeight;          // 展开后停在最新一步
 }
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
   const _icName=(failed?(st==='approval_required'?'lock':'cross'):(st==='ok'?'check':'dot'));
   icEl.innerHTML=svgIcon(_icName);
   icEl.className='ss-ic urow-ic '+(failed?'bad':(st==='ok'?'ok':''));
  }
  const tool=String(last.tool||'').trim()||(failed?tr('系统'):tr('汇总'));
  const _why=errText||String(last.reason||'').trim();
  one.innerHTML='<b class="urow-label" title="'+esc(tool)+'">'+esc(toolLabel(tool))+'</b>'
    +(_why?'<span class="urow-text" title="'+esc(_why)+'">· '+esc(_why)+'</span>':'');
  one.title=one.textContent;
  const clock=document.getElementById('toolLogClock');
  if(clock)clock.textContent=last._time
    ? new Date(Number(last._time)*1000).toLocaleTimeString():'';
 }
 // 浮层内容：**打开时才建**（1500 步全量建 DOM 要 ~0.9s，而它默认是收起的）
 toolLogSteps=all;
 // 行内展开时才建 DOM；且只在签名变化时重建 + 120ms 防抖。
 // 之前每次快照（~1s）都全量重建（388 步 → 573 个节点），逐秒重排会把机器拖死。
 if(!body.hidden){
   const _bsig=all.length+'|'+(last?String(last.step??''):'')+'|'+String(toolLogSteps.length);
   if(_bsig!==toolLogBodySig){
     toolLogBodySig=_bsig;
     clearTimeout(toolLogBodyTimer);
     toolLogBodyTimer=setTimeout(()=>{try{fillToolLogBody()}catch(e){}},120);
   }
 }
}
/* 回合流：把「发送 / 回答 / 工具调用」按回合边界合成一条时间线。
   数据来自 /v1/agent/sessions/{sid}/turns（老会话没有 prompt 就标"未留存"）。 */
var turnCache={sid:null,rows:[]};
async function loadTurns(sid){
 if(turnCache.sid===sid)return turnCache.rows;
 try{
  const d=await api('/v1/agent/sessions/'+encodeURIComponent(sid)+'/turns');
  turnCache={sid:sid,rows:(d&&d.turns)||[]};
 }catch(e){turnCache={sid:sid,rows:[]}}
 return turnCache.rows;
}
/* 把回合回答摘要成一句人话（原始 JSON 放进 title/详情） */
function summarizeAnswer(raw){
 const txt=String(raw||'').trim();
 let o=null;
 try{o=JSON.parse(txt)}catch(e){}
 if(o&&typeof o==='object'){
  const t=String(o.type||'');
  if(t==='tool')return tr('调用')+' '+(o.tool?toolLabel(o.tool):'')+' · '+String(o.reason||'');
  if(t==='actions'){
   const n=Array.isArray(o.actions)?o.actions.length:0;
   const names=(o.actions||[]).map(x=>x&&x.tool?toolLabel(x.tool):'').filter(Boolean).slice(0,3);
   return tr('批量 {n} 个动作',{n:n})+(names.length?'（'+names.join('、')+(n>names.length?'…':'')+'）':'');
  }
  if(t==='final')return String(o.content||'').split('\n').filter(x=>x.trim())[0]||tr('（空）');
 }
 return txt.split('\n').filter(x=>x.trim())[0]||tr('（空）');
}

function turnRow(kind,text,full){
 const ic=kind==='send'?svgIcon('pulse'):svgIcon('check');
 const label=kind==='send'?tr('发送'):tr('回答');
 const e=document.createElement('div');
 e.className='streamstep turnrow '+(kind==='send'?'tbody-send':'tbody-answer');
 e.dataset.side=(kind==='send'?'right':'left');
 e.innerHTML='<span class="ss-ic">'+ic+'</span>'
   +'<span class="ss-tool">'+esc(label)+'</span>'
   +'<span class="ss-reason clickable" data-i18n-skip="1" title="'+esc(full||'')+'">'
   +esc(text||'')+'</span>';
 // 点说明文字展开完整内容（JSON 回答同样可展开，不再只有工具步骤能点开）
 if(full&&String(full).trim().length>String(text||'').trim().length+8){
  const det=document.createElement('details');
  det.className='ss-det';
  const sum=document.createElement('summary');
  sum.className='sr-only'; sum.textContent=tr('展开');
  det.appendChild(sum);
  const pre=document.createElement('pre');
  pre.className='turnfull'; pre.textContent=String(full||'');
  det.appendChild(pre);
  e.appendChild(det);
  const rs=e.querySelector('.ss-reason');
  if(rs)rs.addEventListener('click',ev=>{
   ev.stopPropagation();
   det.open=!det.open;
   rs.classList.toggle('open',det.open);
  });
 }
 return e;
}
var turnShown=20;                  // 默认只画最近 20 个回合（可点"显示更早"加量）
async function renderTurnFlow(sid,steps){
 const body=document.getElementById('toolLogBody');
 if(!body||!sid)return;
 const allTurns=await loadTurns(sid);
 if(!allTurns.length)return false;
 const list=(steps||[]).slice().sort((x,y)=>(Number(x.step)||0)-(Number(y.step)||0));
 const from=Math.max(0, allTurns.length-turnShown);
 const turns=allTurns.slice(from);
 // 被裁掉的回合所占的步骤也要跳过
 let cursor=from>0?(Number(allTurns[from-1].steps_after)||0):0;
 body.innerHTML='';
 if(from>0){
  const more=document.createElement('div');
  more.className='streamstep turnmore';
  more.innerHTML='<span class="ss-tool" style="cursor:pointer">'
    +esc(tr('显示更早的 {n} 个回合',{n:from}))+'</span>';
  more.addEventListener('click',()=>{
    turnShown+=40;
    try{fillToolLogBody()}catch(e){}
  });
  body.appendChild(more);
 }
 turns.forEach((t,idx)=>{
  const upto=Number(t.steps_after)||0;
  const mine=list.filter(s=>{const n=Number(s.step)||0;return n>cursor&&n<=upto});
  cursor=Math.max(cursor,upto);
  // 发送行（提示词首行；截断展示，悬停看全文）
  const promptTxt=String(t.prompt||'').split('\n').filter(x=>x.trim())[0]||'';
  body.appendChild(turnRow('send',promptTxt||tr('（旧回合未存档）'),String(t.prompt||'')));
  mine.forEach(s=>body.appendChild(stepRowEl(s)));
  // 回答行
  const _ansTxt=String(t.answer||'').trim();
  if(_ansTxt)body.appendChild(turnRow('answer',summarizeAnswer(_ansTxt),_ansTxt));
  // 没有回答内容的回合（失败/被限流打断）不画「回答（空）」——那是噪声
 });
 // 最后一个回合之后若还有步骤（例如最终收尾），补上
 list.filter(s=>(Number(s.step)||0)>cursor).forEach(s=>body.appendChild(stepRowEl(s)));
 try{translateAttrs(body)}catch(e){}
 body.scrollTop=body.scrollHeight;
 return true;
}

function fillToolLogBody(){
 const body=document.getElementById('toolLogBody');
 if(!body)return;
 if(document.hidden)return;                 // 后台标签页不渲染，省 CPU/内存
 (async()=>{
  const ok=await renderTurnFlow(window.current,toolLogSteps);
  if(ok)return;                      // 有回合数据：用"发送→回答→工具"的流
  body.innerHTML='';
  const _tail=toolLogSteps.slice(-300);     // 上千步全画会拖死浏览器
  for(const s of _tail)body.appendChild(stepRowEl(s));
  try{translateAttrs(body)}catch(e){}
  body.scrollTop=body.scrollHeight;
 })();
}


function renderMessages(messages,opts){
 opts=opts||{};
 let box=document.getElementById('chatlog');box.innerHTML='';
 window.lastMessages=messages||[];
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
 if(!window.current)return;
 const st=String(state||'');
 // 又跑起来了（续聊/恢复）：清掉"已拉取"标记，下次结束时重新统计
 if(!CHANGE_DONE.includes(st)){if(changesSid===window.current)changesState='';return}
 if(changesSid===window.current&&changesState===st)return;
 loadChanges(window.current,st)}
async function loadChanges(sid,state){
 if(!sid)return;
 try{
  const d=await api('/v1/agent/sessions/'+encodeURIComponent(sid)+'/changes');
  if(window.current!==sid)return;
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
 if(!d||changesSid!==window.current||(!files.length&&!deliv.length)){if(card)card.remove();return}
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
 const sid=window.current;if(!sid)return;
 document.getElementById('liveTitle').textContent=path+' · '+tr('查看差异');
 const body=document.getElementById('liveBody');
 body.textContent=tr('读取差异…');
 openMask('liveMask');
 try{
  const d=await api('/v1/agent/sessions/'+encodeURIComponent(sid)+'/changes/diff?path='+encodeURIComponent(path));
  if(window.current!==sid)return;
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

export { toggleToolLog, renderToolLog, loadTurns, summarizeAnswer, turnRow, renderTurnFlow, fillToolLogBody, renderMessages, maybeLoadChanges, loadChanges, clearChanges, renderChangesCard, openChangeDiff, CHANGE_DONE, HL_LABEL };
