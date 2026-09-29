// cov.py 夹具 A：**声明 4 个点、4 个全打到（100%）**。
//
// 和 tb_cov_b.v 一起演示"合并 ≠ 平均"，一份完整的测试台配一份有洞的：
//
//     A = 4/4 = 100%        （本份，WIDTH=3，够得着回绕）
//     B = 1/3 =  33%        （那份 WIDTH=4，回绕天然够不着）
//     ────────────────────────────────────────────────
//     平均        = (100% + 33%) / 2 = 66.5%
//     合并（并集）= 4/5 = 80%          ← 真值
//
// 差的这 13 个点全在"平均把两份当成同等分量"上：A 声明得少但打得满，
// B 声明得多但打不满，**分母不同就不该等权平均**。而且 B 那个回绕的洞
// 恰好被 A 补上了 —— 这正是"一组测试台合起来看"的意义，
// 单看 B 会以为回绕没验过。
//
// 本份同时还证明"薄"这件事：A 独有的 3 个点会被 cov.py 标成
// "只有 1/2 份打到"——不是洞，但正是最省力的加深方向。
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_cov_a;

    // WIDTH=3：数 8 拍一定回绕到 0，count_wraps 才打得到
    reg         clk = 0;
    reg         rst_n;
    reg         en;
    wire [2:0]  q;

    counterN #(.WIDTH(3)) dut (
        .clk   (clk),
        .rst_n (rst_n),
        .en    (en),
        .q     (q)
    );

    always #5 clk = ~clk;

    `VLEARN_TIMEOUT_GUARD(10000)

    integer i;

    initial begin
        `VLEARN_COVER_DECL("reset_clears_q")
        `VLEARN_COVER_DECL("hold_when_disabled")
        `VLEARN_COVER_DECL("count_up_sequence")
        `VLEARN_COVER_DECL("count_wraps")

        // —— 场景：复位把 q 清零 ——
        rst_n = 0;
        en    = 0;
        @(posedge clk);
        #1;
        `VLEARN_CHECK("reset_clears_q", 3'b000, q)
        `VLEARN_COVER_HIT("reset_clears_q")

        // —— 场景：en=0 时保持不变 ——
        rst_n = 1;
        @(posedge clk);
        @(posedge clk);
        #1;
        `VLEARN_CHECK("hold_when_disabled", 3'b000, q)
        `VLEARN_COVER_HIT("hold_when_disabled")

        // —— 场景：连续计数，一拍加一 ——
        en = 1;
        @(posedge clk);
        #1;
        `VLEARN_CHECK("count_up_step1", 3'd1, q)
        @(posedge clk);
        #1;
        `VLEARN_CHECK("count_up_step2", 3'd2, q)
        `VLEARN_COVER_HIT("count_up_sequence")

        // —— 场景：从 2 数到回绕（再来 6 拍，共 8 拍回到 0）——
        for (i = 0; i < 6; i = i + 1) @(posedge clk);
        #1;
        `VLEARN_CHECK("count_wraps_to_zero", 3'd0, q)
        `VLEARN_COVER_HIT("count_wraps")

        vlearn_summary;
        $finish;
    end

endmodule
