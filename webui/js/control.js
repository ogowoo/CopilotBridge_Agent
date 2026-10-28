// control.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
async function refreshChat(){
 if(!window.current)return;
 const myId=window.current;
 try{let d=await api('/v1/agent/sessions/'+encodeURIComponent(myId));
  if(window.current!==myId)return;
  let as=(d.messages||[]).filter(m=>m.role==='assistant').length;
  renderStream(d.messages||[],d.steps||[]);
  lastAssistantCount=as}catch(e){}}

/* ── 操作 ── */
async function control(a){if(!window.current)return;try{let r=await api(`/v1/agent/sessions/${encodeURIComponent(window.current)}/actions/${a}`,{method:'POST'});if(a==='resume')toast(noteText(r, r.runner_started?tr('已恢复，正在续跑任务'):tr('已恢复')), r.note_kind==='err'?'err':'ok');await load(window.current)}catch(e){toast(tr('操作失败: ')+e,'err')}}
async function activate(){if(!window.current)return;try{let d=await api(`/v1/agent/sessions/${encodeURIComponent(window.current)}/activate`,{method:'POST'});window.currentUrl=d.url;toast(tr('已将自动化 Edge 切换到该会话'),'ok')}catch(e){toast(tr('操作失败: ')+e,'err')}}
function openCopilot(){if(window.currentUrl){if(!/^https?:\/\//i.test(window.currentUrl)){toast(tr('地址不是 http(s)，已拒绝打开'),'err');return}window.open(window.currentUrl,'_blank','noopener,noreferrer')}
 else toast(isProviderModel(window.currentModel)?tr('该会话用的是 API 提供商模型，没有可打开的网页对话'):tr('该 Session 暂无网页会话地址'),'err')}
/* 删除会话：**自动判断是否用过 Copilot**，用过就一并删远端记录。
   实测痛点：任务删不掉——一是用户不知道该点"仅删本地"还是"连同 Copilot"，
   二是远端删不掉时整个删除被中止、任务留在列表里。现在合并成一个入口。 */
async function removeSession(auto){
 if(!window.current)return;
 const sid=window.current;
 let plan=null;
 try{ plan=await api('/v1/agent/sessions/'+encodeURIComponent(sid)+'/deletion-plan') }catch(e){ plan=null }
 if(plan&&plan.running){toast(tr('该任务正在运行，先停止再删除'),'err');return}
 const remote=plan? !!plan.suggest_delete_remote : !!auto;
 const extra=remote
   ?tr('，并删除该任务关联的 {n} 个 Copilot 网页会话（标题里可能已没有 [GANTRY-AGENT] 标记，删除时会用会话正文核对归属）',{n:(plan&&plan.remote_count)||1})
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
  if(failed.length&&d.deleted){showDeleteResult(d,sid)}      // 本地已删，展示远端失败明细
  else if(!d.deleted){showDeleteResult(d,sid);return}
  if(d.deleted){
   stopEvents();         // 删掉当前会话必须停 SSE，否则重连定时器会一直敲已删除的会话
   window.current=null;
   document.getElementById('sessionView').style.display='none';
   document.getElementById('emptyState').style.display='flex';
   await refreshSessions()}
 }catch(e){toast(tr('删除失败: ')+(e.text||e),'err')}}
/* ── 限流倒计时：居中卡片，可收起为右下角小标 ── */
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
 updateRetryCountdown(window.lastRuntime)}
/* 服务不可用（短暂退避）时的顶栏小标：不弹遮罩、不打断操作，但一直看得见。
   返回 true 表示"这个状态已经用小标表达了"。 */
