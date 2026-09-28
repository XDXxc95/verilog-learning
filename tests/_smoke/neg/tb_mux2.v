// mux2 的自检测试台。**故意不用时钟**：激励靠 #10 的绝对延时推进。
//
// 与 tb_shift / tb_counter4 不同，这里没有任何信号能凑出"规整的跳变间隔"，
// 所以 observe 应该老实报 clock = null，而不是挑一个跳变最多的信号当周期。
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_mux2;

    reg  a, b, sel;
    wire y;

    mux2 dut (
        .a   (a),
        .b   (b),
        .sel (sel),
        .y   (y)
    );

    `VLEARN_TIMEOUT_GUARD(1000)

    initial begin
        $dumpfile("build/_smoke_mux2.vcd");
        $dumpvars(0, tb_mux2);
    end

    // 组合逻辑没有时钟沿可以对齐，只能靠延时让赋值稳定下来再判定。
    // #10 远大于任何门延时（这里连 specify 都没有），够稳。
    initial begin
        a = 0; b = 1; sel = 0;
        #10 `VLEARN_CHECK("sel0_picks_a", 1'b0, y)

        sel = 1;
        #10 `VLEARN_CHECK("sel1_picks_b", 1'b1, y)

        b = 0;
        #10 `VLEARN_CHECK("b_changes_follows", 1'b0, y)

        vlearn_summary;
        $finish;
    end

endmodule
