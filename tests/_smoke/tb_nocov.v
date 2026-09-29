// cov.py 的**混合**夹具：一份**能跑通、有正常判定、但一个覆盖点都没声明**的测试台。
//
// 它钉的是那条最容易混的数：
//
//     跑了 2 份，其中 1 份带覆盖数据
//     —— 这句话里 "2" 和 "1" 是**两个不同的数**，缺一个就说不清情况。
//
// 具体到覆盖率，没声明覆盖点的那份**既不加分子也不加分母**：
// 它不是"覆盖了 0 个点"（那会拉低合并比值），而是**没有分母**。
// 和 `sim.py` 的 `lint.ran` / `lint.n_warnings` 是同一条教训 ——
// `ran=false`（没跑）和 `n_warnings=0`（跑了、干净）在下游长得一模一样，
// 含义相反。这里 `n_runs=2` 而 `n_runs_with_cover=1`，两个数都必须报。
//
// 它的判定是**正常**的（用例全过、status=pass），所以它也不该被算成
// "跑失败的那一份" —— `failed_runs` 必须仍然是空的。三条路要分得清清楚楚：
//
//     ① 跑了、有覆盖数据        → 进分母进分子
//     ② 跑了、没有覆盖声明      → 不进分母不进分子，但 n_runs 要算它
//     ③ 压根没跑出结果          → 单独报 failed_runs，因为"没数据"的原因不同
//
// ②和③混起来的话，一份编译失败的测试台会被读成"只是没写覆盖点"。
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_nocov;

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
        // 刻意**不写** VLEARN_COVER_DECL / VLEARN_COVER_HIT：
        // 这份测试台只关心判定，不声明覆盖点。
        rst_n = 0;
        en    = 0;
        @(posedge clk);
        #1;
        `VLEARN_CHECK("reset_clears_q", 4'b0000, q)

        rst_n = 1;
        @(posedge clk);
        @(posedge clk);
        #1;
        `VLEARN_CHECK("hold_when_disabled", 4'b0000, q)

        en = 1;
        for (i = 0; i < 8; i = i + 1) @(posedge clk);
        #1;
        `VLEARN_CHECK("count_8_clocks", 4'd8, q)

        vlearn_summary;
        $finish;
    end

endmodule
