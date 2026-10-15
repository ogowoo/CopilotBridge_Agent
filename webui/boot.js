/* 首屏主题与侧栏状态初始化。
 * 之所以是**独立文件**而不是 <head> 里的内联 <script>：内联脚本会迫使 CSP 保留
 * script-src 'unsafe-inline'，而内联脚本/内联事件处理器正是 XSS 的主要放大器。
 * 必须在样式表之前执行，否则会看到暗→亮闪烁。 */
try {
  const t = localStorage.getItem("cbp-theme");
  if (t && t !== "dark") document.documentElement.dataset.theme = t;
  if (localStorage.getItem("cbp-rail") === "closed") document.documentElement.dataset.rail = "closed";
} catch (e) { /* 隐私模式下 localStorage 可能抛异常，忽略即可 */ }
