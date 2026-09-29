// cnt_enc 的自检测试台（GRAY=0 / GRAY=1 共用），check_toggle.py 的夹具。
//
// 一份测试台跑两种编码，靠 `--param GRAY=1` 切。**这是故意的**：
// toggle.py 要比的两份波形必须是"同一套激励"，同一份测试台 + 一个参数
// 是保证这件事最省事、也最不容易跑偏的方式。
//
//     python tools/sim.py tests/_smoke/cnt_enc.v --tb tests/_smoke/tb_cnt_enc.v \
//         --top tb_cnt_enc --param GRAY=0 --name cnt_bin --workdir build/tg/b
//     python tools/sim.py tests/_smoke/cnt_enc.v --tb tests/_smoke/tb_cnt_enc.v \
//         --top tb_cnt_enc --param GRAY=1 --name cnt_gray --workdir build/tg/g
//     python tools/toggle.py build/tg/b/cnt_bin.json build/tg/g/cnt_gray.json \
//         --signal st --label 二进制 --label 格雷
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_cnt_enc #(
    parameter GRAY  = 0,
    // 跑多少拍。**这个参数不是给正常用的**——它是给 check_toggle.py 造
    // "两份波形不可比"的负例用的：改掉它就换了激励，而 toggle.py 必须
    // 拒绝拿两份不同激励的翻转数做比较。
    parameter N_CYCLE = 34
);

    reg        clk = 0;
    reg        rst_n;
    reg        en;
    wire [3:0] q;
    wire [3:0] st_raw;

    cnt_enc #(.GRAY(GRAY)) dut (
        .clk    (clk),
        .rst_n  (rst_n),
        .en     (en),
        .q      (q),
        .st_raw (st_raw)
    );

    always #5 clk = ~clk;

    `VLEARN_TIMEOUT_GUARD(20000)

    initial begin
        $dumpfile("build/_smoke_cnt_enc.vcd");
        $dumpvars(0, tb_cnt_enc);
    end

    integer   i;
    reg [3:0] exp_q;
    reg [3:0] exp_st;

    initial begin
        rst_n = 0;
        en    = 0;
        @(posedge clk);
        @(posedge clk);
        #1;
        `VLEARN_CHECK("reset_clears_q", 4'h0, q)

        rst_n = 1;
        @(posedge clk);
        #1;
        `VLEARN_CHECK("hold_when_disabled", 4'h0, q)

        en = 1;
        // 逐拍核对 q。**两种编码的 q 序列必须完全相同** —— 否则它们就不是
        // "功能一样、只有编码不同"，翻转数的比较也就失去前提。
        //
        // 默认 34 拍 = 两整轮 16 拍 + 2 拍。**两轮不是为了凑数**：
        // 一轮里各位翻的次数很不均衡（bit0 翻 16 次、bit3 只翻 2 次），
        // 只跑一轮不足以把"平均每次翻几位"这个比值稳定下来。
        for (i = 0; i < N_CYCLE; i = i + 1) begin
            @(posedge clk);
            #1;
            exp_q = (i + 1) % 16;
            `VLEARN_CHECK("count_sequence", exp_q, q)
        end

        en = 0;
        @(posedge clk);
        #1;
        exp_q  = N_CYCLE % 16;
        exp_st = GRAY ? (exp_q ^ (exp_q >> 1)) : exp_q;
        `VLEARN_CHECK("hold_after_disable", exp_q, q)

        // 编码方式**不该泄漏到接口上**：q 是解码后的二进制，两种编码下序列一样。
        // 但内部寄存器 st_raw 该按各自的编码走。
        // 这条把"GRAY 参数根本没接上"顶出来：那样两份波形会一模一样，
        // 翻转数当然也一样，而 toggle.py 会把它报成"打平"——一个看着合理的假结论。
        `VLEARN_CHECK("state_register_encoded", exp_st, st_raw)

        vlearn_summary;
        $finish;
    end

endmodule
