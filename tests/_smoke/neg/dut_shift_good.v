// 两级移位寄存器的**正确**写法：时序块里用非阻塞赋值。
// q1 和 q2 在同一时钟沿各自取到"沿之前"的旧值，所以 d 需要两个周期才走到 q2。
`timescale 1ns/1ps

module shift2 (
    input  wire clk,
    input  wire d,
    output reg  q1,
    output reg  q2
);

    always @(posedge clk) begin
        q1 <= d;
        q2 <= q1;   // 读的是旧 q1
    end

endmodule
