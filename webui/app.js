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
  rs.setProperty('--retry-h', Math.max(h('retryCountdown'), h('retryChip'))+'px');
  rs.setProperty('--toast-h', h('toasts')+'px');
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
let approvalDismissed=null;   // 用户点过「暂不授权」的 key：同一个待授权不再自动弹出
function showApprovalBar(p){
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
  span.textContent='需要授权：'+p.tool+' 属于不可回滚操作；授权仅对这一次执行有效（用后即焚）';
  const btn=document.createElement('button');
  btn.className='btn accent';btn.type='button';btn.textContent='授权执行';
  btn.onclick=async()=>{
   btn.disabled=true;btn.textContent='授权中…';
   try{
    await api(`/v1/agent/sessions/${encodeURIComponent(current)}/approvals`,
              {method:'POST',headers:{'Content-Type':'application/json'},
               body:JSON.stringify({tool:p.tool,args_hash:p.args_hash||''})});
    await api(`/v1/agent/sessions/${encodeURIComponent(current)}/actions/resume`,{method:'POST'});
    toast('已授权并继续执行（该授权仅对这一次操作有效）','ok');
    el.style.display='none';approvalSeen=null;await runtime();
   }catch(e){btn.disabled=false;btn.textContent='授权执行';toast('授权失败: '+(e.text||e),'err')}};
  const no=document.createElement('button');
  no.className='btn ghost';no.type='button';no.textContent='暂不授权';
  no.onclick=()=>{el.style.display='none';approvalDismissed=key};   // 记住这次拒绝
  el.appendChild(span);el.appendChild(btn);el.appendChild(no)}
 el.style.display='flex'}

async function api(u,o={}){
  if(DEMO)return demoApi(u,o);
  const call=tk=>{const opt=Object.assign({},o);opt.headers=Object.assign({},o.headers||{});
    if(tk)opt.headers['X-CBP-Token']=tk;return fetch(u,opt)};
  let r=await call(apiToken());
  if(r.status===401){
    // 服务端配置了 api_token：就地询问一次并记住（仅本机浏览器 localStorage）。
    // 复用同一个 promise，避免轮询期间连弹多次、把用户正在看的确认框顶掉。
    if(tokenPromptCancelled){throw new ApiError(401,'已取消令牌输入','');}
    if(!tokenPrompting){
      tokenPrompting=(async()=>{
        try{
          const tk=await dlgPrompt('本服务启用了 api_token，请输入访问令牌（只保存在本机浏览器）',apiToken(),'','需要访问令牌');
          if(tk){try{localStorage.setItem('cbp-token',tk);tokenPromptCancelled=false}catch(e){}}
          else{tokenPromptCancelled=true;showConnBanner('未授权：已取消令牌输入（点此重新输入）',true)}
          return tk;
        }finally{tokenPrompting=null}
      })();
    }
    const tk=await tokenPrompting;
    if(tk)r=await call(tk);
  }
  let t=await r.text();if(!r.ok)throw new ApiError(r.status,t,u);return t?JSON.parse(t):{}}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
/* ── 通道无关的显示名 ──────────────────────────────────────────────
   服务现在既支持 Copilot 网页，也支持 API 提供商（deepseek/… 等），
   界面文案不该再写死 "Copilot"。 */
const BRAND={deepseek:'DeepSeek',kimi:'Kimi',openai:'OpenAI',grok:'Grok',moonshot:'Kimi',
             anthropic:'Claude',google:'Gemini',qwen:'Qwen',zhipu:'智谱',myapi:'API'};
function isProviderModel(m){return String(m||'').indexOf('/')>=0}
function assistantLabel(model){
 const m=String(model==null?(currentModel||''):model);
 if(!m)return '助手';
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
 }catch(e){toast('切换模型失败: '+(e.text||e),'err');refreshSessModelOptions()}}
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
 let sig=q+'|'+list.map(s=>s.id+s.state+s.total_steps+s.estimated_tokens+(s.conversation_index??'')+(s.title||'')+(s.attachment_count??'')).join('|')+'|'+current;
 if(sig===sessSig)return;
 sessSig=sig;
 box.innerHTML='';
 if(!list.length){box.innerHTML='<div class="sess-empty">没有匹配的 Session</div>';return}
 list.forEach(s=>{let e=document.createElement('div');e.className='sess'+(s.id===current?' sel':'');e.setAttribute('role','button');e.setAttribute('aria-pressed',s.id===current?'true':'false');
  // 会话标识必须显示：标题可能相似（甚至被 Copilot 改名），删错代价很大
  e.innerHTML=`<span class="dot ${stateClass(s.state)}"></span><div class="smeta"><b>${esc(s.title)}</b><small><code class="sid" title="点击复制会话 ID">${esc(s.id)}</code><br>步骤 ${esc(s.total_steps??0)} · 会话 ${esc((s.conversation_index??0)+1)} · Token ${esc(s.estimated_tokens??0)}</small></div><span class="stag">${esc(STATE_TEXT[s.state]||s.state)}</span>${(s.attachment_count||0)>0?`<span class="attchip" title="含 ${s.attachment_count} 条带图片附件的消息">📎${s.attachment_count}</span>`:''}`;
  let chip=e.querySelector('.sid');
  if(chip)chip.onclick=ev=>{ev.stopPropagation();try{navigator.clipboard.writeText(s.id);chip.textContent='已复制 ✓';setTimeout(()=>chip.textContent=s.id,900)}catch(_){}};
  e.onclick=()=>load(s.id);
  // 键盘可达：会话切换是主入口，纯 div+onclick 对键盘用户等于不可用
  e.tabIndex=0;
  e.onkeydown=ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();load(s.id)}};
  box.appendChild(e)})}

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

