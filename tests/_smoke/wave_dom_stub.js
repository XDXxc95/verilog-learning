// 极简 DOM / Canvas 2D 桩子，让 wave.py 生成的 JS 能在 node 里**真的跑一遍**。
//
// 目的不是"渲染出图"，而是把 ReferenceError / TypeError 逼出来：
// 语法检查放行 `foo.bar()` 这种写错的名字，真跑一遍才会当场炸。
// canvas 调用全部记成计数，还能顺带断言"确实画了东西"（线条数为 0 说明绘制分支没进）。
"use strict";

const calls = {};
function rec(name) { calls[name] = (calls[name] || 0) + 1; }

// 记下每次 fillText 的**内容和当时的 textAlign**：
// 光标时刻的值是用 textAlign="right" 贴在信号名右边的，
// 于是"右对齐的最后一个字符串"就是最下面那行信号在光标处的值。
// 有了它就能端到端断言：画出来的值 == 解析器算出来的值。
const texts = [];

function makeCtx() {
  const ctx = {
    font: "", textBaseline: "", fillStyle: "", strokeStyle: "",
    lineWidth: 1, textAlign: "left",
    clearRect: () => rec("clearRect"),
    fillRect: () => rec("fillRect"),
    beginPath: () => rec("beginPath"),
    moveTo: () => rec("moveTo"),
    lineTo: () => rec("lineTo"),
    rect: () => rec("rect"),
    clip: () => rec("clip"),
    stroke: () => rec("stroke"),
    fill: () => rec("fill"),
    save: () => rec("save"),
    restore: () => rec("restore"),
    setTransform: () => rec("setTransform"),
    fillText: (s) => { rec("fillText"); texts.push({ s: String(s), align: ctx.textAlign }); },
    measureText: (s) => ({ width: String(s).length * 7 }),
  };
  return ctx;
}

function makeEl(id) {
  const listeners = {};
  const el = {
    id,
    textContent: "",
    className: "",
    style: {},
    width: 1200, height: 700,
    clientWidth: 1200, clientHeight: 700,
    offsetX: 0, offsetY: 0,
    parentElement: null,
    getContext: () => makeCtx(),
    getBoundingClientRect: () => ({ top: 40, left: 0, width: 1200, height: 700 }),
    addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); },
    fire: (t, ev) => { for (const f of listeners[t] || []) f(Object.assign({ preventDefault() {} }, ev)); },
    _listeners: listeners,
  };
  el.parentElement = { clientWidth: 1200 };
  return el;
}

// 注意：用 globalThis.xxx = 而不是 const xxx = ——
// 这是 CJS 模块，模块顶层的 const 只存在于模块作用域里，
// 被 eval 执行的波形 JS 是看不到的（会直接 "document is not defined"）。
const els = {};
globalThis.document = {
  documentElement: { className: "" },
  getElementById: (id) => (els[id] = els[id] || makeEl(id)),
  createElement: makeEl,
};

// window 上的监听不能是空操作：波形脚本把 mouseup 绑在 window 上
// （拖动时鼠标可能移出画布，绑在 canvas 上会漏掉松手）。桩子吞掉它的话，
// dragging 永远为真，之后的 mousemove 全被当成拖动 —— 被测代码看着没毛病，
// 是桩子把路径走岔了。所以这里如实记录下来，并暴露一个触发口。
const winListeners = {};
globalThis.window = {
  innerHeight: 800,
  devicePixelRatio: 1,
  addEventListener: (t, f) => { (winListeners[t] = winListeners[t] || []).push(f); },
};
globalThis.__fireWindow = (t, ev) => {
  for (const f of winListeners[t] || []) f(Object.assign({ preventDefault() {} }, ev));
};
globalThis.matchMedia = () => ({ matches: false });
globalThis.getComputedStyle = () => ({
  getPropertyValue: (n) => ({ "--bg": "#fff", "--fg": "#000", "--dim": "#666",
    "--grid": "#eee", "--panel": "#f9f9f9", "--line": "#ddd", "--accent": "#06f",
    "--v0": "#077", "--v1": "#b40", "--vx": "#d00", "--vz": "#73a",
    "--bus": "#333", "--cursor": "#e44" }[n] || "#000"),
});
globalThis.__calls = calls;
globalThis.__els = els;
globalThis.__texts = texts;
globalThis.__resetTexts = () => { texts.length = 0; };
