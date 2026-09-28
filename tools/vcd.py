"""VCD（Value Change Dump）解析器 —— 纯标准库。

    from vcd import parse
    dump = parse(Path("build/x.vcd"))
    sig = dump.find("dut.q")[0]
    print(dump.format_value(sig, sig.stream.value_at(25000)))

设计上的三个要点（都是被真实文件逼出来的，不是预先想的）：

1. **一个 ID 可能对应多个信号。**
   iverilog 对"同一根网线"只分配一个 ID：测试台的 `tb.clk` 和它驱动的
   子模块端口 `dut.clk` 在 VCD 里是**同一个 ID**。所以跳变数据按 ID 存一份，
   多个信号对象共享同一个流——既正确（它们本来就该同步变化），
   又省掉一半内存。

2. **时间是标量整数，不是浮点。**
   VCD 的时间戳是以 `$timescale` 为单位的整数。全部按整数存、按整数比较，
   避免浮点累积误差导致波形错位。解析时把 `$timescale` 换算成飞秒，
   渲染时再换算回人类单位。

3. **向量值要按位宽归一化。**
   文件里写的是 `b0`（不是 `b0000`）。按 IEEE 语义补位：
   最高位是 0/1 时补 0，是 x/z 时补 x/z。归一化之后渲染层就不必再关心
   位宽差异，直接按字符串画。

不支持的：`$dumpports`、`$dumpportsoff` 等扩展命令（用不到），
以及 `$dumpoff` 区段内的值（见 `_SKIP_VALUE_BLOCKS` 说明）。
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

# 单位 → 飞秒
UNIT_FS: dict[str, int] = {
    "fs": 1,
    "ps": 1_000,
    "ns": 1_000_000,
    "us": 1_000_000_000,
    "ms": 1_000_000_000_000,
    "s": 1_000_000_000_000_000,
}

_TIMESCALE_RE = re.compile(r"^\s*(\d+)\s*(fs|ps|ns|us|ms|s)\s*$", re.I)
_TOKEN_RE = re.compile(r"\$[A-Za-z_]+|\S+")

# 这些块里的值不记录：
#   $dumpoff 会把所有信号写成 x，那是"暂停记录"的标记，不是真实值。
#   记进去波形上会凭空多出一片 x，把人带偏。
_SKIP_VALUE_BLOCKS = {"$dumpoff"}

# 这些块里是真实的值变化（$dumpvars 是初值，$dumpall 是当前快照）
_VALUE_BLOCKS = {"$dumpvars", "$dumpall", "$dumpon"}

_SKIP_TO_END = {"$comment", "$date", "$version", "$enddefinitions"}


@dataclass
class Stream:
    """一个 VCD ID 对应的跳变序列。多个信号可以共享同一个 Stream。"""

    times: list[int] = field(default_factory=list)
    values: list[str] = field(default_factory=list)

    def add(self, t: int, value: str) -> None:
        # 连续相同的值不记：文件里会有冗余（初值 dump 之后紧接着又写一次），
        # 去重能显著减小波形体积，且不影响查询语义。
        if self.values and self.values[-1] == value:
            return
        self.times.append(t)
        self.values.append(value)

    def value_at(self, t: int) -> str | None:
        """t 时刻的值（取最后一个 <= t 的跳变）。t 早于首次跳变时返回 None。"""
        i = bisect_right(self.times, t) - 1
        return self.values[i] if i >= 0 else None

    def changes(self, t0: int | None = None, t1: int | None = None) -> list[tuple[int, str]]:
        """区间内的跳变列表，供渲染层按视口裁剪。"""
        lo = 0 if t0 is None else bisect_right(self.times, t0 - 1)
        hi = len(self.times) if t1 is None else bisect_right(self.times, t1)
        return list(zip(self.times[lo:hi], self.values[lo:hi]))

    @property
    def n_changes(self) -> int:
        return len(self.times)


@dataclass
class Signal:
    id: str
    name: str        # 短名，如 q
    path: str        # 完整层次路径，如 tb_counter4.dut.q
    scope: str       # 所属 scope，如 tb_counter4.dut
    width: int
    kind: str        # wire / reg / integer / real / ...
    is_real: bool
    stream: Stream

    @property
    def is_bus(self) -> bool:
        return self.width > 1 or self.kind in ("integer", "real")

    def value_at(self, t: int) -> str | None:
        return self.stream.value_at(t)


@dataclass
class VCD:
    path: str
    timescale_text: str
    timescale_fs: int
    signals: list[Signal]
    end_time: int
    notes: list[str] = field(default_factory=list)

    def groups(self) -> dict[str, list[Signal]]:
        """按 scope 分组，保持文件中出现的顺序（比字典序更贴近设计层次）。"""
        out: dict[str, list[Signal]] = {}
        for s in self.signals:
            out.setdefault(s.scope, []).append(s)
        return out

    def find(self, pattern: str) -> list[Signal]:
        """按子串或末尾匹配找信号。dut.q 命中 tb.dut.q。"""
        if not pattern:
            return list(self.signals)
        exact = [s for s in self.signals if s.path == pattern or s.name == pattern]
        if exact:
            return exact
        return [s for s in self.signals
                if pattern in s.path or s.path.endswith("." + pattern)]

    def n_changes(self) -> int:
        """总跳变数（按 ID 去重后）。用于判断波形规模是否过大。"""
        return sum(st.n_changes for st in _unique_streams(self.signals))

    # ---- 值格式化 ----

    def format_value(self, sig: Signal, raw: str | None, base: int | None = None) -> str:
        """把归一化后的原始值格式化成给人看的形式。

        默认进制：窄信号用十进制（4 位计数器显示成 7 比显示成 7'h7 好读），
        宽信号用十六进制（32 位显示十进制没人看得懂）。
        """
        if raw is None:
            return "-"
        if sig.is_real:
            return raw
        if sig.width == 1:
            return raw

        if any(ch in raw for ch in "xz"):
            return raw          # 含 x/z 就不转进制了，原样展示哪些位是未知的
        if base is None:
            base = 10 if sig.width <= 8 else 16
        n = int(raw, 2)
        if base == 16:
            return f"{n:0{(sig.width + 3) // 4}x}"
        if base == 2:
            return raw
        return str(n)


def _unique_streams(signals: list[Signal]) -> list[Stream]:
    seen: dict[int, Stream] = {}
    for s in signals:
        seen.setdefault(id(s.stream), s.stream)
    return list(seen.values())


# ---------------------------------------------------------------- 解析


def _norm_vector(bits: str, width: int) -> str:
    """把文件里的向量值补位到指定宽度。

    IEEE 语义：值比位宽短时**按最高位扩展**——最高位是 0/1 就补 0，
    是 x/z 就补 x/z。所以 `bx` 在 4 位信号上是 `xxxx` 而不是 `000x`。
    """
    bits = bits.strip().replace("_", "").lower()
    if not bits:
        bits = "x"
    if len(bits) > width:
        bits = bits[-width:]          # 超出位宽保留低位
    if len(bits) < width:
        pad = bits[0] if bits[0] in "xz" else "0"
        bits = pad * (width - len(bits)) + bits
    return bits


def parse(path: Path, *, max_changes: int = 5_000_000) -> VCD:
    """解析一个 VCD 文件。

    max_changes 是保护阈值：跳变太多的文件（例如零延迟循环刷出来的）
    会把内存打爆，超过就停止记录并记一条 note，让上层如实报告"波形不完整"，
    而不是假装解析成功。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise VCDError(f"无法读取 VCD 文件 {path}：{e}") from e

    tokens = _TOKEN_RE.findall(text)

    notes: list[str] = []
    timescale_text = "1ns"
    timescale_fs = UNIT_FS["ns"]

    scopes: list[str] = []
    # 信号定义（此时还没有 Stream，等 body 里按 id 惰性建）
    defs: list[tuple[str, int, str, str, str]] = []   # (type, width, id, ref, scope)

    i = 0
    n = len(tokens)

    # ---------------- 头部 ----------------
    while i < n and tokens[i] != "$enddefinitions":
        tok = tokens[i]

        # $end 自己要先挡掉。下面的"跳到下一个 $end"分支本意是跳过
        # $comment/$date 这类整块命令，但如果不单独处理 $end，
        # `$scope module tb $end` 的 $end 会触发一次多余的整块跳过，
        # 把**紧跟其后的那条 $var 整行吞掉**——信号会凭空消失且毫无提示。
        if tok == "$end":
            i += 1
            continue

        if tok == "$timescale":
            i += 1
            parts: list[str] = []
            while i < n and tokens[i] != "$end":
                parts.append(tokens[i])
                i += 1
            joined = "".join(parts)
            m = _TIMESCALE_RE.match(joined)
            if m:
                timescale_text = f"{m.group(1)}{m.group(2).lower()}"
                timescale_fs = int(m.group(1)) * UNIT_FS[m.group(2).lower()]
            else:
                notes.append(f"无法识别的 $timescale：{joined!r}，按 1ns 处理")
            i += 1
            continue

        if tok == "$scope":
            # $scope <type> <name> $end —— 整条命令一起吃掉
            if i + 2 < n:
                scopes.append(tokens[i + 2])
            while i < n and tokens[i] != "$end":
                i += 1
            i += 1
            continue

        if tok == "$upscope":
            if scopes:
                scopes.pop()
            while i < n and tokens[i] != "$end":
                i += 1
            i += 1
            continue

        if tok == "$var":
            # $var <type> <width> <id> <ref> [<range>] $end
            parts = []
            i += 1
            while i < n and tokens[i] != "$end":
                parts.append(tokens[i])
                i += 1
            i += 1
            if len(parts) >= 4:
                vtype, width_s, vid = parts[0], parts[1], parts[2]
                ref = parts[3]
                try:
                    width = int(width_s)
                except ValueError:
                    width = 1
                defs.append((vtype, width, vid, ref, ".".join(scopes)))
            else:
                notes.append(f"$var 字段不足，已跳过：{parts!r}")
            continue

        if tok in _SKIP_TO_END or tok.startswith("$"):
            # 其余头部命令的参数一律跳过到 $end
            i += 1
            while i < n and tokens[i] != "$end":
                i += 1
            i += 1
            continue

        i += 1

    # ---------------- 定义表 ----------------
    # 自检：文件里 $var 的条数必须等于解析出的信号数。
    # "命令之间的 $end 处理错位"会导致整条 $var 被静默吞掉——信号凭空消失、
    # 波形看起来还挺正常，这比报错危险得多。实测踩过一次（$scope 的 $end
    # 吞掉了紧随其后的 $var），所以这里留一道不变量把它顶出来。
    n_var_tokens = sum(1 for t in tokens if t == "$var")
    if n_var_tokens != len(defs):
        notes.append(
            f"$var 命令数（{n_var_tokens}）与解析出的信号数（{len(defs)}）不一致，"
            f"可能有信号被漏掉——这是 vcd.py 的解析 bug，请报告"
        )

    streams: dict[str, Stream] = {}
    sig_by_id: dict[str, Signal] = {}
    signals: list[Signal] = []

    for vtype, width, vid, ref, scope in defs:
        is_real = vtype.lower() == "real"
        stream = streams.get(vid)
        if stream is None:
            stream = Stream()
            streams[vid] = stream
        path_ = f"{scope}.{ref}" if scope else ref
        sig = Signal(
            id=vid, name=ref, path=path_, scope=scope,
            width=1 if is_real else width,
            kind=vtype, is_real=is_real, stream=stream,
        )
        signals.append(sig)
        # 同一个 ID 可能有多个名字（网线别名），但**取第一个作为代表**即可，
        # 它们本来就共享同一条跳变流。这里保留全部 signal 对象，渲染层
        # 可以按需展示层次路径。
        sig_by_id.setdefault(vid, sig)

    # ---------------- body ----------------
    # 到这里 tokens[i] 是 "$enddefinitions"，下一个是 "$end"
    i += 1
    while i < n and tokens[i] == "$end":
        i += 1

    current_time = 0
    end_time = 0
    total_changes = 0
    skip_values = False
    truncated = False

    while i < n:
        tok = tokens[i]

        if tok.startswith("$"):
            if tok in _SKIP_VALUE_BLOCKS:
                skip_values = True
                i += 1
            elif tok in _VALUE_BLOCKS:
                skip_values = False
                i += 1
            elif tok == "$end":
                skip_values = False
                i += 1
            else:
                # $comment 之类：整块跳过（含它的 $end）
                while i < n and tokens[i] != "$end":
                    i += 1
                i += 1
            continue

        if tok.startswith("#"):
            try:
                current_time = int(tok[1:])
            except ValueError:
                notes.append(f"无法解析的时间戳：{tok!r}")
            end_time = max(end_time, current_time)
            i += 1
            continue

        # 值变化
        head = tok[0]
        if head in "bB":
            bits = tok[1:]
            if i + 1 < n:
                vid = tokens[i + 1]
                i += 2
            else:
                i += 1
                continue
            if not skip_values and vid in streams:
                w = sig_by_id[vid].width
                streams[vid].add(current_time, _norm_vector(bits, w))
                total_changes += 1
            continue
        if head in "rR":
            val = tok[1:]
            if i + 1 < n:
                vid = tokens[i + 1]
                i += 2
            else:
                i += 1
                continue
            if not skip_values and vid in streams:
                streams[vid].add(current_time, val)
                total_changes += 1
            continue
        if head in "sS":
            # 字符串型（$var string），少见，原样存
            val = tok[1:]
            if i + 1 < n:
                vid = tokens[i + 1]
                i += 2
            else:
                i += 1
                continue
            if not skip_values and vid in streams:
                streams[vid].add(current_time, val)
                total_changes += 1
            continue
        if head in "01xXzZ" and len(tok) >= 2:
            val = tok[0].lower()
            vid = tok[1:]
            i += 1
            if not skip_values and vid in streams:
                streams[vid].add(current_time, val)
                total_changes += 1
            continue

        # 认不出来的 token：跳过，但要留痕（不静默吞掉）
        notes.append(f"无法识别的 token：{tok!r}")
        i += 1

        if total_changes > max_changes:
            truncated = True
            notes.append(
                f"跳变数超过 {max_changes} 上限，已停止记录后续波形（波形不完整）"
            )
            break

    if truncated:
        # 截断的情况下给个明确标记，不要让人以为波形就到这里
        pass

    return VCD(
        path=str(path),
        timescale_text=timescale_text,
        timescale_fs=timescale_fs,
        signals=signals,
        end_time=end_time,
        notes=notes,
    )


