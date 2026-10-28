// dialogs.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
function gmShow(o){if(_gmResolve){const prev=_gmResolve;_gmResolve=null;prev(null)}  // 重入时先了结上一个，防 Promise 悬挂
 return new Promise(res=>{
 _gmResolve=res;
 document.getElementById('gmTitle').textContent=o.title||tr('提示');
 let m=document.getElementById('gmMsg');m.textContent=o.msg||'';m.style.display=o.msg?'':'none';
 let inp=document.getElementById('gmInput');
 if(o.input!==undefined&&o.input!==null){inp.style.display='';inp.value=o.input}else{inp.style.display='none'}
 // 可选：插入方式选择（step 立即遵守 / turn 整轮重排），只有强行插入用得上
 let modes=document.getElementById('gmModes');
 if(modes){
  modes.style.display=o.modes?'':'none';
  if(o.modes){let pick=modes.querySelector('input[value="'+o.modes.default+'"]');
   if(pick)pick.checked=true}}
 let c=document.getElementById('gmCancel');c.style.display=o.cancelText===null?'none':'';c.textContent=o.cancelText||tr('取消');
 let ok=document.getElementById('gmOk');ok.textContent=o.okText||tr('确定');ok.className='btn '+(o.danger?'danger':'accent');
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
let _maskZ=100;
function openMask(id){
 const m=document.getElementById(id);if(!m)return;
 _focusBeforeMask=document.activeElement;
 m.classList.add('open');
 // **后打开的盖住先打开的**：面板从插件管理里打开时必须在上层（实测被挡住）。
 m.style.zIndex=String(++_maskZ);
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
 const lastMode=(()=>{try{return localStorage.getItem('gantry-inject-mode')||defaultMode||'step'}catch(e){return defaultMode||'step'}})();
 return gmShow({title,msg,input:def,okText:tr('插入'),modes:{default:lastMode}})};

/* ── 跟进消息模式切换（卡片头部按钮，不再占用输入区） ── */

export { gmShow, openMask, closeMask, gmClose, dlgAlert, dlgConfirm, dlgInjectPrompt, dlgPrompt };