function showServiceBadge(){
 const chip=document.getElementById('retryChip');
 if(!chip)return false;
 // 有明确恢复时间点（retry_resume_at）时，倒计时卡片优先显示——
 // 否则用户只能看到 ⚠ badge，永远不知道还要等多久（issue #5 实测）
 const resumeAt=(window.lastRuntime&&window.lastRuntime.retry_resume_at)||null;
 if(resumeAt&&resumeAt*1000>Date.now())return false;
 const err=String((window.lastRuntime&&window.lastRuntime.service_error)||"");
 const waiting=!!(window.lastRuntime&&window.lastRuntime.service_waiting);
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
 window.lastRuntime=r||window.lastRuntime;
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
 let isProvider=!!(window.currentModel&&window.currentModel.indexOf('/')>=0);
 let title=box.querySelector('.rc-title');
 // 这两条以前是硬编码中文：英文模式下只有副标题是英文、标题仍是中文（用户实测）
 if(title)title.textContent='⏳ '+(isProvider?tr('API 服务限流/不可用，预计恢复')
                                        :tr('Copilot 限流/不可用，预计恢复'));
 box.querySelector('.rc-count').textContent=text;
 let sub=box.querySelector('.rc-sub');
 const suspended=String(window.lastRuntime&&(window.lastRuntime.session_state||window.lastRuntime.state)||'')==='suspended';
 sub.textContent=suspended
  ? tr('Copilot 网页限流，已挂起：到点自动继续，也可以换下面的提供商模型立即继续')
  : tr('第 {n} 次 · 自动重试中，可点「暂停」稍后继续',{n:window.lastRuntime.retry_attempt||1});
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
 if(!model||!window.current)return;
 try{
  await api(`/v1/agent/sessions/${window.current}/model`,{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({model})});
  await api(`/v1/agent/sessions/${window.current}/actions/resume`,{method:'POST',
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
 if(!window.current){toast(tr('请先选择 Session'),'err');return}
 try{
  let s=await api('/v1/agent/sessions/'+encodeURIComponent(window.current)+'/snapshots');
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
  let d=await api(`/v1/agent/sessions/${encodeURIComponent(window.current)}/rollback`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let tail=d.failed?`，失败 ${d.failed}`:'';
  toast(tr('回滚完成：还原 {a}、删除 {b}{tail}',{a:d.restored,b:d.deleted,tail:tail}), d.failed?'err':'ok');
  await load(window.current)
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
function showDeleteResult(d, sid){
 lastDeleteResult=d;
 // 记下这次删除对应的会话与失败 URL：本地删掉后 current 会被清空，
 // 重试按钮不能依赖 current（以前就是这里静默 return，按钮像坏了）。
 try{
  if(sid)lastDeleteResult._sid=sid;
  lastDeleteResult._urls=(d.remote_results||[]).filter(x=>!x.deleted&&x.url).map(x=>x.url);
 }catch(e){}
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
 // 用弹窗记住的 sid（本地删掉后 current 已经是 null）
 const sid=(lastDeleteResult&&lastDeleteResult._sid)||window.current;
 if(!sid){toast(tr('没有可清理的会话'),'err');return}
 const btn=document.getElementById('dlgForce');
 btn.disabled=true;btn.textContent=tr('处理中…');
 try{
  let qs=['delete_remote=true','force_local=true'];
  const urls=(lastDeleteResult&&lastDeleteResult._urls)||[];
  urls.forEach(u=>qs.push('urls='+encodeURIComponent(u)));
  let d=await api('/v1/agent/sessions/'+encodeURIComponent(sid)+'?'+qs.join('&'),{method:'DELETE'});
  if(d.deleted){
   closeDlg();window.current=null;
   document.getElementById('sessionView').style.display='none';
   document.getElementById('emptyState').style.display='flex';
   await refreshSessions();
  }else{
   showDeleteResult(d, sid);
  }
 }catch(e){toast(tr('强制清理失败: ')+e,'err')}
 finally{btn.disabled=false;btn.textContent=tr('重试远程并强制清理本地记录')}}
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
随后注册 <code>self_*</code> 工具（白名单：<code>gantry/ webui/ scripts/ docs/ tests/ skills/</code> 等；
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
文件：gantry/bridge.py（安装目录相对路径，不要写绝对路径）
现状：&lt;你观察到的行为，例如 resume 时提示只拼原任务&gt;
要改：&lt;越具体越好，能贴函数名/字段名/代码片段就贴&gt;
边界：只改上面这个文件或这段逻辑，其它不要动
验收：self_validate + self_unittest 双通过
交付：告诉我改了哪些行、验证结果、需要重启、以及回滚命令</pre>
<p>也可以直接说短句，效果一样：</p>
<ul>
<li>「把 <code>webui/css/tokens.css</code> 里 <code>.app</code> 的侧栏宽度 318px 改成 300px，改完跑 self_validate 和 self_unittest」</li>
<li>「看 <code>gantry/api.py</code> 的 <code>/v1/shutdown</code>，把优雅停机超时从 8 秒改成 15 秒，其他别动」</li>
</ul>
<p>先确认开关真的生效：问它一句「你有哪些 self_ 开头的工具」，应该能列出
<code>self_read_text</code> / <code>self_write_text_b64</code> / <code>self_append_text_b64</code> /
<code>self_replace_text</code> / <code>self_validate</code> / <code>self_unittest</code> /
<code>self_rollback</code> 共 7 个；关着的时候是 0 个。</p>
<h4>它会遵守的硬约束</h4>
<ul>
<li><b>只能改白名单</b>：<code>gantry/</code> <code>webui/</code> <code>scripts/</code> <code>docs/</code>
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
<li><b>Pause / resume</b>: pause stops after the window.current turn; resume continues from the checkpoint.</li>
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
so you do not have to wait for the window.current turn to finish.</p>
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
<li><b>Scope</b>: <code>global</code> is visible to every task; <code>project</code> only within the window.current workspace.</li>
<li><b>Retrieval</b>: only the few most relevant entries are injected (never everything); on conflict the window.current
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
<code>skills/&lt;name&gt;/SKILL.md</code> in the window.current workspace).</li>
<li>Writing to the global <code>skills/</code> (effective for all workspaces) requires <code>allow_self_upgrade</code>.</li>
<li>Skills are <b>usable immediately</b>, no restart; a project-level skill overrides the global one of the same name.</li>
</ul>

<h3>Interface</h3>
<ul>
<li><b>◐</b> theme cycle: dark / light / Nord blue / eye-friendly beige / Dracula purple / soft pink / mint, kept across reloads.</li>
<li>The thin handle on the rail's left edge collapses/expands the sidebar; the live panel (run state / window.current answer /
sent content / follow-up) is tabbed — <b>⤢ Expand</b> shows the full text.</li>
<li>Each step in the timeline shows its duration; "View result" expands the structured JSON.</li>
</ul>
<h3>Service self-upgrade (off by default)</h3>
<ol>
<li>Set <code>"allow_self_upgrade": true</code> in the agent section of <code>config.json</code> and restart; the
<code>self_*</code> tools are then registered (allowlist: <code>gantry/ webui/ scripts/ docs/ tests/ skills/</code> etc.;
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
File: gantry/bridge.py (install-dir relative path, never an absolute path)
Current: &lt;the behaviour you observed, e.g. resume only re-sends the original task&gt;
Change: &lt;be specific; paste function/field names or a code snippet if you can&gt;
Scope: only the file or logic above — touch nothing else
Accept: self_validate + self_unittest both pass
Deliver: which lines changed, verification result, restart needed, and the rollback command</pre>
<p>Short one-liners work just as well:</p>
<ul>
<li>"Change the default sidebar width in <code>webui/css/tokens.css</code> (<code>.app</code>) from 318px to 300px, then run
self_validate and self_unittest"</li>
<li>"Look at <code>/v1/shutdown</code> in <code>gantry/api.py</code> and raise the graceful-shutdown timeout
from 8s to 15s; change nothing else"</li>
</ul>
<p>First confirm the switch really took effect: ask "which tools do you have that start with self_?" —
you should get 7: <code>self_read_text</code>, <code>self_write_text_b64</code>,
<code>self_append_text_b64</code>, <code>self_replace_text</code>, <code>self_validate</code>,
<code>self_unittest</code>, <code>self_rollback</code>. With the flag off there are zero.</p>
<h4>Hard limits it must respect</h4>
<ul>
<li><b>Allowlisted paths only</b>: <code>gantry/</code> <code>webui/</code> <code>scripts/</code>
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

export { refreshChat, control, activate, openCopilot, removeSession, fmtCountdown, hideRetryUI, dismissRetryOverlay, showServiceBadge, updateRetryCountdown, fillRetrySwap, swapModelResume, rollbackSession, rollbackSelfUpgrade, showDeleteResult, closeDlg, forceDeleteLocal, MANUAL_HTML, MANUAL_HTML_EN, MANUAL_COLLAPSE };
