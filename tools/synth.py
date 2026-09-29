"""综合产物：门级网表（blif）与资源估算（sizer）。

    python tools/synth.py dff.v                        # 资源估算
    python tools/synth.py bin.v onehot.v gray.v        # 三种状态编码横向对比
    python tools/synth.py fifo.v --netlist             # 顺带出 blif 网表

**这里综合的是待测模块，不是测试台。** 合测试台必定失败（`$display`、
`#delay`、`initial` 都合不出来），所以不要像 sim.py 那样把 tb 传进来。

## 先说清楚它不是什么（这一段比用法重要）

iverilog 不是综合器，`-t sizer` / `-t blif` 是它自带的**结构提取**后端：

- **不做逻辑优化、不做工艺映射。** `Logic Gates` 数的是未映射的原始门
  （`LOG[n]` / `MUX[n]` / `ADDER[n]` 这些宏单元），不是真实面积。
  所以它**只能横向比**（三种写法谁更省），不能当"这个设计 N 个门"的报告用。
- **只吃门级式的可综合子集**：`assign`、组合 `always`、不含延迟的时序
  `always`。碰到合不了的构造它直接拒绝，不给部分结果。
- **不认识的构造一律说"合不了"**，绝不猜一个数出来。

真要做综合（面积、时序、让工具自己挑状态编码）得上 yosys，见
`tools/check_tools.py --level L2`。

## 三个必须挡住的坑（都是实测踩出来的）

1. **`-t sizer` 不写 stdout，只写 `-o` 指向的文件；不给 `-o` 就什么都不输出，
   退出码还是 0。** "跑成功了、什么都没发生"最容易被读成"这个设计是空的"。
   所以本脚本**永远**显式给 `-o`，退出码 0 而文件不在就判失败。
2. **`-g2012` 下它会一边报 `The root scope $unit must be a module.`、退出码 1，
   一边照样把数字写进文件。** 只信"文件在不在"就会把一次失败的运行报成成功。
   所以本脚本**只信退出码**：rc≠0 一律不读文件内容（但会如实标出
   `output_written_despite_error`，因为那本身就是这条坑的证据）。
   `-g2005` 下没有这个问题，所以这里固定用 `-g2005`。
3. **失败时输出文件不会被清空。** 上一次运行的内容留在原地，
   下一次失败会读到**上一份设计的数字** —— 一个完全静默的错答案。
   所以每次都先删掉目标文件再跑。

## 两条腿：sizer（资源估算）和 blif（网表）

**它们能接受的 RTL 不一样，必须分开报。** 实测：三份状态机夹具用
`always @(posedge clk or negedge rst_n)`，sizer 全都出数字（2/4/2 个触发器），
blif 全都拒绝 —— `sorry: blif: Asynchronous clear not implemented yet`。

所以 `ok` 和 `n_sizer_ok` 是两个独立的计数（同 `lint.ran` / `n_warnings` 的教训）。
`--netlist` 下网表没出来会**把 `ok` 翻成 false**：用户显式要了网表、一份没出，
还报 `pass` 就是"跑绿了但你要的东西不在"。

## 四类失败（各有各的话要说）

- `async_reset_unsupported`：**RTL 没问题，是 iverilog 后端的限制。**
  `always @(posedge clk or negedge rst_n)` 在 yosys/Vivado/DC 里都是标准写法，
  只有 iverilog 的 blif 后端没实现异步 clear/set。混进 `not_synthesizable`
  等于告诉学习者"你写法有问题"——假话。sizer 的数字不受影响。
- `not_synthesizable`：合不了。`sorry:` 开头是"没实现"，`error:` 开头是
  "这个构造不支持"，两种都带文件行号。
- `iverilog_crash`：**iverilog 自己断言崩了**（退出码 `0xC0000409`）。
  实测触发条件是：组合 `always` 里的 `case` 不全 → 推断出锁存器 →
  `synth2.cc` 的断言 `sel_need >= 2` 挂掉。这不是你的语法错，
  但**源头是 RTL 的问题**：`tools/_lint.py` 早在编译期就会报 `latch_inference`，
  先去看那条。
- `compile_error`：前端就过不去（语法错、模块名错）。和 sim.py 报的是同一批东西。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _console import init_console  # noqa: E402
from _proc import run as run_proc  # noqa: E402
from _toolchain import detect, repo_root  # noqa: E402

SCHEMA = "vlearn.synth/1"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ENV = 4
EXIT_USAGE = 64

# -g2005 是硬要求，不是口味：-g2012 下 sizer 的 elaboration 直接报
# "The root scope $unit must be a module."（退出码 1），而且**照样写出数字**。
# 见文件头第 2 条坑。
GFLAG = "-g2005"

# iverilog 内部断言崩溃的退出码。Windows 上 abort() 走的是这个 NTSTATUS。
# 0xC0000409 = STATUS_STACK_BUFFER_OVERRUN，msvcrt 的 abort() 用它。
CRASH_CODES = {0xC0000409, 0xC0000005}

_SCOPE_RE = re.compile(r"^\*{4}\s*module/scope:\s*(\S+)\s*$")
_TOTALS_RE = re.compile(r"^\*{4}\s*TOTALS\s*$")
_FF_RE = re.compile(r"^\s*Flip-Flops\s*:\s*(\d+)\s*$")
_GATES_RE = re.compile(r"^\s*Logic Gates\s*:\s*(\d+)\s*$")
_CELL_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\[(\d+)\]\s*:\s*(\d+)\s*(.*?)\s*$")

_BLIF_LATCH_RE = re.compile(
    r"^\.latch\s+(\S+)\s+(\S+)(?:\s+(\S+))?(?:\s+(\S+))?(?:\s+(\S+))?")
# blif 里以 # 开头的注释行是 iverilog 自己写的"展开说明"，例如
#     # dff.v:23: IVL_LPM_ADD: width=4
# 它正是学生最想知道的"我这一行被合成成了什么硬件"，所以留下来单独报，
# 而不是当噪声丢掉。
_BLIF_NOTE_RE = re.compile(r"^#\s*(.+?):\s*(IVL_\w+)(?::\s*(.*))?$")

_LIMITS = [
    "iverilog 的 sizer/blif 不是综合器：不做逻辑优化、不做工艺映射，"
    "门数只能横向比（三种写法谁更省），不能当真实面积报告用。",
    "只吃门级式可综合子集：assign / 组合 always / 不含延迟的时序 always。",
    "碰到合不了的构造它直接拒绝，不会给部分结果。",
    "**它只对结构差异敏感，对编码差异不敏感。**实测：同一台状态机，"
    "二进制和格雷给出一模一样的数字（都是 2 FF / 21 门），独热才分出"
    "4 FF / 41 门 —— 因为前者只是改了 case 里的常量，结构没变。"
    "★ 编码的代价是**两半**，这里只管面积那一半；另一半是翻转（动态功耗的代理），"
    "**这个层级量不出来，而且装了 yosys 也量不出来** —— 综合器一概不做活动性分析，"
    "它不知道哪些跳变是热的。那半只有仿真知道，见 tools/toggle.py（零下载）："
    "同一台 4 位计数器、同一套激励，二进制 34 次变化翻 63 位，格雷 34 次翻 34 位。",
]


# ---------------------------------------------------------------- sizer 解析


def parse_sizer(text: str) -> tuple[list[dict], list[str]]:
    """把 sizer 的输出切成一份份「报告」，外加没能识别的行。

    ★ 为什么要按 `**** TOTALS` 分块，而不是只留一个总和：
      **一个源文件里有两个顶层模块时，sizer 会把两棵独立的层次各报一遍**
      （实测：两个 scope 块、两个 TOTALS 块）。合并成一份等于把两个不相干的
      电路加在一起，得到一个没有意义的和，而且读的人看不出来。
      所以保持分块，让上层如实说"这份文件有 2 个顶层"。

    未识别的行一律收集进 unparsed 原样带出去 —— 静默丢行和静默丢结构是一类错。

    状态机只有三句话：
      `module/scope: X` → 新开一个 scope，数字进它；**若上一份已经收过 TOTALS，
                          说明这是下一个顶层，先把上一份结掉**
      `TOTALS`          → 这一份的汇总段开始，之后的数字进这份的 totals
      其它              → 进"当前该去的地方"（scope 或 totals）

    注意 `TOTALS` **不是**报告的结束标志，它是报告内部的一段：
    一个顶层的输出就是 `scope 块 + TOTALS 块`。第一版把它当结束标志，
    结果一个单顶层文件被切成了两份报告（一份有 scope 没数字、一份有数字没 scope），
    `n_roots` 报成 2 —— 数字一个没丢，但结论全错。
    """
    reports: list[dict] = []
    unparsed: list[str] = []
    cur = {"scopes": [], "totals": {}}
    into_totals = False

    def close() -> None:
        nonlocal cur
        if cur["scopes"] or cur["totals"]:
            reports.append(cur)
        cur = {"scopes": [], "totals": {}}

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        m = _SCOPE_RE.match(line)
        if m:
            if into_totals:
                close()
            into_totals = False
            cur["scopes"].append({"scope": m.group(1),
                                  "ff": None, "gates": None, "cells": {}})
            continue

        if _TOTALS_RE.match(line):
            into_totals = True
            continue

        if into_totals:
            target = cur["totals"]
        elif cur["scopes"]:
            target = cur["scopes"][-1]
        else:
            # 一个 scope 都没有就来了数字：记进 totals，至少不丢。
            # 上层会看到 scopes 为空，从而知道这次的输出不合常规。
            target = cur["totals"]

        m = _FF_RE.match(line)
        if m:
            target["ff"] = int(m.group(1))
            continue
        m = _GATES_RE.match(line)
        if m:
            target["gates"] = int(m.group(1))
            continue
        m = _CELL_RE.match(line)
        if m:
            key = f"{m.group(1)}[{m.group(2)}]"
            target.setdefault("cells", {})[key] = {
                "count": int(m.group(3)), "unit": m.group(4),
            }
            continue
        unparsed.append(line)

    close()
    return reports, unparsed


# ---------------------------------------------------------------- blif 解析


def parse_blif(text: str) -> dict:
    """blif 网表摘要。

    blif 是 **LUT/PLA 式**的网表（`.names` 是一张真值表），不是门级网表：
    一个 `.names` 块对应一个查找表，块里的行数是它的 ON-set 大小。
    所以字段叫 n_lut 而不是"门数" —— 叫错了会让人拿它当门数报告用。
    """
    model = None
    inputs: list[str] = []
    outputs: list[str] = []
    latches: list[dict] = []
    subckts: list[str] = []
    inlined: list[dict] = []
    lut_sizes: list[int] = []
    lut_name: str | None = None
    lut_rows = 0

    def close_lut() -> None:
        nonlocal lut_name, lut_rows
        if lut_name is not None:
            lut_sizes.append(lut_rows)
        lut_name, lut_rows = None, 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = _BLIF_NOTE_RE.match(line)
            if m:
                inlined.append({"where": m.group(1), "kind": m.group(2),
                                "detail": (m.group(3) or "").strip()})
            continue
        if line.startswith("."):
            close_lut()
            if line.startswith(".model"):
                parts = line.split()
                model = parts[1] if len(parts) > 1 else None
            elif line.startswith(".inputs"):
                inputs += line.split()[1:]
            elif line.startswith(".outputs"):
                outputs += line.split()[1:]
            elif line.startswith(".names"):
                parts = line.split()
                lut_name = parts[1] if len(parts) > 1 else ""
                lut_rows = 0
            elif line.startswith(".latch"):
                m = _BLIF_LATCH_RE.match(line)
                if m:
                    latches.append({
                        "in": m.group(1), "out": m.group(2),
                        # blif 的锁存器类型：re=上升沿, fe=下降沿,
                        # ah/al/as=高/低电平敏感
                        "type": m.group(3), "ctrl": m.group(4), "init": m.group(5),
                    })
            elif line.startswith(".subckt"):
                parts = line.split()
                subckts.append(parts[1] if len(parts) > 1 else "")
            continue
        # 真值表行
        if lut_name is not None:
            lut_rows += 1
    close_lut()

    return {
        "model": model,
        "inputs": inputs,
        "outputs": outputs,
        "n_lut": len(lut_sizes),
        # 常量 LUT（`.names` 后面只有一行 `0` 或 `1`）单独数出来：
        # 它们是 iverilog 造的 tie-off，不是学生写的逻辑
        "n_const_lut": sum(1 for n in lut_sizes if n == 1),
        "n_lut_terms": sum(lut_sizes),
        "max_lut_terms": max(lut_sizes) if lut_sizes else 0,
        "n_latch": len(latches),
        "latches": latches,
        "n_subckt": len(subckts),
        "subckts": subckts,
        "inlined": inlined,
    }


# ---------------------------------------------------------------- 失败翻译


def _translate(rc: int, stderr: str) -> dict:
    """把 iverilog 的失败翻译成结构化的 kind + 中文说明。

    置信度**必须诚实**：`sorry:` / 带文件行号的 `error:` 是高置信度；
    断言崩溃只能说"实测的常见源头是 X"，不能断言就是它。
    """
    text = stderr.strip()
    errs = [
        {"file": m.group(1), "line": int(m.group(2)),
         "severity": m.group(3), "message": m.group(4).strip()}
        for m in re.finditer(
            r"^(.+?):(\d+):\s*(sorry|error|assert):\s*(.*)$", text, re.M)
    ]

    if rc in CRASH_CODES:
        return {
            "kind": "iverilog_crash",
            "exit_code": rc,
            "message_zh": (
                "iverilog 自己的断言崩了（不是你的语法错，前端已经过了）。"
                "实测最常见的源头：组合 always 里的 case/if 分支不全，"
                "推断出锁存器之后 sizer 的断言挂掉。先去看 tools/_lint.py 报的 "
                "latch_inference —— 它更早、也更好懂。"
            ),
            "raw_stderr": text,
            "diagnostics": errs,
            "confidence": "medium",
        }

    if "No top level modules" in text:
        return {
            "kind": "no_top",
            "exit_code": rc,
            "message_zh": ("源文件里没有模块，或者没给出顶层（-s）。"
                           "检查文件是不是空的、或者只有注释和宏定义。"),
            "raw_stderr": text,
            "diagnostics": errs,
            "confidence": "high",
        }

    if errs:
        first = errs[0]
        msg = first["message"]
        if "Asynchronous clear not implemented yet" in msg or \
           "Asynchronous set not implemented yet" in msg:
            # 这一条必须单列：**RTL 本身完全没问题**，真实 FPGA/ASIC 流程里
            # 异步复位是最常见的写法。合不了的是 iverilog 的 blif 后端。
            # 混进 not_synthesizable 等于告诉学习者"你的写法有问题" —— 假话。
            return {
                "kind": "async_reset_unsupported",
                "exit_code": rc,
                "message_zh": (
                    f"{first['file']}:{first['line']} 异步复位合不了 —— "
                    "但**这不是你的写法问题**。`always @(posedge clk or negedge rst_n)` "
                    "在真实综合工具（yosys / Vivado / DC）里都是标准写法，"
                    "是 iverilog 的 blif 后端没实现异步 clear/set。"
                    "sizer 的数字（触发器、门数）不受影响，照常可信。"
                ),
                "raw_stderr": text,
                "diagnostics": errs,
                "confidence": "high",
            }
        if msg.startswith("Assignment to variable location in memory"):
            hint = ("对 memory（数组）的赋值综合不了：数组在 iverilog 的 sizer 里"
                    "没有对应的硬件模型。")
        elif "Unable to synthesize" in msg:
            hint = ("这个 always 块合不出来。#延迟、initial 里的赋值、"
                    "系统任务（$display/$finish）都会让整个 process 被放弃 —— "
                    "时间是仿真概念，硬件里不存在。")
        else:
            hint = "iverilog 拒绝了这个构造。"
        return {
            "kind": "not_synthesizable",
            "exit_code": rc,
            "message_zh": f"{first['file']}:{first['line']} {hint}",
            "raw_stderr": text,
            "diagnostics": errs,
            "confidence": "high",
        }

    return {
        "kind": "compile_error",
        "exit_code": rc,
        "message_zh": ("iverilog 前端就没过（语法错、模块名错一类），"
                       "和 sim.py 报的是同一批东西。"),
        "raw_stderr": text,
        "diagnostics": errs,
        "confidence": "high",
    }


# ---------------------------------------------------------------- 一次综合


def synth_one(tc, src: Path, root: Path, workdir: Path, top: str | None,
              params: list[str], defines: list[str], includes: list[str],
              want_netlist: bool, timeout: float) -> dict:
    """对**一个**源文件做一次综合。"""
    workdir.mkdir(parents=True, exist_ok=True)
    stem = src.stem
    sizer_out = workdir / f"{stem}.sizer.txt"
    blif_out = workdir / f"{stem}.blif"

    base = [str(tc.iverilog), GFLAG, "-I", str(root / "tb")]
    for inc in includes:
        base += ["-I", inc]
    for d in defines:
        base += ["-D", d]

    def cmd_for(target: str, out: Path) -> list[str]:
        cmd = base + ["-t", target]
        if top:
            # 有 -s 时 -P 才有着落（-P 的第一段必须是模块名）
            cmd += ["-s", top]
            cmd += [f"-P{top}.{p}" for p in params]
        cmd += ["-o", str(out), str(src)]
        return cmd

    # ★ 跑之前先删掉上一次的产物。失败时 iverilog **不会**清空 -o 指向的文件，
    #   留着上一份设计的数字，读出来就是一个静默的错答案（实测复现过：
    #   拿一个空文件去跑，它把上一次的成功输出原样留在原地）。
    for p in (sizer_out, blif_out):
        if p.exists():
            p.unlink()

    res: dict = {
        "file": src.as_posix(),
        "top": top,
        "params": params,
        "defines": defines,
        "workdir": workdir.as_posix(),
    }

    cmd = cmd_for("sizer", sizer_out)
    r = run_proc(cmd, cwd=str(root), timeout=timeout)
    res["sizer_command"] = cmd
    res["sizer_exit_code"] = r.returncode
    res["sizer_stderr"] = r.stderr.strip()[:4000]

    if r.timed_out:
        res.update({"ok": False, "status": "timeout",
                    "error": {"kind": "timeout", "exit_code": None,
                              "message_zh": "综合超时。展开量太大"
                                            "（大 generate、大位宽运算）时会这样。"}})
        return res

    if r.returncode != 0:
        err = _translate(r.returncode or 0, r.stderr)
        # rc≠0 却写出了文件 —— 这正是 -g2012 那个坑的现场。如实记下来，
        # 但**绝不**把里面的数字当结果用（上面已经从流程上隔离了）。
        err["output_written_despite_error"] = sizer_out.exists()
        res.update({"ok": False, "status": err["kind"], "error": err})
        return res

    if not sizer_out.exists():
        res.update({"ok": False, "status": "no_output",
                    "error": {"kind": "no_output", "exit_code": 0,
                              "message_zh": ("退出码是 0，但没有输出文件。"
                                             "`-t sizer` 只写 `-o` 指向的文件，"
                                             "不给 -o 就什么都不输出、退出码还是 0。"),
                              "confidence": "high"}})
        return res

    text = sizer_out.read_text(encoding="utf-8", errors="replace")
    reports, unparsed = parse_sizer(text)
    res["sizer_raw"] = text
    res["reports"] = reports
    res["sizer_unparsed"] = unparsed
    res["n_roots"] = len(reports)

    if not reports:
        res.update({"ok": False, "status": "empty_output",
                    "error": {"kind": "empty_output", "exit_code": 0,
                              "message_zh": "sizer 的输出里没有任何模块 / scope 块。",
                              "confidence": "high"}})
        return res

    # 单一顶层时才给一份"主结论"；多顶层时只标出来，不硬凑一个总和
    if len(reports) == 1:
        res["totals"] = reports[0]["totals"]
        res["scopes"] = reports[0]["scopes"]
    else:
        res["totals"] = None
        res["notes_zh"] = [
            f"这个源文件里有 {len(reports)} 个顶层模块，sizer 各报了一份，"
            f"**没有合并**（把两棵不相干的层次加在一起得到的和没有意义）。"
        ]

    if want_netlist:
        bcmd = cmd_for("blif", blif_out)
        br = run_proc(bcmd, cwd=str(root), timeout=timeout)
        res["blif_command"] = bcmd
        res["blif_exit_code"] = br.returncode
        if br.returncode == 0 and blif_out.exists():
            nl = parse_blif(blif_out.read_text(encoding="utf-8", errors="replace"))
            nl["file"] = blif_out.as_posix()
            nl["bytes"] = blif_out.stat().st_size
            res["netlist"] = nl
        else:
            # blif 挂了不影响 sizer 的数字，但**不能不影响 ok** ——
            # 用户显式要了网表、一份没出，工具还报 pass，就成了
            # "跑绿了但你要的东西不在"，正是本仓库最要避免的那种报绿。
            res["netlist_error"] = _translate(br.returncode or 0, br.stderr)

    res["ok"] = not (want_netlist and "netlist" not in res)
    res["status"] = "synth_ok" if res["ok"] else "netlist_failed"
    return res


# ---------------------------------------------------------------- 输出


def _fmt_cells(cells: dict) -> str:
    if not cells:
        return ""
    return " ".join(f"{k}×{v['count']}" for k, v in sorted(cells.items()))


def _print_summary(rep: dict) -> None:
    """人类可读摘要，走 stderr（stdout 只放 JSON，和 sim.py 一个约定）。"""
    w = sys.stderr.write
    w("=" * 60 + "\n")
    w(f"综合产物：{len(rep['designs'])} 份设计"
      f"（iverilog {GFLAG} -t sizer，不是综合器，门数只能横向比）\n")
    w("-" * 60 + "\n")
    w(f"  {'文件':<22}{'FF':>5}{'门':>6}   {'宏单元':<30}{'网表'}\n")

    for d in rep["designs"]:
        name = Path(d["file"]).name
        # sizer 成了就照常出数字。网表失败是**另一条**腿的事，
        # 不能因此把已经拿到的触发器/门数一起打成"—"。
        t = d.get("totals")
        if t:
            nl = d.get("netlist") or {}
            net = (f"{nl.get('n_lut')} LUT/{nl.get('n_latch')} FF"
                   if nl else "✗ 没出")
            roots = f"（{d['n_roots']} 个顶层）" if d.get("n_roots", 1) > 1 else ""
            w(f"  {name:<22}{str(t.get('ff', '—')):>5}"
              f"{str(t.get('gates', '—')):>6}   "
              f"{_fmt_cells(t.get('cells') or {}):<30} {net}{roots}\n")
        else:
            w(f"  {name:<22}{'—':>5}{'—':>6}   "
              f"{_err_of(d).get('kind', 'failed')}\n")

    for d in rep["designs"]:
        for n in (d.get("notes_zh") or []):
            w(f"  ! {Path(d['file']).name}：{n}\n")
        if d.get("ok") and d.get("n_roots", 1) > 1:
            # 多顶层时上面那一行是"—"，因为**故意**不给合并后的数字。
            # 这里把每一棵层次单独列出来 —— 不给数字又不给替代品，
            # 读者就只剩"这工具坏了"这一个解释。
            for r in d.get("reports") or []:
                t = r.get("totals") or {}
                names = "、".join(s["scope"] for s in (r.get("scopes") or [])) or "（无名）"
                w(f"      {names:<28}{t.get('ff', '—'):>4} FF"
                  f"{t.get('gates', '—'):>5} 门\n")
        if not d.get("netlist") and d.get("netlist_error"):
            e = d["netlist_error"]
            w(f"\n  ✗ {Path(d['file']).name} 的网表（--netlist）没出来 —— "
              f"[{e.get('kind')}]\n      {e.get('message_zh')}\n")
        elif not d.get("ok"):
            e = _err_of(d)
            w(f"\n  ✗ {Path(d['file']).name} —— {e.get('message_zh')}\n")
            for x in (e.get("diagnostics") or [])[:2]:
                w(f"      {x['file']}:{x['line']} {x['message']}\n")
        nl = d.get("netlist")
        if nl and nl.get("inlined"):
            w(f"\n  {Path(d['file']).name} 里被合成出来的宏"
              f"（这是「这一行变成了什么硬件」的直接答案）：\n")
            for i in nl["inlined"][:6]:
                w(f"    {i['where']}  →  {i['kind']}"
                  f"{(' ' + i['detail']) if i['detail'] else ''}\n")

    w("-" * 60 + "\n")
    w("  说明：门数是未映射的原始门，不是真实面积，只能横向比。\n")
    n_tot = sum(1 for d in rep["designs"] if d.get("totals"))
    w(f"\n资源估算：{n_tot}/{len(rep['designs'])} 份拿到数字\n")
    if rep.get("wanted_netlist"):
        n_nl = sum(1 for d in rep["designs"] if d.get("netlist"))
        w(f"网表（--netlist）：{n_nl}/{len(rep['designs'])} 份产出"
          f"{'' if n_nl == len(rep['designs']) else '  ← 你要了网表，这几份没给出来'}\n")
    w(f"结果：{'全部成功 ✓' if rep['ok'] else '有未完成项 ✗'}\n")
    w("=" * 60 + "\n")


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    init_console()
    root = repo_root()

    ap = argparse.ArgumentParser(
        prog="synth.py",
        description="综合产物：blif 门级网表 + sizer 资源估算（iverilog 自带后端）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("## 先说清楚它不是什么", 1)[0].split("\n", 1)[1].strip(),
    )
    ap.add_argument("designs", nargs="*", type=Path,
                    help="设计源文件（可多个 → 出一张横向对比表）。**不要传测试台**")
    ap.add_argument("--top", help="顶层模块名（不写就由 iverilog 自己找未被例化的那个）")
    ap.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                    help="覆盖**设计自己**的参数（可重复）。这里的目标是设计的顶层，"
                         "不是测试台 —— 和 regress.py 的 --param 目标不同，"
                         "用 --param 时必须同时给 --top")
    ap.add_argument("--define", action="append", default=[], metavar="NAME=VALUE",
                    help="宏定义（可重复）")
    ap.add_argument("--include", action="append", default=[], metavar="DIR",
                    help="额外的 include 目录（可重复）")
    ap.add_argument("--netlist", action="store_true",
                    help="同时生成并解析 blif 网表（默认只做资源估算）")
    ap.add_argument("--workdir", type=Path, help="产物目录（默认 build/synth）")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--json", type=Path, help="把 JSON 结果同时写入该文件")
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not args.designs:
        ap.print_help()
        return EXIT_USAGE

    if args.param and not args.top:
        # -P 的写法是 -P<模块名>.<参数名>=<值>，模块名从哪来只能是 --top。
        # 不给就在这里拦住，不要生成一个 -PNAME=V 的畸形命令行让 iverilog 去猜。
        print("--param 必须配 --top：iverilog 的 -P 要写成 "
              "-P<模块名>.<参数名>=<值>，没有 --top 就没有模块名。",
              file=sys.stderr)
        return EXIT_USAGE

    missing = [str(p) for p in args.designs if not p.is_file()]
    if missing:
        print(f"找不到这些文件：{', '.join(missing)}", file=sys.stderr)
        return EXIT_USAGE

    tb_like = [p.name for p in args.designs
               if p.name.startswith("tb_") or "testbench" in p.name.lower()]
    if tb_like:
        # 合测试台必定失败（$display/#delay/initial），所以这不是"警告一下"，
        # 是用法错误。说清楚原因，别让人以为是工具不支持。
        print(f"这些看着是测试台：{', '.join(tb_like)}。"
              f"综合的对象是**待测模块**，测试台里有 $display / #延迟 / initial，"
              f"iverilog 的 sizer 合不出来（这不是工具缺陷，是那些东西没有"
              f"对应的硬件）。只传设计文件。", file=sys.stderr)
        return EXIT_USAGE

    tc = detect(None)
    if not tc.ok:
        rep = {"schema": SCHEMA, "ok": False, "status": "env_error",
               "error": {"kind": "no_toolchain", "message": "找不到 iverilog。"},
               "toolchain": tc.as_dict(),
               "next_actions_zh": ["先跑 python tools/setup_toolchain.py"]}
        _emit(rep, args)
        return EXIT_ENV

    workdir = args.workdir or (root / "build" / "synth")
    designs: list[dict] = []
    for src in args.designs:
        # 每份设计一个子目录。同名文件（三份实现都叫 fsm.v 是常事）会撞，
        # 撞了的后果是产物互相覆盖 —— 而报告里的数字还是各自的，对不上账。
        sub = workdir / src.stem
        n = sum(1 for d in designs if Path(d["file"]).stem == src.stem)
        if n:
            sub = workdir / f"{src.stem}_{n}"
        designs.append(synth_one(
            tc, src, root, sub, args.top, args.param, args.define, args.include,
            args.netlist, args.timeout))

    # 两个计数必须分开报：`n_ok` 是**整体**成没成，`n_sizer_ok` 是 sizer 那条腿
    # 单独成没成。和 lint.ran / lint.n_warnings 是同一条教训 ——
    # 只报一个数，"资源估算拿到了但网表没出来"就无处表达。
    n_ok = sum(1 for d in designs if d.get("ok"))
    n_sizer_ok = sum(1 for d in designs if d.get("totals"))
    rep = {
        "schema": SCHEMA,
        "ok": n_ok == len(designs),
        "status": "pass" if n_ok == len(designs) else "fail",
        "handler": "synth.py（iverilog -t sizer / -t blif）",
        "tool": {
            "exe": str(tc.iverilog),
            "gflag": GFLAG,
            "version": _version(tc.iverilog),
        },
        "designs": designs,
        "n_ok": n_ok,
        "n_sizer_ok": n_sizer_ok,
        "wanted_netlist": bool(args.netlist),
        "limits_zh": list(_LIMITS),
        "notes_zh": [
            "判定与这里无关：综合产物回答的是「这么写合成出多少硬件」，"
            "不是「功能对不对」。功能对不对只有 sim.py 说了算。",
        ],
        "next_actions_zh": _next_actions(designs),
    }
    _emit(rep, args)
    return EXIT_OK if rep["ok"] else EXIT_FAIL


def _err_of(d: dict) -> dict:
    """这次设计失败的**主要原因**：sizer 挂了就是 sizer 的错，
    sizer 成了、网表挂了就是网表的错。"""
    return d.get("error") or d.get("netlist_error") or {}


def _next_actions(designs: list[dict]) -> list[str]:
    out: list[str] = []
    kinds = {_err_of(d).get("kind") for d in designs if not d.get("ok")}
    if "iverilog_crash" in kinds:
        out.append("iverilog 断言崩溃：先去补全那个组合 always 的分支"
                   "（它推断出了锁存器），再看 sim.py 里的 lint 块。")
    if "async_reset_unsupported" in kinds:
        out.append("异步复位合不了**是 iverilog blif 后端的限制，不是写法错** —— "
                   "资源估算的数字照常可信。要出网表就得改用同步复位"
                   "（`always @(posedge clk)` + 里层 `if (!rst_n)`），"
                   "或者装 yosys（它能吃异步复位）。别为了出网表把 RTL 改坏。")
    if "not_synthesizable" in kinds:
        out.append("有构造合不了：#延迟 / initial 赋值 / 系统任务在硬件里"
                   "没有对应物，要挪出可综合的模块。")
    if {"no_top", "compile_error"} & kinds:
        out.append("前端就没过：先用 python tools/sim.py --syntax-only 看具体报错。")
    if not out and len(designs) > 1:
        out.append("横向比「门」这一列：这就是同一份功能不同写法的代价差。")
    if not out:
        out.append("要出网表加 --netlist；要对比编码方式，把几份实现一起传进来。")
    return out


def _version(exe: Path) -> str | None:
    r = run_proc([str(exe), "-V"], timeout=10)
    m = re.search(r"Icarus Verilog version\s+(\S+)", r.combined)
    return m.group(1) if m else None


def _emit(rep: dict, args) -> None:
    if not args.quiet:
        _print_summary(rep)
    text = json.dumps(rep, ensure_ascii=False, indent=2 if args.pretty else None)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
