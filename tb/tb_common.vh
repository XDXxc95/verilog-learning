// ============================================================================
//  vlearn 测试台公共协议
// ----------------------------------------------------------------------------
//  测试台通过打印下面这些标记行，让 tools/sim.py 能够**机器判定**对错。
//  判定标记的格式是 sim.py 与测试台之间的契约，改动会导致判定失效，请勿修改。
//
//    TEST_BEGIN    name=<用例名>
//    TEST_PASS     name=<用例名> time=<时刻>
//    TEST_FAIL     name=<用例名> expected=<期望> got=<实际> time=<时刻>
//    TEST_SUMMARY  total=<n> pass=<n> fail=<n>
//    TEST_DONE
//    TEST_TIMEOUT  limit=<n>             （协作式超时触发时打印）
//
//  覆盖点（可选，见文件末尾）：
//    TEST_COVER_DECL  name=<覆盖点>      （声明一个要求验到的场景）
//    TEST_COVER_HIT   name=<覆盖点>      （该场景真的跑到了）
//  ★ 这两个**不是判定标记**：只有它们、没有 TEST_PASS/FAIL 时，sim.py 仍判
//    「没判定」。覆盖点不进 status，它回答的是"测得够不够深"。
//
//  为什么用 === 而不是 == ：
//    x/z 参与 == 比较时结果是 x，if 会走 else 分支，但你看不出为什么。
//    === 是"完全相同"比较，x 与 x 相等，结果永远是确定的 0 或 1。
//
//  ★★ 改本文件前必读：iverilog 会在**字符串字面量内部**替换宏参数 ★★
//    `define SHOW(limit) $display("limit=%0d", limit)
//    `SHOW(7)  实际打印出的是   "7=7"，不是 "limit=7"。
//    这是纯文本替换，连引号里都不放过，而且**不报错**——只是数据悄悄错掉。
//    实测（iverilog 14）：
//        `define SHOW(limit, expected) \
//            $display("limit=%0d expected=%0h", limit, expected);
//        `SHOW(7, 8'h42)   →   7=7 8'h42=42
//    所以本文件里**所有宏参数一律加 vx_ 前缀**：
//    参数名一旦和要打印的标签同名（limit / expected / actual 这些词最容易撞），
//    打印出来的就是参数的值而不是标签本身，判定链路会拿到垃圾数据。
//    加前缀是唯一稳妥的做法——换成短名字（exp、e）更糟，会作为子串命中
//    "expected" 里的字母。任务（task）不受影响，只有宏有这个问题。
//
//  用法示例：
//      `timescale 1ns/1ps              // ← 必须写，时间单位要明确
//      `include "tb_common.vh"
//      module tb_xxx;
//          ...
//          `VLEARN_TIMEOUT_GUARD(100000)   // 协作式超时（可选但推荐）
//          initial begin
//              `VLEARN_CHECK("reset", 4'h0, dut.q)
//              `VLEARN_CHECK("count1", 4'h1, dut.q)
//              vlearn_summary;
//              $finish;
//          end
//      endmodule
// ============================================================================

