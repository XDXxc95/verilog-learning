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


// ---- 3. generate 边界能算出来：应当展开，而不是拒绝 ----
// 循环上界是裸字面量 4，光看源码就知道跑 4 圈，没有任何理由不画。
// 展开后 4 条 assign 在信号级（位下标已抹掉）**完全一样**，合并成一条
// 并标 ×4 —— 画 4 个同样的框会让人以为是 4 段不同的逻辑。
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


// ---- 6. 参数化 generate + 参数化位宽 ----
// 边界走参数 N，位宽走参数 DW。两者都由默认值算出来：N=4、DW=8。
// 这条同时验证两件事：循环展开了 4 份、`[DW-1:0]` 被算成了 8 位。
module gen_param_test #(
    parameter N  = 4,
    parameter DW = 8
)(
    input  wire [DW-1:0] a,
    output wire [DW-1:0] y
);
    genvar i;
    generate
        for (i = 0; i < N; i = i + 1) begin : bit_loop
            assign y[i] = ~a[i];
        end
    endgenerate
endmodule


// ---- 7. 嵌套 generate：两层循环，变量替换要一层层套 ----
module gen_nest_test #(
    parameter R = 2,
    parameter C = 3
)(
    input  wire [R*C-1:0] a,
    output wire [R*C-1:0] y
);
    genvar r, c;
    generate
        for (r = 0; r < R; r = r + 1) begin : row
            for (c = 0; c < C; c = c + 1) begin : col
                assign y[r*C+c] = ~a[r*C+c];
            end
        end
    endgenerate
endmodule


// ---- 8. if generate：只有一支是真的 ----
// 参数化结构图的意义就在这儿：同一份源码，WIDE 取 0 或 1 是两个不同的电路。
// 图只画当前取值的那一支，**另一支必须写进说明**，否则读者会以为
// 源码里根本没有它。
module gen_if_test #(
    parameter WIDE = 1
)(
    input  wire [3:0] a,
    output wire [3:0] y
);
    generate
        if (WIDE) begin : wide_path
            assign y = ~a;
        end else begin : narrow_path
            assign y = a;
        end
    endgenerate
endmodule