function renderMessages(messages,opts){
 opts=opts||{};
 let box=document.getElementById('chatlog');box.innerHTML='';
 lastMessages=messages||[];
 if(!messages.length){box.innerHTML='<div class="meta">当前没有人工续聊记录。下面发送的消息会发给该 Session 正在使用的模型。</div>';return}
 messages.forEach(m=>{box.appendChild(msgEl(m.role,m.content,m.created,{attachments:m.attachments||[]}))});
 // 新到达的消息做一次渐显（纯动画，不改内容；尊重 prefers-reduced-motion）
 if(opts.animateLast&&box.lastElementChild)box.lastElementChild.classList.add('reveal');
 applyMsgFold(box);
 applyCodePrefs(box);          // 行号/软换行偏好（可全局开关，记住选择）
 if(findQuery)applyFind();
 box.scrollTop=box.scrollHeight}
/* 语言显示名（对齐 Copilot 的写法：PowerShell 而不是 powershell） */
const HL_LABEL={python:'Python',js:'JavaScript',typescript:'TypeScript',json:'JSON',
 bash:'Bash',powershell:'PowerShell',sql:'SQL',yaml:'YAML',html:'HTML',css:'CSS',
 code:'代码'};
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
  return '<span class="cl"><span class="ln" data-act="codeLineSel" title="点击选中整行">'+(i+1)+'</span>'+prefix+ln+tail+'</span>'});
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
  chip.textContent='图片加载失败';
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
  ['↩ '+(noWrap?'自动换行':'关闭换行'),'data-act="mWrap"',noWrap],
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
 try{navigator.clipboard.writeText(m.dataset.raw||'');btn.textContent='已复制 ✓';setTimeout(()=>btn.textContent='复制',900)}catch(e){}}
function copyCode(btn){let w=btn.closest('.md-codewrap');if(!w)return;let c=w.querySelector('code');
 try{navigator.clipboard.writeText(c?c.textContent:'');btn.textContent='已复制 ✓';setTimeout(()=>btn.textContent='复制',900)}catch(e){}}
/* 复制任意文本（JSON 代码块等）：按钮文案短暂反馈 */
function copyText(text,btn){
 try{navigator.clipboard.writeText(text||'');
     if(btn){const old=btn.textContent;btn.textContent='已复制 ✓';
      setTimeout(()=>btn.textContent=old,900)}}catch(e){}}
function toggleCodeFold(btn){
 let w=btn.closest('.md-codewrap');if(!w)return;
 let folded=w.classList.toggle('collapsed');
 btn.textContent=folded?'展开':'收起'}
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
  let b=document.createElement('button');b.className='msg-fold';b.textContent='展开全文';
  b.onclick=()=>{let c=m.classList.toggle('collapsed');b.textContent=c?'展开全文':'收起'};
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
 css:'css',scss:'css',less:'css'};
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
  return '<div class="act-badge ok">最终答复</div><div class="md-body">'+mdToHtml(body)+'</div>'}
 if(t==='tool')return actionCardHtml(obj);
 if(t==='actions'){
  const list=Array.isArray(obj.actions)?obj.actions:[];
  return '<div class="act-badge">批量动作 · '+list.length+' 个</div>'+
   list.map(actionCardHtml).join('')}
 return '<div class="act-badge">JSON 回复</div>'+jsonViewBox(obj)}
