"""viz.py 的编排检查：仿真顶层到底给了谁、没波形时怪谁。

    python tests/_smoke/check_viz.py

这里盯的是 viz.py 的两个**静默出错**点。它们不抛异常、不崩、退出码
也未必红，真正的伤害是**把工具的错说成使用者测试台的错** —— 学习者唯一
能控制的那样东西被冤枉，而他明明写对了：

1. **仿真顶层必须来自 `--tb`。** iverilog 收到 `-s <顶层>` 只展开那一棵
   子树；拿设计模块当顶层，测试台根本不会被实例化（不打印标记、不执行
   `$dumpfile`）。症状是「无法判定 / 测试台缺少判定标记 / 没有产生 VCD」。
   2026-09-30 实测，唯一差别就是顶层名：

       -s counter4      status=inconclusive   markers= 0
       -s tb_counter4   status=pass           markers=12

   `viz.py` 原来用**同一个** `top` 变量喂结构图和仿真，而 `_guess_top()`
   是按结构图的偏好写的（优先非 `tb*`）—— 于是不给 `--top` 时必然踩中。
   `--compare` 更隐蔽：它的 `--top` 是**被对比的模块名**（文档示例给的是
   `--top decoder3_8`），喂给仿真同样出不来波形，整组对比全是空的。

2. **找不到波形时，不许断言测试台没写 `$dumpfile`。** `$dumpfile` 的路径
   是相对**进程 cwd** 解析的，写到 `build/` 之外一样是有效波形。原来只看
   `cfg.workdir`，于是「文件就躺在仓库根」被报成「测试台里需要 $dumpfile
   + $dumpvars」—— 同一类谎。

两条都配了负例（`--- 负例 ---` 那两节），改 `viz.py` 的 `_guess_sim_top`
或 `sim.py` 的 `_snapshot_vcd` 之前先看这里。

★ 变异测试记录（各改一处、确认变红后还原）：
  · `_guess_sim_top` 改成直接返回 `_guess_top(designs + [tb])`（即退回旧行为）
    → 第 1 节和第 2 节都红；
  · `_snapshot_vcd` 的 roots 去掉 cwd 那一项 → 第 4 节红；
  · 把 `_observe_block` 的 reason 改回「测试台里需要 $dumpfile + $dumpvars」
    → 第 3 节红。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from _console import init_console  # noqa: E402

init_console()

import viz  # noqa: E402

SMOKE = ROOT / "tests" / "_smoke"
TOOLS = ROOT / "tools"
WORK = ROOT / "build" / "_smoke_viz"
ROOT_VCD = ROOT / "_smoke_vcdroot.vcd"

DUT = SMOKE / "dut_counter4.v"
TB = SMOKE / "tb_counter4.v"
DUT_N = SMOKE / "dut_counterN.v"
TB_N = SMOKE / "tb_counterN.v"
TB_ROOT = SMOKE / "tb_vcdroot.v"

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        fails.append(msg)


def run_viz(*args: str) -> tuple[int, str, str]:
    p = subprocess.run(
        [sys.executable, str(TOOLS / "viz.py"), *args],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return p.returncode, p.stdout, p.stderr


def sim_json(*args: str) -> dict:
    """跑 sim.py 拿它的结构化结果（stdout 只放 JSON，摘要走 stderr）。"""
    p = subprocess.run(
        [sys.executable, str(TOOLS / "sim.py"), *args],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"_parse_error": p.stdout[-500:], "_stderr": p.stderr[-500:]}


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    ROOT_VCD.unlink(missing_ok=True)

    # ---------------- 1. 两个「顶层」必须是两个概念 ----------------
    print("\n--- 仿真顶层 ≠ 结构图顶层 ---")
    sim_top = viz._guess_sim_top([DUT], TB)
    sch_top = viz._guess_top([DUT, TB])
    check(sim_top == "tb_counter4",
          f"★ 有 --tb 时仿真顶层必须取**测试台文件里的**模块，实际 {sim_top!r}")
    check(sch_top == "counter4",
          f"结构图顶层该是设计模块，实际 {sch_top!r}")
    check(sim_top != sch_top,
          "★ 这两个 fixture 下两个顶层本来就不同 —— 一旦相等，说明它们又被合成"
          "一个变量了（这正是原来那个 bug 的形态）")
    # 没有 --tb 时退回设计模块，别取 None
    check(viz._guess_sim_top([DUT], None) == "counter4",
          "没有测试台时该退回设计模块当顶层")
    print("  ✓ 仿真取测试台、结构图取设计，两者分开（且无 --tb 时能退回）")

    # ---------------- 2. 端到端：不给 --top 也要真跑到测试台 ----------------
    print("\n--- 不给 --top，端到端跑一遍 ---")
    code, out, err = run_viz(str(DUT), "--tb", str(TB),
                             "--wave", "--no-schematic", "--ascii",
                             "--out", str(WORK))
    check(code == 0, f"该成功退出，实际 {code}；{err[-300:]}")
    check("通过（5/5" in out,
          f"★ 判定该跑到测试台并拿到 5/5（拿设计模块当顶层的话这里是"
          f"「无法判定 / 测试台缺少判定标记」）：{out[:400]}")
    check("测试台缺少判定标记" not in out,
          "★ 不许出现「测试台缺少判定标记」—— 那是把工具的顶层选错了说成"
          "学习者的测试台有问题")
    waves = list(WORK.rglob("wave.html"))
    check(len(waves) == 1, f"该正好出 1 份 wave.html，实际 {len(waves)} 份")
    check("tb_counter4.q" in out,
          "ASCII 波形里该有测试台作用域的信号（没有就说明测试台没被跑起来）")
    print("  ✓ 不给 --top 照样 5/5、波形出得来、信号带 tb_ 作用域")

    # ---------------- 3. 找不到波形时的措辞 ----------------
    print("\n--- 找不到波形时，不许怪测试台 ---")
    r = sim_json(str(DUT_N), "--tb", str(TB_N), "--top", "tb_counterN")
    check(r.get("artifacts", {}).get("vcd") is None,
          "tb_counterN.v 本来就没有 $dumpfile，vcd 该是 None")
    reason = (r.get("observe") or {}).get("reason") or ""
    check("测试台里需要" not in reason and "测试台里没有" not in reason,
          f"★ 「找不到」不等于「他没写」—— 不许断言测试台的错：{reason!r}")
    check("build" in reason,
          f"要说清**找了哪里**，实际：{reason!r}")
    check(r.get("status") == "pass",
          f"★ 没有波形绝不能改判定（它只是少一层辅助）：status={r.get('status')!r}")
    print("  ✓ 措辞说「已找过哪」，不断言测试台；且判定不受影响（仍是 pass）")

    # ---------------- 4. 负例：$dumpfile 落在 build/ 之外 ----------------
    print("\n--- 负例：波形落在仓库根，也必须被发现 ---")
    r = sim_json(str(DUT), "--tb", str(TB_ROOT), "--top", "tb_vcdroot")
    check(r.get("status") == "pass",
          f"这一份测试台自身是通过的，status 该是 pass：{r.get('status')!r}")
    vcd = (r.get("artifacts") or {}).get("vcd")
    check(vcd == "_smoke_vcdroot.vcd",
          f"★ 落在仓库根的波形也要认出来（按仓库根相对记路径），实际 {vcd!r}")
    check((r.get("observe") or {}).get("ok") is True,
          f"认出来就该能观察，实际 {r.get('observe')!r}")
    check(ROOT_VCD.is_file(), "$dumpfile 那个相对路径确实落在了仓库根")
    print("  ✓ 仓库根下的波形被认出来、能观察，路径按仓库根记")

    shutil.rmtree(WORK, ignore_errors=True)
    ROOT_VCD.unlink(missing_ok=True)

    print()
    if fails:
        for f in fails:
            print(f"  ✗ {f}")
        print(f"\nviz 检查未通过：{len(fails)} 项")
        return 1
    print("viz 检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
