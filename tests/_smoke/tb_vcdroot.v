// check_viz.py 的夹具：$dumpfile 写的是**光秃秃的相对路径**，
// 解析出来落在仓库根，而不是 build/。
//
// 这不是刁难：学习者写 `$dumpfile("dump.vcd")` 再自然不过，而
// `$dumpfile` 的路径是**相对进程 cwd** 解析的 —— sim.py 里
// `_snapshot_vcd` 若只看 workdir，就会报出「测试台里需要 $dumpfile」
// 这句假话，而文件就躺在仓库根。
//
// 功能与 tb_counter4.v 逐条相同，只有 dump 路径不同 —— 这样
// 「没找到波形」与「判定通过」两个事实可以同时成立，用来钉住
// 「没有波形绝不许改判定」。
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_vcdroot;

    reg        clk = 0;
    reg        rst_n;
    reg        en;
    wire [3:0] q;

    counter4 dut (
        .clk   (clk),
        .rst_n (rst_n),
        .en    (en),
        .q     (q)
    );

    always #5 clk = ~clk;

    // 协作式超时：在模块层级展开，独立于下面的激励块
    `VLEARN_TIMEOUT_GUARD(10000)

    initial begin
        $dumpfile("_smoke_vcdroot.vcd");
        $dumpvars(0, tb_vcdroot);
    end

    // 激励一律在时钟沿之后 #1 施加，避开沿上的竞争
    initial begin
        rst_n = 0;
        en    = 0;
        @(posedge clk);
        @(posedge clk);
        #1;
        `VLEARN_CHECK("reset_clears_q", 4'h0, q)

        rst_n = 1;
        @(posedge clk);
        #1;
        `VLEARN_CHECK("hold_when_disabled", 4'h0, q)

        en = 1;
        @(posedge clk);
        #1;
        `VLEARN_CHECK("count_to_1", 4'h1, q)

        @(posedge clk);
        #1;
        `VLEARN_CHECK("count_to_2", 4'h2, q)

        en = 0;
        @(posedge clk);
        #1;
        `VLEARN_CHECK("hold_after_disable", 4'h2, q)

        vlearn_summary;
        $finish;
    end

endmodule