function renderAssistantBody(content){
 const raw=String(content==null?'':content);
 const obj=tryParseJson(raw);
 if(obj)return renderJsonReply(obj);
 // 看起来像 JSON 但解析不了：明确告诉用户是原文，而不是让人以为界面坏了
 if(/^\s*[{[]/.test(raw))return '<div class="act-badge warn">JSON 解析失败（原文）</div>'+
   '<div class="md-body">'+mdToHtml(raw)+'</div>';
 return '<div class="md-body">'+mdToHtml(raw)+'</div>'}
function msgEl(role,content,ts,opts){
 opts=opts||{};
 let who=opts.who||(role==='user'?'你':assistantLabel());
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
 e.innerHTML=`<div class="msg-av">${esc(String(who).charAt(0))}</div>`+
  `<div class="msg-main"><div class="msg-head"><span class="msg-role">${esc(who)}</span>`+
  (t?`<span class="msg-time">${t}</span>`:'')+
  `<button class="msg-copy" title="复制这条消息" data-act="copyMsg">复制</button></div>`+
  body+attsHtml+`</div>`;
 e.dataset.raw=content||'';
 return e}

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
   const rawCode=buf.join('\n');
   // ```json 代码块直接用 JSON 视图（键值着色 + 可滚），比纯文本好读
   if(/^(json|jsonc)$/i.test(String(lang))){
    const parsed=tryParseJson(rawCode);
    if(parsed){out.push('<div class="md-codewrap"><div class="md-codebar">'+
      '<span class="md-ico">⟨/⟩</span><span class="md-lang">JSON</span>'+
      '<button type="button" class="md-icon" title="复制代码" data-act="copyRaw" data-raw="'+esc(rawCode)+'">⧉</button>'+
      '<button type="button" class="md-icon" title="更多" data-act="codeMenu">…</button></div>'+
      jsonViewBox(parsed)+'</div>');continue}}
   // 高亮内部会先分词再转义（入参是原始代码，不要预先 esc）
   const codeHtml=highlight(rawCode,lang);
   const long=buf.length>22;
   out.push(`<div class="md-codewrap${long?' foldable collapsed':''}"><div class="md-codebar">`+
            `<span class="md-ico">⟨/⟩</span><span class="md-lang">${esc(langLabel(lang))}</span>`+
            (long?`<span class="md-lines">${buf.length} 行</span>`:'')+
            `<button type="button" class="md-copsel" data-act="copySelLines" style="display:none">复制选中 0 行</button>`+
            `<button type="button" class="md-icon" title="复制代码" data-act="copyCode">⧉</button>`+
            `<button type="button" class="md-icon" title="更多" data-act="codeMenu">…</button>`+
            (long?`<button type="button" class="md-fold" data-act="toggleCodeFold">展开</button>`:'')+
            `</div>`+
            `<pre class="md-code"><code>${wrapCodeLines(codeHtml)}</code></pre></div>`);
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
 // 可选：插入方式选择（step 立即遵守 / turn 整轮重排），只有强行插入用得上
 let modes=document.getElementById('gmModes');
 if(modes){
  modes.style.display=o.modes?'':'none';
  if(o.modes){let pick=modes.querySelector('input[value="'+o.modes.default+'"]');
   if(pick)pick.checked=true}}
 let c=document.getElementById('gmCancel');c.style.display=o.cancelText===null?'none':'';c.textContent=o.cancelText||'取消';
 let ok=document.getElementById('gmOk');ok.textContent=o.okText||'确定';ok.className='btn '+(o.danger?'danger':'accent');
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
function openMask(id){
 const m=document.getElementById(id);if(!m)return;
 _focusBeforeMask=document.activeElement;
 m.classList.add('open');
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
 return gmShow({title,msg,input:def,okText:'插入',modes:{default:lastMode}})};

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
 // 守卫紧跟 await：原实现写在全部 DOM 写入之后，迟到响应会把旧会话的标题/KPI/时间线
 // 写到用户已切过去的新会话界面上（经典 stale-response）。
 if(current!==id)return;
 if(d.token_budget)setKpis({token_budget:d.token_budget,estimated_tokens:s.estimated_tokens??0});
 currentModel=s.model||null;currentUrl=s.current_url;renderMessages(d.messages||[]);
 // 跟进消息应跟随 Session 自己的模型，而不是左侧新任务表单的选择
 let _ms=document.getElementById('model');
 if(currentModel&&[..._ms.options].some(o=>o.value===currentModel))_ms.value=currentModel;
 refreshSessModelOptions();   // 会话页选择器显示该会话自己的模型
 updateChannelUI();           // 按通道显示/隐藏"网页对话"专用按钮与文案
 document.getElementById('title').textContent=s.title;
 let _sc=document.getElementById('sidChip');
 if(_sc){_sc.textContent=s.id;_sc.onclick=()=>{try{navigator.clipboard.writeText(s.id);toast('已复制会话 ID: '+s.id,'ok')}catch(e){toast(s.id)}}}
 document.getElementById('status').textContent=(STATE_TEXT[s.state]||s.state)+' · '+s.workspace;
 setPill(s.state);setKpis(s);
 document.getElementById('steps').innerHTML='';renderedSteps=0;lastDetail='';
 renderSteps(d.steps);lastStepCount=d.steps.length;
 document.getElementById('emptyState').style.display='none';
 document.getElementById('sessionView').style.display='';
 // 守卫已前移到 await 之后（见函数开头）
 // 记住助手消息数（SSE 新消息通知据此决定是否播渐显动画）
 lastAssistantCount=(d.messages||[]).filter(m=>m.role==='assistant').length;
 startEvents(current);     // 用 current 而非入参：等待期间可能已切到别的会话
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
  await applyRuntime(r);
 }catch(e){}
 finally{runtimeInflight=false}}
