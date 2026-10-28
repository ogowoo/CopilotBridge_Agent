// main.js — ES 模块入口（app.js 解耦的入口）。
//
// 迁移策略（增量、每步全绿）：
// 1. 从 app.js 拆出一个自包含块 → webui/js/<块>.js（ES 模块）；
// 2. 这里 import 它，把老代码仍在调用的全局函数绑到 window（window.startScope 等）；
// 3. app.js 删掉原块；事件路径的调用点不用动（模块是 deferred，用户交互时早已绑定）。
//
// 等所有块迁完，app.js 就空了，index.html 只留这一个 <script type="module">。
import { S } from './state.js';
import { startScope, stopScope, scopeFrame } from './scope.js';

// 共享可变状态（现在是空的，随块迁过来再填）
window.__S = S;

// 老代码在用的全局入口（app.js 里 startScope()/stopScope() 的调用点照旧工作）
window.startScope = startScope;
window.stopScope = stopScope;
window.scopeFrame = scopeFrame;   // 调试/审计脚本有用（量波形用）
