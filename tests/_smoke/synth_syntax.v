// `compile_error` 的夹具：**前端就过不去的语法错**。
//
// 两处，都是初学者最常打的字：
//
//   第 12 行  少写一个分号（`wire y` 后面直接换行了）—— iverilog 的报错点在**下一行**，
//             这种"报错行不等于出错行"是刚上手时最耗时间的一类
//   第 16 行  `endmodul` 拼错 —— 报的是 `endmodule` 处不匹配
//
// 它和 `no_top` / `not_synthesizable` 的区别在于：**这一类的报错和 sim.py 报的是
// 同一批东西**（同一个前端），所以工具应该说"先去看 sim.py --syntax-only"，
// 而不是让人在两个工具之间来回猜哪个更权威。
//
// 顺带：这个文件**喂给 sim.py 也是这个错**。判定链路上它连编译都过不去，
// 结构检查（lint）压根不会跑 —— 所以 `sim.py` 的 `lint.ran` 会是 false。
// 那正是 `ran` 和 `n_warnings` 必须分开报的原因：没跑 ≠ 跑了且干净。
`timescale 1ns/1ps

module synth_syntax (
    input  wire a
    output wire y
);

    assign y = a;

endmodul
