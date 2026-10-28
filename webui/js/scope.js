// scope.js — 任务进行中的示波器浮层（从 app.js 拆出的第一块 ES 模块）。
// 自包含：不依赖 app.js 的任何函数/变量；状态都是模块私有的。
// 由 js/main.js 把 startScope/stopScope/scopeFrame 绑到 window，
// 老代码（app.js 的事件路径）照旧调用。

let _scopeRAF=null, _scopeStyle=null;
let _scopeIntensity=0, _scopeTarget=0, _scopeTimer=0;
let _scopeSeeds={f1:0.045,f2:0.12,f3:0.03,mix:15};
function _scopeCtx(){
 const cv=document.getElementById('teamScope');
 if(!cv)return null;
 const dpr=Math.min(2, window.devicePixelRatio||1);
 const w=Math.max(1, Math.round(cv.clientWidth)), hgt=Math.max(1, Math.round(cv.clientHeight));
 if(cv.width!==Math.round(w*dpr)||cv.height!==Math.round(hgt*dpr)){
  cv.width=Math.round(w*dpr); cv.height=Math.round(hgt*dpr);
 }
 const ctx=cv.getContext('2d');
 if(!ctx)return null;
 ctx.setTransform(dpr,0,0,dpr,0,0);
 return {ctx, w, h: hgt, cv};
}
function _scopeColors(){
 const card=document.getElementById('teamCard');
 const cs=getComputedStyle(card);
 let bg=cs.backgroundColor;
 if(!bg||bg==='rgba(0, 0, 0, 0)'||bg==='transparent')bg='#ffffff';
 let accent=cs.getPropertyValue('--accent').trim()||'#2563eb';
 // 规范化：canvas 会把 color-mix()/oklch() 之类解析成 #rrggbb，
 // 否则 _rgba() 拿到未解析的字符串会画不出颜色（不同主题的 --accent 写法不一）。
 try{
  const probe=document.createElement('canvas').getContext('2d');
  probe.fillStyle='#000000';
  probe.fillStyle=accent;
  const norm=probe.fillStyle;
  if(norm&&norm!=='#000000')accent=norm;
 }catch(e){}
 return {bg, accent};
}
function _rgba(color, alpha){
 const c=String(color||'').trim();
 let m=/^#([0-9a-f]{6})$/i.exec(c);
 if(m){
  const n=parseInt(m[1],16);
  return `rgba(${(n>>16)&255}, ${(n>>8)&255}, ${n&255}, ${alpha})`;
 }
 m=/^#([0-9a-f]{3})$/i.exec(c);
 if(m){
  const h=m[1];
  return `rgba(${parseInt(h[0]+h[0],16)}, ${parseInt(h[1]+h[1],16)}, ${parseInt(h[2]+h[2],16)}, ${alpha})`;
 }
 return c;
}
/* 一帧：绝对随机、原地跳动（用户最终选定的算法） */
let _scopeFrameNo=0;
function scopeFrame(){
 const o=_scopeCtx();
 if(!o)return;
 const {ctx, w, h}=o;
 _scopeFrameNo++;
 if(!_scopeStyle||_scopeFrameNo%20===0)_scopeStyle=_scopeColors();   // 切主题后自动跟上
 const bg=_scopeStyle.bg, accent=_scopeStyle.accent;

 // 每帧清成**全透明**（不要背景，只要那条线）
 ctx.clearRect(0,0,w,h);

 // 状态机：爆发（并重组骨架）/ 熄灭
 _scopeTimer -= 1;
 if(_scopeTimer<=0){
  if(Math.random()>0.4){
   _scopeTarget=0.4+Math.random()*1.6;
   _scopeTimer=6+Math.floor(Math.random()*10);
   _scopeSeeds={
    f1:0.02+Math.random()*0.05,
    f2:0.08+Math.random()*0.12,
    f3:0.01+Math.random()*0.04,
    mix:5+Math.random()*20
   };
  }else{
   _scopeTarget=0.10+Math.random()*0.12; // 平静期：不再完全趴平，保留轻微起伏
   _scopeTimer=12+Math.floor(Math.random()*28);
  }
 }
 // 快速缓动（弹缩感）
 _scopeIntensity += (_scopeTarget-_scopeIntensity)*0.65;

 const mid=h/2;
 // 振幅按半高的 95%（demo 是 85/225 ≈ 38%，这里取更满一点，峰值顶到带边）
 const amp=(h/2)*0.80, k=amp/115;
 const xz=Math.max(1, 900/w);              // 采样密度对齐 demo 的 900px 基准
 const grad=ctx.createLinearGradient(0,0,w,0);
 grad.addColorStop(0,   _rgba(accent,0));
 grad.addColorStop(0.15,_rgba(accent,1));
 grad.addColorStop(0.85,_rgba(accent,1));
 grad.addColorStop(1,   _rgba(accent,0));

 ctx.beginPath();
 ctx.strokeStyle=grad;
 ctx.lineWidth=1.8;
 ctx.lineJoin='miter';
 ctx.miterLimit=10;
 ctx.shadowBlur=0;                          // 零光晕，刀锋质感
 ctx.shadowColor='transparent';

 // 动态高斯宽度：越高越窄（紧绷），越低越宽（松弛）
 const dynamicMaskWidth = 18 + Math.max(0, _scopeIntensity) * 16;
 const s=_scopeSeeds;
 for(let x=0;x<w;x++){
  const xs=x*xz;
  const base=Math.sin(xs*s.f1);
  const sign=base>=0?1:-1;
  let sharp=sign*(1-Math.pow(1-Math.abs(base),0.35))*85;
  sharp += Math.sin(xs*s.f2)*s.mix*Math.cos(xs*s.f3);
  const d=(x-w/2)/(w/2);
  const mask=Math.exp(-dynamicMaskWidth*(d*d));   // ← 弹性"肥瘦联动"就在这里
  const y=mid-(sharp*k)*_scopeIntensity*mask;
  if(x===0)ctx.moveTo(x,y); else ctx.lineTo(x,y);
 }
 ctx.stroke();
}
function startScope(){
 if(_scopeRAF)return;
 try{if(localStorage.getItem('gantry-teamscope-heartbeat')==='0')return;}catch(e){}
 const reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
 _scopeStyle=null;
 _scopeIntensity=0.5; _scopeTarget=0.9; _scopeTimer=12;
 scopeFrame();
 if(reduce)return;
 const tick=()=>{ if(document.hidden){_scopeRAF=null;return;} scopeFrame(); _scopeRAF=requestAnimationFrame(tick); };
 _scopeRAF=requestAnimationFrame(tick);
}
function stopScope(){
 if(_scopeRAF){cancelAnimationFrame(_scopeRAF);_scopeRAF=null}
 const o=_scopeCtx(); if(!o)return;
 try{o.ctx.clearRect(0,0,o.w,o.h)}catch(e){}
}


export { startScope, stopScope, scopeFrame };
