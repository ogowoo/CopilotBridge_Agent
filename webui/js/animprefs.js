// animprefs.js — 动画参数（示波器 + 输入框呼吸）。
//
// 为什么单独一个模块：这两组参数**两个地方都要用**——
//   · 设置页负责读写（webui/js/settings.js）；
//   · 示波器每帧要用（webui/js/scope.js），呼吸是纯 CSS（css/composer.css 里的变量）。
// 存 localStorage，键名沿用 `gantry-teamscope-*`（老用户的心跳开关不丢），
// 应用方式：呼吸 → 写 `<html>` 上的 CSS 变量；示波器 → scope.js 读这个模块。
//
// 所有数值都过 clamp：手改 localStorage 写坏一个 `abc`，也不该让动画变成 NaN 静默消失。

const ANIM_DEFAULTS = {
  scopeOn: true,          // 任务进行时画示波器
  scopeAmp: 1.0,          // 振幅倍数（1.0 = 原设计：半高的 80%）
  scopeLine: 1.8,         // 线宽 px
  scopeOpacity: 1.0,      // 线的不透明度（1.0 = 原设计）
  scopeSpeed: 1.0,        // 横向速度倍数（波形疏密/流动）
  breatheOn: true,        // 运行中输入框边框呼吸
  breatheDur: 1.8,        // 一个呼吸周期（秒）
  breatheStrength: 100,   // 强度 %（100 = 原设计：边框混 88% 强调色 + 18px 光晕）
};

const ANIM_RANGES = {
  scopeAmp: [0.4, 1.6],
  scopeLine: [0.6, 4],
  scopeOpacity: [0.15, 1],
  scopeSpeed: [0.3, 2.5],
  breatheDur: [0.6, 4],
  breatheStrength: [0, 100],
};

const ANIM_KEYS = {
  scopeOn: 'gantry-teamscope-heartbeat',
  scopeAmp: 'gantry-teamscope-amp',
  scopeLine: 'gantry-teamscope-line',
  scopeOpacity: 'gantry-teamscope-opacity',
  scopeSpeed: 'gantry-teamscope-speed',
  breatheOn: 'gantry-breathe-on',
  breatheDur: 'gantry-breathe-dur',
  breatheStrength: 'gantry-breathe-strength',
};

function clampNum(v, lo, hi) {
  const n = Number(v);
  if (!isFinite(n)) return lo;
  return Math.min(hi, Math.max(lo, n));
}

/** 读全部参数（带默认值与区间钳制）。 */
function readAnimPrefs() {
  const out = Object.assign({}, ANIM_DEFAULTS);
  try {
    if (localStorage.getItem(ANIM_KEYS.scopeOn) === '0') out.scopeOn = false;
    if (localStorage.getItem(ANIM_KEYS.breatheOn) === '0') out.breatheOn = false;
    Object.keys(ANIM_RANGES).forEach((k) => {
      const raw = localStorage.getItem(ANIM_KEYS[k]);
      if (raw !== null && String(raw).trim() !== '') {
        out[k] = clampNum(raw, ANIM_RANGES[k][0], ANIM_RANGES[k][1]);
      }
    });
  } catch (e) { /* 隐私模式下 localStorage 会抛，按默认值跑 */ }
  return out;
}

/** 把呼吸参数写成 CSS 变量（scope.js 那边靠 'gantry-animprefs' 事件刷新缓存）。 */
function applyAnimPrefs() {
  const p = readAnimPrefs();
  const root = document.documentElement;
  const s = clampNum(p.breatheStrength, 0, 100) / 100;
  root.style.setProperty('--breathe-dur', p.breatheDur.toFixed(2) + 's');
  root.style.setProperty('--breathe-hi', (32 + 56 * s).toFixed(1) + '%');
  root.style.setProperty('--breathe-ring', (3 * s).toFixed(2) + 'px');
  root.style.setProperty('--breathe-ring-a', (18 * s).toFixed(1) + '%');
  root.style.setProperty('--breathe-glow', (18 * s).toFixed(1) + 'px');
  root.style.setProperty('--breathe-glow-a', (22 * s).toFixed(1) + '%');
  if (p.breatheOn) root.removeAttribute('data-breathe');
  else root.setAttribute('data-breathe', 'off');
  try {
    document.dispatchEvent(new CustomEvent('gantry-animprefs', { detail: p }));
  } catch (e) { /* 老浏览器没有 CustomEvent 构造器也不影响主流程 */ }
  return p;
}

/** 写一个参数并立即生效（设置页的滑杆/开关调它）。 */
function setAnimPref(key, value) {
  const storageKey = ANIM_KEYS[key];
  if (!storageKey) return readAnimPrefs();
  let v = value;
  if (key === 'scopeOn' || key === 'breatheOn') {
    v = (value === true || value === 'true' || value === '1' || value === 1) ? '1' : '0';
  } else if (ANIM_RANGES[key]) {
    v = String(clampNum(value, ANIM_RANGES[key][0], ANIM_RANGES[key][1]));
  }
  try { localStorage.setItem(storageKey, String(v)); } catch (e) { /* 忽略配额/隐私模式 */ }
  return applyAnimPrefs();
}

/** 把参数显示成一行字（设置页右侧的数值）。 */
function animPrefLabel(key, prefs) {
  const p = prefs || readAnimPrefs();
  if (key === 'scopeAmp') return Math.round(p.scopeAmp * 100) + '%';
  if (key === 'scopeLine') return p.scopeLine.toFixed(1) + 'px';
  if (key === 'scopeOpacity') return Math.round(p.scopeOpacity * 100) + '%';
  if (key === 'scopeSpeed') return p.scopeSpeed.toFixed(2) + 'x';
  if (key === 'breatheDur') return p.breatheDur.toFixed(1) + 's';
  if (key === 'breatheStrength') return Math.round(p.breatheStrength) + '%';
  return '';
}

export {
  ANIM_DEFAULTS, ANIM_RANGES, ANIM_KEYS,
  readAnimPrefs, applyAnimPrefs, setAnimPref, animPrefLabel, clampNum,
};
