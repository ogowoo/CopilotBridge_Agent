// atmenu.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
let _atTimer=null, _atItems=[], _atIdx=0, _atOpen=false;
function _atQuery(){
 const ta=document.getElementById('followup');
 if(!ta)return null;
 const pos=ta.selectionStart||0;
 const before=ta.value.slice(0,pos);
 // 光标前最后一个 @（前面是空格/开头），取到光标为止的连续非空白字符
 const m=/(^|[\s])@([^\s@]{0,80})$/.exec(before);
 if(!m)return null;
 return {query:m[2], start:pos-m[2].length};
}
async function atUpdate(){
 const q=_atQuery();
 const menu=document.getElementById('atMenu');
 if(!menu)return;
 if(!q){atClose();return}
 try{
  const d=await api('/v1/agent/workspace/files?sid='+encodeURIComponent(window.current||'')
   +'&q='+encodeURIComponent(q.query)+'&limit=50');
  _atItems=d.files||[]; _atIdx=0;
 }catch(e){_atItems=[]}
 if(!_atItems.length){menu.innerHTML='<div class="meta" style="padding:8px 10px">'
  +esc(tr('没有匹配的文件'))+'</div>';}
 else menu.innerHTML=_atItems.map((f,i)=>`<div class="atrow${i===_atIdx?' on':''}" data-i="${i}">
   <code>📄</code><span class="sdesc" title="${esc(f)}">${esc(f)}</span></div>`).join('');
 menu.hidden=false; _atOpen=true;
 menu.querySelectorAll('.atrow').forEach(row=>{
  row.onclick=()=>atPick(Number(row.dataset.i));
 });
}
function atPick(i){
 const f=_atItems[i]; if(!f)return;
 const ta=document.getElementById('followup'), q=_atQuery();
 if(!ta||!q){atClose();return}
 // 用选中的路径替换 @query（保留 @ 前缀）
 ta.value=ta.value.slice(0,q.start)+f+ta.value.slice(q.start+q.query.length);
 ta.focus(); atClose();
}
function atClose(){
 const menu=document.getElementById('atMenu');
 if(menu)menu.hidden=true; _atOpen=false;
}

export { atUpdate, atPick, atClose };


/* 输入框事件绑定：和菜单状态同住（_atOpen/_atIdx/_atItems/_atTimer 都是本模块私有，
   app.js 里的旧 handler 碰不到它们——迁移时留下的坑，2026-10-28 修）。 */
export function bindAtMenu(el){
 if(!el)return;
 el.addEventListener('input',()=>{clearTimeout(_atTimer);_atTimer=setTimeout(atUpdate,200)});
 el.addEventListener('keydown',e=>{
  if(!_atOpen)return;
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){
   e.preventDefault();
   _atIdx=Math.max(0,Math.min(_atItems.length-1,_atIdx+(e.key==='ArrowDown'?1:-1)));
   document.querySelectorAll('#atMenu .atrow').forEach((r,i)=>r.classList.toggle('on',i===_atIdx));
  }else if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();atPick(_atIdx)}
  else if(e.key==='Escape'){e.preventDefault();atClose()}
 });
 document.addEventListener('click',e=>{if(_atOpen&&!e.target.closest('#atMenu')&&!e.target.closest('#followup'))atClose()});
}

