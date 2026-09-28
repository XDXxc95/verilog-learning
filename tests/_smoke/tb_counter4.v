// counter4 的自检测试台，同时作为 sim.py 的冒烟用例。
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_counter4;

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
        $dumpfile("build/_smoke_counter4.vcd");
        $dumpvars(0, tb_counter4);
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
