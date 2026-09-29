// 同一台 4 状态机，**独热编码**。三兄弟（bin / onehot / gray）功能完全一样，
// 只有状态编码不同 —— 这是 synth 检查里"横向对比"的夹具。
//
// 独热的取舍非常直观：4 个状态用 4 个触发器（状态寄存器变贵），
// 换来次态逻辑里不再需要译码（每个状态一位、直接就是使能信号，组合逻辑变便宜）。
// 具体差多少要数字说话，跑：
//
//     python tools/synth.py tests/_smoke/fsm_bin.v tests/_smoke/fsm_onehot.v \
//                           tests/_smoke/fsm_gray.v
//
//   IDLE --go--> RUN --done--> DONE --> IDLE
//                  |--err--> ERR --> IDLE
//   busy = (st == RUN)     fin = (st == DONE)
`timescale 1ns/1ps

module fsm_onehot (
    input  wire clk,
    input  wire rst_n,
    input  wire go,
    input  wire done,
    input  wire err,
    output wire busy,
    output wire fin
);

    localparam [3:0] IDLE = 4'b0001,
                     RUN  = 4'b0010,
                     DONE = 4'b0100,
                     ERR  = 4'b1000;

    reg [3:0] st, nx;

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
