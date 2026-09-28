// 纯组合逻辑的冒烟用例。
//
// 存在的理由只有一个：给 observe 层造一个**没有时钟**的波形。
// 之前的组合逻辑用例（latch_incomplete_case.v）没有测试台、不产波形，
// 拿它去断言 "observe.clock 为 null" 会**因为 observe.ok 是 false 而通过**——
// 那是"没波形"冒充"没时钟"，断言绿得毫无意义。
// 这个用例有 $dumpfile，波形读得到，里面确实一个规整跳变的信号都没有，
// 这样断言才是真的在验"不会硬造一个时钟周期出来"。
`timescale 1ns/1ps

module mux2 (
    input  wire a,
    input  wire b,
    input  wire sel,
    output wire y
);

    assign y = sel ? b : a;

endmodule
