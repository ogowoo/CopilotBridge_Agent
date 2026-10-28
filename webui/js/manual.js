// manual.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
import { MANUAL_HTML, MANUAL_HTML_EN, MANUAL_COLLAPSE } from './control.js';
function enhanceManual(){
 const body=document.getElementById('manualBody');
 if(!body)return;
 body.querySelectorAll('.mtoc').forEach(x=>x.remove());
 const hs=[...body.querySelectorAll('h3')];
 hs.forEach((h,i)=>h.id='mh'+i);
 let toc=document.createElement('nav');
 toc.className='mtoc';
 toc.innerHTML='<b>'+esc(tr('目录'))+'</b>'+hs.map((h,i)=>`<a href="#mh${i}">${esc(h.textContent)}</a>`).join('');
 body.insertBefore(toc, body.firstChild);
 toc.querySelectorAll('a').forEach(a=>a.onclick=e=>{
  e.preventDefault();
  const t=document.getElementById(a.getAttribute('href').slice(1));
  if(t)t.scrollIntoView({behavior:'smooth',block:'start'});});
 hs.forEach(h=>{
  if(!MANUAL_COLLAPSE().some(k=>h.textContent.includes(k)))return;
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

function openManual(){const body=document.getElementById('manualBody');
 body.innerHTML=(currentLang()==='en')?MANUAL_HTML_EN:MANUAL_HTML;enhanceManual();
 // 手册是动态注入的：注入后再走一遍翻译。章节标题有英文条目，正文按取舍保持中文
 // （长文档整篇翻译成本高且易失真，该取舍写在 README 的 i18n 说明里）。
 try{if(typeof applyI18n==='function')applyI18n(body)}catch(e){}
 openMask('manualMask')}
function closeManual(){closeMask('manualMask')}
function openTask(){
 try{loadMcpPrompts()}catch(e){}
 // 侧栏收起时编辑器区域虽然还在，但"从左侧选会话"的提示会误导：先展开侧栏
 try{if(document.documentElement.dataset.rail==='closed')toggleRail()}catch(e){}
 // 新会话视图（DSH 式）：空态就是编辑器，没有再套一层弹窗
 document.getElementById('emptyState').style.display='flex';
 document.getElementById('sessionView').style.display='none';
 current=null; try{drawSessions()}catch(e){}
 const t=document.getElementById('task'); if(t){t.value='';t.focus()}
}

document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeDlg();closeLive();gmClose(null);closeManual();closePlugins();closeWs();closePanels();try{closeSettings()}catch(_){}}});

/* 把独立的「步骤时间线」卡片并入实时面板标签页（像 DSH 那样一个面板看全部）。
   直接搬 DOM 节点：筛选下拉、导出按钮、每步的展开/收起全部照旧，不重写渲染逻辑。 */

export { enhanceManual, openManual, closeManual, openTask };
