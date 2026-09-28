// 故意的 latch 推断：组合 always 里的 case 没有 default，且分支没覆盖全部取值。
// 2'b10 / 2'b11 时 y 保持上一次的值 → 综合出锁存器。
//
// 用途：验证 sim.py 能识别出锁存器并打上 latch_inference 标签。
//
// 注意：这个模块**能编译、能仿真**，功能测试甚至可能通过 ——
// 它错在综合后的硬件结构不对，是典型的"仿真看不出来"的问题。
//
// 并且 iverilog **不会**对此报警 —— 它是事件驱动仿真器，没有综合概念，
// `-Wall` 实测一句 warning 都没有。所以这条判定来自 tools/_lint.py
// 自己的源码扫描，不是 iverilog 的输出。
`timescale 1ns/1ps

module latch_incomplete_case (
    input  wire [1:0] sel,
    input  wire       a,
    input  wire       b,
    output reg        y
);

    always @(*) begin
        case (sel)
            2'b00: y = a;
            2'b01: y = b;
            // 缺 default：sel 为 2'b10 / 2'b11 时 y 保持不变 → 锁存器
        endcase
    end

endmodule
