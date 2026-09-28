// 两级移位寄存器的自检测试台。
//
// 它对 dut_shift_good.v（非阻塞赋值）判**通过**，
// 对 dut_shift_bad.v（阻塞赋值）判**失败**且报出 q2 早了一拍。
// 两边都跑一遍才算真的验证了判定链路 —— 只跑通过的那一半是自欺欺人：
// 一个永远报 PASS 的测试台看起来也"全绿"。
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_shift;

    reg  clk = 0;
    reg  d;
    wire q1, q2;

    shift2 dut (
        .clk (clk),
        .d   (d),
        .q1  (q1),
        .q2  (q2)
    );

    always #5 clk = ~clk;

    `VLEARN_TIMEOUT_GUARD(10000)

    initial begin
        $dumpfile("build/_smoke_shift.vcd");
        $dumpvars(0, tb_shift);
    end

    // 激励一律在时钟沿之后 #1 施加，避开沿上的竞争。
    //
    // 等两拍再开始判定：这个设计没有复位，q1/q2 的初值是 x。
    // 第一个时钟沿上 q1 <= d 拿到 0，但同一沿上 q2 <= q1 读到的还是**初始的 x**，
    // 于是 q2 要到第二个沿才变成 0。少等这一拍会看到"期望 0、实际 x"，
    // 那是真实的 x 传播，不是工具误判。
    initial begin
        d = 0;

        @(posedge clk);
        @(posedge clk);
        #1;
        `VLEARN_CHECK("idle_q1_low", 1'b0, q1)
        `VLEARN_CHECK("idle_q2_low", 1'b0, q2)

        // d 拉高的这一拍：q1 取到新值，q2 取到的应该还是**沿之前的旧 q1**
        d = 1;
        @(posedge clk);
        #1;
        `VLEARN_CHECK("d_reaches_q1", 1'b1, q1)
        `VLEARN_CHECK("q2_still_old_this_cycle", 1'b0, q2)

        // 再一拍，q1 的旧值 1 才走到 q2
        d = 0;
        @(posedge clk);
        #1;
        `VLEARN_CHECK("q1_takes_new_d", 1'b0, q1)
        `VLEARN_CHECK("q2_gets_previous_q1", 1'b1, q2)

        vlearn_summary;
        $finish;
    end

endmodule
