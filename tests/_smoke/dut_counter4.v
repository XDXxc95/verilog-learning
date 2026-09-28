// 工具链冒烟测试用设计：带同步复位与使能的 4 位计数器。
// 用途是验证 tools/ 下的工具本身工作正常，不是课程内容。
`timescale 1ns/1ps

module counter4 (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       en,
    output reg  [3:0] q
);

    always @(posedge clk) begin
        if (!rst_n)  q <= 4'h0;
        else if (en) q <= q + 4'h1;
    end

endmodule