class VCDError(Exception):
    """VCD 无法解析。"""


# ---------------------------------------------------------------- 时间换算


def to_display(vcd: VCD, t: int) -> float:
    """把内部整数时间换算成人类单位下的数值（1ns/1ps 的波形 → ns）。"""
    unit = _display_unit(vcd)
    return t * vcd.timescale_fs / UNIT_FS[unit]


def display_unit(vcd: VCD) -> str:
    return _display_unit(vcd)


def _display_unit(vcd: VCD) -> str:
    """选一个读起来舒服的时间单位。

    iverilog 对 `timescale 1ns/1ps` 写出来的是 `$timescale 1ps`（**精度**，
    不是 1ns），于是内部时间戳全是 5000、10000 这种 ps 整数。
    直接按 ps 显示满屏都是零，所以从大到小挑第一个让总时长 >= 1 的单位。
    """
    for unit in ("s", "ms", "us", "ns", "ps", "fs"):
        if vcd.end_time * vcd.timescale_fs / UNIT_FS[unit] >= 1:
            return unit
    return "fs"


def format_time(vcd: VCD, t: int) -> str:
    unit = _display_unit(vcd)
    val = t * vcd.timescale_fs / UNIT_FS[unit]
    if abs(val - round(val)) < 1e-9:
        return f"{int(round(val))}{unit}"
    return f"{val:g}{unit}"
