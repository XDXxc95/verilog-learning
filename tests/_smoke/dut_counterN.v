// 工具链冒烟测试用设计：位宽可参数化的计数器。
// 用途是验证 tools/ 下的工具本身工作正常，不是课程内容。
//
// 为什么专门要一个"参数化"的设计：--param 的整条链路（sim.py 定根模块 →
// iverilog -P → 测试台透传给 DUT）只有在位宽真的变了才看得出来。
// 关键在于**位宽错了不会报错**——iverilog 对放错模块的 -P 一声不响，
// 仿真照常跑完，只是用了默认值。所以配套测试台专门做了一个
// 「数 8 拍之后的值是多少」的判定：它随 WIDTH 变化（8 或 0），
// 参数没生效时这条必红。
`timescale 1ns/1ps

module counterN #(
    parameter WIDTH = 4
) (
    input  wire            clk,
    input  wire            rst_n,
    input  wire            en,
    output reg  [WIDTH-1:0] q
);

    always @(posedge clk) begin
        if (!rst_n)  q <= {WIDTH{1'b0}};
        else if (en) q <= q + 1'b1;
    end

endmodule
