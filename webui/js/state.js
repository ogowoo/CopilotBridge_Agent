// state.js — 共享可变状态的家（ES Modules 迁移的第一块基石）。
//
// 背景：webui/app.js（5270 行单体）正在按块拆成 ES 模块（见 docs/ESM-MIGRATION）。
// 经典脚本时代所有函数共享顶层变量；模块化后，**可变状态**统一住在这个 S 对象里，
// 各模块 `import { S } from './state.js'` 然后 `S.xxx = ...`（ES 模块导出是只读绑定，
// 直接改导出变量会 TypeError，必须用对象属性承载可变状态）。
//
// 现在还是空的：随块迁过来时再往里搬（current / sessCache / lastRuntime / settingsValues…）。
export const S = {};
