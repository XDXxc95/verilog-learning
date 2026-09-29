// 同一台 4 状态机，**格雷编码**。三兄弟（bin / onehot / gray）功能完全一样，
// 只有状态编码不同 —— 这是 synth 检查里"横向对比"的夹具。
//
// 格雷编码的取舍和另外两种都不一样：触发器数和二进制一样（2 个），
// 但相邻状态之间只翻一位 —— 好处在**跨时钟域和多比特信号**上（翻转位数少，
// 亚稳态窗口和动态功耗都小），代价是次态逻辑通常比二进制更绕。
//
//   IDLE --go--> RUN --done--> DONE --> IDLE
//                  |--err--> ERR --> IDLE
//   busy = (st == RUN)     fin = (st == DONE)
//
// ★ 实测（别以为工具坏了）：sizer 给 bin 和 gray **一模一样的数字**
//   （都是 2 FF / 21 门 / MUX[2]×6）。原因是 sizer 不做优化，它只是把同一份
//   case 结构翻译一遍 —— 常量从 00/01/10/11 换成 00/01/11/10，结构没变。
//   它对"状态寄存器变宽"敏感（onehot → 4 FF / 41 门），对"同宽度下换编码"
//   不敏感。**格雷编码省功耗、少翻转的好处，sizer 这个层级根本量不出来**，
//   要真看编码的代价得上 yosys（它会做状态编码优化）。
`timescale 1ns/1ps

module fsm_gray (
    input  wire clk,
    input  wire rst_n,
    input  wire go,
    input  wire done,
    input  wire err,
    output wire busy,
    output wire fin
);

    localparam [1:0] IDLE = 2'b00,
                     RUN  = 2'b01,
                     DONE = 2'b11,
                     ERR  = 2'b10;

    reg [1:0] st, nx;

    always @(*) begin
        case (st)
            IDLE:    nx = go          ? RUN  : IDLE;
            RUN:     nx = err ? ERR : (done ? DONE : RUN);
            DONE:    nx = IDLE;
            default: nx = IDLE;
        endcase
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) st <= IDLE;
        else        st <= nx;
    end

    assign busy = (st == RUN);
    assign fin  = (st == DONE);

endmodule
