// codeux.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
import { HL_ALIAS } from './find.js';
import { HL_LABEL } from './toollog.js';
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
function applySessionChrome(s){
 s=s||{};
 document.getElementById('title').textContent=s.title||window.current||'—';
 const _sc=document.getElementById('sidChip');
 if(_sc){_sc.textContent=s.id||window.current||'';
  _sc.onclick=()=>{try{navigator.clipboard.writeText(window.current);toast(tr('已复制会话 ID: ')+window.current,'ok')}catch(e){toast(window.current)}}}
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

/* 调用记录：行内展开（与待办/子代理/状态同一种方式，不再浮层弹出） */
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
   否则 gantry/invariants.py 的"data-act 与 ACTIONS 一一对应"静态核对看不见它，
   会把 mCopy 等当成未引用的死代码。 */
const CODE_EXT={python:'py',js:'js',typescript:'ts',json:'json',bash:'sh',
                powershell:'ps1',sql:'sql',yaml:'yml',html:'html',css:'css'};
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
 try{const v=localStorage.getItem('gantry-code-'+key);return v===null?def:v==='1'}catch(e){return def}}
function setCodePref(key,val){try{localStorage.setItem('gantry-code-'+key,val?'1':'0')}catch(e){}}
function applyCodePrefs(scope){
 const noWrap=!codePref('wrap',true);
 const noLines=!codePref('lines',true);
 (scope||document).querySelectorAll('.md-codewrap').forEach(w=>{
  w.classList.toggle('no-wrap',noWrap);
  w.classList.toggle('no-lines',noLines)})}
/* 复制整条消息 / 复制代码块 */
function copyMsg(btn){let m=btn.closest('.msg');if(!m)return;
 try{navigator.clipboard.writeText(m.dataset.raw||'');btn.textContent=tr('已复制 ✓');setTimeout(()=>btn.textContent='复制',900)}catch(e){}}
function copyCode(btn){let w=btn.closest('.md-codewrap');if(!w)return;let text=w.dataset.raw;
 if(text==null){let c=w.querySelector('code'),clean=c&&c.cloneNode(true);if(clean){clean.querySelectorAll('.fc-ln').forEach(n=>n.remove());text=clean.textContent||''}else text=''}
 try{navigator.clipboard.writeText(text);btn.textContent=tr('已复制 ✓');setTimeout(()=>btn.textContent=tr('复制'),900)}catch(e){}}
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

export { guessResultLang, stepResultRender, applySessionChrome, langLabel, wrapCodeLines, codeLineSel, copySelLines, imgZoom, imgZoomClose, closeCodeMenu, codeMenu, codePref, setCodePref, applyCodePrefs, copyMsg, copyCode, copyText, toggleCodeFold, applyMsgFold, CODE_EXT };
