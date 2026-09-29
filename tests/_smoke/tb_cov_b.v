// cov.py 夹具 B：**声明 3 个点、只打到 1 个（33%）**，而且两个洞的成因不同。
//
//     `count_wraps`        —— WIDTH=4，只数 8 拍，**够不着**回绕
//                             （和 tb_counterN.v 的已知洞同源：天然够不着，
//                               不是代码错，是激励不够）
//     `reset_during_count` —— ★ **拼错了命中名**（见下面那行 HIT），
//                             声明是对的、激励也是真的跑了，就是名字对不上
//
// 第二个洞是这份夹具最重要的地方：**一个拼错的覆盖点，症状是覆盖率静默变低。**
// 不报错、不警告，只是那个点在报告里永远是洞。而且它还会**额外**留下一条
// "有命中却没有声明"（`reset_during_cnt`）—— 那一条正是诊断它的线索：
// 只要 cov.py 把所有命中名和所有声明名并排摆出来，拼错就藏不住。
//
// 合并后（与 tb_cov_a.v 的并集，5 个点）：
//     reset_clears_q      ← A
//     hold_when_disabled  ← A、B       （两份都打到，"厚"）
//     count_up_sequence   ← A
//     count_wraps         ← A          ★ B 的洞被 A 补上了，合并后**不是洞**
//     reset_during_count  ← 谁都没打到  ← 合并后仍然是洞（因为 B 拼错了）
//     → 4/5 = 80%，而两份比值的平均只有 66.5%。**平均低报了。**
//
// 另外 `reset_during_cnt`（拼错的那个）**绝不能**被算进分子：
// 没有分母的命中不是覆盖。它只出现在 undeclared_hits 里。
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_cov_b;

    // WIDTH=4：刻意和 A 的 3 不同 —— 回绕这条本份天然够不着
    reg         clk = 0;
    reg         rst_n;
    reg         en;
    wire [3:0]  q;

    counterN #(.WIDTH(4)) dut (
        .clk   (clk),
        .rst_n (rst_n),
        .en    (en),
        .q     (q)
    );

    always #5 clk = ~clk;

    `VLEARN_TIMEOUT_GUARD(10000)

    integer i;

    initial begin
        `VLEARN_COVER_DECL("count_wraps")
        `VLEARN_COVER_DECL("hold_when_disabled")
        `VLEARN_COVER_DECL("reset_during_count")

        rst_n = 0;
        en    = 0;
        @(posedge clk);
        @(posedge clk);
        #1;
        `VLEARN_CHECK("reset_clears_q", 4'b0000, q)

        // —— 场景：en=0 时保持不变（这个打得到）——
        rst_n = 1;
        @(posedge clk);
        @(posedge clk);
        #1;
        `VLEARN_CHECK("hold_when_disabled", 4'b0000, q)
        `VLEARN_COVER_HIT("hold_when_disabled")

        // —— 数 8 拍：4 位宽回绕不了，count_wraps 这条**故意不打** ——
        en = 1;
        for (i = 0; i < 8; i = i + 1) @(posedge clk);
        #1;
        `VLEARN_CHECK("count_8_clocks", 4'd8, q)

        // —— 场景：计数途中复位。激励是真的，名字拼错了 ——
        rst_n = 0;
        @(posedge clk);
        #1;
        `VLEARN_CHECK("reset_during_count", 4'b0000, q)
        // ★ 声明写的是 reset_during_count，这里少写了一个 "ou"。
        //   于是这个点在报告里永远是洞，同时多出一条"有命中没声明"。
        `VLEARN_COVER_HIT("reset_during_cnt")

        vlearn_summary;
        $finish;
    end

endmodule
