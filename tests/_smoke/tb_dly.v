// dly 的自检测试台，作为 --specify 的冒烟用例。
//
// 同一个测试台被跑两次：
//     不加 --specify → specify 块被忽略，y 立刻跟着 a 变 → delay_holds 红
//     加上 --specify → 路径延迟生效，t=25 时 y 还是 0     → 两条都过
// 这一对用例证明的就是"那个开关真的在起作用"，而不是"开了也没变化"。
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_dly;

    reg  a = 0;
    wire y;

    dly u (.a(a), .y(y));

    initial begin
        #20 a = 1;
        // t=25：延迟（10ns）还没走完，y 应仍是 0。零延迟时这里会读到 1。
        #5  `VLEARN_CHECK("delay_holds", 1'b0, y)
        // t=35：延迟走完，y 应已变 1
        #10 `VLEARN_CHECK("delay_elapsed", 1'b1, y)

        vlearn_summary;
        $finish;
    end

endmodule
