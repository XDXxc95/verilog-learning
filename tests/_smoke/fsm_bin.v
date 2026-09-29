// 同一台 4 状态机，**二进制编码**。三兄弟（bin / onehot / gray）功能完全一样，
// 只有状态编码不同 —— 这是 regress/synth 检查里"横向对比"的夹具。
//
// 为什么要这三份：仿真里三种编码的行为**一模一样**，波形、判定、覆盖全都
// 分不出差别。差别只在硬件上：状态寄存器几个触发器、次态逻辑多少门。
// 也就是说，这是"仿真验证不到、只能靠综合产物看"的那类问题 —— 正是
// tools/synth.py 存在的理由。
//
//   IDLE --go--> RUN --done--> DONE --> IDLE
//                  |--err--> ERR --> IDLE
//   busy = (st == RUN)     fin = (st == DONE)
`timescale 1ns/1ps

module fsm_bin (
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
                     DONE = 2'b10,
                     ERR  = 2'b11;

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
