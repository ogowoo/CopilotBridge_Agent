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
let tokenPrompting=null;   // 并发 401 只弹一次：每秒轮询可能同时发起多个请求
async function api(u,o={}){
  if(DEMO)return demoApi(u,o);
  const call=tk=>{const opt=Object.assign({},o);opt.headers=Object.assign({},o.headers||{});
    if(tk)opt.headers['X-CBP-Token']=tk;return fetch(u,opt)};
  let r=await call(apiToken());
  if(r.status===401){
    // 服务端配置了 api_token：就地询问一次并记住（仅本机浏览器 localStorage）。
    // 复用同一个 promise，避免轮询期间连弹多次、把用户正在看的确认框顶掉。
    if(!tokenPrompting){
      tokenPrompting=(async()=>{
        try{
          const tk=await dlgPrompt('本服务启用了 api_token，请输入访问令牌（只保存在本机浏览器）',apiToken(),'需要访问令牌');
          if(tk){try{localStorage.setItem('cbp-token',tk)}catch(e){}}
          return tk;
        }finally{tokenPrompting=null}
      })();
    }
    const tk=await tokenPrompting;
    if(tk)r=await call(tk);
  }
  let t=await r.text();if(!r.ok)throw Error(t);return t?JSON.parse(t):{}}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function jv(v,key){
 if(v===null||v===undefined)return '<span class="nl">null</span>';
 if(typeof v==='boolean')return '<span class="bl">'+v+'</span>';
 if(typeof v==='number')return '<span class="nm">'+v+'</span>';
 if(typeof v==='string'){
  if(key==='content_b64')return '<span class="nl">[Base64 内容，共 '+v.length+' 字符]</span>';
  let t=v.length>4000?v.slice(0,4000)+'\n…[共 '+v.length+' 字符，已截断]':v;
  return '<span class="st">"'+esc(t)+'"</span>'}
 if(Array.isArray(v)){if(!v.length)return '[]';
  return '[<div class="ind">'+v.map(x=>'<div>'+jv(x)+'</div>').join('')+'</div><span class="nl">]</span>'}
 if(typeof v==='object'){let ks=Object.keys(v);if(!ks.length)return '{}';
  return '{<div class="ind">'+ks.map(k=>'<div><span class="ky">'+esc(k)+'</span>: '+jv(v[k],k)+'</div>').join('')+'</div><span class="nl">}</span>'}
 return esc(String(v))}

/* ── 状态呈现 ── */
const STATE_TEXT={running:'运行中',paused:'已暂停',waiting:'等待服务',ready:'就绪',completed:'已完成',failed:'失败',cancelled:'已取消',expired:'已过期',archived:'已归档'};
function stateClass(s){return s==='running'?'run':(s==='paused'||s==='waiting')?'warn':(s==='failed')?'bad':(s==='completed')?'done':''}
function setPill(s){let e=document.getElementById('statepill');e.textContent=STATE_TEXT[s]||s||'—';e.className='pill '+stateClass(s)}
function setKpis(o){
 if(o.token_budget){budget=o.token_budget;document.getElementById('kpiBudget').textContent=' / '+Number(budget).toLocaleString()}
 document.getElementById('kpiState').textContent=STATE_TEXT[o.state||o.session_state]||o.state||o.session_state||'—';
 document.getElementById('kpiSteps').textContent=o.total_steps??0;
 document.getElementById('kpiConv').textContent=o.conversation_steps??0;
 document.getElementById('kpiRoll').textContent=o.conversation_index??0;
 let tk=o.estimated_tokens??0;
 document.getElementById('kpiToken').textContent=Number(tk).toLocaleString();
 document.getElementById('tokenbar').style.width=(budget?Math.min(100,tk/budget*100):0)+'%'}

/* ── Session 列表（支持筛选） ── */
/* ── 模型列表：从 /v1/models 动态加载，与后端（含 config.models 扩展）保持一致 ── */
async function loadModels(){
 if(DEMO){modelCache=['copilot-auto','copilot-think-deeper','copilot-gpt-5-6-sol-think-deeper'];
  fillModelSelect(document.getElementById('sessModel'),modelCache.map(id=>({id})),currentModel);return}
 try{let d=await api('/v1/models'),sel=document.getElementById('model');
  if(!d.data||!d.data.length)return;
  // 只显示可用的：未配 API Key 的提供商模型、菜单扫描没见到的 Copilot 模型不显示
  let avail=d.data.filter(m=>m.available!==false);
  if(!avail.length)avail=d.data;  // 兜底：全不可用时不过滤，防止下拉为空
  modelCache=avail.map(m=>m.id);
  fillModelSelect(document.getElementById('sessModel'), avail, document.getElementById('sessModel').value);
  let prev=sel.value;
  sel.innerHTML='';
  avail.forEach(m=>{let o=document.createElement('option');o.value=m.id;o.textContent=m.id;if(m.description)o.title=m.description;sel.appendChild(o)});
  sel.value=avail.some(m=>m.id===prev)?prev:avail[0].id;
  refreshSessModelOptions()}catch(e){}
}
/* 填充模型下拉；keep 为当前会话的模型（不在可用列表里也要保留并标注） */
function fillModelSelect(sel, avail, keep){
 if(!sel)return;
 let prev=keep||sel.value||'';
 let seen=avail.map(m=>m.id);
 sel.innerHTML='';
 avail.forEach(m=>{let o=document.createElement('option');o.value=m.id;o.textContent=m.id;if(m.description)o.title=m.description;sel.appendChild(o)});
 if(prev&&!seen.includes(prev)){
  let o=document.createElement('option');o.value=prev;o.textContent=prev+'（菜单里未找到）';sel.appendChild(o)}}
