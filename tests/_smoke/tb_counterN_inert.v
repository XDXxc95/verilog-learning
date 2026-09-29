// regress.py 的**负例夹具**：一个"参数扫了等于没扫"的测试台。
//
// 它复现的是参数化验证里最难发现的一种失败：
//
//     `module tb_counterN_inert #(parameter WIDTH = 4);`
//     ...
//     counterN u (.clk(clk), .rst_n(rst_n), .en(en), .q(q));   ← 忘了 #(.WIDTH(WIDTH))
//
// WIDTH 声明在测试台上，所以 `iverilog -P` **接受**它，编译通过、仿真正常退出、
// 用例全过。但待测模块用的还是自己的默认值（4 位），扫 WIDTH=3/4/5
// 跑的是同一个电路，每一轮结果一模一样。
//
// ★ 关键：iverilog 对这件事**一声不响**。没有 warning、没有 error，
//   退出码 0。如果只看"三轮全绿"，会得出"三个位宽都验证过了"的结论，
//   而实际上只验证了一个 —— 这正是 regress.py 要挡的谎报。
//
// 和 tb_counterN.v 的差别只有两处，都是刻意的：
//   1. 例化时不透传 WIDTH（`counterN u (...)` 而不是 `counterN #(.WIDTH(WIDTH)) u (...)`）
//   2. 不打印 `EFFECTIVE_WIDTH=`，所以连"生效值核对"这条线索也没有
// 第 2 条是让这个负例落到"没有证据"那条分支上的必要条件 —— 有生效值证据时
// 结论就变成"参数确实传下去了，只是不影响观察项"，那是另一回事（见 tb_macrow.v）。
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_counterN_inert #(parameter WIDTH = 4);

    reg        clk = 0;
    reg        rst_n;
    reg        en;
    // 固定 4 位，和待测模块的默认位宽对上 —— 参数不透传时这也是唯一能跑通的写法
    wire [3:0] q;

    // ★ 就是这一行漏了 #(.WIDTH(WIDTH))，整个测试台对 WIDTH 的取值就不敏感了
    counterN u (
        .clk   (clk),
        .rst_n (rst_n),
        .en    (en),
        .q     (q)
    );

    always #5 clk = ~clk;

    `VLEARN_TIMEOUT_GUARD(10000)

    integer i;

    initial begin
        `VLEARN_COVER_DECL("counts_eight")

        rst_n = 0;
        en    = 0;
        @(posedge clk);
        @(posedge clk);
        #1;
        `VLEARN_CHECK("reset_clears_q", 4'd0, q)

        rst_n = 1;
        en    = 1;
        for (i = 0; i < 8; i = i + 1) @(posedge clk);
        #1;
        `VLEARN_CHECK("count_8_clocks", 4'd8, q)
        `VLEARN_COVER_HIT("counts_eight")

        vlearn_summary;
        $finish;
    end

endmodule
