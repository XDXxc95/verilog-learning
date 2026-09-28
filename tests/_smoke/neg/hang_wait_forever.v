// 运行期挂死：等一个**永远不成立**的条件。
//
// q 是 4 位，最大 4'hF，而等待条件写的是 5'h1F —— 永远等不到。
// 时钟还在跑、仿真时间还在推进、程序既不结束也不报错，
// 就这么一直跑下去。这是新手写测试台最典型的挂死方式：
// 忘了自己写的是几位宽的信号，或者忘了给够激励。
//
// 关键：这种形状**能通过 elaborate**，iverilog 抓不到
//（它只会静态拒绝 `always #0` 那种一眼可见的零延迟循环，
//  那种情况见 hang_zero_delay.v）。只能靠运行时超时兜底。
//
// 期望：--timeout 5 时约 5 秒返回，timed_out=true，整棵进程树被杀掉，
//       并且**之前已经打印的标记要保留**（能看到卡在哪一步）。
//
// 这里特意走 tb_common.vh 的宏、而不是裸 $display：
// 头文件的每个标记打印后面都跟了 $fflush。vvp 在管道上默认全缓冲，
// 不 flush 的话被 kill 时缓冲区里那几行会整段丢失，
// 于是"卡在哪一步"这个协作式超时最核心的价值就没了。
// 本文件同时是那条 $fflush 的回归用例 —— 去掉它会看到 tail 变空。
`timescale 1ns/1ps
`include "tb_common.vh"

module hang_wait_forever;

    reg        clk = 0;
    reg  [3:0] q   = 0;

    always #5 clk = ~clk;

    always @(posedge clk) q <= q + 4'h1;

    initial begin
        vlearn_begin("wait_for_unreachable");
        vlearn_info("q_width", "4");
        vlearn_info("waiting_for", "5'h1F");
        wait (q == 5'h1F);   // ← 4 位变量永远到不了 5'h1F
        `VLEARN_PASS
        vlearn_summary;
        $finish;
    end

endmodule
