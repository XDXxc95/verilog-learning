// schematic.py 的解析夹具：每一条都是**踩过或差点踩到**的坑。
// 这个文件不参与仿真，只被 check_schematic.py 读文本。

`timescale 1ns/1ps

// ---- 1. 数字字面量不能变成信号名 ----
// `4'h0` 里的 `h0` 在词法上完全长得像标识符。不抠掉的话图上会凭空
// 多出一个 h0 节点，而且它连不到任何地方。
module lit_test (
    input  wire       clk,
    input  wire [3:0] d,
    output reg  [3:0] q
);
    always @(posedge clk) begin
        if (d == 4'h0) q <= 4'd9;
        else           q <= q + 4'b0001;
    end
endmodule


// ---- 2. 括号里的比较运算不是赋值 ----
// `if (cnt <= 4'd5)` 的 `<=` 在括号深度 1，是**比较**；
// 只看符号的话会把它当成赋值，于是 cnt 被从"被读到的信号"里抠掉，
// 图上少一条边且毫无提示。
module cmp_test (
    input  wire       clk,
    input  wire [3:0] cnt,
    output reg        flag
);
    always @(*) begin
        if (cnt <= 4'd5) flag = 1'b1;
        else             flag = 1'b0;
    end
endmodule


// ---- 3. generate：必须显式声明"不支持"，而不是猜 ----
module gen_test (
    input  wire [3:0] a,
    output wire [3:0] y
);
    genvar i;
    generate
        for (i = 0; i < 4; i = i + 1) begin : g
            assign y[i] = ~a[i];
        end
    endgenerate
endmodule


// ---- 4. 显式敏感列表：值得提醒（漏信号会导致仿真/综合不一致）----
module sens_test (
    input  wire a,
    input  wire b,
    output reg  y
);
    always @(a) begin
        y = a & b;
    end
endmodule


// ---- 5. 层次 + 例化输出方向 ----
module leaf (
    input  wire       clk,
    input  wire [3:0] d,
    output reg  [3:0] q
);
    always @(posedge clk) q <= d;
endmodule

module mid (
    input  wire       clk,
    input  wire [3:0] d,
    output wire [3:0] q
);
    leaf u_leaf (.clk(clk), .d(d), .q(q));
endmodule

module sch_top (
    input  wire       clk,
    input  wire [3:0] d,
    output wire [3:0] q
);
    mid u_mid (.clk(clk), .d(d), .q(q));
endmodule
