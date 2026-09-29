// counterN 的自检测试台，同时作为 --param 与覆盖点协议的冒烟用例。
//
// 这个测试台被**跑两次**，靠 --param 换 WIDTH：
//     不传参数      → WIDTH=4，数 8 拍得到 8，不产生回绕
//     --param WIDTH=3 → WIDTH=3，数 8 拍回绕到 0
// 判定期望值由 WIDTH 现算（8 % (1<<WIDTH)），所以：
//   · 参数真的生效 → 两条都过
//   · 参数被忽略（iverilog 对放错模块的 -P 是**静默忽略**的）→ 期望值按 3 算、
//     实际按 4 跑 → count_8_clocks 必红
// 也就是说这个测试台自带一条"参数没生效"的负例。
//
// 覆盖点也随 WIDTH 变化：数 8 拍回绕到 0 才算覆盖到"回绕"这个场景，
// WIDTH=4 时天然覆盖不到，正好用来验 cover.missed 的汇报。
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_counterN #(parameter WIDTH = 4);

    reg              clk = 0;
    reg              rst_n;
    reg              en;
    wire [WIDTH-1:0] q;

    counterN #(.WIDTH(WIDTH)) dut (
        .clk   (clk),
        .rst_n (rst_n),
        .en    (en),
        .q     (q)
    );

    always #5 clk = ~clk;

    `VLEARN_TIMEOUT_GUARD(10000)

    integer i;

    initial begin
        // 把**生效的**位宽原样打出来。JSON 里的判定结果分不出"参数生效了"
        // 和"参数被静默忽略、恰好也过了"，这一行才是证据（run.tail 里有它）。
        $display("EFFECTIVE_WIDTH=%0d", WIDTH);
        $fflush;

        `VLEARN_COVER_DECL("reset_while_idle")
        `VLEARN_COVER_DECL("enable_gating")
        `VLEARN_COVER_DECL("count_wraps")

        rst_n = 0;
        en    = 0;
        @(posedge clk);
        @(posedge clk);
        #1;
        `VLEARN_CHECK("reset_clears_q", {WIDTH{1'b0}}, q)
        `VLEARN_COVER_HIT("reset_while_idle")

        rst_n = 1;
        @(posedge clk);
        #1;
        `VLEARN_CHECK("hold_when_disabled", {WIDTH{1'b0}}, q)
        `VLEARN_COVER_HIT("enable_gating")

        // 连数 8 拍：回不回绕完全由 WIDTH 决定
        en = 1;
        for (i = 0; i < 8; i = i + 1) @(posedge clk);
        #1;
        `VLEARN_CHECK("count_8_clocks", 8 % (1 << WIDTH), q)
        if (q == {WIDTH{1'b0}}) `VLEARN_COVER_HIT("count_wraps")

        vlearn_summary;
        $finish;
    end

endmodule
