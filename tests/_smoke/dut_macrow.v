// 宏参数化的计数器：位宽由**编译宏** `WIDTH` 决定，而不是 parameter。
//
// 为什么要专门有一个宏版本：`--param` 和 `--define` 是两条完全不同的通路 ——
//
//     parameter  由 `-P<根模块>.NAME=VALUE` 覆盖，只对**根模块**有效
//     宏         由 `-DNAME=VALUE` 覆盖，**全局**生效（include 顺序、`ifdef
//                分支、位宽、乃至模块存不存在都能被它改掉）
//
// 课程里"用宏做条件编译"和"用 parameter 做参数化"经常是同一道题的两种写法，
// 批量回归要能同时扫这两种，否则那种题的证据链是断的。
//
// `ifndef` 兜默认值是刻意写的：这样不带 -D 也能单独跑（默认 4 位），
// 而带了 -D 时命令行赢 —— 后者正是 --define 扫描要验证的性质。
`ifndef WIDTH
  `define WIDTH 4
`endif

`timescale 1ns/1ps

module macrow (
    input  wire                 clk,
    input  wire                 rst_n,
    input  wire                 en,
    output reg  [`WIDTH-1:0]    q
);

    always @(posedge clk) begin
        if (!rst_n)  q <= {`WIDTH{1'b0}};
        else if (en) q <= q + 1'b1;
    end

endmodule
