// 负例：只打覆盖点标记、不打任何判定标记的测试台。
//
// 要害是 sim.py 必须仍然判「没判定」（inconclusive）——
// 覆盖点标记 (TEST_COVER_DECL / TEST_COVER_HIT) **不是判定标记**，
// 它们一旦把 verdict.protocol_used 置起来，"测试台没按协议汇报判定"
// 这条判据就被绕过了，一个只声明覆盖点的测试台可能被判成「判定了」。
//
// 同时验证：这种情况下 cover 块仍然如实给出数据（ok=true，有声明也有命中），
// 也就是说"没判定"和"有覆盖数据"是两件互不干扰的事。
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_cover_only;

    initial begin
        `VLEARN_COVER_DECL("declared_point")
        `VLEARN_COVER_HIT("declared_point")
        // 故意不打 TEST_PASS / TEST_FAIL / TEST_DONE
        #10 $finish;
    end

endmodule
