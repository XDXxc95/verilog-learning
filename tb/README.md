# tb —— 公共测试台头文件

只有 `tb_common.vh` 一个文件，但它**不是普通工具库**：它定义的是测试台和 `tools/sim.py` 之间的**判定协议**。

改动这个文件 = 改判定契约，改错了不会报错，只会**悄悄判错**。

## 用法

测试台里 `` `include "tb_common.vh" `` 即可。`tools/sim.py` 编译时会自动把本目录加进 include 路径，不用写相对路径。

```verilog
`timescale 1ns/1ps
`include "tb_common.vh"

module tb_xxx;
    ...
    `VLEARN_TIMEOUT_GUARD(100000)   // 放模块层级，别放 initial 里
    initial begin
        ...
        `VLEARN_CHECK("用例名", 期望值, 实际值)
        vlearn_summary;
        $finish;
    end
endmodule
```

## 动手改之前

**先读 `tb_common.vh` 的文件头注释。** 那里写全了 iverilog 的两个坑（宏参数会在字符串字面量内被替换、任务里 `$time` 用错时间单位），以及为什么每条标记都要 `$fflush`——这些都是踩过之后写下的，踩第二次没有任何收益。

改完必须跑：

```bash
python tests/_smoke/run_smoke.py
```

协议相关的行为在 `tests/_smoke/` 里有负例守着，跑绿了再收工。
