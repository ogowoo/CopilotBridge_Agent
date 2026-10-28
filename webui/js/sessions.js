// sessions.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
function toggleSessExpand(sid){
 if(expandedSess.has(sid))expandedSess.delete(sid);else expandedSess.add(sid);
 try{localStorage.setItem('gantry-expanded-sess',JSON.stringify([...expandedSess]))}catch(e){}
 sessSig='';drawSessions();
}
try{showSubagents=localStorage.getItem('gantry-show-subagents')==='1'}catch(e){}
async function refreshSessions(){
 if(serviceStopped)return;
 try{
  const q='?include_subagents=true';   // 全量：前端把子代理折叠到父会话下
  let d=await api('/v1/agent/sessions'+q);
  window.sessCache=d.sessions||[];
  subagentCount=d.subagent_count||0;
  drawSessions();
 }catch(e){}
}
function toggleSubagents(){
 // 全部展开 / 全部折叠（子代理会话归集在父会话下）
 const parents=new Set();
 window.sessCache.forEach(s=>{if(String(s.parent_id||''))parents.add(s.parent_id)});
 const anyCollapsed=[...parents].some(p=>!expandedSess.has(p));
 if(anyCollapsed)parents.forEach(p=>expandedSess.add(p));
 else parents.clear && parents.forEach(p=>expandedSess.delete(p));
 try{localStorage.setItem('gantry-expanded-sess',JSON.stringify([...expandedSess]))}catch(e){}
 sessSig='';drawSessions();
 toast(tr(anyCollapsed?'已展开 {n} 个子代理会话':'已折叠 {n} 个子代理会话',
            {n: parents.size}),'ok');
}
/* 列表签名：内容没变就不重建 DOM，避免每秒滚动回顶/点击错位 */
/* 工作区短名：只显示末两段（全路径放 title），避免会话列表被长路径撑开 */
function drawSessions(){
 let q=(document.getElementById('sessFilter').value||'').toLowerCase();
 let box=document.getElementById('sessions');
 let all=window.sessCache.filter(s=>!q||(s.title||'').toLowerCase().includes(q)||(s.id||'').toLowerCase().includes(q)||(s.state||'').includes(q)||(s.tag||'').toLowerCase().includes(q)||(s.workspace||'').toLowerCase().includes(q));
 // 父子归集：子代理会话挂在父会话下（可展开），不再单占一行
 const byId=new Map(all.map(s=>[s.id,s]));
 const kidsOf=new Map();
 all.forEach(s=>{const p=String(s.parent_id||'');if(p&&byId.has(p)){if(!kidsOf.has(p))kidsOf.set(p,[]);kidsOf.get(p).push(s)}});
 const isKid=s=>{const p=String(s.parent_id||'');return !!p&&byId.has(p)};
 let list=all.filter(s=>!isKid(s));
 let sig=q+'|'+all.map(s=>s.id+s.state+s.total_steps+s.estimated_tokens+(s.subagent_steps||0)+(s.subagent_tokens||0)+(s.subagent_count||0)+(s.subagent_running||0)+(s.conversation_index??'')+(s.title||'')+(s.attachment_count??'')+(s.tag||'')+(s.workspace||'')+(s.parent_id||'')).join('|')+'|'+window.current+'|'+[...expandedSess].join(',');
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
  e.className='sess'+(s.id===window.current?' sel':'')+(opts.child?' child':'');
  e.setAttribute('role','button');e.setAttribute('aria-pressed',s.id===window.current?'true':'false');
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
 const cur=((window.sessCache||[]).find(s=>s.id===sid)||{}).tag||'';
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

export { toggleSessExpand, refreshSessions, toggleSubagents, drawSessions, editTag };
