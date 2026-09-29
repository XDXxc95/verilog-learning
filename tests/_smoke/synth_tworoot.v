// 多顶层夹具：一个文件里**两棵互不相干的层次**（a1 和 b1 没有例化关系）。
//
// 它钉的是 `parse_sizer()` 修过的那个 bug。sizer 对每个顶层各出一份报告：
//
//     **** module/scope: a1     **** TOTALS      ← 第 1 份
//     **** module/scope: b1     **** TOTALS      ← 第 2 份
//
// 第一版的解析器把 `**** TOTALS` 当成**一份报告的结束标志**，于是一个单顶层
// 文件被切成了两份报告 —— **数字一个没丢，但结论全错**（本来是"一份设计
// 2 FF"变成"两份设计各 1 FF"，横着比就更离谱）。
// 现在 `TOTALS` 只切换"接下来读的是当前这份的合计"，下一个
// `module/scope:` 才关掉上一份。
//
// ★ 而且它对**合并求和**是明确拒绝的：`n_roots: 2` 时 `totals` 故意不给，
//   因为把两棵不相干的层次的门数加起来得到的和没有意义 ——
//   和 `schematic.py`「宁可说画不出来，也不画一个看着合理但是错的」同一条原则。
//   但光说"不给"不行，所以每棵层次的数字会**分别列出来**，
//   不然读者就只剩"这工具坏了"这一个解释。
`timescale 1ns/1ps

module a1 (input wire x, output wire y);
    assign y = x;
endmodule

module b1 (input wire x, output wire y);
    assign y = ~x;
endmodule
