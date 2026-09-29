// 4 位计数器，**状态编码由参数选**：GRAY=0 二进制、GRAY=1 格雷。
//
// 为什么用「一个模块 + 参数」而不是两份源文件：
//   toggle.py 比的是**同一套激励下**两份波形的翻转数，激励得完全一样。
//   两份独立源文件的测试台很容易在细节上（初值、激励长度、时钟周期）
//   悄悄跑偏，而"跑的不是同一件事"在翻转数上根本看不出来 —— 数字都挺合理。
//   参数化以后只有 `st` 的编码方式在变，其它一个字都不差。
//   toggle.py 的可比性校验（变化时刻逐项相等）也正是为这件事兜底。
//
// ★ 关键设计：`st` 是**状态寄存器本身**，两种编码下它的**数值**不同，
//   但 `q` 是解码后的二进制计数 —— 两种编码下 `q` 的序列一模一样。
//   于是功能可以验成同一套 expected，而翻转数对的是不同的 `st`。
//   这正是真实电路里 FIFO 指针的做法：对外是二进制，内部走格雷。
//
//   q: 0,1,2,...,15,0,1,...    （每拍 +1，两种编码都是这个序列）
//
// 实测（toggle.py，16 拍一轮 + 回到 0）：
//   二进制 st 翻 30 位（bit0 翻 16 次、bit1 8 次、bit2 4 次、bit3 2 次）
//   格雷   st 翻 16 位（每次进位只翻 1 位 —— 这就是格雷编码的全部好处）
//   变化次数两者都是 16 —— **只数"变化了几次"会得出"两者一样"的错结论**。
//   动态功耗看的是后者（翻了几位），差将近一倍。
`timescale 1ns/1ps

module cnt_enc #(
    parameter GRAY = 0
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       en,
    output wire [3:0] q,
    output wire [3:0] st_raw
);

    reg [3:0] st;

    // 二进制下一拍就是 +1；格雷下一拍是"二进制 +1 之后再编码"，
    // 也就是把整个计数器换成格雷码计数器。
    wire [3:0] bin_next = (GRAY ? gray2bin(st) : st) + 4'd1;

    function [3:0] gray2bin;
        input [3:0] g;
        integer i;
        begin
            gray2bin[3] = g[3];
            for (i = 2; i >= 0; i = i - 1)
                gray2bin[i] = gray2bin[i + 1] ^ g[i];
        end
    endfunction

    wire [3:0] st_next = GRAY ? (bin_next ^ (bin_next >> 1)) : bin_next;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) st <= 4'd0;
        else if (en) st <= st_next;
    end

    assign st_raw = st;
    // 对外**永远**是二进制 —— 编码方式不该泄漏到接口上，否则两种编码
    // 就不再是"功能完全一样"，翻转数的比较也就没有意义了。
    assign q = GRAY ? gray2bin(st) : st;

endmodule
