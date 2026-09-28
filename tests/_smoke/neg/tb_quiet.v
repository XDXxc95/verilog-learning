// 一个**不打任何判定标记**的测试台：仿真正常跑完、退出码为 0，
// 但一条 TEST_PASS / TEST_FAIL 都没有。
//
// 用途：验证 sim.py 在**证据不足**时绝不判通过。
// 这类测试台最危险 —— 退出码 0、没有报错、输出干干净净，
// 粗心的判定逻辑会把它当成"全绿"，而实际上它什么都没检查。
//
// 期望：status 既不是 pass，note 里说明"没有输出判定标记"，退出码非 0。
`timescale 1ns/1ps

module tb_quiet;

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

    initial begin
        d = 0;
        repeat (4) @(posedge clk);
        $display("仿真结束，d=%b q1=%b q2=%b", d, q1, q2);
        $finish;
    end

endmodule
