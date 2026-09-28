// 工具功能自检用的探针文件 —— 不是课程材料，是体检样本。
//
// 注意：本文件里**所有注释都不能以 "verilator" 开头**。
// 该工具会把 `// verilator ...` 当成编译指示（pragma）解析，
// 不认识的写法直接报 BADVLTPRAGMA 错误——我在这个文件的头一行就踩过一次。
//
// 为什么需要它：`verilator_bin --version` 在任何情况下都能打印版本号，
// 但 OSS CAD Suite 的构建里烘死了 `VERILATOR_ROOT=/yosyshq`，
// 不覆盖这个变量时 `--lint-only` 会直接
// 「Cannot find verilated_std_waiver.vlt」失败。
// 于是「文件在」与「能用」是两回事，必须真跑一次才能分辨。
//
// 这个模块必须**始终 lint 干净**（退出码 0）：它一旦有告警，
// 自检就会把「工具坏了」和「样本写坏了」混为一谈。
// 所以这里刻意用最保守的可综合写法，别为了省事改成 `always @(*)` 之类。
// 改完务必跑 tests/_smoke/check_manifest.py，它会验证这个样本仍然 lint 干净。
//
// 文件名必须和 module 名一致，否则 verilator 会报 DECLFILENAME 告警，
// 而这条自检是按退出码判定的 —— 一个告警就足以让它失败。
module vlearn_probe (
    input  wire clk,
    input  wire rst_n,
    output reg  q
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) q <= 1'b0;
        else        q <= ~q;
    end
endmodule
