/* 首屏主题与侧栏状态初始化。
 * 之所以是**独立文件**而不是 <head> 里的内联 <script>：内联脚本会迫使 CSP 保留
 * script-src 'unsafe-inline'，而内联脚本/内联事件处理器正是 XSS 的主要放大器。
 * 必须在样式表之前执行，否则会看到暗→亮闪烁。 */
try {
  // 主题取值：null/auto = 跟随系统；"" / "dark" = 深色；其余为主题 id
  const KNOWN = ["amber", "light", "solarized", "mint"];
  let t = localStorage.getItem("cbp-theme");
  if (t === null || t === "" || t === "auto") {
    // 跟随系统：系统偏好浅色就用浅色，否则用默认深色
    t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) ? "light" : "";
  } else if (t === "dark") {
    t = "";
  } else if (KNOWN.indexOf(t) < 0) {
    t = "";   // 旧主题（nord/dracula/roufen）已移除，回到默认而不是留个空属性
  }
  if (t) document.documentElement.dataset.theme = t;
  if (localStorage.getItem("cbp-rail") === "closed") document.documentElement.dataset.rail = "closed";
} catch (e) { /* 隐私模式下 localStorage 可能抛异常，忽略即可 */ }