/* 会话页模型选择器：显示当前会话自己的模型 */
function refreshSessModelOptions(){
 let sel=document.getElementById('sessModel');if(!sel)return;
 let want=currentModel||'';
 if(!want)return;
 let has=[...sel.options].some(o=>o.value===want);
 if(!has){let o=document.createElement('option');o.value=want;o.textContent=want+'（菜单里未找到）';sel.appendChild(o)}
 sel.value=want;
 sel.title='当前会话模型：'+want+'（下一回合生效，服务重启后仍沿用）'}	
async function setSessionModel(model){
 if(!current||!model)return;
 try{
  let d=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/model`,
                  {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model})});
  currentModel=d.model||model;
  toast('会话模型已切换为 '+(d.model||model)+(d.note?'（'+d.note+'）':'')+'，下一回合生效', d.note?'':'ok')
 }catch(e){toast('切换模型失败: '+e,'err');refreshSessModelOptions()}}
async function refreshModels(){
 if(DEMO){toast('演示模式（API 未连接）：无法刷新模型列表','err');return}
 let btn=document.getElementById('refreshModelsBtn');
 if(btn){btn.disabled=true;btn.textContent='…'}
 try{
  let d=await api('/v1/models/refresh',{method:'POST'});
  await loadModels();
  if(!d.scanned){toast('模型菜单未能扫描：'+(d.reason||'未知原因'),'err');return}
  let n=(d.labels||[]).length;
  if(d.added&&d.added.length)toast('发现新模型: '+d.added.join(', '),'ok');
  else toast('已重新扫描（'+n+' 个菜单项，无新增）','');
 }catch(e){toast('模型菜单读取失败: '+e,'err')}
 finally{if(btn){btn.disabled=false;btn.textContent='↻'}}}

/* ── 浏览器窗口显示/隐藏（headless 是启动参数，切换会带登录态重启浏览器上下文） ── */
function drawBrowserBtn(h){let b=document.getElementById('browserBtn');b.textContent='▣';b.title=h?'当前为无头模式（窗口隐藏），点击显示自动化窗口':'当前显示自动化窗口，点击切换为无头模式（隐藏窗口）'}
async function browserState(){if(DEMO)return;try{let d=await api('/v1/agent/browser');drawBrowserBtn(d.headless)}catch(e){}}
async function toggleBrowser(){
 if(DEMO)return;
 let b=document.getElementById('browserBtn');b.disabled=true;b.textContent='切换中…';
 try{let cur=await api('/v1/agent/browser');
  let d=await api('/v1/agent/browser',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({headless:!cur.headless})});
  drawBrowserBtn(d.headless);
  if(d.fallback)toast('无头模式启动失败，已回退为有窗口模式（旧版 Edge 或安全策略不支持无头持久化）','err')}catch(e){toast('切换失败: '+e,'err');browserState()}
 finally{b.disabled=false}
}

async function refreshSessions(){try{let d=await api('/v1/agent/sessions');sessCache=d.sessions||[];drawSessions()}catch(e){}}
/* 列表签名：内容没变就不重建 DOM，避免每秒滚动回顶/点击错位 */
let sessSig='';
function drawSessions(){
 let q=(document.getElementById('sessFilter').value||'').toLowerCase();
 let box=document.getElementById('sessions');
 let list=sessCache.filter(s=>!q||(s.title||'').toLowerCase().includes(q)||(s.id||'').toLowerCase().includes(q)||(s.state||'').includes(q));
 let sig=q+'|'+list.map(s=>s.id+s.state+s.total_steps+s.estimated_tokens).join('|')+'|'+current;
 if(sig===sessSig)return;
 sessSig=sig;
 box.innerHTML='';
 if(!list.length){box.innerHTML='<div class="sess-empty">没有匹配的 Session</div>';return}
 list.forEach(s=>{let e=document.createElement('div');e.className='sess'+(s.id===current?' sel':'');
  // 会话标识必须显示：标题可能相似（甚至被 Copilot 改名），删错代价很大
  e.innerHTML=`<span class="dot ${stateClass(s.state)}"></span><div class="smeta"><b>${esc(s.title)}</b><small><code class="sid" title="点击复制会话 ID">${esc(s.id)}</code><br>步骤 ${s.total_steps??0} · 会话 ${(s.conversation_index??0)+1} · Token ${s.estimated_tokens??0}</small></div><span class="stag">${esc(STATE_TEXT[s.state]||s.state)}</span>`;
  let chip=e.querySelector('.sid');
  if(chip)chip.onclick=ev=>{ev.stopPropagation();try{navigator.clipboard.writeText(s.id);chip.textContent='已复制 ✓';setTimeout(()=>chip.textContent=s.id,900)}catch(_){}};
  e.onclick=()=>load(s.id);box.appendChild(e)})}

/* ── 时间线：增量渲染，不重建已有 DOM，展开状态不丢失 ── */
function renderSteps(steps){
 let box=document.getElementById('steps');
 document.getElementById('stepCount').textContent=steps.length?steps.length+' 条':'';
 if(steps.length<renderedSteps){box.innerHTML='';renderedSteps=0}
 steps.slice(renderedSteps).forEach(s=>{
  let cls=s.status==='ok'?'ok':(s.status==='error'||s.status==='invalid_action'?'error':'info');
  let e=document.createElement('div');e.className='step '+cls;
  let tm=s._time?new Date(s._time*1000).toLocaleTimeString('zh-CN',{hour12:false}):'';
  e.innerHTML=`<div class="head"><b>步骤 ${esc(s.step??'-')} · ${esc(s.tool||s.status||'event')}</b><span class="schip">${esc(s.status||'')}</span>${tm?`<span class="schip" style="border-style:dashed">${tm}</span>`:''}</div><div class="why">${esc(s.reason||s.error||'')}</div>${s.result?`<details><summary>查看结果</summary><div class="jsonview">${jv(s.result)}</div></details>`:''}`;
  box.prepend(e)});
 renderedSteps=steps.length}

function renderMessages(messages){
 let box=document.getElementById('chatlog');box.innerHTML='';
 if(!messages.length){box.innerHTML='<div class="meta">当前没有人工续聊记录。下面发送的消息会进入该 Session 对应的 Copilot 会话。</div>';return}
 messages.forEach(m=>{let e=document.createElement('div');e.className='msg '+m.role;
  e.innerHTML=`<b>${m.role==='user'?'你':'Copilot'}</b>${esc(m.content)}`;box.appendChild(e)});
 box.scrollTop=box.scrollHeight}

/* 导出完整时间线：调试/复盘用，含每步原始参数、结果与 invalid_action 的 raw_tail */
async function exportTimeline(){
 if(!current){toast('请先选择 Session','err');return}
 try{
  let d=await api('/v1/agent/sessions/'+encodeURIComponent(current));
  let payload={exported_at:new Date().toISOString(),app:'CopilotBridge Agent',
               api:'/v1/agent/sessions/'+encodeURIComponent(current),
               session:d.session||{},steps:d.steps||[],messages:d.messages||[],files:d.files||[]};
  let blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'});
  let a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='cbp-'+current+'-timeline-'+new Date().toISOString().slice(0,19).replace(/[:T]/g,'-')+'.json';
  document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),3000);
  toast('时间线已导出（'+((payload.steps||[]).length)+' 步）','ok')}catch(e){toast('导出失败: '+e,'err')}}

/* ── 页面内通知（toast）与通用模态层（替代原生 alert/confirm/prompt） ── */
function toast(msg,type=''){let box=document.getElementById('toasts'),e=document.createElement('div');
 e.className='toast '+type;e.textContent=msg;box.appendChild(e);
 setTimeout(()=>{e.style.opacity='0';e.style.transition='opacity .3s';setTimeout(()=>e.remove(),320)},type==='err'?5000:2600)}
let _gmResolve=null;
function gmShow(o){if(_gmResolve){const prev=_gmResolve;_gmResolve=null;prev(null)}  // 重入时先了结上一个，防 Promise 悬挂
 return new Promise(res=>{
 _gmResolve=res;
 document.getElementById('gmTitle').textContent=o.title||'提示';
 let m=document.getElementById('gmMsg');m.textContent=o.msg||'';m.style.display=o.msg?'':'none';
 let inp=document.getElementById('gmInput');
 if(o.input!==undefined&&o.input!==null){inp.style.display='';inp.value=o.input}else{inp.style.display='none'}
 let c=document.getElementById('gmCancel');c.style.display=o.cancelText===null?'none':'';c.textContent=o.cancelText||'取消';
 let ok=document.getElementById('gmOk');ok.textContent=o.okText||'确定';ok.className='btn '+(o.danger?'danger':'accent');
 document.getElementById('gmMask').classList.add('open');
 if(inp.style.display!=='none')setTimeout(()=>{inp.focus();inp.select()},60)})}
function gmClose(v){document.getElementById('gmMask').classList.remove('open');if(_gmResolve){_gmResolve(v);_gmResolve=null}}
document.getElementById('gmOk').onclick=()=>{let inp=document.getElementById('gmInput');gmClose(inp.style.display==='none'?true:inp.value)};
document.getElementById('gmCancel').onclick=()=>gmClose(null);
document.getElementById('gmInput').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();document.getElementById('gmOk').click()}});
const dlgAlert=(msg,title='提示')=>gmShow({title,msg,cancelText:null});
const dlgConfirm=(msg,title='确认',danger=false)=>gmShow({title,msg,danger}).then(v=>v===true);
const dlgPrompt=(msg,def='',title='输入')=>gmShow({title,msg,input:def});

/* ── 跟进消息模式切换（卡片头部按钮，不再占用输入区） ── */
let followupMode='agent';
function setFollowupMode(m){followupMode=m;
 document.getElementById('modeAgent').className='btn seg'+(m==='agent'?' on':'');
 document.getElementById('modeChat').className='btn seg'+(m==='chat'?' on':'')}

/* ── 视图重置：发起新任务时清空右侧残留的旧 Session 内容 ── */
function resetView(){
 document.getElementById('title').textContent='—';
 document.getElementById('status').textContent='—';
 let _sc=document.getElementById('sidChip'); if(_sc)_sc.textContent='';
 setPill('');
 setKpis({total_steps:0,conversation_steps:0,conversation_index:0,estimated_tokens:0});
 document.getElementById('steps').innerHTML='';renderedSteps=0;lastStepCount=-1;lastDetail='';
 document.getElementById('answer').textContent='等待回答…';
 document.getElementById('prompt').textContent='—';
 renderMessages([])}

async function load(id){
 current=id;let d=await api('/v1/agent/sessions/'+encodeURIComponent(id)),s=d.session;
 if(d.token_budget)setKpis({token_budget:d.token_budget,estimated_tokens:s.estimated_tokens??0});
 currentModel=s.model||null;currentUrl=s.current_url;renderMessages(d.messages||[]);
 // 跟进消息应跟随 Session 自己的模型，而不是左侧新任务表单的选择
 let _ms=document.getElementById('model');
 if(currentModel&&[..._ms.options].some(o=>o.value===currentModel))_ms.value=currentModel;
 refreshSessModelOptions();   // 会话页选择器显示该会话自己的模型
 document.getElementById('title').textContent=s.title;
 let _sc=document.getElementById('sidChip');
 if(_sc){_sc.textContent=s.id;_sc.onclick=()=>{try{navigator.clipboard.writeText(s.id);toast('已复制会话 ID: '+s.id,'ok')}catch(e){toast(s.id)}}}
 document.getElementById('status').textContent=(STATE_TEXT[s.state]||s.state)+' · '+s.workspace;
 setPill(s.state);setKpis(s);
 document.getElementById('steps').innerHTML='';renderedSteps=0;lastDetail='';
 renderSteps(d.steps);lastStepCount=d.steps.length;
 document.getElementById('emptyState').style.display='none';
 document.getElementById('sessionView').style.display='';
 if(current!==id)return;   // 等待期间已切换会话：丢弃迟到的详情
 await runtime();await refreshSessions();refreshQueue()}

/* 轮询在飞保护 + 会话切换竞态防护：
   延迟>1s 时不再堆叠请求；await 返回时若已切到别的会话，丢弃迟到数据 */
let runtimeInflight=false;
async function runtime(){
 if(!current||runtimeInflight)return;
 runtimeInflight=true;
 const myId=current;
 try{let r=await api(`/v1/agent/sessions/${encodeURIComponent(myId)}/runtime`);
  if(current!==myId)return;  // 等待期间用户已切换会话
  currentUrl=r.current_url||currentUrl;
  document.getElementById('pulse').className=r.busy?'pulse':'dot-off';
  document.getElementById('activity').textContent=`阶段: ${r.phase||r.session_state}\n${r.detail||''}\n总步骤: ${r.total_steps}\n当前会话步骤: ${r.conversation_steps}\nToken估算: ${r.estimated_tokens}\n会话序号: ${(r.conversation_index??0)+1}\n${r.queue_pending?`队列: ${r.queue_pending} 条待处理\n`:''}最后更新: ${r.updated?new Date(r.updated*1000).toLocaleTimeString():''}${r.model_warning?`\n\n⚠ ${r.model_warning}`:''}`;
  let _ans=r.answer_preview||'等待 Copilot 回答…',_ae=document.getElementById('answer'),_pj=null;
  try{_pj=JSON.parse(_ans)}catch(e){}
  if(_pj&&typeof _pj==='object'){_ae.innerHTML='<div class="jsonview">'+jv(_pj)+'</div>'}else{_ae.textContent=_ans}
  if(_ans!==lastAns){lastAns=_ans;if(liveTabName!=='answer')document.getElementById('tabAnswer').classList.add('new')}
  document.getElementById('prompt').textContent=r.prompt_preview||'';
  setPill(r.session_state);setKpis(r);
  updateRetryCountdown(r);
  if(r.total_steps!==lastTotal||(r.detail||'')!==lastDetail){
   lastDetail=r.detail||'';
   lastTotal=r.total_steps??lastTotal;
   let d=await api('/v1/agent/sessions/'+encodeURIComponent(myId));
   if(current!==myId)return;
   renderSteps(d.steps);lastStepCount=d.steps.length}
 }catch(e){}
 finally{runtimeInflight=false}}

/* ── 操作 ── */
async function control(a){if(!current)return;try{let r=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/actions/${a}`,{method:'POST'});if(a==='resume')toast(r.runner_started?'已恢复，正在续跑任务':'已恢复');await load(current)}catch(e){toast('操作失败: '+e,'err')}}
async function activate(){if(!current)return;try{let d=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/activate`,{method:'POST'});currentUrl=d.url;toast('已将自动化 Edge 切换到该会话','ok')}catch(e){toast('操作失败: '+e,'err')}}
function openCopilot(){if(currentUrl)window.open(currentUrl,'_blank');else toast('该 Session 暂无 Copilot URL','err')}
async function removeSession(remote){
 if(!current)return;
 let extra=remote?'，以及该任务关联的所有 Copilot 网页会话（标题里可能已没有 [CBP-AGENT] 标记，删除时会用会话正文核对归属）':'';
 let msg=`即将删除的会话：\n\n  ID: ${current}\n  标题: ${(document.getElementById('title').textContent||'').trim()}\n\n仅删除本地 Session${extra}。此操作不可撤销，确定继续？`;
 if(!await dlgConfirm(msg,'删除确认',true))return;
 try{let url='/v1/agent/sessions/'+encodeURIComponent(current)+(remote?'?delete_remote=true':'');
  let d=await api(url,{method:'DELETE'});
  if(!d.deleted){showDeleteResult(d);return}
  current=null;
  document.getElementById('sessionView').style.display='none';
  document.getElementById('emptyState').style.display='flex';
  await refreshSessions()}catch(e){toast('删除失败: '+e,'err')}}
/* ── Copilot 限流倒计时浮窗 ── */
function fmtCountdown(total){
 total=Math.max(0,Math.ceil(total/1000));
 let h=Math.floor(total/3600),m=Math.floor((total%3600)/60),s=total%60;
 return h>0?`${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`
            :`${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`}
function updateRetryCountdown(r){
 let box=document.getElementById('retryCountdown');if(!box)return;
 let resumeAt=r&&r.retry_resume_at;
 if(!resumeAt||resumeAt*1000<=Date.now()){box.style.display='none';box.removeAttribute('data-resume');return}
 box.style.display='block';
 box.dataset.resume=resumeAt;
 box.querySelector('.rc-count').textContent=fmtCountdown(resumeAt*1000-Date.now());
 let sub=box.querySelector('.rc-sub');
 sub.textContent=`第 ${r.retry_attempt||1} 次 · 自动重试中，可点「暂停」稍后继续`;
}
/* 每秒把浮窗倒数刷新一次（与 runtime 轮询解耦，数字平滑跳动） */
setInterval(()=>{
 let box=document.getElementById('retryCountdown');
 if(!box||box.style.display==='none'||!box.dataset.resume)return;
 let ms=parseFloat(box.dataset.resume)*1000-Date.now();
 if(ms<=0){box.style.display='none';box.removeAttribute('data-resume');return}
 box.querySelector('.rc-count').textContent=fmtCountdown(ms);
},1000);

/* ── 回滚任务改动：按 Copilot 回合的版本回滚工作区 ── */
async function rollbackSession(){
 if(!current){toast('请先选择 Session','err');return}
 try{
  let s=await api('/v1/agent/sessions/'+encodeURIComponent(current)+'/snapshots');
  let files=s.files||[];
  if(!files.length){toast('该任务没有可回滚的文件改动','err');return}
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
    return `  · 回合 ${r}（${t}）：${paths}`;
   }).join('\n');
   let input=await dlgPrompt(`该任务共 ${revs.length} 个回合版本（按时间先后）：\n\n${lines}\n\n输入「0」或留空 = 整任务回滚到开始前；输入版本号 = 撤销该回合之后的改动。`,`0`,'回滚到哪个版本？');
   if(input===null)return;
   version=parseInt(input,10);
   if(isNaN(version))version=0;
   version=version>0?version:null;
  }
  let preview=files.slice(0,12).map(f=>`· [${f.kind}] ${f.path}`).join('\n');
  let more=files.length>12?`\n… 另有 ${files.length-12} 个`:'';
  let target=version?`回滚到版本 ${version}（撤销该回合之后的改动）`:`整任务回滚到开始前`;
  let msg=`${target}：${files.length} 个路径。\n修改过的还原、任务新建的删除。\n\n${preview}${more}\n\n此操作不可撤销，确定继续？`;
  if(!await dlgConfirm(msg,'回滚任务改动',true))return;
  let body=version?{version}:{};
  let d=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/rollback`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let tail=d.failed?`，失败 ${d.failed}`:'';
  toast(`回滚完成：还原 ${d.restored}、删除 ${d.deleted}${tail}`, d.failed?'err':'ok');
  await load(current)
 }catch(e){toast('回滚失败: '+e,'err')}}
/* ── 回滚服务自升级：还原 CopilotBridge 自身代码（需重启服务生效） ── */
async function rollbackSelfUpgrade(){
 try{
  let st=await api('/v1/agent/self/status');
  if(!st.snapshot_scopes||!st.snapshot_scopes.length){toast('没有可回滚的自升级记录','err');return}
  let list=[];
  for(const scope of st.snapshot_scopes.slice(0,5)){
   try{let s=await api('/v1/agent/self/snapshots?sid='+encodeURIComponent(scope.replace(/^_self_/,'')));list.push(`${scope}：${(s.files||[]).length} 个文件`)}catch(e){}
  }
  let msg=`将把服务自身代码恢复到自升级前的状态（最近一次）：\n\n${list.join('\n')}\n\n回滚后必须重启服务才生效。确定继续？`;
  if(!await dlgConfirm(msg,'回滚服务自升级',true))return;
  let d=await api('/v1/agent/self/rollback?last=true',{method:'POST'});
  toast(`自升级已回滚：还原 ${d.restored||0}、删除 ${d.deleted||0}${d.failed?`，失败 ${d.failed}`:''} · 请重启服务`, d.failed?'err':'ok');
 }catch(e){toast('自升级回滚失败: '+e,'err')}}
/* ── 删除结果可视化弹窗 ── */
let lastDeleteResult=null;
function showDeleteResult(d){
 lastDeleteResult=d;
 let list=d.remote_results||[],ok=list.filter(x=>x.deleted).length,fail=list.length-ok;
 document.getElementById('dlgHint').textContent=list.length?`共 ${list.length} 个 Copilot 会话 · 成功 ${ok} · 失败 ${fail}`:'';
 let msg=document.getElementById('dlgMsg');msg.style.display=d.message?'':'none';msg.textContent=d.message||'';
 let box=document.getElementById('dlgList');box.innerHTML='';
 if(!list.length)box.innerHTML='<div class="dmsg">没有远程删除明细。</div>';
 list.forEach(r=>{let e=document.createElement('div');e.className='dres '+(r.deleted?'ok':'fail');
  e.innerHTML=`<div class="rhead"><span class="ric">${r.deleted?'✓':'✗'}</span><span class="ttl" title="${esc(r.expected_title||'')}">${esc(r.expected_title||'(无标记)')}</span><span class="mth">${esc(r.method||'')}</span></div>`+
   (r.url?`<div class="url" title="点击复制完整 URL">${esc(r.url)}</div>`:'')+
   (r.error?`<div class="err">${esc(r.error)}</div>`:'');
  if(r.url)e.querySelector('.url').onclick=ev=>{try{navigator.clipboard.writeText(r.url);ev.target.textContent='已复制 ✓  '+r.url}catch(_){dlgAlert(r.url,'链接')}};
  box.appendChild(e)});
 document.getElementById('dlgForce').style.display=fail?'':'none';
 document.getElementById('dlgMask').classList.add('open')}
function closeDlg(){document.getElementById('dlgMask').classList.remove('open')}
document.getElementById('dlgCopy').onclick=()=>{if(!lastDeleteResult)return;try{navigator.clipboard.writeText(JSON.stringify(lastDeleteResult,null,2))}catch(e){}};
document.getElementById('dlgForce').onclick=()=>forceDeleteLocal();
async function forceDeleteLocal(){
 if(!current)return;let b=document.getElementById('dlgForce');b.disabled=true;b.textContent='处理中…';
 try{let d=await api('/v1/agent/sessions/'+encodeURIComponent(current)+'?delete_remote=true&force_local=true',{method:'DELETE'});
  if(d.deleted){closeDlg();current=null;document.getElementById('sessionView').style.display='none';document.getElementById('emptyState').style.display='flex';await refreshSessions()}
  else showDeleteResult(d)}catch(e){toast('强制清理失败: '+e,'err')}
 finally{b.disabled=false;b.textContent='重试远程并强制清理本地记录'}}
/* ── 使用手册 ── */
const MANUAL_HTML=`<h3>快速上手</h3>
<ul>
<li>左侧「新任务」：填任务内容，选 <b>Agent 执行</b>（可读写工作区文件、自动验证）或 <b>普通对话</b>（纯问答），点「启动任务」。</li>
<li>每个任务对应一个 Session，背后是一个独立的 Copilot 网页会话；左侧列表点选查看。</li>
<li>自动化浏览器窗口可用 <b>▣</b> 按钮显示/隐藏（无头切换会重启浏览器但保留登录态）。</li>
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
（默认窗口 3660 秒 = 1 小时 + 1 分钟余量，可用配置调整），避免反复手动重试。</p>
<p>倒计时结束后服务会自动继续；你也可以在此期间暂停或取消任务。</p>

<h3>长任务机制</h3>
<ul>
<li>上下文超阈值自动压缩；单会话过长自动换会话接力（检查点摘要带过去）。</li>
<li>Copilot 限流时状态变为「等待服务」并指数退避自动重试；对话被 Copilot 判满时自动换会话重发。</li>
<li>服务重启后自动恢复被中断的任务（auto_resume）。</li>
</ul>
<h3>批量动作（省 Copilot 回合）</h3>
<p>模型可一次返回多个动作：<code>{"type":"actions","actions":[动作1,动作2,...]}</code>，本地按顺序执行，每个动作仍单独记一步。</p>
<ul><li>上限由 <code>max_actions_per_turn</code> 控制（默认 4，硬顶 12）。</li>
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

<h3>常见问题</h3>
<ul>
<li><b>服务重启后短暂停在登录页</b>：企业 SSO 会经过公司 ADFS 自动回跳，约需 40 秒，一般无需手动登录；若停在账户选择页，手动点一下即可。</li>
<li><b>提示找不到输入框/模型按钮</b>：登录态失效，运行 <code>python login.py</code> 重新登录（或在有窗口模式下直接登录）。</li>
<li><b>任务显示等待服务</b>：Copilot 在限流，等它自动恢复；持续不可用会安全暂停，恢复后点「恢复」。</li>
</ul>`;
function openManual(){document.getElementById('manualBody').innerHTML=MANUAL_HTML;document.getElementById('manualMask').classList.add('open')}
function closeManual(){document.getElementById('manualMask').classList.remove('open')}
function openTask(){document.getElementById('taskMask').classList.add('open');setTimeout(()=>document.getElementById('task').focus(),60)}
function closeTask(){document.getElementById('taskMask').classList.remove('open')}

document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeDlg();closeLive();gmClose(null);closeManual();closeTask()}});

