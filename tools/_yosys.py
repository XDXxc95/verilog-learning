"""yosys 真综合：把「让综合器自己挑状态编码」和「优化+映射后的面积」接进仓库。

由 `synth.py --engine yosys` 调用，不是独立命令。

和 `synth.py` 里 iverilog 那条腿的分工：

    iverilog -t sizer   **不做优化、不做工艺映射**，门数是未映射的原始宏单元。
                        只能横向比，而且对"同宽度下换编码"**完全不敏感**。
                        好处是零下载（L1/L2 不装任何东西也能用）
    _yosys.py（本模块） 真跑 opt / techmap / abc。能回答两个 sizer 答不了的问题：
                        ① 状态编码换成 one-hot 或 binary 之后硬件变多少
                        ② 优化+映射之后到底多少个门

## 先说清楚它**做不到**什么（这一段比用法重要）

1. **不能当真实面积/时序报告用。** 这里没有工艺库，`abc` 用的是内置的
   通用映射（映射到 `$_AND_` / `$_DFF_*` 这些内部门），不是某个 FPGA 或
   某个工艺的 LUT/标准单元。所以数字**仍然只能横向比**，只是比 sizer
   那个层级可信得多（做了优化和映射）。要真实面积得上 `abc -liberty`，
   那需要工艺库文件（本仓库没有，也不打算假装有）。
2. **`fsm_recode` 只认 `one-hot` 和 `binary`，没有 gray / johnson。**
   实测 `-encoding auto` / 其它值直接 `ERROR: FSM encoding 'auto' is not supported!`。
   课程里讲的三种编码，综合器能替你挑的只有两种 —— 格雷必须自己写。
   这条必须说清楚，否则学习者会以为灰度也能让工具自动选。
3. **它不判对错。** 综合产物回答的是「这么写合成出多少硬件」，
   功能对不对只有 sim.py 说了算。这条和 iverilog 那条腿一致。

## 四条实测硬事实（决定了这个模块的形状，改之前先看）

### ① 「退出码 0」不是跑过的证据，「有输出」也不是

实测两种情况都能给出 **rc=0**：

- **空文件**（一个模块都没有）：跑完整个流程、rc=0、日志里正常打印
  `10. Printing statistics.`，但**一个 `=== <模块> ===` 块都没有**。
- **只把 `bin` 放进 PATH 忘了 `lib`**：`yosys -V` 返回 rc=0、
  **零字节输出**（缺 DLL 时进程在 main 之前就没了）。

所以完成的硬判据取 **`tee -o <file> stat` 落下来的那个文件里有没有
`=== <模块> ===` 块** —— 和 `_vlint.py` 拿 `Verilation Report` 当
elaboration 的证据同一条道理。命令示例里那个 `10. Printing statistics.`
**不算证据**：空文件也有它。

### ② 仿真专用的 cell 会活到 `abc` 之后，而且 rc=0、数字看着很正常

实测 `tests/_smoke/synth_unsynth.v`（含 `$display` / `#2` / `initial`）：
跑完整条流水线 → **rc=0、96 个 cell**，其中含 `$print`。
而同一份文件在 iverilog 那条腿上判 `not_synthesizable`、整体 fail。

**直接把 96 报成「门数」就是假话。** 所以有 `non_hw_cells` 这一栏：
非硬件 cell 在场时 `ok=False`、`gate_count_meaningful=False`，
门数照旧打印出来（它是事实）但**明确标注不可当结论用**。

### ③ `#延迟` 被**静默丢弃**，日志里一句警告都没有

实测 `#2 q <= d;` 跑完不报任何 warning（整个日志里唯一的 Warning 是
`ABC: Warning: The network is combinational`）。而 `$monitor` 会被
`Warning: Ignoring call to system task $monitor.` 记一笔后丢掉 ——
**同一类问题，一个吭声一个不吭声**。所以本模块把 yosys 的 warning
原样收进 `warnings`，谁被丢了至少有一行字可查。

### ④ 「没有状态机」和「编码换了没效果」是两件事

实测 `dut_counter4.v`（纯计数器，没有状态机）：`fsm` 阶段
**没有** `Found FSM state register` 这一行，三种编码给出一模一样的
10 cells / 4 个触发器位。数字相同**不等于**"编码对面积没影响"，
只说明这次压根没比较到东西。`compare_encodings()` 因此分出
`comparable` 和 `identical` 两位 —— 见那里的说明。

★ 顺带纠正一条**测错了的**旧结论：探索阶段一度以为
`opt -nodffe -nosdff` 不能省（省了 `fsm_detect` 就认不出状态机、
`-encoding` 变成静默空操作）。**复测三份夹具后不成立**：
`proc; opt; fsm -encoding X` 与 `proc; opt -nodffe -nosdff; fsm -encoding X`
给出的 cells / 触发器位**逐项相同**（one-hot 13/4、binary 11/2）。
所以 `PIPELINE` 里用的是普通的 `opt`，没有 `-nodffe -nosdff`。
写在这里是因为**当初那个结论看着非常合理**，正是本仓库最防的一类 ——
一条没复现过的"经验"混进文档，比没有这条经验坏得多。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import _manifest
from _manifest import ManifestError
from _proc import run as run_proc

# 清单里的软件 id。和 `_vlint.py` 的 VERILATOR_ID 同一条理由：
# "怎么解析 yosys 的 stat" 天生就是 yosys 专属的。
YOSYS_ID = "yosys"

# 允许的编码。**没有 gray / johnson** —— 见模块头第 2 条。
#   auto      不给 -encoding，让 yosys 的 `fsm_recode` 按 `auto` 自己挑
#             （它内部会挑 binary 或 one-hot，日志里写 `mapping auto encoding to ...`）
#   one-hot   显式指定
#   binary    显式指定
ENCODINGS = ("auto", "one-hot", "binary")

# ★ 固定、显式的 pass 列表，写死在这里并原样进 JSON 报告。
#
# 为什么不用 yosys 自带的 `synth` 脚本：**不同 pass 顺序给出的数字不可比**。
# 报告里只有数字没有流程，读者没法判断两份报告能不能放在一起看。
# 把流程原样打印出来，这是本仓库对"可比性"能给出的最低保证。
#
#   proc            把 always 块变成电路
#   opt             简单优化（常量折叠、去重、DFF 优化）
#   fsm [-encoding] 抽出状态机并按指定编码重编码；**不给 = auto**
#   techmap         把 `$add` / `$mux` 这类行为级 cell 展开成基本门
#   abc             把逻辑映射成逐 bit 的门（这一步之后触发器才是一 bit 一个 cell）
#   tee -o F stat   统计；同时把这一段原样写进 F（F 就是"跑到了"的硬证据）
#
# `opt` 在 techmap 前后各来一次，和 yosys 自己的 synth 脚本一致。
PIPELINE_STAGES = (
    "read_verilog",
    "hierarchy -check",
    "proc",
    "opt",
    "fsm",
    "opt",
    "techmap",
    "opt",
    "abc",
    "opt",
    "tee -o <statfile> stat",
)

# `tee -o` 的目标固定成这个**相对文件名**，并且让 yosys 的 cwd 就是产物目录。
#
# 为什么不传绝对路径：yosys 的 `-p` 脚本有自己的一套分词，路径里带空格时
# 加了引号**照样不生效，而且 rc 还是 0**（实测）。干脆一个路径都不往脚本里塞。
STAT_FILE = "vlearn.yosys.stat"

# yosys 启动横幅。它出现 = 进程真的起来了。
# 这条专门用来抓"缺 DLL 起不来"：那种情况下 rc 可能是 0、输出是零字节，
# 除了看横幅没有任何办法把它和"跑了但没结果"分开。
_BANNER = "Yosys Open SYnthesis Suite"

_VERSION_RE = re.compile(r"^\s*Yosys\s+(\S+)", re.M)

# stat 里一个模块的分节头
_MODULE_RE = re.compile(r"^===\s*(\S+)\s*===\s*$")

# 数字行：`       10 cells` / `        1   $_AND_`
_COUNT_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")

# stat 里那些"是个数量"的标签。**必须逐字匹配**：
# `public wires` 和 `wires` 是两行，用 `in` 判会串。
_COUNT_LABELS = (
    "wires",
    "wire bits",
    "public wires",
    "public wire bits",
    "ports",
    "port bits",
    "memories",
    "memory bits",
    "processes",
    "cells",
)

# 时序 cell（状态寄存器/触发器的**位**）。
# 映射后是一 bit 一个 cell，所以这个和就是寄存器位数 ——
# 这正是编码对比该用的指标，而不是 `stat` 的 cell 总数
# （`$adff` 无论状态寄存器多宽都只算一个 cell，实测）。
_SEQ_RE = re.compile(r"^\$_[A-Z0-9_]*(?:DFF|LATCH)[A-Z0-9_]*_$")

# 非硬件 cell：**仿真概念，硬件里没有对应物**。
#
# 实测到的只有 `$print`（`$display` 会被编译成它）。其余是按 yosys 的
# cell 命名补的同类项 —— 这个表**宽一点只会更保守**（多标一个就少给一次
# 门数结论），不会造出乐观的谎报，所以宁宽勿窄。
_NON_HW_PREFIXES = (
    "$print", "$display", "$write", "$monitor",
    "$finish", "$stop", "$fatal", "$error", "$warning", "$info",
    "$assert", "$assume", "$cover", "$live", "$fair", "$check",
    "$anyseq", "$anyconst", "$allseq", "$allconst",
    "$dumpfile", "$dumpvars", "$fdisplay", "$fwrite", "$time",
    "$random", "$test$plusargs", "$value$plusargs",
)

# 日志里一行 warning：`<file>:<line>: Warning: <msg>` 或 `ABC: Warning: <msg>`
#
# ★ 位置段用 `.*?` 而**不是** `[^:]*?`。第一版写的是后者，实测一条都收不到：
# `<file>:<line>:` 里那个行号前面也有冒号，`[^:]*?` 匹配到文件路径就卡住，
# 而后面紧跟的是 `:4:` 不是 `: Warning:` —— 于是**所有带行号的告警全被静默丢掉**，
# 只剩 `ABC: Warning:` 那种不带行号的能收到。非贪婪的 `.*?` 才会退到
# 真正的那一个 `: Warning:` 上。
_WARN_RE = re.compile(r"^\s*(?P<where>.*?):\s*Warning:\s*(?P<msg>.+?)\s*$")
_ERROR_RE = re.compile(r"^\s*(?P<where>.*?):\s*ERROR:\s*(?P<msg>.+?)\s*$")
# 收尾那行 `Warnings: 1 unique messages, 1 total`
_WARN_TOTAL_RE = re.compile(r"^Warnings:\s*(\d+)\s+unique messages,\s*(\d+)\s+total")

# 已知的噪声告警：**照数不照印**。
#
# `ABC: The network is combinational` 在**任何没有组合逻辑要映射**的设计上都会出现
# （实时测：fsm_bin 这种纯时序设计每次都有），和"你的代码有问题"毫无关系。
# 前例见 `_vlint.py` 里为什么不用 `-Wall`：噪声的代价不是多几行字，
# 而是学习者开始整体忽略这个工具 —— 那时真正要命的那条也被一起忽略了。
#
# ★ 只是**不印**，不是**不算**：`n_warnings_total` 仍然是 yosys 自己报的那个数，
# 两者分开，和 `_MAX_ERRORS` / `n_errors_total` 是同一条规矩。
_WARN_NOISE = (
    "The network is combinational",
)

# FSM 被认出来的证据（实测原话）。没有它，三种编码的数字相同**不是结论**。
_FSM_FOUND_RE = re.compile(r"^Found FSM state register\s+(\S+)\.?\s*$", re.M)
# `fsm_recode` 实际用了什么编码：`  mapping auto encoding to `binary` for this FSM.`
_RECODE_RE = re.compile(r"mapping \S+ encoding to `(\S+?)` for this FSM")

_MAX_WARNINGS = 20
_MAX_ERRORS = 10


@dataclass
class YosysModule:
    """stat 里**一个模块**的计数。"""

    name: str
    counts: dict[str, int] = field(default_factory=dict)
    cell_counts: dict[str, int] = field(default_factory=dict)
    unparsed: list[str] = field(default_factory=list)

    def n(self, label: str) -> int | None:
        return self.counts.get(label)

    @property
    def n_cells(self) -> int | None:
        return self.counts.get("cells")

    @property
    def seq_cells(self) -> dict[str, int]:
        return {k: v for k, v in self.cell_counts.items() if _SEQ_RE.match(k)}

    @property
    def n_state_bits(self) -> int:
        return sum(self.seq_cells.values())

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "counts": dict(self.counts),
            "cells": dict(self.cell_counts),
            "n_state_bits": self.n_state_bits,
            "unparsed": list(self.unparsed),
        }


def _is_non_hw(cell: str) -> bool:
    return any(cell.startswith(p) for p in _NON_HW_PREFIXES)


def _is_mapped_gate(cell: str) -> bool:
    """`$_XXX_` 是 techmap/abc 之后的内部门级 cell —— 只有它们算"门"。"""
    return cell.startswith("$_") and cell.endswith("_")


@dataclass
class YosysResult:
    """一次 yosys 综合的结果。

    字段的含义边界（别混，混了就会谎报）：

        started        yosys 的横幅出现过 —— 进程**真的起来了**
        reached_stat   流水线跑到底了（stat 文件在，这是硬证据）
        ran            reached_stat 且解析出了至少一个 `=== 模块 ===` 块
        ok             ran 且拿到的是**硬件**（无非硬件 cell、无未映射 cell、没超时）
        gate_count_meaningful
                       n_cells 能不能当"门数"说出口。非硬件 cell 或未映射
                       cell 在场时它是 False —— 数字照旧打印（它是事实），
                       但**不许拿它下结论**
    """

    started: bool = False
    reached_stat: bool = False
    ran: bool = False
    ok: bool = False
    exe: str | None = None
    version: str | None = None
    command: str | None = None
    pipeline: list[str] = field(default_factory=list)
    encoding: str = "auto"
    # yosys 实际用的编码（从 recode 日志里读的）。`auto` 时这才是有信息量的那个。
    encoding_used: str | None = None
    fsm_detected: bool = False
    modules: list[YosysModule] = field(default_factory=list)
    # 非硬件 cell 名 → 个数。**非空就不许报门数结论**（模块头第 ② 条）。
    non_hw_cells: dict[str, int] = field(default_factory=dict)
    # `$` 开头但不是 `$_XXX_`：techmap/abc 没映射掉的行为级 cell
    # （`$mem` 之类）。它们在，说明拿到的不是门级网表。
    unmapped_cells: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    n_warnings_total: int = 0
    # 被当成噪声**没印出来**的那部分（见 _WARN_NOISE）。
    # 必须单独记：`warnings` 的长度和 `n_warnings_total` 对不上时要解释得清，
    # 否则读者会以为"报了几条"和"其实有几条"是同一件事。
    n_warnings_noise: int = 0
    errors: list[str] = field(default_factory=list)
    n_errors_total: int = 0
    skipped_reason_zh: str | None = None
    notes_zh: list[str] = field(default_factory=list)
    timed_out: bool = False
    duration_ms: int = 0
    stat_raw: str = ""
    tail: str = ""

    @property
    def gate_count_meaningful(self) -> bool:
        return (not self.non_hw_cells) and (not self.unmapped_cells)

    @property
    def top_module(self) -> YosysModule | None:
        return self.modules[0] if self.modules else None

    @property
    def n_cells(self) -> int | None:
        m = self.top_module
        return m.n_cells if m else None

    @property
    def n_state_bits(self) -> int | None:
        m = self.top_module
        return m.n_state_bits if m else None

    def as_dict(self) -> dict:
        return {
            "ran": self.ran,
            "ok": self.ok,
            "started": self.started,
            "reached_stat": self.reached_stat,
            "tool": YOSYS_ID,
            "exe": self.exe,
            "version": self.version,
            "command": self.command,
            "pipeline": list(self.pipeline),
            "encoding": self.encoding,
            "encoding_used": self.encoding_used,
            "fsm_detected": self.fsm_detected,
            "n_modules": len(self.modules),
            "modules": [m.as_dict() for m in self.modules],
            # ★ 这三个是分开的三位，不能挤成一个：
            #   非硬件 cell 在场 → 硬件这一半不成立
            #   未映射 cell 在场 → "门数"这个说法不成立
            #   两者都空        →  才可以给门数结论
            "gate_count_meaningful": self.gate_count_meaningful,
            "non_hw_cells": dict(self.non_hw_cells),
            "unmapped_cells": dict(self.unmapped_cells),
            "warnings": list(self.warnings),
            "n_warnings_total": self.n_warnings_total,
            "n_warnings_shown": len(self.warnings),
            "n_warnings_noise": self.n_warnings_noise,
            "errors": list(self.errors),
            "n_errors_total": self.n_errors_total,
            "skipped_reason_zh": self.skipped_reason_zh,
            "notes_zh": list(self.notes_zh),
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "tail": self.tail,
        }


# ---------------------------------------------------------------- 解析


def parse_stat(blob: str) -> tuple[list[YosysModule], list[str]]:
    """把 `stat` 的输出切成一份份模块计数。

    设计原则与 `sim.py` / `_vlint.py` 一致：**尽力而为，绝不抛异常**。
    yosys 换了措辞就整条链路崩掉，比解析不出来坏得多。

    未识别的行收集进每个模块自己的 `unparsed`；分节之外的行进调用方
    拿得到的第二个返回值。**静默丢行和静默丢结构是一类错。**
    """
    modules: list[YosysModule] = []
    outside: list[str] = []
    cur: YosysModule | None = None

    for raw in blob.splitlines():
        line = raw.rstrip()
        m = _MODULE_RE.match(line)
        if m:
            cur = YosysModule(name=m.group(1))
            modules.append(cur)
            continue
        if not line.strip():
            continue
        # 统计段的那两行装饰和表头
        if line.strip().startswith("+---") or line.strip().startswith("|"):
            continue
        if cur is None:
            # `10. Printing statistics.` 这类流程行，不属于任何模块
            if line.strip() and not line.strip().startswith("End of script"):
                outside.append(line)
            continue

        cm = _COUNT_RE.match(line)
        if cm and cm.group(2) in _COUNT_LABELS:
            cur.counts[cm.group(2)] = int(cm.group(1))
            continue
        if cm and cm.group(2).startswith("$"):
            cur.cell_counts[cm.group(2)] = int(cm.group(1))
            continue
        cur.unparsed.append(line)

    return modules, outside


def parse_log(blob: str) -> dict:
    """从日志里取 banner / 版本 / warning / error / FSM 证据。

    全都是"尽力而为"：取不到就是 None / 空，绝不抛。
    """
    started = _BANNER in blob
    vm = _VERSION_RE.search(blob)

    warnings: list[str] = []
    errors: list[str] = []
    n_warn = 0
    n_noise = 0
    n_err = 0
    for line in blob.splitlines():
        wm = _WARN_RE.match(line)
        if wm:
            n_warn += 1
            msg = f"{wm.group('where')}: {wm.group('msg')}"
            if any(nz in msg for nz in _WARN_NOISE):
                n_noise += 1
            elif len(warnings) < _MAX_WARNINGS:
                warnings.append(msg)
            continue
        em = _ERROR_RE.match(line)
        if em:
            n_err += 1
            if len(errors) < _MAX_ERRORS:
                errors.append(f"{em.group('where')}: {em.group('msg')}")
            continue
        tm = _WARN_TOTAL_RE.match(line)
        if tm:
            # yosys 自己报的总数比我们数出来的准（它有去重/抑制），以它为准
            n_warn = max(n_warn, int(tm.group(2)))

    fm = _FSM_FOUND_RE.search(blob)
    rm = _RECODE_RE.search(blob)
    return {
        "started": started,
        "version": vm.group(1) if vm else None,
        "warnings": warnings,
        "errors": errors,
        "n_warnings_total": n_warn,
        "n_warnings_noise": n_noise,
        "n_errors_total": n_err,
        "fsm_detected": fm is not None,
        "fsm_register": fm.group(1) if fm else None,
        "encoding_used": rm.group(1) if rm else None,
    }


# ---------------------------------------------------------------- 执行


def _yosys_software():
    """从清单里取 yosys 条目。清单坏了要**大声说**，不能静默跳过。"""
    m = _manifest.load()
    sw = m.software_by_id(YOSYS_ID)
    if sw is None:
        raise ManifestError(f"清单里没有 {YOSYS_ID} 条目")
    return sw


def locate(exe_hint: Path | None = None) -> Path | None:
    """找到 yosys 可执行文件，**不做功能自检**（同 `_manifest.locate` 的理由）。

    注意 `_manifest.locate()` 用 `Software.check_exe`，清单里 yosys 写的是
    `exe: "yosys"`、`lib_dirs: ["lib"]` —— 后者由 `tool_env()` 并进 PATH，
    那正是"只把 bin 放 PATH 会 rc=0 零输出"（模块头第 ① 条）的解法。
    """
    try:
        sw = _yosys_software()
    except ManifestError:
        return None
    return _manifest.locate(sw, extra=exe_hint)


def build_script(
    files: list[Path],
    top: str | None,
    encoding: str,
    sv: bool = False,
    defines: list[str] | None = None,
    includes: list[str] | None = None,
    stat_file: str = STAT_FILE,
) -> str:
    """拼出那串固定的 pass 列表。**原样进报告**，见 PIPELINE_STAGES 的说明。"""
    args = ["read_verilog"]
    if sv:
        args.append("-sv")
    for d in (defines or []):
        args += ["-D", d]
    for inc in (includes or []):
        args += ["-I", inc]

    # 两件事一起做，缺一个都不行：
    #
    # ① **转成绝对路径。** yosys 的 cwd 是产物目录（为了那个相对的 stat 文件名，
    #    见 STAT_FILE），用调用方给的相对路径它一个文件都找不到。
    #    实测：传 `tests/_smoke/fsm_bin.v` 时三次全跑出
    #    「没跑到 stat」，而日志里只是平平常常一句找不到文件。
    # ② **转成 posix 风格。** `.resolve()` 在 Windows 上给的是 `D:\a\b.v`，
    #    反斜杠在 yosys 的脚本分词里是转义字符，会把路径吃成一个畸形 token。
    for f in files:
        args.append(Path(f).resolve().as_posix())

    stages = [" ".join(args)]
    hier = "hierarchy -check" + (f" -top {top}" if top else " -auto-top")
    stages.append(hier)
    stages.append("proc")
    stages.append("opt")
    # 不给 -encoding 就是让 yosys 自己挑（auto）。显式给才加这一个开关。
    stages.append("fsm" if encoding == "auto" else f"fsm -encoding {encoding}")
    stages.append("opt")
    stages.append("techmap")
    stages.append("opt")
    stages.append("abc")
    stages.append("opt")
    stages.append(f"tee -o {stat_file} stat")
    return "; ".join(stages)


def run(
    files: list[Path],
    top: str | None = None,
    encoding: str = "auto",
    sv: bool = False,
    defines: list[str] | None = None,
    includes: list[str] | None = None,
    workdir: Path | None = None,
    timeout: float = 60.0,
    exe_hint: Path | None = None,
    cwd: str | None = None,
) -> YosysResult:
    """对一批设计文件跑一次固定流水线的综合。

    `workdir` 同时是 yosys 的 cwd 和 stat 文件的落点（见 STAT_FILE 的说明）。
    """
    if encoding not in ENCODINGS:
        return YosysResult(
            encoding=encoding,
            skipped_reason_zh=(
                f"不支持的编码 {encoding!r}。可用：{', '.join(ENCODINGS)}。"
                "**没有 gray / johnson** —— yosys 的 fsm_recode 不支持，"
                "格雷编码必须自己在 RTL 里写。"
            ),
        )
    if not files:
        return YosysResult(
            encoding=encoding,
            skipped_reason_zh="没有设计文件可综合",
        )

    try:
        sw = _yosys_software()
    except ManifestError as exc:
        return YosysResult(encoding=encoding, skipped_reason_zh=str(exc))

    exe = _manifest.locate(sw, extra=exe_hint)
    if exe is None:
        where = f"（已在 {exe_hint} 里找过）" if exe_hint else ""
        return YosysResult(
            encoding=encoding,
            skipped_reason_zh=(
                f"没找到 yosys{where}。装法："
                f"python tools/setup_tools.py --install {YOSYS_ID} --lock"
            ),
        )

    import _manifest as _m  # 就近取 tool_env，避免顶部再引一次名字

    out_dir = Path(workdir) if workdir else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    stat_path = out_dir / STAT_FILE
    # 每次都先删掉上一次的 stat。**失败时旧文件留在原地就会被读成新结果**，
    # 这正是 synth.py 文件头第 3 条坑的同一个形状（那条是 iverilog 的 `-o`）。
    if stat_path.exists():
        stat_path.unlink()

    script = build_script(files, top, encoding, sv, defines, includes)

    cmd = [str(exe), "-p", script]
    env = {**os.environ, **_m.tool_env(sw)}
    cwd_s = cwd if cwd is not None else str(out_dir)

    try:
        res = run_proc(cmd, cwd=cwd_s, timeout=timeout, env=env)
    except Exception as exc:  # noqa: BLE001 —— 调用方还会再包一层
        return YosysResult(
            encoding=encoding,
            exe=str(exe),
            command=script,
            skipped_reason_zh=f"yosys 起不来：{type(exc).__name__}: {exc}",
        )

    blob = res.stdout + "\n" + res.stderr
    info = parse_log(blob)

    out = YosysResult(
        started=info["started"],
        exe=str(exe),
        version=info["version"],
        command=script,
        pipeline=list(PIPELINE_STAGES),
        encoding=encoding,
        encoding_used=info["encoding_used"],
        fsm_detected=info["fsm_detected"],
        warnings=info["warnings"],
        n_warnings_total=info["n_warnings_total"],
        n_warnings_noise=info["n_warnings_noise"],
        errors=info["errors"],
        n_errors_total=info["n_errors_total"],
        timed_out=res.timed_out,
        duration_ms=res.duration_ms,
        tail=blob[-2000:],
    )

    if res.timed_out:
        out.skipped_reason_zh = (
            f"yosys 超时（{timeout:g}s）被终止，这一轮没有结果。"
            "展开量太大（大 generate、大位宽乘法、深组合逻辑）时会这样。"
        )
        return out

    if not stat_path.exists():
        # 硬证据不在 = 没跑到 stat。这时**绝不能**看 rc，更不能看有没有输出：
        # 缺 DLL 时 rc=0 且零字节（模块头第 ① 条）。
        if not out.started:
            first = next((l for l in blob.splitlines() if l.strip()), "")
            out.skipped_reason_zh = (
                "yosys 连启动横幅都没有，说明它没跑起来"
                "（多半是缺运行时 DLL：只把 bin 放进 PATH、lib 没放）。"
                + (f" 首行输出：{first[:200]}" if first else " 它一个字节都没输出——"
                   "「退出码 0」在这种情况下照样成立，所以这里不看退出码。")
            )
        else:
            first_err = out.errors[0] if out.errors else ""
            out.skipped_reason_zh = (
                "yosys 起来了，但没跑到 stat（见下面的 errors / tail）。"
                + (f" 第一条：{first_err}" if first_err else "")
            )
        return out

    out.reached_stat = True
    raw = stat_path.read_text(encoding="utf-8", errors="replace")
    out.stat_raw = raw
    modules, _outside = parse_stat(raw)
    out.modules = modules

    if not modules:
        # 跑到 stat 了、但没有一个模块 —— 空文件/只有宏定义的文件就是这样。
        # **这不是崩溃**，是"没什么可综合的"，要说清楚（和 iverilog 的 no_top 同类）。
        out.skipped_reason_zh = (
            "yosys 跑完了整条流水线，但设计里**一个模块都没有**，没有东西可综合。"
            "检查文件是不是空的、路径是不是写错了、或者模块在别的文件里没一起传进来。"
            "★ 注意这种情况 yosys 的退出码是 0、日志里也正常打印 "
            "`Printing statistics.` —— 所以「退出码 0」和「有没有输出」都不能当跑过的证据。"
        )
        return out

    out.ran = True

    # 汇总所有模块的 cell。通常只有一个（给了 -top），多顶层时全算上并说明。
    non_hw: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    for m in modules:
        for cell, cnt in m.cell_counts.items():
            if _is_non_hw(cell):
                non_hw[cell] = non_hw.get(cell, 0) + cnt
            elif not _is_mapped_gate(cell):
                unmapped[cell] = unmapped.get(cell, 0) + cnt
    out.non_hw_cells = non_hw
    out.unmapped_cells = unmapped

    if len(modules) > 1:
        out.notes_zh.append(
            f"这次综合里有 {len(modules)} 个模块（"
            + "、".join(m.name for m in modules)
            + "），门数是**逐个模块分别给的，没有合并** —— "
            "把几个不相干的层次加在一起得到的和没有意义。"
        )

    if non_hw:
        out.notes_zh.append(
            "★ 这次的设计里出现了**仿真专用**的东西（"
            + "、".join(f"{k}×{v}" for k, v in sorted(non_hw.items()))
            + "）。它们是硬件里没有对应物的系统任务，"
            "但 yosys **不会因此拒绝**：它照常跑完、退出码还是 0。"
            "所以这次拿到的是**一堆不是硬件的东西的个数**，"
            "**不能当门数**。要综合，先把 $display / $finish 这类挪出可综合的模块。"
        )
    if unmapped:
        out.notes_zh.append(
            "★ 有 " + "、".join(f"{k}×{v}" for k, v in sorted(unmapped.items()))
            + " 没能映射成门级 cell（techmap/abc 之后仍然停在行为级）。"
            "这些确实是硬件，但**没被映射**，所以 `cells` 里这一部分不是门数。"
        )

    out.ok = out.gate_count_meaningful
    return out


# ---------------------------------------------------------------- 编码对比


def compare_encodings(
    runs: dict[str, YosysResult],
    order: tuple[str, ...] = ("auto", "one-hot", "binary"),
) -> dict:
    """把几次不同编码的结果收成一张对比表，**并明确回答"这次比得了吗"**。

    ★ 三个字段必须分开，挤成一个就会写出假话：

        comparable   至少有一次运行真的**认出了状态机**（日志里有
                     `Found FSM state register`）。为 False 时下面那些数字
                     之间的相同**不是结论** —— 只说明没有东西可比。
        identical    认出来了，但三种编码给出的 (寄存器位数, cell 数) 完全一样。
                     这是一条**真结论**（本设计上编码对面积没影响），
                     和 comparable=False 长得像、意思相反。
        all_ok       三次是不是都拿到了可用的门数。有一次被非硬件 cell
                     挡下，就不能整表当结论用。

    没有单独的 `comparable` 位时，"没有状态机"和"编码没影响"在下游
    都表现为"三行数字一样"，而这两件事给学习者的下一步完全相反。
    """
    rows: list[dict] = []
    for enc in order:
        r = runs.get(enc)
        if r is None:
            continue
        top = r.top_module
        rows.append({
            "encoding": enc,
            "ran": r.ran,
            "ok": r.ok,
            "fsm_detected": r.fsm_detected,
            # yosys 在 auto 下实际挑了哪个，日志里有；显式指定时就是指定的那个
            "encoding_used": r.encoding_used,
            "n_cells": r.n_cells,
            "n_state_bits": r.n_state_bits,
            "gate_count_meaningful": r.gate_count_meaningful,
            "non_hw_cells": dict(r.non_hw_cells),
            "unmapped_cells": dict(r.unmapped_cells),
            "skipped_reason_zh": r.skipped_reason_zh,
            "cells": dict(top.cell_counts) if top else {},
        })

    usable = [r for r in rows if r["ran"] and r["gate_count_meaningful"]]
    comparable = any(r["fsm_detected"] for r in rows)
    sigs = {(r["n_state_bits"], r["n_cells"]) for r in usable}
    identical = bool(usable) and len(sigs) == 1

    notes: list[str] = []
    if not rows:
        notes.append("一次都没跑成。")
    elif not comparable:
        notes.append(
            "★ **本次没能比较。** 三种编码都给不出差别，原因不是"
            "「编码对面积没影响」，而是 yosys 在这些设计里**没有认出状态机**"
            "（日志里没有 `Found FSM state register`）。"
            "最可能的情况：这份设计里根本没有状态机（纯计数器、纯组合逻辑）。"
            "没有状态机，`-encoding` 就没有作用对象 —— 上面那些数字是"
            "**同一份电路被量了三遍**，不是三个方案。"
        )
    elif identical:
        notes.append(
            "这次三种编码给出的寄存器位数和 cell 数**相同**。"
            "状态机是认出来了，所以这是一条真结论：**在这份设计上**"
            "编码方式不影响（这个层级量得到的）面积。"
            "不同状态机、不同次态逻辑会有不同结果，别推广成通则。"
        )
    if len(usable) < len(rows):
        bad = [r["encoding"] for r in rows if not (r["ran"] and r["gate_count_meaningful"])]
        notes.append(
            f"★ {'、'.join(bad)} 这几次没拿到可用的门数"
            "（要么没跑成，要么出现了非硬件 cell / 未映射 cell）。"
            "**整张表都不能当结论用** —— 缺格子的对比表比没有表更误导人。"
        )

    return {
        "comparable": comparable,
        "identical": identical,
        "all_ok": bool(rows) and len(usable) == len(rows),
        "rows": rows,
        "notes_zh": notes,
    }
