// comb_loop 的自检测试台。纯组合逻辑，激励靠 #10 推进（同 tb_mux2）。
//
// **它必须全过。** 这个用例的全部价值就在于「功能用例 3/3 通过，而结构上是错的」——
// 如果它自己就判 fail，那测的是别的东西，证明不了 lint 补上了判定链路的盲区。
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_comb_loop;

    reg  en, d;
    wire q;

    comb_loop dut (
        .en (en),
        .d  (d),
        .q  (q)
    );

    `VLEARN_TIMEOUT_GUARD(1000)

    initial begin
        $dumpfile("build/_smoke_comb_loop.vcd");
        $dumpvars(0, tb_comb_loop);
    end

    initial begin
        // en=1 时直通
        en = 1; d = 1;
        #10 `VLEARN_CHECK("pass_through_high", 1'b1, q)

        en = 1; d = 0;
        #10 `VLEARN_CHECK("pass_through_low", 1'b0, q)

        // en=0 时 q 反馈回自己，值定住——iverilog 这里会收敛，不会挂
        en = 0;
        #10 `VLEARN_CHECK("hold_last_value", 1'b0, q)

        vlearn_summary;
        $finish;
    end

endmodule
