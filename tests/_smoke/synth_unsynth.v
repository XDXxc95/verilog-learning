// `not_synthesizable` 的夹具：一个**看着像普通时序逻辑、其实合不出来**的模块。
//
// 三样东西全都踩在"时间是仿真概念、硬件里不存在"这条线上：
//
//   #2 q <= d;              延迟语句 —— 硬件里没有"等 2 个时间单位"这回事
//   initial n = 0;          上电初值 —— 硬件上电是未知态，不是 0
//   $display(...)           系统任务 —— 打印是仿真里的动作
//
// iverilog 的报法是 `error: Unable to synthesize synchronous process`，
// 注意它会把**整个 always 块**放弃掉 —— 不是只丢掉那一行。
// 这是本仓库反复讲的一条：不是"写了就能综合"，得逐句问"这行是什么硬件"。
//
// 用 `python tools/sim.py` 跑它完全没问题（仿真里这些都是合法的），
// 这正是 sim.py 判 PASS、synth.py 报失败的场景 —— **两边都对，问的不是同一件事**。
`timescale 1ns/1ps

module synth_unsynth (
    input  wire clk,
    input  wire d,
    output reg  q
);

    integer n;

    initial n = 0;

    always @(posedge clk) begin
        #2 q <= d;              // 延迟：硬件里没有对应物
        n = n + 1;              // 阻塞赋值 + 调试计数：也不是硬件
        $display("n=%0d", n);   // 系统任务：同样不是硬件
    end

endmodule