/* ── 实时面板选项卡 + 放大查看 ── */
const LIVE_NAMES={activity:'当前执行状态',answer:'Copilot 当前回答',prompt:'最近发送给 Copilot 的内容',chat:'继续对话'};
const LIVE_TAB={activity:'tabActivity',answer:'tabAnswer',prompt:'tabPrompt',chat:'tabChat'};
let liveTabName='activity';
let lastAns='';
function liveTab(n){liveTabName=n;
 for(const k in LIVE_NAMES){document.getElementById(k).style.display=k===n?'':'none';
  document.getElementById(LIVE_TAB[k]).className='tab'+(k===n?' on':'')}}  // className 覆盖即清除新内容圆点
function expandLive(){
 document.getElementById('liveTitle').textContent=LIVE_NAMES[liveTabName];
 // 对话页只复制聊天记录，避免把输入框（含重复 ID）一并搬进弹窗
 document.getElementById('liveBody').innerHTML=liveTabName==='chat'
  ?document.getElementById('chatlog').innerHTML
  :document.getElementById(liveTabName).innerHTML;
 document.getElementById('liveMask').classList.add('open')}
function closeLive(){document.getElementById('liveMask').classList.remove('open')}
document.getElementById('liveCopy').onclick=()=>{try{navigator.clipboard.writeText(document.getElementById('liveBody').textContent||'')}catch(e){}};
async function continueAgent(){
 if(!current){toast('请先选择 Session','err');return}
 let instruction=await dlgPrompt('请输入要继续执行的任务','继续执行当前任务，从未完成事项开始','继续执行');
 if(!instruction)return;
 document.getElementById('followup').value=instruction;
 setFollowupMode('agent');
 await sendFollowup()}
