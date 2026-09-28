// 组合环（combinational loop）的冒烟用例。
//
// 存在的理由：这是**只有 verilator 报得出来**的一类结构错误。
// iverilog 对下面这个 assign 完全沉默（实测 -Wall 一句 warning 都没有），
// 仿真也能收敛、功能用例能全过——所以单靠判定链路发现不了它。
// 它必须同时证明两件事：
//   1. status 不受影响（功能上确实是对的，latch 那类"仿真过但硬件错"的问题同理）
//   2. lint 块如实报告 verilator 跑过，并且报出了 combinational_loop
//
// 顺带说明为什么不能拿"多驱动"当这个用例：两个 always 同时写一个 reg 时，
// 谁最后生效是**仿真器实现相关**的，用它写断言今天绿明天可能就红。
// 组合环的仿真结果是确定的，断言才立得住。
`timescale 1ns/1ps

module comb_loop (
    input  wire en,
    input  wire d,
    output wire q
);

    // 意图：en 为 0 时让 q「保持上一次的值」。
    // 坑：组合逻辑里根本没有「上一次」这个概念。写成自反馈之后
    // q 经过自己再算一遍自己，形成一个零延迟的环——
    // 仿真器靠内部迭代顺序求解，综合出来的电路则会振荡/出毛刺，
    // 静态时序分析也算不出有意义的路径延时。
    // 想要保持，必须用寄存器（always @(posedge clk) q <= ...）打断这个环。
    assign q = en ? d : q;

endmodule