// ---- 9. 算不出来就必须拒绝，绝不猜份数 ----
// 默认值是**编译宏**，源码里没有它的值（由 -D 或 include 提供）。
// 这里必须：① 不画 ② 说清楚为什么 ③ 那条 `for` 绝不能被当成例化名
// 解析出一个叫 `for` 的假节点 ④ **手写的部分照画** —— 算不出来的
// 只是那块 generate，把整个模块停掉是过度反应。
// `sel` 的位宽也依赖这个算不出来的 N，所以要照抄源码原文显示，
// 而且它被手写的那条 assign 读到，因此真的会出现在图上。
module gen_macro_test #(
    parameter N = `NUM_CH
)(
    input  wire [3:0]   a,
    input  wire [N-1:0] sel,
    output wire [3:0]   y
);
    assign y = sel[3:0] ^ a;

    genvar i;
    generate
        for (i = 0; i < N; i = i + 1) begin : g
            assign y[i] = a[i];
        end
    endgenerate
endmodule


// ---- 10. 循环条件依赖的标识符压根不存在 ----
// 比宏那条更常见的真实 bug：参数忘了声明。同样必须拒绝并报出名字。
// 位宽 `[WIDTH-1:0]` 也因此算不出来 —— 这里要照抄源码原文显示，
// 而不是空着（空着会被读成"这是个 1 位信号"，是另一回事）。
module gen_undef_test (
    input  wire [3:0]     a,
    input  wire [WIDTH-1:0] w,
    output wire [3:0]     y
);
    genvar i;
    generate
        for (i = 0; i < WIDTH; i = i + 1) begin : g
            assign y[i] = a[i];
        end
    endgenerate
endmodule


// ---- 11. case generate：明确不支持 ----
// 不是"没实现"，是**故意不画**：case 的分支按 `值 : 语句` 切，
// 而冒号在位宽和三目运算符里都出现，切错了会静默丢分支。
module gen_case_test #(
    parameter MODE = 0
)(
    input  wire [3:0] a,
    output wire [3:0] y
);
    generate
        case (MODE)
            0:       assign y = a;
            1:       assign y = ~a;
            default: assign y = 4'b0;
        endcase
    endgenerate
endmodule


// ---- 12. generate 里例化子模块：合并成一条 ×N ----
// 例化名 `u_leaf` 在展开后重复 N 次是**正常**的（唯一名字由层次名给出），
// 不是重名错误。
module gen_inst_test (
    input  wire       clk,
    input  wire [3:0] d,
    output wire [3:0] q
);
    genvar i;
    generate
        for (i = 0; i < 4; i = i + 1) begin : col
            leaf u_leaf (.clk(clk), .d(d[i]), .q(q[i]));
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


// ============================================================
// 以下是第二轮加的对抗性用例。它们各自抓出过一个真 bug，
// 所以留在夹具里当护栏 —— 不是"多写几个例子"，是"这几个都漏过"。
// ============================================================

// ---- 13. 隐式 generate：不带 generate/endgenerate 关键字 ----
// Verilog-2001 起，模块体里直接写 for 就是 generate。
// 不认它的话那条 for 会掉进 _parse_instance，凭空多一个叫 `for` 的节点。
module sg_implicit_for (
    input  wire [3:0] a,
    output wire [3:0] y
);
    genvar i;
    for (i = 0; i < 4; i = i + 1) begin : g
        assign y[i] = ~a[i];
    end
endmodule


// ---- 14. 循环体没有 begin-end ----
module sg_no_begin (
    input  wire [3:0] a,
    output wire [3:0] y
);
    genvar i;
    generate
        for (i = 0; i < 4; i = i + 1) assign y[i] = a[i];
    endgenerate
endmodule


// ---- 15. generate 里套 always：展开的是时序块 ----
module sg_gen_always #(parameter W = 2) (
    input  wire         clk,
    input  wire [W-1:0] d,
    output reg  [W-1:0] q
);
    genvar i;
    generate
        for (i = 0; i < W; i = i + 1) begin : ff
            always @(posedge clk) q[i] <= d[i];
        end
    endgenerate
endmodule


// ---- 16. 位宽和表达式里都有 $clog2 ----
// 系统函数名在词法上长得像标识符（`$` 是非单词字符，`\b` 断在它后面），
// 不整段抠掉就会报出「未在本模块声明的名字：clog2」——一条虚构的警告，
// 而且还会往图上多画一个节点。
module sg_clog2 #(parameter DEPTH = 16) (
    input  wire                     clk,
    input  wire [$clog2(DEPTH)-1:0] addr,
    output reg  [DEPTH-1:0]         onehot
);
    genvar i;
    generate
        for (i = 0; i < DEPTH; i = i + 1) begin : dec
            always @(posedge clk) onehot[i] <= (addr == i[$clog2(DEPTH)-1:0]);
        end
    endgenerate
endmodule


// ---- 17. always 里面的 for / if / case **不是** generate ----
// 这是最要紧的一条反面用例：把 always 体内的循环当成 generate 展开，
// 会得出一个完全错误的结构图，而表面上一切正常。
module sg_not_generate (
    input  wire       clk,
    input  wire [3:0] d,
    output reg  [3:0] q
);
    integer k;
    always @(posedge clk) begin
        for (k = 0; k < 4; k = k + 1) q[k] <= d[k];
        if (d == 4'd0) q <= 4'd1;
        case (d)
            4'd1:    q <= 4'd2;
            default: q <= d;
        endcase
    end
endmodule


// ---- 18. 注释里写着 generate / for / endgenerate ----
module sg_comment_trap (
    input  wire [3:0] a,
    output wire [3:0] y
);
    // 行注释里写了 generate 和 for (i = 0; i < 99; i = i + 1)
    /* 块注释里再来一套：generate for (i = 0; i < 7; i = i + 1) */
    genvar i;
    generate
        for (i = 0; i < 4; i = i + 1) begin : g
            assign y[i] = ~a[i];   // 行尾注释：endgenerate
        end
    endgenerate
endmodule


// ---- 19. 循环里套 if generate：标签要按取到的那一支取 ----
module sg_nested_if #(parameter MODE = 2, parameter W = 3) (
    input  wire [W-1:0] a,
    output wire [W-1:0] y
);
    genvar i;
    generate
        for (i = 0; i < W; i = i + 1) begin : bits
            if (MODE == 0) begin : inv
                assign y[i] = ~a[i];
            end else begin : buf
                assign y[i] = a[i];
            end
        end
    endgenerate
endmodule


// ---- 20. 循环跑 0 圈：合法，什么都不画，但要说一声 ----
module sg_zero_iter #(parameter N = 0) (
    input  wire [3:0] a,
    output wire [3:0] y
);
    genvar i;
    generate
        for (i = 0; i < N; i = i + 1) begin : g
            assign y[i] = a[i];
        end
    endgenerate
endmodule


// ---- 21. function 体内的 if：不是 generate，也不能变成假节点 ----
// 从前这里会冒出一个叫 `begin if` 的实例：iter_statements 对
// `function ... begin ... end ... endfunction` 的 begin/end 计数会切出
// 碎片，碎片的头是 `begin`，落到 _parse_instance 就成了节点。
module sg_func_if (
    input  wire       clk,
    input  wire [3:0] d,
    output reg  [3:0] q
);
    function integer fmax(input integer x);
        begin
            if (x > 4) fmax = 4;
            else       fmax = x;
        end
    endfunction

    always @(posedge clk) q <= d;
endmodule