/* 渲染 runtime 快照：轮询与 SSE 推送共用（SSE 直接送快照，省一次往返） */
async function applyRuntime(r){
 if(!r)return;
 const myId=current;
 // SSE 推送与 1s/350ms 轮询可能并发到达同一函数：统一以"当前会话"为准，
 // 迟到的或属于别的会话的快照直接丢弃（否则会把 A 的步骤渲染进 C 的时间线）。
 if(!myId)return;
 if(r.session_id&&r.session_id!==myId)return;
 lastRuntime=r;
 currentUrl=r.current_url||currentUrl;
  document.getElementById('pulse').className=r.busy?'pulse':'dot-off';
  const _actTxt=`阶段: ${r.phase||r.session_state}\n${r.detail||''}\n总步骤: ${r.total_steps}\n当前会话步骤: ${r.conversation_steps}\nToken估算: ${r.estimated_tokens}\n会话序号: ${(r.conversation_index??0)+1}\n${r.queue_pending?`队列: ${r.queue_pending} 条待处理\n`:''}最后更新: ${r.updated?new Date(r.updated*1000).toLocaleTimeString():''}${r.model_warning?`\n\n⚠ ${r.model_warning}`:''}`;
  const _actEl=document.getElementById('activity');
  if(_actEl.textContent!==_actTxt)_actEl.textContent=_actTxt;   // 内容没变不重写：否则每秒打断文本选择/重排
  let _ans=r.answer_preview||'等待模型回答…',_ae=document.getElementById('answer'),_pj=null;
  try{_pj=JSON.parse(_ans)}catch(e){}
  // 结构化动作走 JSON 视图；普通回答按 Markdown 渲染（`.term` 面板里 code 块仍可读）
  if(_ans!==lastAnsHtml){   // 仅内容变化才重建：保住代码块展开状态与滚动位置
   if(_pj&&typeof _pj==='object'){_ae.innerHTML='<div class="jsonview">'+jv(_pj)+'</div>'}
   else{_ae.innerHTML='<div class="md-body">'+mdToHtml(_ans)+'</div>'}
   applyCodePrefs(_ae);     // 回答面板里的代码块同样吃行号/换行偏好
   lastAnsHtml=_ans}
  document.getElementById('prompt').textContent=r.prompt_preview||'';
  setPill(r.session_state);setKpis(r);
  // 只在真的等待授权时显示（后端已按 waiting 过滤，这里再兜一层）
  try{showApprovalBar(r.session_state==='waiting'?r.approval_pending:null)}catch(e){}
  updateRetryCountdown(r);
  if(r.total_steps!==lastTotal||(r.detail||'')!==lastDetail){
   lastDetail=r.detail||'';
   lastTotal=r.total_steps??lastTotal;
   // 详情请求失败不能变成 unhandled rejection（此前会让时间线永久停在旧数据且无提示）
   try{
    let d=await api('/v1/agent/sessions/'+encodeURIComponent(myId));
    if(current!==myId)return;
    renderSteps(d.steps);lastStepCount=d.steps.length
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
  renderMessages(d.messages,{animateLast:as>lastAssistantCount});
  lastAssistantCount=as}catch(e){}}

/* ── 操作 ── */
async function control(a){if(!current)return;try{let r=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/actions/${a}`,{method:'POST'});if(a==='resume')toast(r.note||(r.runner_started?'已恢复，正在续跑任务':'已恢复'), r.note_kind==='err'?'err':'ok');await load(current)}catch(e){toast('操作失败: '+e,'err')}}
async function activate(){if(!current)return;try{let d=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/activate`,{method:'POST'});currentUrl=d.url;toast('已将自动化 Edge 切换到该会话','ok')}catch(e){toast('操作失败: '+e,'err')}}
function openCopilot(){if(currentUrl){if(!/^https?:\/\//i.test(currentUrl)){toast('地址不是 http(s)，已拒绝打开','err');return}window.open(currentUrl,'_blank','noopener,noreferrer')}
 else toast(isProviderModel(currentModel)?'该会话用的是 API 提供商模型，没有可打开的网页对话':'该 Session 暂无网页会话地址','err')}
async function removeSession(remote){
 if(!current)return;
 let extra=remote?'，以及该任务关联的所有 Copilot 网页会话（标题里可能已没有 [CBP-AGENT] 标记，删除时会用会话正文核对归属）':'';
 let msg=`即将删除的会话：\n\n  ID: ${current}\n  标题: ${(document.getElementById('title').textContent||'').trim()}\n\n仅删除本地 Session${extra}。此操作不可撤销，确定继续？`;
 if(!await dlgConfirm(msg,'删除确认',true))return;
 try{let url='/v1/agent/sessions/'+encodeURIComponent(current)+(remote?'?delete_remote=true':'');
  let d=await api(url,{method:'DELETE'});
  if(!d.deleted){showDeleteResult(d);return}
   stopEvents();          // 删掉当前会话必须停 SSE，否则重连定时器会一直敲已删除的会话
  current=null;
  document.getElementById('sessionView').style.display='none';
  document.getElementById('emptyState').style.display='flex';
  await refreshSessions()}catch(e){toast('删除失败: '+(e.text||e),'err')}}
/* ── 限流倒计时：居中卡片，可收起为右下角小标 ── */
let retryDismissedAt=null;   // 用户已收起的那个终点时间戳（新一轮限流会自动再展开）
let lastRuntime=null;        // 最近一次 runtime 响应，供小标展开时立即重绘
function fmtCountdown(total){
 total=Math.max(0,Math.ceil(total/1000));
 let h=Math.floor(total/3600),m=Math.floor((total%3600)/60),s=total%60;
 return h>0?`${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`
            :`${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`}
function hideRetryUI(){
 let box=document.getElementById('retryCountdown'),chip=document.getElementById('retryChip');
 if(box){box.style.display='none';box.removeAttribute('data-resume')}
 if(chip){chip.style.display='none';chip.removeAttribute('data-resume')}}
function updateRetryCountdown(r){
 let box=document.getElementById('retryCountdown'),chip=document.getElementById('retryChip');
 if(!box)return;
 lastRuntime=r||lastRuntime;
 let resumeAt=lastRuntime&&lastRuntime.retry_resume_at;
 if(!resumeAt||resumeAt*1000<=Date.now()){retryDismissedAt=null;hideRetryUI();return}
 // 换了新的限流终点（新一轮）→ 重新展开卡片
 if(retryDismissedAt!==null&&retryDismissedAt!==resumeAt)retryDismissedAt=null;
 let showCard=retryDismissedAt!==resumeAt;
 let text=fmtCountdown(resumeAt*1000-Date.now());
 box.dataset.resume=resumeAt;
 if(chip){chip.dataset.resume=resumeAt;chip.textContent='⏳ '+text}
 if(!showCard){box.style.display='none';if(chip)chip.style.display='block';return}
 box.style.display='block';if(chip)chip.style.display='none';
 // 标题按实际通道措辞：API 提供商（模型 ID 含 /，如 deepseek/deepseek-chat）
 // 的 5xx/超时也走同一个窗口，写成"Copilot 限流"不准确。
 let isProvider=!!(currentModel&&currentModel.indexOf('/')>=0);
 let title=box.querySelector('.rc-title');
 if(title)title.textContent=isProvider?'⏳ API 服务限流/不可用，预计恢复'
                                     :'⏳ Copilot 限流/不可用，预计恢复';
 box.querySelector('.rc-count').textContent=text;
 let sub=box.querySelector('.rc-sub');
 sub.textContent=`第 ${lastRuntime.retry_attempt||1} 次 · 自动重试中，可点「暂停」稍后继续`;
}
/* 收起（×）与展开（点小标） */
document.getElementById('retryMin').onclick=e=>{
 e.stopPropagation();
 let box=document.getElementById('retryCountdown');
 retryDismissedAt=parseFloat(box.dataset.resume)||null;
 updateRetryCountdown(lastRuntime)};
document.getElementById('retryChip').onclick=()=>{retryDismissedAt=null;updateRetryCountdown(lastRuntime)};
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
   let input=await dlgPrompt(`该任务共 ${revs.length} 个回合版本（按时间先后）：\n\n${lines}\n\n输入「0」或留空 = 整任务回滚到开始前；输入版本号 = 撤销该回合之后的改动。`,'0','','回滚到哪个版本？');
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
 }catch(e){toast('回滚失败: '+(e.text||e),'err')}}
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
 openMask('dlgMask')}
