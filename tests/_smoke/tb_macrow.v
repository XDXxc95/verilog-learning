// macrow 的自检测试台，同时是 regress.py 的 `--define` 扫描正例。
//
// 它被扫两个轴，各自验证一条不同的路径：
//
//   `WIDTH`  ∈ {3, 4}  → **结果必须不同**。位宽决定"数 8 拍回不回绕"
//                         （8 % 2^WIDTH），并且覆盖点 count_wraps 只有窄位宽打得到。
//                         regress.py 应当报"2 组对照里 2 组有差异"。
//   `TRACE`  ∈ {0, 1}  → **结果必须相同**，而且这是**正确**的：TRACE 只控制
//                         一行调试打印，不影响判定、用例和覆盖点。
//
// 第二个轴是刻意造出来的，它验证的是 regress.py 里最容易做错的那条分支：
// 「扫下来结果一样」到底该报错还是放行？答案取决于**有没有参数确实传下去了的证据**。
// 这里两个宏都在下面用 $display 自报了生效值（EFFECTIVE_...），
// 所以证据齐全 → 报成"这一维在这套激励下观察不到差别"（info），判定仍通过。
//
// 对照 tb_counterN_inert.v：那边没有自报，同样"结果一样"就必须判失败。
// 两个夹具合起来才把这条分界线钉死 —— 只看其中任何一个都推不出正确行为。
`ifndef WIDTH
  `define WIDTH 4
`endif
`ifndef TRACE
  `define TRACE 0
`endif

`timescale 1ns/1ps
`include "tb_common.vh"

module tb_macrow;

    reg                 clk = 0;
    reg                 rst_n;
    reg                 en;
    wire [`WIDTH-1:0]   q;

    macrow u (
        .clk   (clk),
        .rst_n (rst_n),
        .en    (en),
        .q     (q)
    );

    always #5 clk = ~clk;

    `VLEARN_TIMEOUT_GUARD(10000)

    integer i;

    initial begin
        // 自报生效值。这两行是 regress.py 核对参数到底有没有生效的**唯一**依据
        // —— 少了它们，"结果一样"就只能被当成可疑，而不是被核对过的结论。
        // 约定：名字必须是 EFFECTIVE_<宏名>，值紧跟等号，中间不要有空格。
        $display("EFFECTIVE_WIDTH=%0d", `WIDTH);
        $display("EFFECTIVE_TRACE=%0d", `TRACE);
        $fflush;

        `VLEARN_COVER_DECL("counts_eight")
        `VLEARN_COVER_DECL("count_wraps")

        rst_n = 0;
        en    = 0;
        @(posedge clk);
        @(posedge clk);
        #1;
        `VLEARN_CHECK("reset_clears_q", {`WIDTH{1'b0}}, q)

        rst_n = 1;
        en    = 1;
        for (i = 0; i < 8; i = i + 1) @(posedge clk);
        #1;
        // 期望值随宏变化：位宽够就数到 8，不够就回绕到 0
        `VLEARN_CHECK("count_8_clocks", 8 % (1 << `WIDTH), q)
        `VLEARN_COVER_HIT("counts_eight")
        if (q == {`WIDTH{1'b0}}) `VLEARN_COVER_HIT("count_wraps")

        // TRACE 只影响这一行，所以它扫出来"结果一样"**是对的**。
        // 放在判定之后，免得它影响任何观察到的东西。
        if (`TRACE) $display("TRACE: final q=%0d", q);

        vlearn_summary;
        $finish;
    end

endmodule
