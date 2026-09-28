"""verilator lint 解析与降级规则的检查。

    python tests/_smoke/check_vlint.py

**不跑 verilator。** 样本输出是手写的，理由和 `check_observe.py` 一样：

    要验的是「解析与提炼的规则」，不是 verilator 的输出格式本身。
    手写才能精确造出「verilator 一个字都没输出就挂了」「有警告但没有
    Verilation Report」「续行里全是文档链接」这类形状——靠真跑要么凑不出来，
    要么哪天换个 verilator 版本就变了。

    真正跑 verilator 的那部分（它到底报不报得出来组合环）在 run_smoke.py 里，
    那几条用例带 needs_verilator，缺工具时会跳过。

这个文件守的核心是一条：**「没跑」不能被报成「检查了，干净」。**
两者在下游都是零条诊断，长得一模一样，含义却相反。护栏在这里，不在
run_smoke 的端到端用例里——因为端到端只能证明"这条路径通了"，
证明不了"所有看起来像成功的形状都被区分了"。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from _console import init_console  # noqa: E402
from _vlint import parse_output  # noqa: E402
import _vlint  # noqa: E402

init_console()

FAILS: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILS.append(f"{label}\n      实际 {got!r}\n      期望 {want!r}")


def ok(label: str) -> None:
    print(f"  ✓ {label}")


# ---------------------------------------------------------------- 样本输出

# 报告行被拆成 "V e r i l a t i o n   R e p o r t"，而且两个词的拆法不一样。
# 照着源码写死字面量会匹配不上——这条断言就是防这个的。
REPORT = (
    "- V e r i l a t i o n   R e p o r t: Verilator 5.053 devel rev v5.052-119-g014c9820d (mod)\r\n"
    "- Verilator: Built from 0.035 MB sources in 3 modules\r\n"
    "- Verilator: Walltime 0.010 s; cpu 0.016 s on 1 threads; allocated 26.012 MB\r\n"
)

LATCH_OUT = (
    "%Warning-LATCH: tests\\x.v:17:5: Latch inferred for signal 'q' (not all control paths)\r\n"
    "                                           : ... Suggest use of always_latch for intentional latches\r\n"
    "   17 |     always @(*) begin\r\n"
    "      |     ^~~~~~\r\n"
    "                ... For warning description see https://verilator.org/warn/LATCH?v=5.053\r\n"
    '                ... Use "/* verilator lint_off LATCH */" and lint_on around source to disable this message.\r\n'
)

MULTIDRV_OUT = (
    "%Warning-MULTIDRIVENPROC: b\\m.v:8:27: Variable written to in always block also written by another always block: 'q'\r\n"
    "                                                          : ... note: In instance 'm'\r\n"
    "                          b\\m.v:8:27: \r\n"
    "    8 |     always @(posedge clk) q <= b;\r\n"
    "      |                           ^\r\n"
    "                          b\\m.v:7:27: ... Location of other write\r\n"
)

# Windows 绝对路径：盘符那个冒号不能让定位解析崩掉
ABSPATH_OUT = "%Warning-WIDTHEXPAND: D:\\ws\\verilog-learning\\a.v:10:14: Operator ASSIGNW expects 8 bits\n"

ERRORS_OUT = (
    "%Error: tests\\tb.v:3:10: Cannot find include file: 'tb_common.vh'\r\n"
    "%Error: Define or directive not defined: '`VLEARN_CHECK'\r\n"
    "%Error: Cannot continue\r\n"
    "%Error: Exiting due to 1 error(s)\r\n"
)


# ---------------------------------------------------------------- 用例

def main() -> int:
    print("--- 报告行与版本号 ---")
    w, e, ne, v = parse_output(REPORT)
    check("报告行要认得出来（逐字符带空格的标题）", v, "5.053")
    check("报告行本身不是诊断", (len(w), len(e)), (0, 0))
    ok("报告行被认出来，并取出 verilator 版本")

    print("\n--- 正常警告 ---")
    w, e, ne, v = parse_output(REPORT + LATCH_OUT)
    check("警告条数", len(w), 1)
    d = w[0]
    check("code", d.code, "LATCH")
    check("file", d.file, "tests\\x.v")
    check("line", d.line, 17)
    check("column", d.column, 5)
    check("severity", d.severity, "warning")
    check("沿用 always_latch 那条建议", d.note, "Suggest use of always_latch for intentional latches")
    ok("警告的文件/行/列/编号都取出来了")

    print("\n--- 续行的取舍 ---")
    check(
        "文档链接与 lint_off 说明不算「给下一步的建议」",
        "For warning description" in (d.note or "") or "lint_off" in (d.note or ""),
        False,
    )
    w2, _, _, _ = parse_output(REPORT + MULTIDRV_OUT)
    check("只有 note: In instance 这类噪声时，note 为空", w2[0].note, None)
    check("多驱动消息本身要留住", "another always block" in w2[0].message, True)
    ok("只挑有信息量的续行；文档链接/关警告说明/悬空的 Location 都丢掉")

    print("\n--- Windows 绝对路径 ---")
    w3, _, _, _ = parse_output(REPORT + ABSPATH_OUT)
    check("盘符里的冒号不影响定位", (w3[0].file, w3[0].line, w3[0].column),
          ("D:\\ws\\verilog-learning\\a.v", 10, 14))
    ok("带盘符的路径解析正确")

    print("\n--- %Error：只留原文，不进诊断 ---")
    w4, e4, ne4, _ = parse_output(REPORT + ERRORS_OUT)
    check("错误不进警告列表", len(w4), 0)
    check("错误总数（含没列出来的）", ne4, 3)
    check("原文条数（有一行是收尾语，不算）", len(e4), 3)
    check("收尾语 'Exiting due to N error(s)' 不算一条错误",
          any("Exiting due to" in x for x in e4), False)
    check("没有文件定位的 %Error 也要留住", any("VLEARN_CHECK" in x for x in e4), True)
    ok("%Error 走 errors 而不是 diagnostics —— 它多半是方言差异，会污染中文提示")

    print("\n--- ⚠ 核心：「没跑」不等于「干净」 ---")
    w5, e5, ne5, v5 = parse_output("")
    check("一个字都没输出 → 拿不到版本号", v5, None)
    check("一个字都没输出 → 零条诊断（**看起来像干净，但不能这么报**）", (len(w5), len(e5)), (0, 0))
    ok("空输出时 parse_output 本身无法区分「没跑」和「干净」——"
       "区分靠 run() 里的 Verilation Report 判据")

    # run() 的判据：ran = 报告行在不在。这里直接验那个正则，
    # 因为 run() 要真起进程，而"没跑"这件事本身没法用真进程造出来。
    check("没有报告行 → 判定为没跑",
          bool(_vlint._REPORT_RE.search(LATCH_OUT)), False)
    check("有报告行 → 判定为跑到了 elaboration",
          bool(_vlint._REPORT_RE.search(REPORT)), True)
    ok("ran 的判据是 Verilation Report 的存在性，不是「有没有警告」")

    print("\n--- 开关与体积 ---")
    check("-Wall 不在调用的开关里（它会带出 DECLFILENAME/MULTITOP 一堆噪声）",
          any("Wall" in a for a in _vlint.BASE_ARGS), False)
    check("-Wno-fatal 在（否则「有警告」会让退出码变成 1，退不退出就分不清警告和错误）",
          "-Wno-fatal" in _vlint.BASE_ARGS, True)
    check("错误原文有上限，避免刷屏", _vlint._MAX_ERRORS > 0, True)
    ok("开关集合符合设计（不用 -Wall、用 -Wno-fatal）")

    print()
    if FAILS:
        print(f"vlint 检查未通过：{len(FAILS)} 条断言失败")
        for f in FAILS:
            print(f"  ✗ {f}")
        return 1
    print("vlint 检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