function closeDlg(){closeMask('dlgMask')}
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

<h3>常见问题</h3>
<ul>
<li><b>服务重启后短暂停在登录页</b>：企业 SSO 会经过公司 ADFS 自动回跳，约需 40 秒，一般无需手动登录；若停在账户选择页，手动点一下即可。</li>
<li><b>提示找不到输入框/模型按钮</b>：登录态失效，运行 <code>python scripts/login.py</code> 重新登录（或在有窗口模式下直接登录）。</li>
<li><b>任务显示等待服务</b>：Copilot 在限流，等它自动恢复；持续不可用会安全暂停，恢复后点「恢复」。</li>
</ul>`;
/* ── 手册增强：自动目录 + 低频章节折叠（运行时处理，不改模板）── */
const MANUAL_COLLAPSE=['服务自升级','常见问题'];
function enhanceManual(){
 const body=document.getElementById('manualBody');
 if(!body)return;
 body.querySelectorAll('.mtoc').forEach(x=>x.remove());
 const hs=[...body.querySelectorAll('h3')];
 hs.forEach((h,i)=>h.id='mh'+i);
 let toc=document.createElement('nav');
 toc.className='mtoc';
 toc.innerHTML='<b>目录</b>'+hs.map((h,i)=>`<a href="#mh${i}">${esc(h.textContent)}</a>`).join('');
 body.insertBefore(toc, body.firstChild);
 toc.querySelectorAll('a').forEach(a=>a.onclick=e=>{
  e.preventDefault();
  const t=document.getElementById(a.getAttribute('href').slice(1));
  if(t)t.scrollIntoView({behavior:'smooth',block:'start'});});
 hs.forEach(h=>{
  if(!MANUAL_COLLAPSE.some(k=>h.textContent.includes(k)))return;
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

function openManual(){document.getElementById('manualBody').innerHTML=MANUAL_HTML;enhanceManual();openMask('manualMask')}
function closeManual(){closeMask('manualMask')}
function openTask(){openMask('taskMask')}
function closeTask(){closeMask('taskMask')}

document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeDlg();closeLive();gmClose(null);closeManual();closeTask()}});

/* ── 实时面板选项卡 + 放大查看 ── */
const LIVE_NAMES={activity:'当前执行状态',answer:'当前回答',prompt:'最近发送的内容',chat:'继续对话'};
const LIVE_TAB={activity:'tabActivity',answer:'tabAnswer',prompt:'tabPrompt',chat:'tabChat'};
let liveTabName='activity';
let lastAns='';
let lastAnsHtml='';
function liveTab(n){liveTabName=n;
 for(const k in LIVE_NAMES){document.getElementById(k).style.display=k===n?'':'none';
  document.getElementById(LIVE_TAB[k]).className='tab'+(k===n?' on':'')}}  // className 覆盖即清除新内容圆点
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
 if(!current){toast('请先选择 Session','err');return}
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
 }catch(e){toast('读取初始提示词失败: '+(e.text||e),'err')}}
async function continueAgent(){
 if(!current){toast('请先选择 Session','err');return}
 let instruction=await dlgPrompt('请输入要继续执行的任务','继续执行当前任务，从未完成事项开始','继续执行','继续执行');
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
 let log=document.getElementById('chatlog');
 let label=content+(atts.length?`\n（附 ${atts.length} 张图片）`:'');
 log.appendChild(msgEl('user',label));log.scrollTop=log.scrollHeight;box.value='';
 IMG_STORES.follow=[];renderThumbs();
 let baseAssistants=0;
 try{let dd=await api('/v1/agent/sessions/'+encodeURIComponent(current));baseAssistants=dd.messages.filter(m=>m.role==='assistant').length}catch(e){}
 try{let d=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/messages`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content,attachments:atts,model:currentModel||document.getElementById('model').value,agent_mode:followupMode==='agent'})});
  if(d&&d.queued){
   let note=msgEl('assistant',
     d.position>1?`已排队（前面还有 ${d.position-1} 条）`:'已排队，等待处理…',
     null,{plain:true,who:'队列'});
   log.appendChild(note);log.scrollTop=log.scrollHeight;
   refreshQueue();
   awaitFollowupReply(current,baseAssistants);
   }else{
    // 后端对 /messages 只返回排队/接收元信息（session_id/queued/id/position/mode），
    // 从不返回 content/url —— 原实现在这里读不存在的字段，会渲染一个空气泡。
    // 正确做法：重载会话，把刚落库的新消息渲染出来。
    await load(current)}
 }catch(e){log.appendChild(msgEl('assistant','发送失败: '+e,null,{plain:true,who:'错误'}))}
 finally{btn.disabled=false;btn.textContent='发送'}}

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
 let meta=document.querySelector('#chat .chatbar .meta');
 if(meta)meta.innerHTML=`与 <b>${esc(assistantLabel())}</b> 继续 · <b>Ctrl+J</b> 强行插入指令（运行中即时生效）`}

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
  box.innerHTML=items.map(it=>`<div class="qitem"><span class="qpos">#${it.position}</span><span class="qtext" title="${esc(it.content)}">${esc(it.content)}</span><span class="qmode">${it.mode==='agent'?'Agent':'对话'}</span><button title="编辑" data-edit="${esc(it.id)}">✎</button><button class="qdel" title="删除" data-del="${esc(it.id)}">×</button></div>`).join('');
   // 不再把 id 拼进内联 onclick（id 含引号即可突破 JS 字符串 → XSS）；与 loadMemory 同一范式
   box.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editQueueItem(b.dataset.edit));
   box.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>removeQueueItem(b.dataset.del));
 }catch(e){box.innerHTML=''}}

