// 故意的零延迟死循环。
//
// `always #0 clk = ~clk;` 里 #0 是**零延迟**，不是"很小的时间"：
// 仿真时间永远不推进，这个 always 会在同一个时刻无限循环下去。
// 这是新手非常容易写出来的形状（本意通常是想"让时钟快点"）。
//
// 用途：验证 sim.py 的超时保护真的能杀掉整棵进程树，而不是把会话挂死。
// 期望：--timeout 5 时约 5 秒返回，timed_out=true，退出码 3。
`timescale 1ns/1ps

module hang_zero_delay;

    reg clk = 0;

    always #0 clk = ~clk;   // ← 零延迟，时间永不推进

    initial begin
        $display("TEST_BEGIN name=never_reached");
        #100;
        `ifdef NEVER
        $display("TEST_PASS name=never_reached time=%0t", $time);
        `endif
        $display("TEST_DONE");
    end

endmodule