`ifndef VLEARN_TB_COMMON_VH
`define VLEARN_TB_COMMON_VH

    // 计数器变量统一用 n 前缀，避免与同名任务冲突（曾经 vlearn_pass 变量和
    // vlearn_pass 任务撞名，iverilog 直接报 "already declared"）
    integer vlearn_ntotal = 0;
    integer vlearn_npass  = 0;
    integer vlearn_nfail  = 0;
    // 用例名最长 128 字符，够用且不浪费
    reg [8*128-1:0] vlearn_current = "";

    // ---------------------------------------------------------------- 基本任务
    //
    // 这里有一条容易踩的坑，改代码前先看：
    //   **凡是会打印 $time 的地方，一律写成宏，不用 task。**
    //   任务定义在本头文件里，而本头文件没有自己的 `timescale，
    //   于是任务内 $time 返回的是**编译单元默认时间单位**下的数值（默认约 1s）。
    //   1ns 量级的仿真时间会被直接截断成 0，"t=0ns 期望 8'hA5 实际 8'h00"
    //   这种报错等于没有信息。
    //   宏在使用处展开，时间单位自然跟随测试台文件，数值才是对的。
    //   （`VLEARN_TIMEOUT_GUARD 出于同样的原因写成宏，见文件末尾。）

    // 每打印一行标记就 $fflush 一次，别省。
    //   原因：vvp 的 C 运行库在**管道**上默认全缓冲（4KB 攒够了才吐）。
    //   仿真被超时杀掉时，缓冲区里没吐出来的标记会**整段丢失**，
    //   于是"看到卡在哪一步"这个协作式超时最核心的价值就没了——
    //   实测过一个 wait 永不成立的用例，挂了 5 秒、输出一条不剩。
    //   代价是每次判定多一次系统调用，教学规模的测试台完全可以忽略。
    task vlearn_begin;
        input [8*128-1:0] name;
        begin
            vlearn_current = name;
            vlearn_ntotal  = vlearn_ntotal + 1;
            $display("TEST_BEGIN name=%0s", name);
            $fflush;
        end
    endtask

    // 供测试台补充上下文（sim.py 原样保留，不参与判定）
    task vlearn_info;
        input [8*128-1:0] key;
        input [8*128-1:0] value;
        begin
            $display("TEST_INFO %0s=%0s", key, value);
            $fflush;
        end
    endtask

    task vlearn_summary;
        begin
            $display("TEST_SUMMARY total=%0d pass=%0d fail=%0d",
                     vlearn_ntotal, vlearn_npass, vlearn_nfail);
            $display("TEST_DONE");
            $fflush;
        end
    endtask

    // ---------------------------------------------------------------- 协作式超时
    // 在**模块层级**调用一次即可（不放在 initial 块里）：
    //     `VLEARN_TIMEOUT_GUARD(10000)
    //
    // 到点后打印标记并 $finish。好处是**已经产生的波形会被保留**，
    // 能看到"卡在哪一步"，而硬超时杀进程是拿不到完整波形的。
    //
    // 为什么是宏而不是任务（这是踩过的坑，别改回去）：
    //   任务定义在头文件里，而本头文件没有自己的 `timescale，
    //   于是任务内 #(limit) 的时间单位取决于**编译单元的默认值**，
    //   而不是测试台的 1ns——结果 #10000 变成了约一万秒（约 10¹² 个时钟周期），
    //   仿真看起来就是永久挂死，且一句输出都没有。
    //   宏在被引用的地方展开，时间单位自然跟随测试台文件，语义才是对的。
    // 参数名必须是 vx_limit 而不是 limit —— 否则 "limit=%0d" 里的 limit
    // 会被替换成数值，打成 "10000=10000"（见文件头的说明）。
    `define VLEARN_TIMEOUT_GUARD(vx_limit)                                  \
        initial begin                                                        \
            #(vx_limit);                                                     \
            $display("TEST_TIMEOUT limit=%0d", vx_limit);                    \
            $display("TEST_SUMMARY total=%0d pass=%0d fail=%0d",             \
                     vlearn_ntotal, vlearn_npass, vlearn_nfail);             \
            $display("TEST_DONE");                                           \
            $fflush;                                                         \
            $finish;                                                         \
        end

    // ---------------------------------------------------------------- 便捷宏

    // 时间打印统一走 $timeformat + %0t：
    //   不调 $timeformat 时 %0t 按**全局精度**输出，1ns/1ps 下 16ns 会打成 16000，
    //   读的人得自己脑补单位。设成 -9（ns）之后 %0t 会自动按 ns 定标，
    //   而且对任意 `timescale 都成立，不需要测试台再声明什么。
    //   小数位取 3：需要时能看见 0.5ns 这种半拍，不掩盖细节。
    `define VLEARN_TIME_SETUP  $timeformat(-9, 3, "ns", 12);

    // 通过一个用例。时间在这里打印 —— 必须由宏在测试台文件里展开。
    `define VLEARN_PASS                                                     \
        begin                                                              \
            vlearn_npass = vlearn_npass + 1;                               \
            `VLEARN_TIME_SETUP                                             \
            $display("TEST_PASS name=%0s time=%0t", vlearn_current, $time); \
            $fflush;                                                       \
        end

    // 失败一个用例。
    //   期望与实际都按十六进制打印（自动去掉前导零，x/z 原样显示为 x）。
    //   参数名是 vx_exp / vx_act，不能写成 expected / actual——
    //   否则下面格式串里的 "expected=" 会被替换成参数值（见文件头说明）。
    `define VLEARN_FAIL_H(vx_exp, vx_act)                                 \
        begin                                                              \
            vlearn_nfail = vlearn_nfail + 1;                               \
            `VLEARN_TIME_SETUP                                             \
            $display("TEST_FAIL name=%0s expected=%0h got=%0h time=%0t",   \
                     vlearn_current, vx_exp, vx_act, $time);               \
            $fflush;                                                       \
        end

    // 按二进制打印，便于观察具体是哪一位不对
    `define VLEARN_FAIL_B(vx_exp, vx_act)                                 \
        begin                                                              \
            vlearn_nfail = vlearn_nfail + 1;                               \
            `VLEARN_TIME_SETUP                                             \
            $display("TEST_FAIL name=%0s expected=%0b got=%0b time=%0t",   \
                     vlearn_current, vx_exp, vx_act, $time);               \
            $fflush;                                                       \
        end

    // 打印你自己已经格式化好的字符串（%0s 原样输出，不做进制转换）
    `define VLEARN_FAIL_STR(vx_exp, vx_act)                               \
        begin                                                              \
            vlearn_nfail = vlearn_nfail + 1;                               \
            `VLEARN_TIME_SETUP                                             \
            $display("TEST_FAIL name=%0s expected=%0s got=%0s time=%0t",   \
                     vlearn_current, vx_exp, vx_act, $time);               \
            $fflush;                                                       \
        end

    // 日常最常用的判定。期望/实际可以是任意位宽的表达式。
    `define VLEARN_CHECK(vx_label, vx_exp, vx_act)                        \
        begin                                                              \
            vlearn_begin(vx_label);                                        \
            if ((vx_exp) === (vx_act)) `VLEARN_PASS                        \
            else                       `VLEARN_FAIL_H(vx_exp, vx_act)      \
        end

    // 同上，但按二进制打印
    `define VLEARN_CHECK_B(vx_label, vx_exp, vx_act)                      \
        begin                                                              \
            vlearn_begin(vx_label);                                        \
            if ((vx_exp) === (vx_act)) `VLEARN_PASS                        \
            else                       `VLEARN_FAIL_B(vx_exp, vx_act)      \
        end

    // ---------------------------------------------------------------- 功能覆盖点
    //
    // 覆盖点和"用例过没过"是两件事，别混：
    //   用例过没过 —— 待测模块**对不对**（判定，进 status）
    //   覆盖点     —— 测试台**测得全不全**（观察，不进 status）
    // 用例全过但覆盖有洞，说明测得太浅，不代表模块写错了。反过来，
    // 覆盖打满也不代表模块对——覆盖点是你自己声明的，声明漏了就是自欺。
    // sim.py 把结果放在 JSON 的 cover 块里，并且写死 affects_verdict=false。
    //
    // 用法：**先声明、再在真正跑到那个场景时打命中**。
    //     `VLEARN_COVER_DECL("rst_during_count")   // 写在模块级或 initial 开头
    //     ...
    //         // 下面这段激励就是在测「计数过程中复位」
    //         `VLEARN_COVER_HIT("rst_during_count")
    //
    // 声明是给"没打到的点"留分母的。只写 HIT 不写 DECL 的话，sim.py 算不出
    // 覆盖率（它会在 cover.undeclared_hits 里如实说"有命中没声明"，
    // 不会假装覆盖齐全）。
    //
    // 参数名必须是 vx_point，不能写成 name —— 否则下面格式串里的 "name="
    // 会被替换成参数值（见文件头说明，iverilog 连引号里都替换）。
    `define VLEARN_COVER_DECL(vx_point)                                    \
        begin                                                              \
            $display("TEST_COVER_DECL name=%0s", vx_point);                \
            $fflush;                                                       \
        end

    `define VLEARN_COVER_HIT(vx_point)                                     \
        begin                                                              \
            $display("TEST_COVER_HIT name=%0s", vx_point);                 \
            $fflush;                                                       \
        end

`endif