async function editQueueItem(itemId){
 if(!current)return;
 let d=await api('/v1/agent/sessions/'+encodeURIComponent(current)+'/queue');
 let it=(d.pending||[]).find(x=>x.id===itemId);if(!it)return;
 let content=await dlgPrompt('修改排队消息',it.content,'保存');
 if(content===null)return;
 try{
  await api(`/v1/agent/sessions/${encodeURIComponent(current)}/queue/${itemId}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content})});
  toast('已更新排队消息','ok');refreshQueue()}catch(e){toast('更新失败: '+(e.text||e),'err')}}

async function removeQueueItem(itemId){
 if(!current)return;
 if(!await dlgConfirm('删除这条排队中的消息？此操作不可撤销。','删除排队消息',true))return;
 try{
  await api(`/v1/agent/sessions/${encodeURIComponent(current)}/queue/${itemId}`,{method:'DELETE'});
  toast('已删除排队消息','ok');refreshQueue();load(current)}catch(e){toast('删除失败: '+e,'err')}}
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
    if(f.size>8*1024*1024){toast('图片超过 8MB，已忽略','err');continue}
    if(IMG_STORES[store].length>=4){toast('最多附加 4 张图片','err');continue}
    let r=new FileReader();
    r.onload=()=>{IMG_STORES[store].push({dataUrl:r.result});renderThumbs()};
    r.readAsDataURL(f);
   }
  }
 })}

document.addEventListener('keydown',e=>{if(e.target&&e.target.id==='followup'&&e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();sendFollowup()}});
/* 强行插入指令（像 DSH 的 Ctrl+J）：运行中立即生效，Agent 下一回合优先执行；
   与"排队跟进"不同——后者要等当前这轮跑完才起新一轮。 */
async function injectMessage(){
 if(!current){toast('请先选择 Session','err');return}
 let pick=await dlgInjectPrompt(
  '插入指令（运行中即时生效，会立即中断当前回合后按新指令继续）。插入方式决定语义：',
  '', '强行插入','step');
 if(pick===null)return;
 let content=String(pick.text||'').trim();
 if(!content)return;
 let queue=(pick.mode==='turn')?'turn':'step';
 try{localStorage.setItem('cbp-inject-mode',queue)}catch(e){}
 try{
  let d=await api(`/v1/agent/sessions/${encodeURIComponent(current)}/inject`,
                  {method:'POST',headers:{'Content-Type':'application/json'},
                   body:JSON.stringify({content,queue})});
  toast(d.note||'已插入', d.running?'ok':'');
  liveTab('chat');
  // 让聊天区尽快看到这条指令（标"插入",与普通发送区分）
  let log=document.getElementById('chatlog');
  log.appendChild(msgEl('user',content,null,
    {who:queue==='step'?'你（插入·立即遵守）':'你（插入·整轮重排）'}));
  log.scrollTop=log.scrollHeight;
  refreshSessions();
 }catch(e){toast('插入失败: '+(e.text||e),'err')}}
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
    document.getElementById('emptyState').style.display='flex';toast('任务启动失败: '+(e.text||e),'err')});
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
/* ── Copilot 未就绪提示条（按需启动后：没有 Copilot 也能用提供商模型）── */
let bootStateSeen=null;
function bootBanner(){
 let el=document.getElementById('bootBar');
 if(!el){
  el=document.createElement('div');el.id='bootBar';
  el.innerHTML='<span id="bootText"></span><button class="btn ghost" id="bootRetry" style="padding:2px 10px;font-size:12px">重试浏览器</button>';
  document.body.appendChild(el);
  el.querySelector('#bootRetry').onclick=async()=>{
   const b=el.querySelector('#bootRetry');b.disabled=true;b.textContent='启动中…';
   try{await api('/v1/agent/browser/retry',{method:'POST'});toast('已请求启动浏览器，稍后自动复查','ok')}
   catch(e){toast('请求失败: '+e,'err')}
   finally{b.disabled=false;b.textContent='重试浏览器'}};
 }
 return el;
}
function showBoot(state,ready,err){
 const el=bootBanner();
 if(ready){el.style.display='none';syncFixedLanes();return}
 el.style.display='flex';   // 先写文字再 syncFixedLanes：否则量到的是空框高度，
                            // 内容会在 1.5s 心跳重算时跳一下（截图抓到过空框）
 const map={idle:'浏览器未启动（首次使用 Copilot 时自动启动）',
            starting:'浏览器启动中…',
            browser_up:'浏览器已启动，等待 Copilot 就绪（可能需要在自动化窗口登录）',
            needs_login:'需要在自动化窗口完成 Microsoft 登录',
            failed:'浏览器启动失败'};
 const txt='Copilot 未就绪：'+(map[state]||state||'未知')+(err?('（'+(err+'').slice(0,120)+'）'):'')+'　提供商模型不受影响，可直接使用。';
 document.getElementById('bootText').textContent=txt;
 syncFixedLanes();
}
async function pollBoot(){
 try{const h=await api('/health');showBoot(h.browser_state,h.copilot_ready,h.browser_error)}
 catch(e){showBoot('failed',false,'健康检查失败')}
}
try{setInterval(pollBoot,8000);setTimeout(pollBoot,1200)}catch(e){}

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
(async()=>{try{await api('/v1/agent/sessions');hideConnBanner()}
 catch(e){
  // 以前 fetch 网络失败与 401 落进同一个 catch，一律 DEMO=true → 界面显示硬编码假数据，
  // 用户点"启动任务"其实不发任何请求却看到"任务已建立"（最严重的误导性成功）。
  // 现在按类型分流，且**只有显式 ?demo=1 才允许进演示模式**。
  const isAuth=(e&&e.status===401);
  const isNet=(e instanceof TypeError)||!/\b\d{3}\b/.test(String(e&&e.message||e));
  if(isAuth){showConnBanner('需要 api_token：'+(e.detail||'请输入访问令牌'),false)}
  else if(isNet){showConnBanner('无法连接本地服务（127.0.0.1:8765 是否在运行？）',false)}
  else{showConnBanner('加载会话失败：'+(e.text||e),false)}
  if(location.search.includes('demo=1')){DEMO=true;
   document.getElementById('demoBadge').style.display='block';
   document.getElementById('demoBadge').textContent='演示模式（?demo=1）——数据为本地假数据'}
  }
 loadModels();browserState();
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
const ACTIONS={
 toggleRail:()=>toggleRail(),
 openTask:()=>openTask(),
 openManual:()=>openManual(),
 toggleBrowser:()=>toggleBrowser(),
 toggleTheme:()=>toggleTheme(),
 drawSessions:()=>drawSessions(),
 chatFindInput:()=>chatFindInput(),
 findStepKey:(el,e)=>{if(e.key==='Enter'){e.preventDefault();findStep(e.shiftKey?-1:1)}},
 findStep:(el)=>findStep(Number(el.dataset.arg||1)),
 control:(el)=>control(el.dataset.arg),
 continueAgent:()=>continueAgent(),
 activate:()=>activate(),
 openCopilot:()=>openCopilot(),
 toggleMore:(el,e)=>toggleMore(e),
 rollbackSession:()=>rollbackSession(),
 rollbackSelfUpgrade:()=>rollbackSelfUpgrade(),
 removeSession:(el)=>removeSession(el.dataset.arg==='true'),
 liveTab:(el)=>liveTab(el.dataset.arg),
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
 closeDlg:()=>closeDlg(),
 closeManual:()=>closeManual(),
 closeLive:()=>closeLive(),
 closeTask:()=>closeTask(),
 // 点击遮罩**背景**才关闭（点弹窗内部不关）：原来写在内联里的 if(event.target===this)
 mask:(el,e)=>{if(e.target!==el)return;
  ({dlg:closeDlg,manual:closeManual,live:closeLive,task:closeTask}[el.dataset.arg]||(()=>{}))()},
 maskGm:(el,e)=>{if(e.target===el)gmClose(null)},
 // 动态生成内容里的处理器（消息复制/代码折叠/附件移除）
 copyMsg:(el)=>copyMsg(el),
 copyCode:(el)=>copyCode(el),
 copyRaw:(el)=>copyText(el.dataset.raw||'',el),
 codeMenu:(el)=>codeMenu(el),
 codeLineSel:(el)=>codeLineSel(el),
 copySelLines:(el)=>copySelLines(el),
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
  if(b)b.textContent=folded?'展开':'收起'},
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
