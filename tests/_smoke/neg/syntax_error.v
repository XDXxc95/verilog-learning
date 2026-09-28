// 故意的语法错误：赋号右边缺操作数。
// 用途：验证 sim.py 能把 iverilog 的报错解析成「文件 / 行 / 列 / 类型」，
// 并给出中文摘要，而不是把整段原始 stderr 丢给学习者。
`timescale 1ns/1ps

module syntax_error (
    input  wire a,
    input  wire b,
    output wire y
);

    assign y = a & ;   // ← 这里缺一个操作数

endmodule
