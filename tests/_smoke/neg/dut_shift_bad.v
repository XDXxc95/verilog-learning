// 两级移位寄存器的**错误**写法：时序块里用阻塞赋值。
// q1 = d 立刻生效，紧接着 q2 = q1 读到的是**刚写进去的新值**，
// 于是两级在同一拍一起变 —— 两级寄存器塌缩成一级，d 一个周期就窜到 q2。
// 这就是 blocking_in_seq 这个错因标签要抓的东西。
//
// 它是仿真能编译、能跑，但行为错的典型。配 tb_shift.v 应判定为失败。
//
// 模块名与 dut_shift_good.v 里的**同为 shift2**，这是故意的：
// 同一个测试台、同一个顶层名，只有实现不同，对比才干净
// （viz.py --compare 也是靠这一点把两条波形叠在一起看）。
// 两者**不要同时编译**。
`timescale 1ns/1ps

module shift2 (
    input  wire clk,
    input  wire d,
    output reg  q1,
    output reg  q2
);

    always @(posedge clk) begin
        q1 = d;
        q2 = q1;   // ← 应为 <=
    end

endmodule