async function sendFollowup(){
 if(!current){toast('请先选择 Session','err');return}
 let box=document.getElementById('followup'),content=box.value.trim();
 if(!content&&!IMG_STORES.follow.length){toast('消息不能为空','err');return}
 let atts=IMG_STORES.follow.map(a=>a.dataUrl);
 let btn=document.getElementById('sendFollowup');btn.disabled=true;btn.textContent='发送中';
 liveTab('chat');  // 发送后确保停留在对话页看回复
 let log=document.getElementById('chatlog'),mine=document.createElement('div');
 let label=content+(atts.length?`\n（附 ${atts.length} 张图片）`:'');
 mine.className='msg user';mine.innerHTML=`<b>你</b>${esc(label)}`;
 log.appendChild(mine);log.scrollTop=log.scrollHeight;box.value='';
 IMG_STORES.follow=[];renderThumbs();
 let baseAssistants=0;
 try{let dd=await api('/v1/agent/sessions/'+encodeURIComponent(current));baseAssistants=dd.messages.filter(m=>m.role==='assistant').length}catch(e){}
 try{let d=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/messages`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content,attachments:atts,model:currentModel||document.getElementById('model').value,agent_mode:followupMode==='agent'})});
  if(d&&d.queued){
   let note=document.createElement('div');
   note.className='msg assistant';
   note.innerHTML=`<b>队列</b>${esc(d.position>1?`已排队（前面还有 ${d.position-1} 条）`:'已排队，等待处理…')}`;
   log.appendChild(note);log.scrollTop=log.scrollHeight;
   refreshQueue();
   awaitFollowupReply(current,baseAssistants);
  }else{
   // 兜底：兼容某些直接返回内容的后端
   let reply=document.createElement('div');reply.className='msg assistant';
   reply.innerHTML=`<b>Copilot</b>${esc(d.content)}`;
   log.appendChild(reply);log.scrollTop=log.scrollHeight;
   currentUrl=d.url||currentUrl;await load(current)}
 }catch(e){let er=document.createElement('div');er.className='msg assistant';er.textContent='发送失败: '+e;log.appendChild(er)}
 finally{btn.disabled=false;btn.textContent='发送'}}

/* 队列模式：跟进消息入队后轮询会话消息，助理回复一出现就刷新聊天区 */
async function awaitFollowupReply(sid,baseAssistants){
 for(let i=0;i<300;i++){
  await new Promise(r=>setTimeout(r,1000));
  if(current!==sid)return;                     // 用户切走了就停
  try{
   let d=await api('/v1/agent/sessions/'+encodeURIComponent(sid));
   let as=d.messages.filter(m=>m.role==='assistant');
   if(as.length>baseAssistants){renderMessages(d.messages);return}
  }catch(e){}
 }}

/* ── 排队消息：查看 / 编辑 / 删除 ── */
async function refreshQueue(){
 let box=document.getElementById('queueBox');if(!box)return;
 if(!current){box.innerHTML='';return}
 try{
  let d=await api('/v1/agent/sessions/'+encodeURIComponent(current)+'/queue');
  // 正在处理的那条不算"排队"：它已经在跑，进度由运行状态面板显示，而且
  // 后端只允许改删"等待中"的条目（处理中的会 404）。这里只列等待项。
  let items=(d.pending||[]).filter(it=>!it.processing);
  if(!items.length){box.innerHTML='';return}
  box.innerHTML=items.map(it=>`<div class="qitem"><span class="qpos">#${it.position}</span><span class="qtext" title="${esc(it.content)}">${esc(it.content)}</span><span class="qmode">${it.mode==='agent'?'Agent':'对话'}</span><button title="编辑" onclick="editQueueItem('${it.id}')">✎</button><button class="qdel" title="删除" onclick="removeQueueItem('${it.id}')">×</button></div>`).join('');
 }catch(e){box.innerHTML=''}}

