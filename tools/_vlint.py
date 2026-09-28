"""verilator 真 lint：补上 iverilog **不做**的那一类结构检查。

由 sim.py 在编译通过之后自动调用，不是独立命令。

和 `_lint.py` 的分工 —— 两者都并进 `compile.diagnostics`，用 `source` 字段区分来源：

    _lint.py   自研源码扫描。**零依赖**，装没装 verilator 都能跑。只覆盖锁存器
               一类，但"宁可漏报不可冤枉"，报出来的一定是推导得出来的
               （所以它的中文解释最细）
    _vlint.py  调外部 verilator。能报位宽截断/扩展、组合环、多驱动、未驱动、
               未使用。覆盖广，代价是**装了才有**

三条设计原则（改之前先看）：

1. **只加 warning，永不改 status。** 判定内核是 iverilog + 测试台标记协议。
   verilator 与 iverilog 的接受范围并不一样（SystemVerilog 子集、延时语法、
   include 的查找规则），让 lint 有机会把 pass 翻成 fail 是危险的：学习者会看到
   「代码明明跑通了，工具却说过不了」，而真正的原因只是另一个工具的方言差异。
   所以 verilator 的 `%Error` **也不进 diagnostics**，只进 `errors` 并附一句说明。

2. **「没跑」和「跑了没报」必须分开。** 两者在下游看起来都是「零条诊断」，
   含义却相反：一个是「没检查」，一个是「检查了，干净」。只报零条就等于
   把崩溃谎报成干净——这和 `_observe.py` 里「拿不到波形不能说成没有时钟」
   是同一条教训。所以有 `ran` / `ok` 两个字段和 `skipped_reason_zh`。

3. **失败不影响判定。** 和 `_observe.py` 一样：由调用方整块包在 try 里，
   异常降级成 `ran=False` + 原因，`status` 一个字都不能变。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import _manifest
from _manifest import ManifestError
from _proc import run as run_proc

# 清单里的软件 id。这里是本模块唯一硬编码的工具名——因为
# "怎么解析 verilator 的输出"天生就是 verilator 专属的，做成通用层只会
# 造出一个只有一种实现的抽象。
VERILATOR_ID = "verilator"

# 传给 verilator 的开关。**刻意不用 -Wall**。
#
# -Wall 会连带打开两类与「设计对不对」无关的东西，一进门就把学习者淹掉：
#   DECLFILENAME  文件名与模块名不一致。本仓库的约定就是 dut_counter4.v 里放
#                 counter4、tb_xxx.v 里放 tb_xxx——按 -Wall 几乎每个文件都报，
#                 而它根本不是设计错误
#   MULTITOP      哪个是顶层是**构建配置**问题（iverilog 那边用 -s 指定），
#                 不是源码里写错了什么
# 噪声的代价不是「多几行字」，而是学习者开始整体忽略这个工具。
#
# 显式打开的三项，都是清单里 lint.full 承诺要覆盖、而默认关着的：
#   MULTIDRIVENPROC  同一个 reg 被两个 always 驱动。iverilog **完全沉默**
#                    （实测 -Wall 也不报），而它是「仿真结果忽左忽右」的经典来源
#   UNDRIVEN         声明了却没有任何驱动
#   UNUSEDSIGNAL     声明了却没被读过 —— 会不会是忘了连线？
#
# ⚠ 默认就已开启、**不需要** -Wall 的：LATCH、WIDTHEXPAND / WIDTHTRUNC、
#   UNOPTFLAT（组合环）、CASEINCOMPLETE。别以为它们要 -Wall 才有，
#   也别再加一遍 -Wwarn-*。
_EXTRA_WARNS = [
    "-Wwarn-MULTIDRIVENPROC",
    "-Wwarn-UNDRIVEN",
    "-Wwarn-UNUSEDSIGNAL",
]

BASE_ARGS = ["--lint-only", "-Wno-fatal", "-Wno-MULTITOP", *_EXTRA_WARNS]

# `-Wno-fatal` 的作用：把「有警告」和「退出码非 0」解耦。
# 不加它时 verilator 报完警告就 `%Error: Exiting due to N warning(s)` 并退出 1，
# 于是"退出码"再也不能用来区分警告和真错误——只能去数输出里有没有 %Warning- 前缀。

# 诊断行：  %Warning-LATCH: path/to/x.v:17:5: 消息
#           %Error: Cannot continue                      （没有文件定位）
# 路径用非贪婪匹配到**第一个** `:数字:`，Windows 盘符里的冒号因此不受影响。
_DIAG_RE = re.compile(
    r"^%(?P<sev>Warning|Error)(?:-(?P<code>[A-Za-z0-9_]+))?:[ \t]*"
    r"(?:(?P<file>.+?):(?P<line>\d+):(?:[ \t]*(?P<col>\d+):)?[ \t]*)?"
    r"(?P<msg>.*)$"
)

# 续行：`   : ... Suggest use of always_latch` / `   ... For warning description see …`
_NOTE_RE = re.compile(r"^[ \t]*:?[ \t]*\.\.\.[ \t]*(?P<text>.+?)[ \t]*$")

# 续行里这几类是给读者指路的，不是诊断内容本身。
# "Location of other write" 也排除：verilator 把**具体位置**打在它的上一行
# （不带 `...` 前缀），只留这半句反而成了一个悬空的指路牌。
_NOTE_NOISE = (
    "For warning description see",
    "lint_off",
    "note: In instance",
    "Location of other write",
)

# verilator 收尾时打的一行，不是真错误
_EXIT_LINE_RE = re.compile(r"^Exiting due to \d+ (?:warning|error)\(s\)$")


def _spaced(word: str) -> str:
    """逐字符之间允许空白。

    verilator 的报告行把标题拆成 `V e r i l a t i o n   R e p o r t`，
    照着源码里那样写死字面量匹配不上——而且两个词的拆法还不一样
    （Report 也是拆开的），只能逐字符放开。
    """
    return r"\s*".join(re.escape(c) for c in word)


# 报告行：证明 verilator **真的做到了 elaboration**。这是判断"没跑"还是
# "跑了没报"的硬证据——不能只看"输出里有没有 %Warning"。
_REPORT_RE = re.compile(
    _spaced("Verilation") + r"\s+" + _spaced("Report:") + r"\s*Verilator\s+(\S+)"
)

# 最多带回来多少条 verilator 的 %Error 原文，防止极端情况下刷屏。
# 截断时总数另记（n_errors_total），否则报告出来的条数会比实际少——
# 那正是这个仓库最不想要的"看着正常其实是错的"。
_MAX_ERRORS = 10


@dataclass
class VLintWarning:
    severity: str          # warning / error
    code: str              # LATCH / WIDTHEXPAND / UNOPTFLAT / MULTIDRIVENPROC …
    file: str | None
    line: int | None
    column: int | None
    message: str
    note: str | None = None   # verilator 的 "... Suggest …" 续行

    @property
    def loc(self) -> str:
        if not self.file:
            return ""
        s = self.file
        if self.line:
            s += f":{self.line}"
            if self.column:
                s += f":{self.column}"
        return s


@dataclass
class VLintResult:
    """一次 verilator lint 的结果。

    字段的含义边界（别混，混了就会谎报）：

        ran   进程真的跑到了 elaboration（有 Verilation Report 为证）
        ok    ran 且没有"跑不起来"的迹象（没超时、没 Cannot continue）
        ok=True 且 warnings 为空 才是「检查了，干净」
        ok=False 时**什么都没检查到**，skipped_reason_zh 说明为什么
    """

    ran: bool = False
    ok: bool = False
    exe: str | None = None
    version: str | None = None
    command: list[str] = field(default_factory=list)
    warnings: list[VLintWarning] = field(default_factory=list)
    # 只留前 _MAX_ERRORS 条原文；n_errors_total 是真实条数。
    # 两者分开，是因为"报告了 10 条"和"实际有 50 条"必须能区分。
    errors: list[str] = field(default_factory=list)
    n_errors_total: int = 0
    skipped_reason_zh: str | None = None
    note_zh: str | None = None
    timed_out: bool = False
    duration_ms: int = 0
    tail: str = ""

    def as_dict(self) -> dict:
        return {
            "ran": self.ran,
            "ok": self.ok,
            "tool": VERILATOR_ID,
            "exe": self.exe,
            "version": self.version,
            "command": self.command,
            "n_warnings": len(self.warnings),
            "n_errors": self.n_errors_total,
            "errors": list(self.errors),
            "skipped_reason_zh": self.skipped_reason_zh,
            "note_zh": self.note_zh,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "tail": self.tail,
        }


# ---------------------------------------------------------------- 解析


def parse_output(blob: str) -> tuple[list[VLintWarning], list[str], int, str | None]:
    """把 verilator 的输出解析成 (警告, 错误原文, 错误总数, 版本号)。

    设计原则与 `sim.py` 解析 iverilog 一致：**尽力而为，绝不抛异常**。
    解析不出来就原样留在 errors 里，绝不因为 verilator 换了措辞就让整条链路崩掉。
    """
    warnings: list[VLintWarning] = []
    errors: list[str] = []
    n_errors = 0
    version: str | None = None
    lines = blob.splitlines()

    for i, raw in enumerate(lines):
        rep = _REPORT_RE.search(raw)
        if rep and version is None:
            version = rep.group(1)

        m = _DIAG_RE.match(raw)
        if not m:
            continue

        msg = (m.group("msg") or "").strip()
        code = m.group("code") or ""
        sev = m.group("sev").lower()

        if sev == "error":
            # 只留原文，不往 diagnostics 里送——见模块头第 1 条
            if msg and not _EXIT_LINE_RE.match(msg):
                n_errors += 1
                if len(errors) < _MAX_ERRORS:
                    errors.append(f"{m.group('file') or ''}: {msg}".lstrip(": "))
            continue

        note = _continuation_note(lines, i)
        warnings.append(
            VLintWarning(
                severity="warning",
                code=code or "OTHER",
                file=m.group("file"),
                line=int(m.group("line")) if m.group("line") else None,
                column=int(m.group("col")) if m.group("col") else None,
                message=msg or "(无描述)",
                note=note,
            )
        )

    return warnings, errors, n_errors, version


def _continuation_note(lines: list[str], i: int) -> str | None:
    """取诊断行后面那条有信息量的续行。

    实测 verilator 的续行有好几种，只有 "Suggest use of always_latch" 这类
    真正给了下一步（对我们来说是现成的中文提示素材）；其余是文档链接和
    「怎么关掉这条警告」，读者不需要。
    """
    for nxt in lines[i + 1: i + 5]:
        if _DIAG_RE.match(nxt):     # 已经走到下一条诊断了
            break
        nm = _NOTE_RE.match(nxt)
        if not nm:
            continue
        text = nm.group("text")
        if any(skip in text for skip in _NOTE_NOISE):
            continue
        return text
    return None


# ---------------------------------------------------------------- 执行


def _verilator_software():
    """从清单里取 verilator 条目。清单坏了要**大声说**，不能静默跳过。"""
    m = _manifest.load()
    sw = m.software_by_id(VERILATOR_ID)
    if sw is None:
        raise ManifestError(f"清单里没有 {VERILATOR_ID} 条目")
    return sw


def run(
    designs: list[Path],
    includes: list[str] | None = None,
    exe_hint: Path | None = None,
    timeout: float = 30.0,
    cwd: str | None = None,
) -> VLintResult:
    """对设计文件跑一次 verilator lint。

    **只 lint 设计文件，不 lint 测试台。** 两个理由：测试台是课程提供的、
    不是学习者写的；而且 `tests/` 属于对学习者隐藏的目录（见 sim.py 的
    `_HIDDEN_DIRS`），把它的内部结构写进诊断等于变相泄漏。
    """
    if not designs:
        return VLintResult(
            skipped_reason_zh="没有设计文件可查（只给了测试台）",
        )

    try:
        sw = _verilator_software()
    except ManifestError as exc:
        return VLintResult(skipped_reason_zh=str(exc))

    # 显式指定的路径优先；exe_hint 是文件时直接用，是目录时在里面找
    exe = _manifest.locate(sw, extra=exe_hint)
    if exe is None:
        where = f"（已在 {exe_hint} 里找过）" if exe_hint else ""
        return VLintResult(
            skipped_reason_zh=(
                f"没找到 verilator{where}。装法："
                f"python tools/setup_tools.py --install {VERILATOR_ID} --lock"
            ),
        )

    cmd = [str(exe), *BASE_ARGS]
    for inc in (includes or []):
        cmd += [f"-I{inc}"]
    cmd += [str(p) for p in designs]

    # VERILATOR_ROOT 是**运行必需**的（清单 check.env 里写着）：套件里的
    # verilator_bin.exe 把构建期的 /yosyshq 烘死了，不设它 --lint-only 会报
    # 「Cannot find verilated_std_waiver.vlt」。probe 靠它判 usable，
    # 这里靠它才真跑得起来。
    env = {**os.environ, **_manifest.tool_env(sw)}

    try:
        res = run_proc(cmd, cwd=cwd, timeout=timeout, env=env)
    except Exception as exc:  # noqa: BLE001 —— 调用方还会再包一层
        return VLintResult(
            exe=str(exe),
            command=cmd,
            skipped_reason_zh=f"verilator 起不来：{type(exc).__name__}: {exc}",
        )

    # 警告走 stderr、Verilation Report 走 stdout（实测），两边都得读
    blob = res.stdout + "\n" + res.stderr
    warnings, errors, n_errors, version = parse_output(blob)

    out = VLintResult(
        ok=False,
        exe=str(exe),
        version=version,
        command=cmd,
        warnings=warnings,
        errors=errors,
        n_errors_total=n_errors,
        timed_out=res.timed_out,
        duration_ms=res.duration_ms,
        tail=blob[-2000:],
    )

    if res.timed_out:
        out.skipped_reason_zh = f"verilator lint 超时（{timeout:g}s）被终止，这一轮没有结果"
        return out

    # 「跑到了 elaboration」的硬证据是那行报告。只看 warnings 空不空会把
    # "verilator 压根没起来" 和 "起来了但没报问题" 判成同一件事。
    out.ran = version is not None

    if not out.ran:
        first = next((l for l in blob.splitlines() if l.strip()), "")
        out.skipped_reason_zh = (
            "verilator 没有输出 Verilation Report，说明它没跑完（多半是启动期就失败了）。"
            + (f" 首行输出：{first[:200]}" if first else " 它一个字节都没输出。")
        )
        return out

    out.ok = True
    if n_errors:
        # 注意措辞：这条**不影响判定**。verilator 与 iverilog 支持的范围不同，
        # iverilog 编得过而 verilator 报错的代码是存在的（反之亦然）。
        shown = f"，下面列出前 {len(errors)} 条" if n_errors > len(errors) else ""
        out.note_zh = (
            f"verilator 另外报了 {n_errors} 条它自己处理不了的问题{shown}。"
            "这多半是两者的方言差异，**不影响本次判定**（判定以 iverilog 为准）；"
            "若下面这些提示对你有用就看看，看不懂可以忽略。"
        )
    return out
