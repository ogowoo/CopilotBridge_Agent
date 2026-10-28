// cards.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
/* ── 演示模式：API 不可达时展示界面（仅预览用，正式环境不会触发） ── */
const DEMO_SESSIONS=[
 {id:'task-demo01',title:'分析 gantry 项目并生成 TODO',state:'running',model:'copilot-think-deeper',total_steps:47,conversation_steps:12,conversation_index:1,estimated_tokens:18320,workspace:'C:\\Gantry\\workspace'},
 {id:'task-demo02',title:'整理 Outlook 未读邮件',state:'paused',total_steps:23,conversation_steps:23,conversation_index:0,estimated_tokens:9640,workspace:'C:\\Gantry\\workspace'},
 {id:'task-demo03',title:'对比两个目录差异',state:'failed',total_steps:101,conversation_steps:4,conversation_index:3,estimated_tokens:23110,workspace:'C:\\Gantry\\workspace'},
 {id:'task-demo04',title:'深圳英文旅游手册',state:'completed',total_steps:15,conversation_steps:15,conversation_index:0,estimated_tokens:7420,workspace:'C:\\Gantry\\workspace'}];
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
 if(u.endsWith('/runtime'))return{phase:'waiting',detail:'Agent 回合 a1b2c3d4e5f6 已确认，等待 Copilot 回答',session_state:'running',total_steps:47,conversation_steps:12,estimated_tokens:18320,conversation_index:1,current_url:'https://m365.cloud.microsoft/chat/demo',busy:true,updated:Date.now()/1000,answer_preview:'{"type":"tool","tool":"run_unittest","arguments":{"path":"tests"},"reason":"验证修复闭环"}',prompt_preview:'[GANTRY-AGENT][task-demo01][C001]\n你是本地任务 Agent，可以通过工具处理当前工作目录中的文件。\n…\n当前总步骤 48/1000。只返回下一个 JSON 动作。'};
 let m=u.match(/\/sessions\/([^/?]+)$/);
 if(m)return{session:DEMO_SESSIONS[0],steps:DEMO_STEPS,files:[],messages:DEMO_MSG};
 if(u.includes('/messages'))return{content:'（演示模式）此处为 Copilot 回复',url:null};
 if(o.method==='DELETE'&&u.includes('delete_remote=true'))return{deleted:false,local_deleted:false,message:'部分 Copilot 会话删除失败，本地 Session 已保留。可检查后使用下方按钮重试，或仅清理本地记录。',remote_results:[
  {url:'https://m365.cloud.microsoft/chat?es=SSR',expected_title:'[GANTRY-AGENT][task-demo01][C000]',deleted:false,error:'URL 对应会话不属于当前 Session，拒绝删除；需要前缀: [GANTRY-AGENT][task-demo01]',method:'url-plus-session-marker'},
  {url:'https://m365.cloud.microsoft/chat/conversation/8ae00748-77f1-4e5b-979e-8ff7d26d9791?es=SSR',expected_title:'[GANTRY-AGENT][task-demo01][C001]',deleted:true,error:null,method:'url-plus-session-marker'},
  {url:'https://m365.cloud.microsoft/chat/conversation/f3b21c90-5a4d-4c2e-8b71-1d2e3f4a5b6c?es=SSR',expected_title:'[GANTRY-AGENT][task-demo01][C002]',deleted:false,error:'点击删除后，目标 URL 仍存在于 Chats 列表',method:'url-plus-session-marker'}]};
 return{deleted:true,local_deleted:true,remote_results:[],session_id:'task-demo01'};
}

function apiToken(){try{return localStorage.getItem('gantry-token')
 || localStorage.getItem('cbp-token') || ''}catch(e){return ''}}
/* 页签标题里的版本**不再硬编码**：曾经写死一个预览号，版本一升标题就对不上
   （用户实测）。现在由 loadVersion() 用 GET / 的真实版本拼，见 applyTitleVersion。 */
/* 结构化错误：以前抛原始响应体字符串，调用侧只能靠 msg.includes('401') 嗅探状态码——
   而错误体里并没有 "401" 这三个字符，所以那个分支从未命中，401 被当成网络故障进了
   演示模式（界面显示假数据、点"启动任务"不发任何真实请求）。现在带上 status。 */
class ApiError extends Error{
 constructor(status,body,url){super(body||('HTTP '+status));this.name='ApiError';this.status=status;
  this.body=body;this.url=url;
  try{const j=JSON.parse(body);if(j&&j.detail)this.detail=String(j.detail)}catch(e){}}
 get text(){return this.detail||this.body||('HTTP '+this.status)}}
/* 固定浮层分车道：把三个横幅/浮条的实际高度写进 CSS 变量，供 css/overlays.css 计算各自位置。
   元素是动态创建且高度随文案变化（窄屏换行），所以必须实测而不是写死。 */
function cardOf(kind){
 if(kind==='subagents')return document.getElementById('subagentBar');
 if(kind==='live'){
  const chat=document.getElementById('chat');
  return chat?chat.closest('section.card'):null;
 }
 return null;
}
function applyCardCollapsed(){
 [['subagents','gantry.subagentsCollapsed'],['live','gantry.liveCollapsed']].forEach(([kind,key])=>{
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
 try{localStorage.setItem(kind==='subagents'?'gantry.subagentsCollapsed':'gantry.liveCollapsed',
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

/* ── 任务条（顶栏）折叠：收起后只留标题行（状态行 / 按钮行 / KPI 一起收）──
   展开态 3 行 ≈123px，收起后 ≈57px —— 想多看对话时能把这块收掉。
   状态记 localStorage，跨刷新保留；点箭头或点标题都能切换。 */
function applyTopbarCollapsed(){
 const head=document.querySelector('header.topbar');
 if(!head)return;
 let collapsed=false;
 try{collapsed=localStorage.getItem('gantry.topbarCollapsed')==='1'}catch(e){}
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
 try{localStorage.setItem('gantry.topbarCollapsed',collapsed?'1':'0')}catch(e){}
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

/* 待办条：点横条任意位置都能展开/收起（与状态条一致的交互）。
   内部 `.todolist` 里的条目、以及「待办 x/y」按钮自己会处理点击，别重复触发。 */
document.addEventListener('click',ev=>{
 const bar=ev.target.closest && ev.target.closest('#goalBar');
 if(!bar)return;
 if(ev.target.closest('.todolist'))return;
 if(ev.target.closest('[data-act="toggleTodos"]'))return;   // 按钮自己会切
 try{toggleTodos()}catch(e){}
});

export { demoApi, apiToken, cardOf, applyCardCollapsed, toggleCard, onCardHeadClick, applyTopbarCollapsed, toggleTopbar, ApiError };
