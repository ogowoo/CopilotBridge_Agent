// msgrender.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
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
/* 文件卡片：```cbpfile <kind> <path>
   kind: text（带行号、可折叠）/ image（直接显示）/ binary（类型+大小+下载）。
   原图与下载都走 /v1/sessions/{sid}/file?path=…，不把大内容塞进 data URL。 */
function fileCardHtml(kind,path,content){
 const sid=window.current||'';
 const url='/v1/agent/sessions/'+encodeURIComponent(sid)+'/file?path='+encodeURIComponent(path);
 const tk=apiToken();
 const q=tk?'&token='+encodeURIComponent(tk):'';
 const base=path.split('/').pop()||path;
 const bytes=new Blob([content||'']).size;
 const kb=Math.max(1,Math.round(bytes/1024));
 if(kind==='image'){
  return '<figure class="filecard img"><figcaption><span class="fc-name" title="'+esc(path)+'">'
   +'📄 '+esc(base)+'</span><span class="fc-meta">'+kb+' KB</span>'
   +'<a class="fc-btn" href="'+url+q+'" target="_blank" rel="noopener">原图</a></figcaption>'
   +'<img class="fc-img" src="'+url+q+'" alt="'+esc(base)+'" loading="lazy" data-act="imgZoom" data-arg="'+esc(path)+'">'
   +'</figure>'}
 if(kind==='binary'){
  return '<div class="filecard"><div class="fc-head"><span class="fc-name" title="'+esc(path)+'">'
   +'📦 '+esc(base)+'</span><span class="fc-meta">'+kb+' KB · 二进制</span>'
   +'<a class="fc-btn" href="'+url+'&download=1'+q.replace('&','&')+'" target="_blank" rel="noopener">下载</a>'
   +'</div></div>'}
 const lines=(content||'').split('\n');
 const numbered=lines.map((l,i)=>'<span class="fc-ln">'+(i+1)+'</span>'+esc(l)).join('\n');
 return '<div class="filecard"><div class="fc-head"><span class="fc-name" title="'+esc(path)+'">'
  +'📄 '+esc(base)+'</span><span class="fc-meta">'+lines.length+' 行 · '+kb+' KB</span>'
  +'<button type="button" class="fc-btn" data-act="copyFile" data-arg="'+esc(path)+'">复制</button>'
  +'<a class="fc-btn" href="'+url+'&download=1'+(tk?'&token='+encodeURIComponent(tk):'')+'" target="_blank" rel="noopener">下载</a>'
  +'</div><pre class="fc-body md-code" data-path="'+esc(path)+'">'+numbered+'</pre></div>'}

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
  let m=/^\s*```([^\n]*?)\s*$/.exec(line);
  if(m){
   const info=m[1]||'code',buf=[];
   i++;
   while(i<lines.length&&!/^\s*```\s*$/.test(lines[i])){buf.push(lines[i]);i++}
   closeLists(0);
   // ```cbpfile <kind> <path> —— 文件卡片（文本带行号 / 图片直接显示 / 二进制给下载）
   if(info.startsWith('cbpfile')){
     const rest=info.slice('cbpfile'.length).trim();
     const sp=rest.indexOf(' ');
     const kind=(sp<0?rest:rest.slice(0,sp))||'text';
     const path=(sp<0?'':rest.slice(sp+1)).trim();
     out.push(fileCardHtml(kind,path,buf.join('\n')));
     continue}
   out.push(codeBlockHtml(buf.join('\n'),info));
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
 if(!window.current){toast(tr('请先选择 Session'),'err');return}
 try{
  let d=await api('/v1/agent/sessions/'+encodeURIComponent(window.current));
  // 把**顺序**与**最新状态**写进文件本身：以前导出里 steps 与 messages 方向让人
  // 猜（用户实测："219 那个就是新的，所以我不知道算正序还是倒序"）。现在三个数组
  // 一律正序（旧→新）并在 order 里写清，latest 直接给出最新一步/最新消息时间。
  const _steps=[...(d.steps||[])].sort((a,b)=>(a.step??0)-(b.step??0));
  const _msgs=[...(d.messages||[])].sort((a,b)=>(a.id??0)-(b.id??0));
  const _files=[...(d.files||[])].sort((a,b)=>(a.id??0)-(b.id??0));
  const _last=_steps.length?_steps[_steps.length-1]:null;
  const _lastMsg=_msgs.length?_msgs[_msgs.length-1]:null;
  let payload={exported_at:new Date().toISOString(),app:'Gantry',
               api:'/v1/agent/sessions/'+encodeURIComponent(window.current),
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
  a.download='gantry-'+window.current+'-timeline-'+new Date().toISOString().slice(0,19).replace(/[:T]/g,'-')+'.json';
  document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),3000);
  toast(tr('时间线已导出（')+((payload.steps||[]).length)+' 步）','ok')}catch(e){toast(tr('导出失败: ')+e,'err')}}

/* ── 页面内通知（toast）与通用模态层（替代原生 alert/confirm/prompt） ── */

export { tryParseJson, jsonViewBox, actionCardHtml, renderJsonReply, renderAssistantBody, msgEl, codeBlockHtml, fileCardHtml, mdToHtml, exportTimeline };
