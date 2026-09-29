// `iverilog_crash` 的夹具：**组合 always 里 case 不全**，推断出锁存器。
//
// 这个夹具存在的理由，是那条最反直觉的失败：**iverilog 自己断言崩了**。
//
//     退出码 0xC0000409（STATUS_STACK_BUFFER_OVERRUN）
//     ../iverilog/synth2.cc:679: failed assertion `sel_need >= 2`
//
// 崩溃 ≠ "你的写法有语法错"。前端干干净净地过了，VVP 仿真也跑得好好的 ——
// 是 sizer 在把推断出的锁存器翻译成宏单元时自己挂掉。
//
// 所以工具必须把话说明白：**根子在 RTL 上，但报"iverilog 崩了"不等于骂 iverilog。**
// 真正的入口是 `tools/_lint.py` 的 `latch_inference` —— 它在编译期就报，
// 早得多、也直白得多（"s 没有覆盖全，y 会保持旧值 → 综合出锁存器"）。
//
//   s = 2'd2 或 2'd3 → y 保持上一次的值 → **这就是锁存器**，不是组合逻辑
`timescale 1ns/1ps

module synth_latch (
    input  wire [1:0] s,
    input  wire [3:0] a,
    input  wire [3:0] b,
    output reg  [3:0] y
);

    always @(*) begin
        case (s)
            2'd0: y = a;
            2'd1: y = b;
            // 缺 default：s 为 2/3 时 y 不被赋值 —— 硬件上只能"记住旧值"
        endcase
    end

endmodule
