"""编译 + 仿真 + 判定内核。

    python tools/sim.py <design.v> [<design.v> ...] --tb <tb.v> --top <name> [选项]

职责：把"学习者写的 Verilog 到底对不对"变成**确定性的、机器可读的结论**，
供 Claude 判定与自适应教学使用。

输出约定：
  - stdout 只输出**一个 JSON 对象**（--pretty 控制缩进），便于程序消费
  - 人类可读的摘要走 stderr，不污染 JSON
  - 退出码是便捷映射，**JSON 才是权威**

退出码：
  0 = 判定通过        1 = 判定失败/无法判定    2 = 编译失败
  3 = 运行失败或超时  4 = 环境错误（工具链缺失） 64 = 用法错误

JSON 里的几个块，各自的定位（给消费 JSON 的 agent 看，字段表见 AGENT.md）：

  status / verdict / tests / error_tags   ← **判定**。确定性的，权威的
  compile.diagnostics                      ← 结构化报错，带中文提示与置信度
  lint                                     ← 外部 verilator 真跑了没有、报了几条
  observe                                  ← **观察**。从波形里量出来的事实
  next_actions_zh                          ← 现成的白话建议

**判定与观察是两回事，别混。** status 说"对不对"，observe 说"波形长什么样"。
observe 缺失或 ok=false 只是少了一层辅助信息，**不影响 status 的有效性**；
反过来也成立——波形看着漂亮不等于判定通过。

**lint 同理，而且是更严格的一条：它一条诊断都不许改 status。** 判定内核是
iverilog + 测试台标记协议；verilator 的接受范围和它并不一样，让 lint 有机会
把 pass 翻成 fail，学习者就会遇到"跑通了却说不过"而找不到原因。
所以 verilator 的话全部以 warning 形式挂在 compile.diagnostics 上（`source`
字段标明来路），`lint.ran` / `lint.ok` 单独报告它到底跑没跑——
"没跑"和"跑了没报"长得一样但含义相反，不能混。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _console import init_console  # noqa: E402
from _lint import scan_files as lint_scan  # noqa: E402
from _observe import observe  # noqa: E402
from _proc import run as run_proc  # noqa: E402
from _toolchain import detect, install_guide, repo_root  # noqa: E402
from _vlint import run as vlint_run  # noqa: E402

SCHEMA = "vlearn.sim/1"

# 退出码
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_COMPILE_ERROR = 2
EXIT_RUNTIME_ERROR = 3
EXIT_ENV_ERROR = 4
EXIT_USAGE = 64

DEFAULT_TIMEOUT = 30.0


# ============================================================ iverilog 报错解析

class DiagKind(StrEnum):
    BAD_TOP = "bad_top"
    UNKNOWN_MODULE = "unknown_module"
    UNBOUND = "unbound"
    UNDECLARED = "undeclared"
    FILE_NOT_FOUND = "file_not_found"
    REDEFINITION = "redefinition"
    LATCH_INFERENCE = "latch_inference"
    INFINITE_LOOP = "infinite_loop"
    PORT_MISMATCH = "port_mismatch"
    PARAM_NOT_FOUND = "param_not_found"
    MULTIPLE_DRIVER = "multiple_driver"
    WIDTH_MISMATCH = "width_mismatch"
    UNUSED = "unused"
    SYNTAX = "syntax"
    # 下面两个目前只有 verilator 报得出来（iverilog 对它们完全沉默）
    COMBINATIONAL_LOOP = "combinational_loop"
    CASE_INCOMPLETE = "case_incomplete"
    OTHER = "other"


@dataclass
class Hint:
    what: str
    why: str
    fixes: list[str]
    confidence: str  # high / medium / low —— 必须诚实，不假装确定


# 分类规则。**顺序敏感**：具体规则必须排在泛化规则之前，
# 否则 "Unknown module type: foo" 会被当成普通 error 处理掉。
_RULES: list[tuple[re.Pattern, DiagKind]] = [
    (re.compile(r"top level module not found", re.I), DiagKind.BAD_TOP),
    (re.compile(r"Unknown module type:\s*(\w+)", re.I), DiagKind.UNKNOWN_MODULE),
    # -P 指定的参数在**根模块**上不存在。iverilog 原文：
    #     :0: error: parameter `MAX` not found in `tb_dut`.
    # 注意它没有文件定位（写成 :0:），所以得先去掉这个前缀才解析得到。
    (re.compile(r"parameter\s+\S+\s+not found in", re.I), DiagKind.PARAM_NOT_FOUND),
    (re.compile(r"Unable to bind (wire|reg|memory)", re.I), DiagKind.UNBOUND),
    (re.compile(r"implicit definition of wire", re.I), DiagKind.UNDECLARED),
    (re.compile(r"Unable to open file|No such file or directory", re.I), DiagKind.FILE_NOT_FOUND),
    # iverilog 实际措辞是 "has already been declared"，中间夹了 been，
    # 因此不能写成 "already declared"
    (re.compile(r"already\s+\w*\s*declared|redefinition|Duplicate declaration", re.I),
     DiagKind.REDEFINITION),
    (re.compile(r"inferred latch|incompletely assigned|Latch inferred", re.I),
     DiagKind.LATCH_INFERENCE),
    # iverilog 会**静态**拒绝 always 块里没有延时/事件控制的形状，
    # 措辞是两句：先 "does not have any delay."，再 "A runtime infinite loop will occur."
    (re.compile(r"does not have any delay|runtime infinite loop", re.I),
     DiagKind.INFINITE_LOOP),
    (re.compile(r"multiple (driver|continuous)|already has a driver", re.I),
     DiagKind.MULTIPLE_DRIVER),
    (re.compile(r"port (count |width )?mismatch|too many|too few|Wrong number of", re.I),
     DiagKind.PORT_MISMATCH),
    (re.compile(r"truncat|signed/unsigned|width \d+", re.I), DiagKind.WIDTH_MISMATCH),
    (re.compile(r"assigned but not used|declared but not used|never used", re.I), DiagKind.UNUSED),
    (re.compile(r"syntax error|unexpected|expecting|Invalid|malformed", re.I), DiagKind.SYNTAX),
]


HINTS: dict[DiagKind, Hint] = {
    DiagKind.BAD_TOP: Hint(
        "指定的顶层模块在源码里不存在。",
        "命令行的 --top 参数、或测试台里例化的模块名，与实际定义的 module 名对不上（常是拼写或大小写问题）。",
        ["核对 module 名与 --top 是否逐字符一致（Verilog 区分大小写）",
         "确认该模块所在的 .v 文件已作为输入传入"],
        "high",
    ),
    DiagKind.UNKNOWN_MODULE: Hint(
        "例化了一个 iverilog 找不到的模块。",
        "该模块名没有在任何输入文件中定义，或者定义它的文件没被一起编译。",
        ["检查被例化的模块名拼写（常见：conter / counter）",
         "把定义该模块的 .v 文件一并加入编译"],
        "high",
    ),
    DiagKind.UNBOUND: Hint(
        "测试台引用的信号在待测模块里找不到。",
        "层次化引用（如 tb.dut.q）中的名字与实际端口/信号不符——改名了、拼错了，或该信号并非端口。",
        ["核对待测模块的端口名与测试台引用的名字",
         "若是内部信号，确认它在模块内确实存在且未被优化掉"],
        "high",
    ),
    DiagKind.UNDECLARED: Hint(
        "使用了未声明的标识符，iverilog 按隐式 wire 处理。",
        "这是很危险的：隐式 wire 默认 1 位，会让本该多位的信号被静默截断，波形对不上但语法不报错。",
        ["为该信号补上显式声明（wire / reg，并写清位宽）",
         "若是笔误，修正拼写"],
        "medium",
    ),
    DiagKind.FILE_NOT_FOUND: Hint(
        "有文件打不开。",
        "路径写错、文件不存在，或 `include 的相对路径没找对。",
        ["核对文件路径", "`include 的查找目录用 -I / --include 指定"],
        "high",
    ),
    DiagKind.REDEFINITION: Hint(
        "同一个名字被重复声明或定义。",
        "常见于把变量既写在端口列表又单独声明，或同名 module 被定义了两次。",
        ["删掉重复的那一处声明", "ANSI 风格端口列表里不要再单独 input/output 声明"],
        "high",
    ),
    DiagKind.LATCH_INFERENCE: Hint(
        "综合器推断出了锁存器（latch）。",
        "组合逻辑的 always 块里，某个输出信号没有在所有分支上都被赋值——没写到的分支会「保持原值」，"
        "硬件上就体现为一个锁存器。这几乎总是设计错误，而不是特性。",
        ["给 if 补上 else 分支", "给 case 补上 default 分支",
         "或在 always 块开头给所有输出一个默认赋值"],
        "high",
    ),
    DiagKind.INFINITE_LOOP: Hint(
        "always 块里没有延时或事件控制，会在同一时刻无限循环下去。",
        "最常见的是写了 `always #0 clk = ~clk;` —— **#0 是零延迟，不是「很小的延迟」**。"
        "它不推进仿真时间，这个 always 会在同一时刻反复触发，仿真永远走不出第一步。"
        "（想要时钟请写 `always #5 clk = ~clk;` 这种带真实延时的形式。）"
        "iverilog 在编译期就能发现这种形状，所以你不会看到它挂起。",
        ["把 #0 换成真实的半周期延时，例如 #5",
         "组合逻辑用 always @(*) 并给敏感列表，不要靠延时",
         "时序逻辑用 always @(posedge clk)，不要用延时产生时钟"],
        "high",
    ),
    DiagKind.MULTIPLE_DRIVER: Hint(
        "同一个信号被多个来源驱动。",
        "两处 assign 写了同一个 wire，或既用 assign 驱动又在 always 里赋值。",
        ["找出所有驱动该信号的地方，只保留一处",
         "注意 wire 应由 assign 驱动、reg 应由 always 驱动"],
        "high",
    ),
    DiagKind.PORT_MISMATCH: Hint(
        "端口连接的数量或位宽不匹配。",
        "例化时给的信号个数与模块定义的端口数不一致，或位宽对不上。",
        ["把实例的端口列表与模块定义逐个对齐", "建议改用具名连接 .port(sig) 避免顺序错"],
        "medium",
    ),
    DiagKind.PARAM_NOT_FOUND: Hint(
        "命令行给的参数名，在顶层模块上找不到对应的 parameter。",
        "iverilog 的 -P 只认**根模块**上声明的参数。本仓库跑仿真时根模块是测试台，"
        "所以参数必须声明在测试台上，再由测试台透传给待测模块——直接写待测模块"
        "的名字是不生效的（实测：模块名写错时 iverilog **一声不响**，退出码 0，"
        "仿真照跑，只是参数保持默认值。这是最坏的一种失败：不报错、结果不对）。",
        ["在测试台模块上声明参数：`module tb_xxx #(parameter W = 8);`",
         "把它接到待测模块的实例上：`dut #(.W(W)) u (...);`",
         "参数名逐个字符核对（Verilog 区分大小写）"],
        "high",
    ),
    DiagKind.WIDTH_MISMATCH: Hint(
        "位宽不一致，可能发生截断或符号扩展。",
        "赋值两边的位宽不同。Verilog 不会报错，只会静默截断高位或扩展——波形看起来「莫名不对」的常见根因。",
        ["显式写明位宽，让两边一致",
         "需要截断时用位选明确表达意图，例如 data[3:0]"],
        "medium",
    ),
    DiagKind.UNUSED: Hint(
        "有信号声明了却没用上。",
        "通常无害，但可能意味着你本来想连的线忘了连。",
        ["确认是否漏接", "确实不用就删掉声明"],
        "low",
    ),
    DiagKind.SYNTAX: Hint(
        "语法错误：iverilog 无法把这段文本解析成合法语句。",
        "最常见的是**上一行**漏了分号、begin/end 不配对、关键字拼错（alway、endmodul），"
        "或括号/方括号不匹配。注意：报错行常常是「下一条语句」，真正的问题多在它前一行。",
        ["检查报错行的**上一行**末尾是否漏了分号",
         "检查 begin/end、case/endcase 是否成对",
         "检查 posedge/negedge、assign、always 等关键字拼写",
         "若用了 SystemVerilog 语法，试试 --gflag -g2012"],
        "medium",
    ),
    DiagKind.COMBINATIONAL_LOOP: Hint(
        "组合逻辑绕回了自己，形成了一个环。",
        "某个信号经过几级组合逻辑之后，又参与了它自己的计算。仿真器只能靠"
        "内部迭代顺序求解，**结果可能依赖仿真器实现**——换一个工具可能就不一样；"
        "综合成硬件后更糟：这条路径没有确定的传播延迟，实际电路会振荡或产生毛刺，"
        "静态时序分析也给不出有意义的结论。这是必须打断的一类结构。",
        ["把环路里的某个信号改成寄存器（`always @(posedge clk)`），用一级时序打断组合环",
         "若本意是「保持上一次的值」，那要用寄存器明确写出来，不能靠组合反馈实现",
         "检查是不是把输出信号误写进了它自己的表达式，例如 assign y = a & y"],
        "high",
    ),
    DiagKind.CASE_INCOMPLETE: Hint(
        "case 分支没有覆盖选择信号的全部取值。",
        "综合时没写到的取值会保持原值（或落到 default）。在组合 always 里，"
        "「保持原值」的硬件形态就是锁存器——所以这条常常和 latch_inference 是同一个根因。",
        ["给这个 case 补上 default 分支（最省事，也最不容易漏）",
         "或把剩余的取值逐个列出，让分支真正穷举",
         "或在 always 块开头给输出一个默认赋值，让「没覆盖到」也有确定的值"],
        "high",
    ),
    DiagKind.OTHER: Hint(
        "iverilog 报了一个未归类的错误。",
        "这条信息未匹配到已知模式，请直接阅读原始报错。",
        ["阅读下面的原始输出定位问题"],
        "low",
    ),
}


# 形如  <路径>:<行>:<列>: error: 消息      （Windows 路径含冒号，file 组需贪婪到最后一个 :数字: 之前）
_DIAG_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?:[ \t]*(?P<col>\d+):)?[ \t]*"
    r"(?:(?P<sev>error|warning|note):[ \t]*)?(?P<msg>.*)$"
)
# 没有文件定位的裸报错，例如： error: Unable to open file foo.v
_BARE_RE = re.compile(r"^(?P<sev>error|warning|note):[ \t]*(?P<msg>.+)$")
# iverilog 在**没有文件定位**时会打一个 ":0:" 前缀，例如：
#     :0: error: parameter `MAX` not found in `tb_dut`.
# _DIAG_RE 要求文件名非空，所以 ":0:" 这种行它匹配不上，会静默丢掉——
# 结果是编译明明失败了，diagnostics 却是空的、first_error 是 null。
# 在解析前把这个前缀去掉，让它落进下面的裸报错分支。
_NOFILE_PREFIX_RE = re.compile(r"^:\d+:[ \t]*")
# 源码摘录下方用于指位的那一行
_CARET_RE = re.compile(r"^(?P<pad>[ \t]*)(?P<carets>[\^~]+)[ \t]*$")


@dataclass
class Diagnostic:
    severity: str
    kind: str
    file: str | None
    line: int | None
    column: int | None
    end_column: int | None
    message: str
    source_excerpt: str | None = None
    hint_zh: str | None = None
    why_zh: str | None = None
    fix_suggestions_zh: list[str] = field(default_factory=list)
    confidence: str = "low"
    # 这条是**谁**说的。三种来源的权威性不一样，混成一句会误导：
    #   iverilog   编译报错。硬性的，编译过不去就是过不去
    #   lint       本仓库自研源码扫描（_lint.py）。零依赖，但只覆盖锁存器一类
    #   verilator  外部 lint。覆盖广，但它的接受范围和 iverilog 不同，
    #              报出来的**不一定是错**，所以只作提示
    source: str = "iverilog"

    def as_dict(self) -> dict:
        return {
            "severity": self.severity,
            "kind": self.kind,
            "source": self.source,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "end_column": self.end_column,
            "message": self.message,
            "source_excerpt": self.source_excerpt,
            "hint_zh": self.hint_zh,
            "why_zh": self.why_zh,
            "fix_suggestions_zh": self.fix_suggestions_zh,
            "confidence": self.confidence,
        }

    def brief(self) -> str:
        loc = ""
        if self.file:
            loc = f"{Path(self.file).name}:{self.line or '?'}"
            if self.column:
                loc += f":{self.column}"
            loc = f" [{loc}]"
        return f"{self.severity}{loc} {self.message}"


def _classify(msg: str, severity: str) -> DiagKind:
    for pat, kind in _RULES:
        if pat.search(msg):
            return kind
    return DiagKind.OTHER


def _norm_path(raw: str, base: Path) -> str:
    """把 iverilog 报出的路径规范成绝对路径，方便 Claude 直接定位文件。"""
    p = Path(raw.strip())
    if not p.is_absolute():
        p = base / p
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def parse_iverilog_stderr(text: str, base: Path) -> list[Diagnostic]:
    """把 iverilog 的多行报错解析成结构化诊断。

    设计原则：**尽力而为，绝不抛异常**。解析不出来就归到 other 并保留原文，
    绝不因为格式变化让整个工具崩掉。
    """
    lines = text.splitlines()
    found: list[Diagnostic] = []
    i = 0

    while i < len(lines):
        raw = _NOFILE_PREFIX_RE.sub("", lines[i])
        m = _DIAG_RE.match(raw)

        if m and not _is_bare_only(raw):
            msg = (m.group("msg") or "").strip()
            sev = (m.group("sev") or "").lower()
            if not sev:
                low = msg.lower()
                if "error" in low:
                    sev = "error"
                elif "warning" in low:
                    sev = "warning"
                else:
                    sev = "info"

            excerpt = column = end_column = None
            # 语法类错误会紧跟两行：源码摘录 + 指位符
            if i + 2 < len(lines) and not _DIAG_RE.match(lines[i + 1]):
                cm = _CARET_RE.match(lines[i + 2])
                if cm:
                    excerpt = lines[i + 1]
                    column = len(cm.group("pad")) + 1
                    end_column = column + len(cm.group("carets"))
                    i += 2

            if msg or excerpt:
                found.append(_make_diag(sev, msg or "(无描述)", base, m, excerpt, column, end_column))
            i += 1
            continue

        bare = _BARE_RE.match(raw)
        if bare:
            sev = bare.group("sev").lower()
            found.append(
                _make_diag(
                    sev,
                    bare.group("msg").strip(),
                    base,
                    None,
                    None,
                    None,
                    None,
                )
            )
        i += 1

    return _dedup(found)


def _is_bare_only(line: str) -> bool:
    """排除 "error: xxx" 这种没有文件定位的行被 _DIAG_RE 误吞。"""
    return bool(_BARE_RE.match(line)) and not re.search(r":\d+:", line)


def _make_diag(sev, msg, base, m, excerpt, column, end_column) -> Diagnostic:
    kind = _classify(msg, sev)
    hint = HINTS.get(kind, HINTS[DiagKind.OTHER])
    return Diagnostic(
        severity=sev,
        kind=str(kind),
        file=_norm_path(m.group("file"), base) if m else None,
        line=int(m.group("line")) if m else None,
        column=column,
        end_column=end_column,
        message=msg,
        source_excerpt=excerpt,
        hint_zh=hint.what,
        why_zh=hint.why,
        fix_suggestions_zh=list(hint.fixes),
        confidence=hint.confidence,
    )


def _dedup(diags: list[Diagnostic]) -> list[Diagnostic]:
    """同一 (文件, 行, 类型, 消息) 重复出现时折叠，避免刷屏。"""
    seen: dict[tuple, Diagnostic] = {}
    for d in diags:
        key = (d.file, d.line, d.kind, d.message)
        if key not in seen:
            seen[key] = d
    return list(seen.values())


# ============================================================ 测试台协议

class Marker:
    BEGIN = "TEST_BEGIN"
    PASS = "TEST_PASS"
    FAIL = "TEST_FAIL"
    INFO = "TEST_INFO"
    SUMMARY = "TEST_SUMMARY"
    DONE = "TEST_DONE"
    TIMEOUT = "TEST_TIMEOUT"
    # 功能覆盖点。这两个**不算判定标记**，见 parse_markers 里的说明。
    COVER_DECL = "TEST_COVER_DECL"
    COVER_HIT = "TEST_COVER_HIT"


# 覆盖点标记必须排在判定标记之后单独认——不能写成 TEST_COVER 这种前缀，
# 否则 TEST_COVER_DECL 会被前缀匹配吃成另一个 token。
_MARKER_RE = re.compile(
    r"\b(TEST_BEGIN|TEST_PASS|TEST_FAIL|TEST_INFO|TEST_SUMMARY|TEST_DONE|TEST_TIMEOUT"
    r"|TEST_COVER_DECL|TEST_COVER_HIT)\b(.*)$"
)
_COVER_TOKENS = frozenset({Marker.COVER_DECL, Marker.COVER_HIT})
_KV_RE = re.compile(r"(\w+)=(\S+)")


@dataclass
class TestCase:
    name: str
    status: str  # pass / fail
    time: str | None = None
    expected: str | None = None
    got: str | None = None
    signal: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        d = {"name": self.name, "status": self.status}
        for k in ("time", "expected", "got", "signal", "reason"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d


@dataclass
class Markers:
    protocol_used: bool = False
    done_seen: bool = False
    timeout_seen: bool = False
    cases: list[TestCase] = field(default_factory=list)
    summary: dict | None = None
    infos: list[dict] = field(default_factory=list)
    raw_count: int = 0
    # 覆盖点用**出现顺序去重**，不用 set：顺序是测试台声明的顺序，
    # 报告里按声明顺序列出来读着顺，也方便和源码对照。
    cover_decl: list[str] = field(default_factory=list)
    cover_hit: list[str] = field(default_factory=list)


def parse_markers(text: str) -> Markers:
    """解析测试台打印的判定标记。

    协议见 tb/tb_common.vh。设计上对格式宽容：缺字段也能用，多打的噪声会忽略。

    ★ 覆盖点标记（TEST_COVER_*）**绝不置 protocol_used**。
      这个字段的含义是"测试台按协议汇报了判定结果"，_decide() 拿它区分
      「跑完了没判定」和「压根没按协议来」。覆盖点是可以单独出现的
      （一个只声明覆盖点、不写 TEST_PASS 的测试台仍然是"没判定"），
      让它们置位会把「没判定」误升成「判定了」，进而可能判成通过。
      这和 lint 那条铁律是同一个道理：观察项不许改判定。
    """
    mk = Markers()
    for line in text.splitlines():
        m = _MARKER_RE.search(line)
        if not m:
            continue
        token, rest = m.group(1), m.group(2)
        kv = dict(_KV_RE.findall(rest))
        mk.raw_count += 1

        if token in _COVER_TOKENS:
            point = kv.get("name")
            if point:
                dst = mk.cover_decl if token == Marker.COVER_DECL else mk.cover_hit
                if point not in dst:
                    dst.append(point)
            continue

        mk.protocol_used = True

        if token == Marker.BEGIN:
            mk.cases.append(TestCase(name=kv.get("name", "?"), status="running"))
        elif token == Marker.PASS:
            name = kv.get("name", "?")
            existing = _find_running(mk, name)
            if existing:
                existing.status = "pass"
                existing.time = kv.get("time")
            else:
                mk.cases.append(TestCase(name=name, status="pass", time=kv.get("time")))
        elif token == Marker.FAIL:
            name = kv.get("name", "?")
            existing = _find_running(mk, name)
            tgt = existing or TestCase(name=name, status="fail")
            tgt.status = "fail"
            tgt.time = kv.get("time") or kv.get("at") or tgt.time
            tgt.expected = kv.get("expected")
            tgt.got = kv.get("got")
            tgt.signal = kv.get("signal")
            tgt.reason = kv.get("reason")
            if not existing:
                mk.cases.append(tgt)
        elif token == Marker.INFO:
            mk.infos.append(kv)
        elif token == Marker.SUMMARY:
            mk.summary = {
                k: int(v) for k, v in kv.items() if v.lstrip("-").isdigit()
            }
        elif token == Marker.DONE:
            mk.done_seen = True
        elif token == Marker.TIMEOUT:
            mk.timeout_seen = True

    # 没有闭合的 case 视为异常（测试台中途退出或被超时打断）
    for c in mk.cases:
        if c.status == "running":
            c.status = "fail"
            c.reason = "no_verdict"
    return mk


def _find_running(mk: Markers, name: str) -> TestCase | None:
    for c in reversed(mk.cases):
        if c.name == name and c.status == "running":
            return c
    return None


# ============================================================ 源码预扫描

_MODULE_DEF_RE = re.compile(r"^[ \t]*module[ \t]+([A-Za-z_]\w*)", re.MULTILINE)


def scan_modules(files: list[Path]) -> list[str]:
    """粗略扫出源码中定义的模块名。

    仅用于「顶层模块名写错」这一最高频问题的快速拦截——比让 iverilog 报一句
    'top level module not found' 友好得多。会误把注释里的 module 算进来，
    但那只影响提示文案，不影响编译本身。
    """
    names: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n in _MODULE_DEF_RE.findall(text):
            if n not in names:
                names.append(n)
    return names


# ============================================================ 主流程

@dataclass
class SimConfig:
    designs: list[Path]
    tb: Path | None
    top: str | None
    workdir: Path
    name: str
    timeout: float
    gflags: list[str]
    includes: list[str]
    defines: list[str]
    syntax_only: bool
    no_vcd: bool
    no_observe: bool
    no_lint: bool
    iverilog_path: str | None
    verilator_path: Path | None
    # NAME=VALUE 形式的参数覆盖，**目标一定是根模块**（iverilog 的 -P 只认根）。
    # 见 _resolve_root()：根不是测试台时，参数压根不会生效，而且 iverilog 不吭声。
    params: list[str] = field(default_factory=list)
    # 打开 specify 块里的路径延迟（-gspecify）。默认关：不看时序的练习
    # 开着它只会让波形多出一些看着莫名其妙的延迟。
    specify: bool = False


_PARAM_RE = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.DOTALL)


def _parse_params(raw_params: list[str]) -> tuple[list[str], str | None]:
    """校验 --param 的写法，返回 (归一化后的列表, 出错时的说明)。"""
    out: list[str] = []
    for item in raw_params:
        text = item.strip()
        m = _PARAM_RE.match(text)
        if not m:
            return [], (
                f"--param 的写法应该是 NAME=VALUE，收到的是 {text!r}。"
                "（例如 --param W=8）"
            )
        name, value = m.group(1), m.group(2)
        if not value:
            return [], f"--param {text!r} 只给了名字没给值。"
        out.append(f"{name}={value}")
    return out, None


def _resolve_root(cfg: "SimConfig", modules: list[str]) -> tuple[str | None, str | None]:
    """给 -P 定一个确定的根模块。返回 (根模块名, 拒绝的理由)。

    为什么非要"确定"：iverilog 的 -P 只认**根模块**上的 parameter，而根模块
    不是它时**它一声不响**——实测把 `-Pdut.MAX=6` 加在根为 `tb_dut` 的编译上，
    退出码 0、仿真照常跑完，只是参数保持默认值。这比报错坏得多：
    学习者会以为参数生效了，拿着错的结果去排查别的地方。
    （模块名压根不存在时同样沉默；只有"模块对、参数名错"才是硬报错。）
    所以这里**只认能唯一确定的情形，否则明确拒绝**，不去猜。
    """
    if cfg.top:
        return cfg.top, None
    if cfg.tb and cfg.tb.is_file():
        tb_mods = scan_modules([cfg.tb])
        if len(tb_mods) == 1:
            return tb_mods[0], None
        if len(tb_mods) > 1:
            return None, (
                f"测试台 {cfg.tb.name} 里定义了不止一个模块"
                f"（{'、'.join(tb_mods)}），确定不了哪个是顶层。请用 --top 说明。"
            )
    if len(modules) == 1:
        return modules[0], None
    return None, (
        "确定不了顶层模块，参数不知道该放到哪个模块上。请用 --top 指明顶层模块名。"
        "（参数必须声明在顶层上：iverilog 的 -P 只对根模块生效，放错地方它会静默忽略。）"
    )


def _result(status: str, **extra) -> dict:
    # observe / lint 先给一个"没跑"的默认值，真跑到的路径会把它覆盖掉。
    # 这样**每个 status 下这两个块的形状都是统一的** ——
    # 消费 JSON 的一侧不必区分"这个字段不存在"和"这个字段是 null"，
    # 两者在 JSON 里长得一样，靠想象去补是 bug 的温床。
    #
    # 更实际的原因：**"没跑"和"跑了没报"必须能从形状上分开**。
    # 少了这个默认值，compile_error 的结果里压根没有 observe 这个键，
    # 读的人会把"没这个字段"理解成"波形没问题"。
    base = {
        "schema": SCHEMA,
        "status": status,
        "lint": {
            "ran": False, "ok": False, "tool": "verilator",
            "skipped_reason_zh": "本次没有跑到 lint（编译或环境未通过）",
            "n_warnings": 0, "n_errors": 0,
        },
        "observe": {"ok": False, "reason": "本次没有运行仿真（编译或环境未通过）"},
        "cover": _cover_block(reason="本次没有运行仿真（编译或环境未通过）"),
    }
    base.update(extra)
    return base


def _cover_block(markers: Markers | None = None, reason: str | None = None) -> dict:
    """功能覆盖点汇总。

    ★ 覆盖**不参与判定**：`affects_verdict` 恒为 False，`_decide()` 一眼都不看它。
      理由和 lint 一样，但更直白：覆盖点回答的是"测试台验收的场景跑全了没有"，
      不是"待测模块对不对"。用例全过而覆盖有洞，说明**测得浅**，
      该提醒的是"再加两条激励"，不是"你写错了"——把 status 翻成 fail
      会让学习者看到"明明全过却判失败"，然后去改本来是好的代码。
      所以它是观察项，和 observe / lint 同等地位。

    ★ `ok` 区分的是「**没跑覆盖**」和「**跑了、全覆盖**」：
      没有 TEST_COVER_DECL 时 ok=False 且 n_declared=0、ratio=None，
      这时 `complete` **必须是 False**。若让它取 True，
      下游会把"压根没统计"读成"覆盖齐全"，正好是相反的两件事。
    """
    if markers is None:
        return {
            "ok": False,
            "affects_verdict": False,
            "declared": [], "hit": [], "missed": [], "undeclared_hits": [],
            "n_declared": 0, "n_covered": 0, "n_missed": 0,
            "ratio": None, "complete": False, "reason": reason,
        }

    declared, hit = markers.cover_decl, markers.cover_hit
    hit_set = set(hit)
    missed = [p for p in declared if p not in hit_set]
    undeclared_hits = [p for p in hit if p not in set(declared)]
    n_declared = len(declared)
    n_covered = n_declared - len(missed)

    if n_declared:
        ok, reason = True, None
    elif hit:
        ok = False
        reason = (
            "测试台打印了 TEST_COVER_HIT 却没有 TEST_COVER_DECL，没有分母，"
            "覆盖率算不出来（命中的名字原样留在 hit 里，没有丢）。"
        )
    else:
        ok = False
        reason = "测试台没有使用覆盖点标记（TEST_COVER_DECL / TEST_COVER_HIT），本次没有覆盖数据。"

    return {
        "ok": ok,
        "affects_verdict": False,
        "declared": declared,
        "hit": hit,
        "missed": missed,
        "undeclared_hits": undeclared_hits,
        "n_declared": n_declared,
        "n_covered": n_covered,
        "n_missed": len(missed),
        "ratio": round(n_covered / n_declared, 3) if n_declared else None,
        "complete": bool(n_declared) and not missed,
        "reason": reason,
    }


def _observe_block(
    cfg: SimConfig, vcd_new: list[str], tests: list[dict]
) -> dict:
    """从波形里量出事实，供上层讲解。

    **这里出的任何问题都不许影响判定结果。** 仿真跑完了、对错已经判出来了，
    读波形只是额外的一层辅助——它坏了就少讲两句，不能让 status 跟着变。
    所以整块包在 try 里，异常降级成 ok=False 并把原因写清楚。
    """
    if cfg.no_observe:
        return {"ok": False, "reason": "本次以 --no-observe 运行，未做波形观察"}
    if not vcd_new:
        # ★ 不许写「测试台里需要 $dumpfile + $dumpvars」这种断言 —— 我们
        # **找不到**波形不等于**他没写**（见 _snapshot_vcd 的坑 3）。
        # 说清找了哪儿，让使用者自己判断是没写、还是写到了没扫的地方。
        return {
            "ok": False,
            "reason": (
                "本次仿真没有找到 VCD 波形。已找过："
                f"{cfg.workdir}（递归）和 {Path.cwd()}（只一层）。"
                "要么测试台没写 $dumpfile + $dumpvars，"
                "要么写到了这两个位置之外的别处（$dumpfile 的路径是相对进程 cwd 解析的）"
            ),
        }
    # vcd_new 里存的是绝对路径（见 _snapshot_vcd）
    path = Path(vcd_new[0])
    try:
        return observe(path, tests)
    except Exception as e:  # noqa: BLE001 —— 见上面的说明，观察层不许带崩判定
        return {
            "ok": False,
            "reason": f"波形观察失败（不影响上面的判定）：{e}",
            "vcd": str(path),
        }


def _run_sim(cfg: SimConfig) -> tuple[dict, int]:
    root = repo_root()

    tc = detect(cfg.iverilog_path)
    if not tc.ok:
        return (
            _result(
                "env_error",
                error=install_guide(tc),
                next_actions_zh=["安装 Verilog 工具链后重试"],
            ),
            EXIT_ENV_ERROR,
        )

    inputs = list(cfg.designs) + ([cfg.tb] if cfg.tb else [])
    missing = [str(p) for p in inputs if not p.is_file()]
    if missing:
        return (
            _result(
                "usage_error",
                error={"kind": "input_not_found", "missing": missing},
                next_actions_zh=["检查文件路径是否正确"],
            ),
            EXIT_USAGE,
        )

    modules = scan_modules(inputs)

    # 顶层模块名预检：学习者 90% 的"顶层写错"在这一步就能给出可读提示，
    # 不必等 iverilog 抛一句冷冰冰的 top level module not found。
    if cfg.top and modules and cfg.top not in modules:
        suggestion = _closest(cfg.top, modules)
        bad_top = Diagnostic(
            severity="error", kind=str(DiagKind.BAD_TOP), file=None, line=None,
            column=None, end_column=None,
            message=f"顶层模块 '{cfg.top}' 不在输入文件中",
            hint_zh=HINTS[DiagKind.BAD_TOP].what,
            why_zh=(f"输入文件里只找到了这些模块：{', '.join(modules)}。"
                    + (f"你是不是想写 '{suggestion}'？" if suggestion else "")),
            fix_suggestions_zh=HINTS[DiagKind.BAD_TOP].fixes,
            confidence="high",
        )
        return (
            _result(
                "compile_error",
                design={"files": [str(p) for p in inputs], "top": cfg.top,
                        "modules_found": modules},
                # first_error 必须是这条诊断本身。之前这里写的是 None，
                # 而其他所有路径都会填 —— 结果"按 first_error 取第一条错误"
                # 的消费者在这一条路径上拿到空（冒烟测试就是靠这条断言抓出来的）。
                compile={"ok": False, "returncode": None, "duration_ms": 0,
                         "diagnostics": [bad_top.as_dict()],
                         "diagnostic_summary": {str(DiagKind.BAD_TOP): 1},
                         "first_error": bad_top.as_dict(),
                         "command": None, "raw_stderr_tail": ""},
                error_tags=[str(DiagKind.BAD_TOP)],
                next_actions_zh=[f"把 --top 改成实际存在的模块名，或修正模块定义"],
            ),
            EXIT_COMPILE_ERROR,
        )

    # ---- 参数覆盖：先把根模块定下来 ----
    # 必须放在 bad_top 预检之后：顶层名字写错时，那条提示比这里的更有用。
    params, perr = _parse_params(cfg.params)
    if perr:
        return (
            _result(
                "usage_error",
                error={"kind": "bad_param", "message": perr},
                next_actions_zh=["把 --param 写成 NAME=VALUE，例如 --param W=8"],
            ),
            EXIT_USAGE,
        )
    effective_top = cfg.top
    if params and not effective_top:
        effective_top, rerr = _resolve_root(cfg, modules)
        if rerr:
            return (
                _result(
                    "usage_error",
                    error={"kind": "no_root_for_param", "message": rerr},
                    next_actions_zh=["用 --top 指明顶层模块名后重试"],
                ),
                EXIT_USAGE,
            )

    cfg.workdir.mkdir(parents=True, exist_ok=True)
    vvp_path = cfg.workdir / f"{cfg.name}.vvp"

    # ---- 编译 ----
    cmd = [str(tc.iverilog)]
    cmd += cfg.gflags or ["-g2012"]
    cmd += ["-Wall", "-Wno-timescale", "-pfileline=1"]
    for inc in _include_dirs(cfg, root, inputs):
        cmd += ["-I", inc]
    for d in cfg.defines:
        cmd += ["-D", d]
    if cfg.specify:
        # 让 specify 块里的路径延迟真正生效。默认是**被忽略**的，
        # 也就是说：不写这个开关，源码里的 specify 是摆设，仿真跑的是零延迟。
        cmd += ["-gspecify"]
    # 有 --param 时**一定**把 -s 写死：根模块一旦不是我们以为的那个，
    # -P 就会被静默忽略（见 _resolve_root 的说明）。写上 -s 之后，
    # 猜错会变成一条硬报错，而不是一个悄悄不对的结果。
    if effective_top:
        cmd += ["-s", effective_top]
    for p in params:
        cmd += [f"-P{effective_top}.{p}"]
    if cfg.syntax_only:
        cmd += ["-t", "null", "-o", str(cfg.workdir / f"{cfg.name}.null")]
    else:
        cmd += ["-o", str(vvp_path)]
    cmd += [str(p) for p in inputs]

    t0 = time.monotonic()
    cres = run_proc(cmd, cwd=str(root), timeout=cfg.timeout)
    compile_ms = int((time.monotonic() - t0) * 1000)

    diags = parse_iverilog_stderr(cres.stderr + "\n" + cres.stdout, root)
    errors = [d for d in diags if d.severity == "error"]
    summary: dict[str, int] = {}
    for d in diags:
        summary[d.kind] = summary.get(d.kind, 0) + 1

    compile_block = {
        "ok": cres.returncode == 0 and not errors,
        "returncode": cres.returncode,
        "duration_ms": compile_ms,
        "command": cmd,
        "diagnostics": [d.as_dict() for d in diags],
        "diagnostic_summary": summary,
        "first_error": (errors[0].as_dict() if errors else None),
        "timed_out": cres.timed_out,
        "raw_stderr_tail": cres.stderr[-4000:],
    }

    if compile_block["timed_out"]:
        return (
            _result("compile_error", design=_design_block(inputs, cfg.top, modules),
                    compile=compile_block,
                    next_actions_zh=["编译超时，请检查是否写了极其庞大的展开或递归例化"]),
            EXIT_COMPILE_ERROR,
        )

    if not compile_block["ok"]:
        return (
            _result(
                "compile_error",
                design=_design_block(inputs, cfg.top, modules),
                compile=compile_block,
                error_tags=sorted({d.kind for d in errors}),
                next_actions_zh=_compile_next_actions(errors),
            ),
            EXIT_COMPILE_ERROR,
        )

    # ---- 源码级检查：补 iverilog 不做的部分 ----
    # iverilog 是仿真器不是综合器，对"组合 always 赋值不完整 → 推断锁存器"
    # 一句 warning 都不给（-Wall 也一样）。latch 能编译、能跑、功能测试
    # 甚至能过，错在综合结果上，只能自己扫源码。
    # 放在编译通过之后：语法都不对的时候，扫描结果没有意义。
    #
    # 两层，互补：_lint.py 零依赖、永远能跑，但只覆盖锁存器一类；
    # verilator 覆盖广（位宽/组合环/多驱动/未驱动），但装了才有。
    # 两层的结果都只以 warning 形式挂在 diagnostics 上，**都不参与 compile.ok**。
    lint_diags = _lint_diagnostics(inputs, root)
    vlint_diags, lint_block = _vlint_diagnostics(cfg, root, lint_diags)
    extra_diags = lint_diags + vlint_diags
    if extra_diags:
        diags = diags + extra_diags
        for d in extra_diags:
            summary[d.kind] = summary.get(d.kind, 0) + 1
        compile_block["diagnostics"] = [d.as_dict() for d in diags]
        compile_block["diagnostic_summary"] = summary

    if cfg.syntax_only:
        return (
            _result(
                "pass",
                design=_design_block(inputs, cfg.top, modules),
                compile=compile_block,
                lint=lint_block,
                run=None,
                next_actions_zh=["语法检查通过（未运行仿真）"],
                observe={"ok": False, "reason": "本次以 --syntax-only 运行，没有仿真也就没有波形"},
            ),
            EXIT_PASS,
        )

    # ---- 仿真 ----
    vcd_before = _snapshot_vcd(cfg.workdir)
    rres = run_proc([str(tc.vvp), str(vvp_path)], cwd=str(root), timeout=cfg.timeout)
    markers = parse_markers(rres.stdout + "\n" + rres.stderr)
    vcd_after = _snapshot_vcd(cfg.workdir)
    # 新增的，或者被本次仿真覆盖改写的
    vcd_new = sorted(
        name for name, sig in vcd_after.items() if vcd_before.get(name) != sig
    )

    run_block = {
        "ok": rres.returncode == 0 and not rres.timed_out,
        "returncode": rres.returncode,
        "duration_ms": rres.duration_ms,
        "timed_out": rres.timed_out,
        "tree_killed": rres.tree_killed,
        "notes": rres.notes,
        "stdout_bytes": rres.stdout_total,
        "stderr_bytes": rres.stderr_total,
        "output_truncated": rres.truncated,
        "output_flooded": rres.stdout_total > 8 * 1024 * 1024,
        "protocol_used": markers.protocol_used,
        "done_seen": markers.done_seen,
        "timeout_seen": markers.timeout_seen,
        "marker_count": markers.raw_count,
        "tail": (rres.stdout + rres.stderr)[-3000:],
    }

    tests = [c.as_dict() for c in markers.cases]
    verdict, status = _decide(markers, rres, cfg)

    result = _result(
        status,
        verdict=verdict,
        design=_design_block(inputs, cfg.top, modules),
        compile=compile_block,
        run=run_block,
        tests=tests,
        lint=lint_block,
        artifacts={
            "vvp": str(vvp_path),
            "vcd": (_vcd_display(vcd_new[0], root, cfg.workdir) if vcd_new else None),
            "vcd_all": [_vcd_display(v, root, cfg.workdir) for v in vcd_new],
        },
        observe=_observe_block(cfg, vcd_new, tests),
        cover=_cover_block(markers),
        error_tags=_error_tags(diags, markers, rres),
        next_actions_zh=_run_next_actions(status, markers, rres, tests, diags),
    )

    if status == "timeout":
        return result, EXIT_RUNTIME_ERROR
    if status == "pass":
        return result, EXIT_PASS
    if status == "runtime_error":
        return result, EXIT_RUNTIME_ERROR
    return result, EXIT_FAIL


# 对学习者隐藏的目录：里面的源码只给位置，不给摘录
_HIDDEN_DIRS = {"tests", "solutions"}


def _is_hidden(p: Path, root: Path) -> bool:
    try:
        rel = p.resolve().relative_to(root)
    except (ValueError, OSError):
        return False
    return bool(rel.parts) and rel.parts[0] in _HIDDEN_DIRS


def _read_line(path: Path, line: int | None) -> str | None:
    if not line or line < 1:
        return None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for i, ln in enumerate(fh, 1):
                if i == line:
                    return ln.rstrip("\n")
    except OSError:
        return None
    return None


def _lint_message(f) -> str:
    what = "if 没有 else" if f.construct == "if_without_else" else "case 没有 default"
    return f"疑似锁存器：组合 always 里 {what}，涉及信号 {'/'.join(f.signals)}"


def _lint_diagnostics(inputs: list[Path], root: Path) -> list[Diagnostic]:
    """把源码扫描的结果转成与 iverilog 报错同构的 Diagnostic。

    统一成同一个结构，下游（判定、error_tags、人类摘要、Claude）
    就不必为"两种来源的诊断"各写一套处理。
    severity 固定为 warning：它不影响仿真能否跑通，所以不参与
    compile.ok 的判定，只作为提示和错因标签使用。
    """
    hint = HINTS.get(DiagKind.LATCH_INFERENCE)
    out: list[Diagnostic] = []
    for f in lint_scan([p.resolve() for p in inputs]):
        path = Path(f.file)
        out.append(Diagnostic(
            severity="warning",
            kind=str(DiagKind.LATCH_INFERENCE),
            source="lint",
            file=f.file,
            line=f.line,
            column=None,
            end_column=None,
            message=_lint_message(f),
            # 隐藏目录（tests/ solutions/）的源码不展示给学习者
            source_excerpt=None if _is_hidden(path, root) else _read_line(path, f.line),
            hint_zh=hint.what if hint else None,
            why_zh=f.detail_zh,
            fix_suggestions_zh=list(hint.fixes) if hint else [],
            confidence=f.confidence,
        ))
    return out


# verilator 的警告编号 → 本仓库的诊断类别。**只映射能给出像样中文提示的**，
# 其余一律落 OTHER 保留原文——给一条对不上症状的中文解释，比不给更坏。
# 值里的 confidence 覆盖该 kind 的默认置信度：同一个类别下，
# verilator 报出来的往往比自研扫描更确定（它是真做了 elaborate）。
_VLINT_KIND: dict[str, tuple[DiagKind, str]] = {
    "LATCH": (DiagKind.LATCH_INFERENCE, "high"),
    "CASEINCOMPLETE": (DiagKind.CASE_INCOMPLETE, "high"),
    "CASEINCOMPLETEZ": (DiagKind.CASE_INCOMPLETE, "high"),
    "UNOPTFLAT": (DiagKind.COMBINATIONAL_LOOP, "high"),
    "MULTIDRIVEN": (DiagKind.MULTIPLE_DRIVER, "high"),
    "MULTIDRIVENPROC": (DiagKind.MULTIPLE_DRIVER, "high"),
    "WIDTH": (DiagKind.WIDTH_MISMATCH, "medium"),
    "WIDTHEXPAND": (DiagKind.WIDTH_MISMATCH, "medium"),
    "WIDTHTRUNC": (DiagKind.WIDTH_MISMATCH, "medium"),
    "WIDTHCONCAT": (DiagKind.WIDTH_MISMATCH, "medium"),
    "UNDRIVEN": (DiagKind.UNUSED, "medium"),
    "UNUSEDSIGNAL": (DiagKind.UNUSED, "low"),
    "UNUSEDPARAM": (DiagKind.UNUSED, "low"),
    "UNUSEDGENVAR": (DiagKind.UNUSED, "low"),
    "SELRANGE": (DiagKind.WIDTH_MISMATCH, "high"),
}


def _vlint_diagnostics(
    cfg: "SimConfig", root: Path, py_findings: list[Diagnostic]
) -> tuple[list[Diagnostic], dict]:
    """跑一次 verilator lint，转成诊断 + 报告块。

    **整块包在 try 里。** 和 `_observe_block` 同一条道理：lint 是额外的一层，
    它坏了就少讲两句，绝不能让判定跟着崩。`_vlint.run()` 自己已经处理了
    "找不到工具""跑不起来"这些**预期内**的失败，这里兜的是预期外的异常。
    """
    if cfg.no_lint:
        return [], {
            "ran": False, "ok": False, "tool": "verilator",
            "skipped_reason_zh": "本次以 --no-lint 运行，没有跑 verilator",
            "n_warnings": 0, "n_errors": 0,
        }

    try:
        res = vlint_run(
            [p for p in cfg.designs],
            includes=_include_dirs(cfg, root, list(cfg.designs) + ([cfg.tb] if cfg.tb else [])),
            exe_hint=cfg.verilator_path,
            timeout=cfg.timeout,
            cwd=str(root),
        )
    except Exception as e:  # noqa: BLE001 —— 见上面的说明
        return [], {
            "ran": False, "ok": False, "tool": "verilator",
            "skipped_reason_zh": f"verilator lint 异常（不影响上面的判定）：{e}",
            "n_warnings": 0, "n_errors": 0,
        }

    # 自研扫描已经在某个文件里报过锁存器了，verilator 再报一遍没有增量，
    # 而它的解释是英文的、不如 _lint.py 那条详细。按 (文件, 类别) 去重，
    # 让信息量大的那条留下。
    already = {(d.file, d.kind) for d in py_findings}

    out: list[Diagnostic] = []
    for w in res.warnings:
        kind, conf = _VLINT_KIND.get(w.code, (DiagKind.OTHER, "low"))
        path = Path(w.file) if w.file else None
        file_abs = _norm_path(w.file, root) if w.file else None
        if (file_abs, str(kind)) in already:
            continue
        # ★ 没映射到类别时**不能**借用 OTHER 那条提示：它写的是
        #   "iverilog 报了一个未归类的错误"，而这条是 verilator 报的**警告**——
        #   来源和严重性两处都说错了（上面 _VLINT_KIND 的注释刚说过
        #   "给一条对不上症状的中文解释，比不给更坏"，这里正是那种情况）。
        #   宁可老实说"没有对应解释，请读原文"，并把警告编号留在消息里让人去查。
        hint = None if kind == DiagKind.OTHER else HINTS.get(kind)
        if hint is None:
            why = (
                "这条来自 verilator 的 lint，本仓库还没有对应类别的中文解释。"
                "请直接读原文；方括号里是 verilator 的警告编号，可以拿它去查。"
            )
            if w.note:
                why += f"（verilator 补充：{w.note}）"
            fixes: list[str] = []
        else:
            why = hint.why
            if w.note:
                why = f"{why}（verilator 补充：{w.note}）"
            fixes = list(hint.fixes)
        out.append(Diagnostic(
            severity="warning",
            kind=str(kind),
            source="verilator",
            file=file_abs,
            line=w.line,
            column=w.column,
            end_column=None,
            message=f"[{w.code}] {w.message}",
            source_excerpt=(
                None if (path is None or _is_hidden(path, root))
                else _read_line(path, w.line)
            ),
            hint_zh=hint.what if hint else None,
            why_zh=why,
            fix_suggestions_zh=fixes,
            confidence=conf,
        ))

    block = res.as_dict()
    # n_warnings 报的是"转成了几条诊断"，不是 verilator 报了几条——
    # 上面去重掉的那些会在这里体现出来，两者不一致时必须说清楚是哪一种。
    block["n_warnings"] = len(out)
    block["n_warnings_raw"] = len(res.warnings)
    return out, block


def _include_dirs(cfg: "SimConfig", root: Path, inputs: list[Path]) -> list[str]:
    """include 搜索目录。iverilog 和 verilator 走同一份，避免两边漂移。

    仓库的 `tb/` 默认加入，测试台里直接写 `include "tb_common.vh" 即可。
    """
    dirs: list[str] = []
    tb_dir = root / "tb"
    if tb_dir.is_dir():
        dirs.append(str(tb_dir))
    for inc in cfg.includes:
        dirs.append(str(inc))
    for d in _input_dirs(inputs):
        if d not in dirs:
            dirs.append(d)
    return dirs


def _input_dirs(inputs: list[Path]) -> list[str]:
    dirs: list[str] = []
    for p in inputs:
        d = str(p.resolve().parent)
        if d not in dirs:
            dirs.append(d)
    return dirs


def _vcd_display(p: str, root: Path, workdir: Path) -> str:
    """内部用的绝对路径 → 对外的展示路径。

    在 workdir 里的保持原样（workdir 相对，和以前一致）；
    落在别处的按**仓库根相对**记（posix 风格），这和 `toggle.py` 从
    JSON 里取路径时的约定是同一条 —— 相对路径换个 cwd 就跑不通。
    """
    path = Path(p)
    try:
        return str(workdir / path.relative_to(workdir.resolve()))
    except ValueError:
        pass
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _snapshot_vcd(workdir: Path) -> dict[str, tuple[int, int]]:
    """记录「本次可能写出波形的目录」里所有 *.vcd 的 (mtime, size)。

    键是**解析后的绝对路径** —— 因为要扫的不止一个根目录，用相对路径做键会撞。
    对外展示的路径由 `_vcd_display()` 负责转换。

    三个坑：

    1. 只比较**文件名**不够。学习者反复跑同一个测试台时，
       `$dumpfile("build/x.vcd")` 每次覆盖同一个名字，第二次起的差值就是空集，
       `artifacts.vcd` 会悄悄变成 None，下游的可视化跟着一起失效。
       所以 mtime/size 必须纳入比较。

    2. `glob("*.vcd")` 不递归，而 `$dumpfile` 的路径由设计者随便写 ——
       写到 `build/sub/x.vcd` 就找不到了，"明明产生了波形却报告没有"。
       改用 rglob，并且靠上面的 mtime 比对把无关的旧文件排除掉，
       递归不会带来误报。

    3. ★ workdir 只是「我们建议你往这儿写」，而 `$dumpfile("dump.vcd")`
       的路径是**相对进程 cwd** 解析的。学习者写一个光秃秃的相对路径
       非常自然，文件会落在仓库根，而只看 workdir 就会报出
       「测试台里需要 $dumpfile + $dumpvars」——**文件就躺在旁边，
       工具却把他的测试台说成没写**。这正是本仓库最防的那类谎：
       把工具的眼界说成使用者的错。
       所以 cwd 也要扫，但**只扫一层**：递归扫仓库根会一路走进
       `.tools/`（4 GB），代价和收益不成比例。
       ⚠️ 代价要认：`$dumpfile("sub/x.vcd")` 写到别的目录深处仍然找不到。
       这时报的话必须改为「**我找了哪里**」，不能再断言他没写 $dumpfile。
    """
    roots: list[tuple[Path, bool]] = [(workdir, True)]   # (根, 是否递归)
    cwd = Path.cwd()
    if cwd != workdir.resolve():
        roots.append((cwd, False))

    out: dict[str, tuple[int, int]] = {}
    for base, deep in roots:
        try:
            for p in (base.rglob("*.vcd") if deep else base.glob("*.vcd")):
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                except OSError:
                    continue
                out[str(p.resolve())] = (st.st_mtime_ns, st.st_size)
        except OSError:
            continue
    return out


def _design_block(inputs: list[Path], top: str | None, modules: list[str]) -> dict:
    return {
        "files": [str(p) for p in inputs],
        "top": top,
        "modules_found": modules,
    }


def _decide(markers: Markers, rres, cfg: SimConfig) -> tuple[dict, str]:
    """判定。原则：**证据不足时绝不判通过**。"""
    total = passed = failed = 0

    if markers.summary:
        total = markers.summary.get("total", 0)
        passed = markers.summary.get("pass", 0)
        failed = markers.summary.get("fail", 0)
    else:
        for c in markers.cases:
            total += 1
            if c.status == "pass":
                passed += 1
            else:
                failed += 1

    verdict = {
        "passed": passed,
        "failed": failed,
        "total": total,
        "protocol_used": markers.protocol_used,
        "done_seen": markers.done_seen,
        "inconclusive": False,
        "note_zh": None,
    }

    if rres.timed_out:
        verdict["inconclusive"] = True
        verdict["note_zh"] = (
            "仿真超时被强制终止。通常是写了零延迟的无限循环"
            "（例如 always 块里没有 #延迟 或 @(posedge clk)），"
            "或测试台在等待一个永远不会到来的事件。"
        )
        return verdict, "timeout"

    if markers.timeout_seen:
        verdict["inconclusive"] = True
        verdict["note_zh"] = "测试台自带的超时保护触发了，仿真未能正常跑完。"
        return verdict, "timeout"

    if not markers.protocol_used:
        # 没有任何判定标记：说明测试台没按协议汇报。
        # 此时**不能**因为退出码是 0 就判通过——那只是"程序没崩"，不等于"逻辑正确"。
        verdict["inconclusive"] = True
        if rres.returncode not in (0, None):
            verdict["note_zh"] = (
                f"测试台没有输出判定标记，且仿真以非零码 {rres.returncode} 退出，"
                "通常意味着 $fatal 或断言失败。"
            )
            return verdict, "fail"
        verdict["note_zh"] = (
            "测试台没有输出任何判定标记（TEST_PASS / TEST_FAIL）。"
            "仿真虽然正常退出，但这只说明程序没崩，不能证明逻辑正确，因此不判通过。"
        )
        return verdict, "inconclusive"

    if not markers.done_seen:
        verdict["inconclusive"] = True
        verdict["note_zh"] = (
            "测试台输出了判定标记，但没有 TEST_DONE 收尾标记，"
            "仿真可能中途退出——结果不完整。"
        )
        return verdict, "inconclusive"

    if failed > 0:
        return verdict, "fail"

    if total == 0:
        verdict["inconclusive"] = True
        verdict["note_zh"] = "测试台汇报了零个测试用例。"
        return verdict, "inconclusive"

    return verdict, "pass"


def _error_tags(diags: list[Diagnostic], markers: Markers, rres) -> list[str]:
    tags = {d.kind for d in diags if d.severity in ("error", "warning")}

    for c in markers.cases:
        if c.status != "fail":
            continue
        # 值差 1 → 边界/计数范围问题；含 x/z → 复位缺失或多驱动
        got, exp = (c.got or ""), (c.expected or "")
        if "x" in got.lower() or "z" in got.lower():
            tags.add("x_or_z")
        elif _as_int(got) is not None and _as_int(exp) is not None:
            if abs(_as_int(exp) - _as_int(got)) == 1:
                tags.add("off_by_one")
        if c.reason == "no_verdict":
            tags.add("case_no_verdict")

    if rres.timed_out:
        tags.add("sim_timeout")
    if rres.stdout_total > 8 * 1024 * 1024:
        tags.add("output_flood")
    return sorted(tags)


def _as_int(v: str) -> int | None:
    """把 4'h3 / 3 / 8'b1010 这类表示尽量解析成整数，用于判断是否差 1。"""
    if not v:
        return None
    s = v.strip().lower()
    m = re.match(r"^(?:\d+)?'([hdb])([0-9a-fxz_]+)$", s)
    try:
        if m:
            base = {"h": 16, "d": 10, "b": 2}[m.group(1)]
            digits = m.group(2).replace("_", "")
            if any(ch in digits for ch in "xz"):
                return None
            return int(digits, base)
        return int(s, 0)
    except ValueError:
        return None


def _closest(name: str, candidates: list[str]) -> str | None:
    """给拼错的模块名猜一个最接近的候选，用于提示。"""
    def dist(a: str, b: str) -> int:
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]

    if not candidates:
        return None
    best = min(candidates, key=lambda c: dist(name.lower(), c.lower()))
    return best if dist(name.lower(), best.lower()) <= max(2, len(name) // 3) else None


def _compile_next_actions(errors: list[Diagnostic]) -> list[str]:
    if not errors:
        return ["编译未通过，请查看 diagnostics 中的原始报错"]
    kinds = {d.kind for d in errors}
    out: list[str] = []
    if DiagKind.SYNTAX in kinds:
        out.append("语法错误：优先检查报错行的**上一行**是否漏了分号")
    if DiagKind.LATCH_INFERENCE in kinds:
        out.append("推断出锁存器：给 if 补 else、给 case 补 default，或在块首给默认赋值")
    if DiagKind.UNDECLARED in kinds:
        out.append("有未声明的信号被当作 1 位隐式 wire，请补上带位宽的显式声明")
    if DiagKind.UNKNOWN_MODULE in kinds:
        out.append("实例化的模块找不到，确认模块名拼写并确保其源文件已加入编译")
    if DiagKind.PARAM_NOT_FOUND in kinds:
        out.append(
            "参数名在顶层模块上不存在：请把 parameter 声明在测试台上"
            "（module tb_xxx #(parameter W = 8);），再透传给待测模块"
        )
    if not out:
        out.append("编译未通过，请按 first_error 中的提示逐条修复")
    return out


def _run_next_actions(
    status: str,
    markers: Markers,
    rres,
    tests: list[dict],
    diags: list[Diagnostic],
) -> list[str]:
    # latch 提示排在最前：它和"用例过没过"是两回事，通过了也可能有，
    # 而且这类问题综合之后才暴露，越早知道越好。
    latch = [d for d in diags if d.kind == str(DiagKind.LATCH_INFERENCE)]
    latch_notes = [
        f"{d.message}（{Path(d.file).name}:{d.line}）—— 用仿真看不出来，请回头看代码结构"
        for d in latch[:3]
    ] if latch else []

    # 覆盖点没打到的，**只在判定通过时说**。
    # 判定失败时学习者的注意力该在"哪里错了"，这时再叠一句"还有个场景没测"
    # 只会分散注意力；而"全过"恰恰是最需要这句话的时刻——用例是他自己写的，
    # 全过的时候他最容易以为"验完了"。
    missed = [p for p in markers.cover_decl if p not in set(markers.cover_hit)]
    cover_notes = []
    if missed and status == "pass":
        shown = "、".join(missed[:4])
        more = f" 等 {len(missed)} 个" if len(missed) > 4 else ""
        cover_notes = [
            f"有 {len(missed)} 个覆盖点没打到：{shown}{more}。"
            "这不影响本次判定（用例确实都过了），但说明测得不够深——"
            "建议补激励把场景覆盖上，或确认这个点是不是已经不需要了。"
        ]

    if status == "pass":
        return latch_notes + cover_notes + [
            "全部用例通过。可以查看波形，把代码和时序对上。"
        ]
    if status == "timeout":
        return latch_notes + ["检查 always 块是否缺少延时或事件控制，导致仿真时间无法推进"]
    if status == "inconclusive":
        return latch_notes + [
            "测试台缺少判定标记，无法判定正确性。",
            "测试台应按协议打印 TEST_PASS / TEST_FAIL / TEST_DONE（见 tb/tb_common.vh）。",
        ]

    failed = [t for t in tests if t.get("status") == "fail"]
    out = list(latch_notes)
    for t in failed[:3]:
        piece = f"用例 {t['name']} 未通过"
        if t.get("expected") is not None and t.get("got") is not None:
            piece += f"：期望 {t['expected']}，实际 {t['got']}"
        if t.get("time"):
            piece += f"（t={t['time']}）"
        if t.get("signal"):
            piece += f"，观察信号 {t['signal']}"
        out.append(piece)
    if not out:
        out.append("仿真判定失败，请查看 run.tail 中的原始输出")
    return out


# ============================================================ CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="sim.py",
        description="编译 + 仿真 + 判定，输出结构化 JSON",
        add_help=True,
    )
    ap.add_argument("designs", nargs="*", type=Path, help="设计源文件（可多个）")
    ap.add_argument("--tb", type=Path, help="测试台文件")
    ap.add_argument("--top", help="顶层模块名")
    ap.add_argument("--workdir", type=Path, default=Path("build"), help="产物目录")
    ap.add_argument("--name", help="产物文件名前缀（默认取 --top 或第一个设计文件）")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help=f"编译与仿真各自的超时秒数（默认 {DEFAULT_TIMEOUT:g}）")
    ap.add_argument("--gflag", action="append", default=[], help="传给 iverilog 的 -g 参数")
    ap.add_argument("--include", action="append", default=[], help="额外的 include 目录")
    ap.add_argument("--define", action="append", default=[], help="宏定义 NAME[=VALUE]")
    ap.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                    help="覆盖顶层模块的参数（可重复）。名字必须声明在顶层模块上——"
                         "iverilog 的 -P 只认根模块，本仓库的根是测试台")
    ap.add_argument("--specify", action="store_true",
                    help="让 specify 块里的路径延迟生效（-gspecify）。默认忽略")
    ap.add_argument("--syntax-only", action="store_true", help="只做编译检查，不仿真")
    ap.add_argument("--no-vcd", action="store_true", help="忽略波形产物")
    ap.add_argument("--no-observe", action="store_true",
                    help="不做波形观察（不解析 VCD、不出 observe 块，略快一点）")
    ap.add_argument("--no-lint", action="store_true",
                    help="不跑 verilator lint（去掉那几百毫秒；自研的锁存器扫描不受影响）")
    ap.add_argument("--iverilog-path", help="显式指定 iverilog 的 bin 目录")
    ap.add_argument("--verilator-path", type=Path,
                    help="显式指定 verilator 的可执行文件或其所在目录（默认为空，去 .tools/ 找）")
    ap.add_argument("--json", type=Path, help="把 JSON 结果同时写入该文件")
    ap.add_argument("--pretty", action="store_true", help="JSON 缩进输出")
    ap.add_argument("--quiet", action="store_true", help="不输出人类可读摘要")
    return ap


def main(argv: list[str] | None = None) -> int:
    init_console()
    ap = build_parser()
    args = ap.parse_args(argv)

    if not args.designs and not args.tb:
        ap.print_help(sys.stderr)
        return EXIT_USAGE

    cfg = SimConfig(
        designs=list(args.designs),
        tb=args.tb,
        top=args.top,
        workdir=args.workdir,
        name=args.name or args.top or (args.designs[0].stem if args.designs else "sim"),
        timeout=args.timeout,
        gflags=args.gflag,
        includes=args.include,
        defines=args.define,
        syntax_only=args.syntax_only,
        no_vcd=args.no_vcd,
        no_observe=args.no_observe,
        no_lint=args.no_lint,
        iverilog_path=args.iverilog_path,
        verilator_path=args.verilator_path,
        params=args.param,
        specify=args.specify,
    )

    result, code = _run_sim(cfg)

    payload = json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None)
    print(payload)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(payload, encoding="utf-8")

    if not args.quiet:
        _print_summary(result, code)

    return code


def _print_summary(result: dict, code: int) -> None:
    """人类可读摘要走 stderr，避免污染 stdout 的 JSON。"""
    w = sys.stderr.write
    w("\n" + "=" * 60 + "\n")
    status = result.get("status")
    label = {
        "pass": "判定通过 ✓",
        "fail": "判定失败 ✗",
        "inconclusive": "无法判定 ?",
        "compile_error": "编译失败 ✗",
        "timeout": "仿真超时 ✗",
        "runtime_error": "运行错误 ✗",
        "env_error": "环境错误 ✗",
        "usage_error": "用法错误 ✗",
    }.get(status, status)
    w(f"结果：{label}\n")

    comp = result.get("compile") or {}
    if comp.get("diagnostics"):
        w("-" * 60 + "\n")
        for d in comp["diagnostics"][:8]:
            loc = Path(d["file"]).name if d.get("file") else ""
            line = f":{d['line']}" if d.get("line") else ""
            col = f":{d['column']}" if d.get("column") else ""
            # 来自 verilator 的要标出来源。它和 iverilog 的权威性不一样，
            # 不区分的话读的人会把"另一个工具的方言差异"当成硬报错。
            src = d.get("source") or "iverilog"
            tag = "" if src == "iverilog" else f"[{src}]"
            w(f"  {d['severity']}{tag}: {loc}{line}{col} {d['message']}\n")
            if d.get("source_excerpt"):
                w(f"      {d['source_excerpt'].strip()}\n")
            if d.get("hint_zh"):
                w(f"      → {d['hint_zh']}\n")

    # lint 跑没跑：**必须打出来**。"零条 lint 诊断"在下游有两种相反的成因
    # （没检查 / 检查了干净），只看有没有诊断是分不出来的。
    lint = result.get("lint") or {}
    if lint.get("tool"):
        w("-" * 60 + "\n")
        if lint.get("ok"):
            n = lint.get("n_warnings", 0)
            warn_bit = f"，{n} 条 lint 提示" if n else "，没有结构性问题"
            w(f"  lint：verilator {lint.get('version') or ''} 检查过{warn_bit}\n")
        else:
            w(f"  lint：**没跑** —— {lint.get('skipped_reason_zh')}\n")

    v = result.get("verdict") or {}
    if v.get("total") is not None and result.get("status") != "compile_error":
        w("-" * 60 + "\n")
        w(f"用例：{v.get('passed', 0)}/{v.get('total', 0)} 通过")
        if v.get("failed"):
            w(f"，{v['failed']} 个失败")
        w("\n")

    for t in (result.get("tests") or []):
        if t.get("status") == "fail":
            detail = ""
            if t.get("expected") is not None:
                detail = f"  期望 {t['expected']} / 实际 {t.get('got')}"
            w(f"  ✗ {t['name']}{detail}\n")

    # 观察块：只有真跑过仿真且解析出波形才有。这里刻意打印，
    # 是因为"第一个错误发生在第几个时钟沿"通常比"哪几条用例没过"更好下手。
    ob = result.get("observe") or {}
    if ob.get("ok"):
        bits: list[str] = []
        clk = ob.get("clock")
        bits.append(
            f"时钟 {clk['signal']}（周期 {clk['period_text']}）" if clk
            else "未识别到时钟信号"
        )
        div = ob.get("first_divergence")
        if div:
            piece = f"首次分歧 {div.get('case')} @ t={div.get('time_text')}"
            if div.get("edge_index") is not None:
                piece += f"（第 {div['edge_index']} 个上升沿"
                if div.get("offset_from_edge_text"):
                    piece += f"之后 {div['offset_from_edge_text']}"
                piece += "）"
            if div.get("expected") is not None:
                piece += f"：期望 {div['expected']} 实际 {div.get('got')}"
            bits.append(piece)
        w("-" * 60 + "\n")
        w("  观察：" + "；".join(bits) + "\n")

    # 覆盖点。**只在真有数据（或有值得说的异常）时打印**——
    # 这一点和 lint 不同：lint 是每次编译都会跑的一项检查，所以"没跑"
    # 必须显式说出来；而覆盖点是个可选功能，绝大多数练习压根不用它，
    # 每次都报一句"本次没有覆盖数据"只是噪音，会把人训练成不看这一行。
    # 机器要区分"没统计/统计了"仍然可以——cover.ok 在 JSON 里一直都在。
    # 但 `undeclared_hits` 这种**异常**不能省：那是测试台自己的 bug。
    cov = result.get("cover") or {}
    if cov.get("ok"):
        piece = f"覆盖点 {cov.get('n_covered', 0)}/{cov.get('n_declared', 0)}"
        if cov.get("ratio") is not None:
            piece += f"（{cov['ratio'] * 100:.0f}%）"
        if cov.get("missed"):
            piece += "，没打到：" + "、".join(cov["missed"][:4])
            if len(cov["missed"]) > 4:
                piece += f" 等 {len(cov['missed'])} 个"
        w("-" * 60 + "\n")
        w(f"  {piece}\n")
        w("  （覆盖点不影响判定，只说明测得够不够深）\n")
    elif cov.get("undeclared_hits"):
        w("-" * 60 + "\n")
        w("  覆盖点：有命中却没有声明——"
          + "、".join(cov["undeclared_hits"][:4])
          + "（测试台少了 TEST_COVER_DECL，覆盖率算不出来）\n")

    if v.get("note_zh"):
        w(f"  说明：{v['note_zh']}\n")

    for a in (result.get("next_actions_zh") or []):
        w(f"  · {a}\n")
    w("=" * 60 + "\n")


if __name__ == "__main__":
    sys.exit(main())
