// 工具链冒烟测试用设计：带 specify 路径延迟的连线。
// 用途是验证 tools/ 下的工具本身工作正常，不是课程内容。
//
// ★ 这个模块**必须自己带 `timescale**。
//   specify 块里的延迟默认按**编译单元**的时间单位解释；本模块若没有
//   `timescale，那个 10 会变成 10 个默认单位（约 1 秒），而测试台是按 ns
//   推进的——仿真结束时 y 还停在 x，看起来像"延迟大得离谱"，
//   实际是单位根本不是一回事。这个坑实测踩过。
`timescale 1ns/1ps

module dly (
    input  wire a,
    output wire y
);

    assign y = a;

    // iverilog 默认**忽略** specify 块（仿真按零延迟跑），
    // 要 sim.py --specify 才会加上 -gspecify 让它生效。
    specify
        (a => y) = 10;
    endspecify

endmodule