async function editQueueItem(itemId){
 if(!current)return;
 let d=await api('/v1/agent/sessions/'+encodeURIComponent(current)+'/queue');
 let it=(d.pending||[]).find(x=>x.id===itemId);if(!it)return;
 let content=await dlgPrompt('修改排队消息','',it.content,'保存');
 if(content===null)return;
 try{
  await api(`/v1/agent/sessions/${encodeURIComponent(current)}/queue/${itemId}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content})});
  toast('已更新排队消息','ok');refreshQueue()}catch(e){toast('更新失败: '+e,'err')}}

async function removeQueueItem(itemId){
 if(!current)return;
 if(!await dlgConfirm('删除这条排队中的消息？此操作不可撤销。','删除排队消息',true))return;
 try{
  await api(`/v1/agent/sessions/${encodeURIComponent(current)}/queue/${itemId}`,{method:'DELETE'});
  toast('已删除排队消息','ok');refreshQueue();load(current)}catch(e){toast('删除失败: '+e,'err')}}
/* ── 粘贴图片/截图附件 ── */
const IMG_STORES={task:[],follow:[]};

function thumbsHtml(arr,store){
 return arr.map((a,i)=>`<span class="thumb"><img src="${a.dataUrl}" alt=""><button class="rm" title="移除" onclick="removeAttachment('${store}',${i})">×</button></span>`).join('')}
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
    if(f.size>8*1024*1024){toast('图片超过 8MB，已忽略','err');continue}
    if(IMG_STORES[store].length>=4){toast('最多附加 4 张图片','err');continue}
    let r=new FileReader();
    r.onload=()=>{IMG_STORES[store].push({dataUrl:r.result});renderThumbs()};
    r.readAsDataURL(f);
   }
  }
 })}

document.addEventListener('keydown',e=>{if(e.target&&e.target.id==='followup'&&e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();sendFollowup()}});
/* ── 新任务模式：Agent 执行 / 普通对话 ── */
let taskMode='agent';
function setTaskMode(m){taskMode=m;
 document.getElementById('ntAgent').className='btn seg'+(m==='agent'?' on':'');
 document.getElementById('ntChat').className='btn seg'+(m==='chat'?' on':'')}

async function runTask(){
 let task=document.getElementById('task').value.trim();if(!task)return;
 let atts=IMG_STORES.task.map(a=>a.dataUrl);
 let b={model:document.getElementById('model').value,messages:[{role:'user',content:task}],stream:false,session_id:document.getElementById('sid').value.trim()||('task-'+Date.now().toString(36)),workspace:document.getElementById('workspace').value.trim()||null,reuse_session:true,agent_mode:taskMode==='agent',attachments:atts};
 current=b.session_id;
 currentModel=b.model;      // 会话页模型选择器随即显示本次任务用的模型
 IMG_STORES.task=[];renderThumbs();
 closeTask();
 document.getElementById('emptyState').style.display='none';
 document.getElementById('sessionView').style.display='';
 resetView();
 // 先用本地已知信息填标题/状态：/v1/agent/completions 会一直挂到整轮任务
 // 结束才返回，等它的回调会让整个任务期间标题都显示"—"
 let title=(task.split('\n')[0]||'').trim().slice(0,120)||current;
 document.getElementById('title').textContent=title;
 let _sc=document.getElementById('sidChip');
 if(_sc){_sc.textContent=current;_sc.onclick=()=>{try{navigator.clipboard.writeText(current);toast('已复制会话 ID: '+current,'ok')}catch(e){toast(current)}}}
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
    document.getElementById('emptyState').style.display='flex';toast('任务启动失败: '+e,'err')});
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

function toggleMore(e){e.stopPropagation();document.getElementById('moreMenu').classList.toggle('open')}
document.addEventListener('click',e=>{let m=document.getElementById('moreMenu');if(m&&m.classList.contains('open')&&!e.target.closest('#moreBtn'))m.classList.remove('open')});

const THEMES=[["","深色"],["light","浅色"],["nord","Nord 蓝"],["solarized","护眼米"],["dracula","Dracula 紫"],["roufen","柔粉"],["mint","薄荷"]];
function toggleTheme(){
 let cur=document.documentElement.dataset.theme||'';
 let i=THEMES.findIndex(x=>x[0]===cur);if(i<0)i=0;
 let n=THEMES[(i+1)%THEMES.length];
 if(n[0]){document.documentElement.dataset.theme=n[0]}else{document.documentElement.removeAttribute('data-theme')}
 try{localStorage.setItem('cbp-theme',n[0]||'dark')}catch(e){}
 document.getElementById('themeBtn').title='主题：'+n[1]+'（点击切换）'}
/* ── 左侧栏折叠（归零只剩边缘拉手） ── */
function toggleRail(){let r=document.documentElement,c=r.dataset.rail==='closed';
 if(c){delete r.dataset.rail}else{r.dataset.rail='closed'}
 document.getElementById('railBtn').textContent=c?'‹':'›';
 try{localStorage.setItem('cbp-rail',c?'':'closed')}catch(e){}}
function railNewTask(){if(document.documentElement.dataset.rail==='closed')toggleRail();openTask()}
try{if(document.documentElement.dataset.rail==="closed")document.getElementById("railBtn").textContent="›"}catch(e){}
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
 b.title='长期记忆：查看 / 检索 / 置顶 / 删除（点击打开面板）';
 // 放到顶部工具栏：优先贴着手册按钮，其次常见工具栏容器，最后才退回 body
 // 与「使用手册 / 主题」按钮同一行：左侧栏 .listhead 内（排在手册按钮后面）
 const bar=document.querySelector('.listhead');
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
   <div class="chead" style="border-bottom:1px solid var(--line)">长期记忆
    <span class="hint"><button class="btn ghost" id="memClose" style="padding:2px 10px;font-size:12px">关闭</button></span></div>
   <div style="padding:12px 15px;display:flex;flex-direction:column;gap:8px;min-height:0">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
     <select id="memScopeSel" class="btn ghost" style="padding:4px 8px">
      <option value="all">全部作用域</option><option value="global">仅全局</option><option value="project">仅当前项目</option>
     </select>
     <input id="memSearch" class="btn ghost" style="flex:1;min-width:150px;padding:4px 10px;text-align:left" placeholder="关键词检索…（回车）">
     <button class="btn ghost" id="memReload" style="padding:4px 12px">检索</button>
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
  `共 ${st.total||0} 条 · 置顶 ${st.pinned||0} · 从未命中 ${st.unused||0} · ${JSON.stringify(st.by_kind||{})} · ${(d.scopes||[]).join(', ')}`;
 const box=document.getElementById('memList');
 if(!(d.items||[]).length){
  box.innerHTML='<div class="mem-empty">没有匹配的记忆。Agent 完成任务时会自动沉淀，也可以直接让它「记到长期记忆」。</div>';return}
 box.innerHTML=d.items.map(m=>`<div class="mem-row">
   <div class="mem-meta">
    <span>#${esc(m.id)}</span><span class="mem-kind">${esc(m.kind)}</span><span>${esc(m.scope)}</span>
    <span>命中 ${m.use_count||0}</span>${m.pinned?'<span class="mem-pin">★ 置顶</span>':''}
    ${(m.tags||[]).map(t=>`<span>#${esc(t)}</span>`).join('')}
    <span class="sp"></span>
    <button class="btn ghost" data-pin="${m.id}" data-v="${m.pinned?1:0}" style="padding:2px 10px;font-size:12px">${m.pinned?'取消置顶':'置顶'}</button>
    <button class="btn ghost" data-del="${m.id}" style="padding:2px 10px;font-size:12px;color:var(--danger-text);border-color:var(--danger-border)">删除</button>
   </div>
   <div class="mem-text">${esc(m.text)}</div>
  </div>`).join('');
 box.querySelectorAll('[data-del]').forEach(b=>b.onclick=async()=>{
  if(!await dlgConfirm('删除这条长期记忆？删除后不再参与检索。','删除记忆',true))return;
  try{await api('/v1/agent/memory/'+b.dataset.del+'?scope='+encodeURIComponent(memScope),{method:'DELETE'});
   toast('已删除记忆 #'+b.dataset.del,'ok');loadMemory()}
  catch(e){toast('删除失败: '+e,'err')}});
 box.querySelectorAll('[data-pin]').forEach(b=>b.onclick=async()=>{
  const want=b.dataset.v!=='1';
  try{await api('/v1/agent/memory/'+b.dataset.pin+'/pin?pinned='+want+'&scope='+encodeURIComponent(memScope),{method:'POST'});
   toast(want?'已置顶（检索时恒定注入）':'已取消置顶','ok');loadMemory()}
  catch(e){toast('操作失败: '+e,'err')}});
}
try{memBtn()}catch(e){}
try{const _rs=document.getElementById('btnRefreshSess');if(_rs)_rs.onclick=async()=>{
 if(_rs.disabled)return;_rs.disabled=true;_rs.style.opacity=.5;
 try{await refreshModels();refreshSessModelOptions();toast('模型列表已刷新','ok')}
 catch(e){toast('刷新失败: '+e,'err')}
 finally{_rs.disabled=false;_rs.style.opacity=1}};}catch(e){}



try{let cur=document.documentElement.dataset.theme||'',i=THEMES.findIndex(x=>x[0]===cur);document.getElementById('themeBtn').title='主题：'+THEMES[i<0?0:i][1]+'（点击切换）'}catch(e){}
/* ── 启动：API 不可达时进入演示模式（仅用于界面预览） ── */
document.getElementById('sessModel').onchange=e=>setSessionModel(e.target.value);
attachPasteHandler('task','task');
attachPasteHandler('followup','follow');
(async()=>{try{await api('/v1/agent/sessions')}
 catch(e){let msg=String(e&&e.message||e);
  if(msg.includes('401')){
   // 配置了 api_token：先让 api() 的令牌输入框有机会弹出，再继续加载，
   // 不要在这里 return（否则界面永久空白、连输入令牌的入口都没有）。
   document.getElementById('demoBadge').style.display='block';
   document.getElementById('demoBadge').textContent='需要 api_token，请输入后重试';
  }else{
   DEMO=true;document.getElementById('demoBadge').style.display='block';
  }}
 loadModels();browserState();
 refreshSessions().then(()=>{if(DEMO&&sessCache.length&&!current)load(sessCache[0].id)});
 setInterval(()=>{refreshSessions();runtime();refreshQueue()},1000)})();
