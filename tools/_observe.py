"""从仿真波形里抽出「确定性的、可以照着讲的」事实。

    from _observe import observe
    blk = observe(Path("build/x.vcd"), tests)

为什么要有这一层：

    agent 要能讲"你的 q2 早了一拍"，得先**知道** q2 在第几个时钟沿变了、
    变之前是什么。这个事实有两条获取途径，只有一条可靠：

    - **让模型去看波形图**（PNG / SVG）—— 它会编。图上看一眼像"第 3 拍"，
      模型就说第 3 拍，没有任何东西能拦住它说错，而且它说得非常自信。
    - **让 VCD 解析器把跳变时刻读出来** —— 数字来自文件。说错了是解析器
      有 bug，被测得出来。

    本模块只做第二条。**这里不推断、不猜测、不评价对错**，只报告从波形里
    量得到的量。拿不准的一律给 null 并附一条 note 说清为什么拿不准 ——
    「宁可说测不出来，也不给一个看着合理但是编的」。
    判断"这些数字说明什么"是上层的活，见 AGENT.md。

两个容易搞错的地方，都写在各自函数那里：

1. `tests[].time` 是**测试台自己格式化过的字符串**（`"26.000ns"`），
   单位由测试台的 `$timeformat` 决定，跟 VCD 的 `$timescale` 不一定一样。
   所以换算必须先解析字符串里的单位，不能拿 VCD 的显示单位去套。
2. 时钟是靠**跳变规律性**找的，不是靠信号名。叫 clk 的不一定是时钟，
   不叫 clk 的也可能是。所以结果里带 signal 名和 confidence，
   上层能核对；找不到就老实给 None。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import vcd as vcdmod
from vcd import UNIT_FS, VCD, Signal

# 时钟判定门槛。定得比"看上去像"严一些：
# 一个 4 位计数器的 q[0] 每拍都翻转，跳变也很规整，但它的固定间隔是
# **一个时钟周期**而不是半个（见 _find_clock 里的说明）。
_CLK_MIN_CHANGES = 8
_CLK_MIN_REGULARITY = 0.90

# 单次观察最多列多少个信号的细节。超了就截断并**明确标注**——
# 静默截断会让人以为"就这些信号"，比不列更危险。
DEFAULT_MAX_SIGNALS = 24

_TIME_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]+)\s*$")


# ============================================================ 时间换算

def _ticks_from_text(text: str | None, v: VCD) -> int | None:
    """把测试台打印的 `"26.000ns"` 换算成 VCD 内部的时间刻度。

    **单位要取字符串里写的那个，不能取 VCD 的显示单位。**
    两者可以不一样：测试台 `$timeformat(-6, ...)` 打的是 us，
    而 VCD 的 `$timescale 1ns` 又是另一回事。拿错单位会整体差 1000 倍，
    而且差得很安静——时间看着还是个"合理的数"。

    换算是精确整数运算：value(单位) × 该单位多少飞秒 ÷ 一个刻度多少飞秒。
    """
    if not text:
        return None
    m = _TIME_RE.match(text)
    if not m:
        return None
    unit = m.group(2).lower()
    if unit not in UNIT_FS:
        return None
    fs = float(m.group(1)) * UNIT_FS[unit]
    if v.timescale_fs <= 0:
        return None
    return int(round(fs / v.timescale_fs))


# ============================================================ 时钟识别

@dataclass
class Clock:
    signal: str
    toggle_interval_ticks: int      # 相邻跳变的固定间隔 = 半周期
    period_ticks: int               # 上升沿之间的间隔 = 一个周期
    rising_edges: list[int] = field(default_factory=list)
    regularity: float = 0.0

    def edges_at_or_before(self, t: int) -> int:
        """t 时刻之前（含）已经过去了几个上升沿。从 1 开始数。"""
        n = 0
        for e in self.rising_edges:
            if e <= t:
                n += 1
            else:
                break
        return n

    def last_edge_at_or_before(self, t: int) -> int | None:
        last = None
        for e in self.rising_edges:
            if e <= t:
                last = e
            else:
                break
        return last


def _modal_interval(times: list[int]) -> tuple[int, float]:
    """出现次数最多的相邻间隔，以及它占的比例。"""
    deltas = [b - a for a, b in zip(times, times[1:]) if b > a]
    if not deltas:
        return 0, 0.0
    val, n = Counter(deltas).most_common(1)[0]
    return val, n / len(deltas)


def _find_clock(signals: list[Signal]) -> Clock | None:
    """找时钟：只取 0/1、跳变够多、间隔够规整的那个，取间隔最小的。

    为什么要"间隔最小"：
        计数器的低位（q[0]）每个时钟沿都翻转，跳变间隔 = **一个整周期**；
        时钟本身的跳变间隔 = **半个周期**。两者都很规整，靠间隔区分，
        时钟的间隔永远是其中最小的。

    为什么要求只取 0/1：
        一个几位的总线在数值来回切换时也可能凑出规整的间隔，
        但它不是时钟。加上这一条能挡掉大部分误判。
        即便如此也不保证对——所以结果里带 confidence，交给上层核对。

    找不到就返回 None。**不要为了"总有结果"去放宽门槛**：
    纯组合逻辑的题目根本没有时钟，硬报一个"周期"会凭空造出一个概念。
    """
    best: tuple[tuple[float, int], Signal, int] | None = None
    for sig in signals:
        st = sig.stream
        if st.n_changes < _CLK_MIN_CHANGES:
            continue
        # 只取 0/1 且**两个值都出现过**：恒定高/低不是时钟，
        # 几位的总线在数值来回切换时也可能凑出规整间隔，一并挡掉
        if set(st.values) != {"0", "1"}:
            continue
        interval, regularity = _modal_interval(st.times)
        if interval <= 0 or regularity < _CLK_MIN_REGULARITY:
            continue
        # 先比规整度（越高越好），同样规整再比间隔（越小越像时钟）。
        # 存的就是**比较用的那个元组本身**，不要拆成若干字段再拼回去比：
        # 拆开之后，"拿什么比"和"拿什么存"就成了两处独立的写法，
        # 改一处忘另一处不会报错，只是悄悄按老的规则选。
        score = (regularity, -interval)
        if best is None or score > best[0]:
            best = (score, sig, interval)

    if best is None:
        return None

    (regularity, _neg), sig, interval = best
    rising = [t for t, val in zip(sig.stream.times, sig.stream.values) if val == "1"]
    # 兜底，**在当前规则下到不了**：值只可能是 0/1、且 Stream 会把连续相同的值
    # 去掉，所以跳变次数 ≥ 8 必然意味着上升沿 ≥ 4 个。
    # 留着是因为它挡的是"下面要取 rising_edges[0] 和算周期"这件事本身——
    # 哪天放宽了上面任何一条门槛，这里是唯一不让它崩的地方。
    # 也正因为到不了，它**没有对应的负例可验**（拆掉它，所有用例照样绿）。
    if len(rising) < 2:
        return None
    period, _ = _modal_interval(rising)
    if period <= 0:
        period = interval * 2
    return Clock(
        signal=sig.path,
        toggle_interval_ticks=interval,
        period_ticks=period,
        rising_edges=rising,
        regularity=round(regularity, 3),
    )


# ============================================================ 单信号事实

def _has_unknown(value: str) -> bool:
    return any(ch in value for ch in "xXzZ")


def _signal_facts(
    sig: Signal, v: VCD, clock: Clock | None, max_edges: int
) -> dict:
    st = sig.stream
    # t=0 那一条是 $dumpvars 写下的**初值**，不是一次"跳变"。
    # 实测：一条 assign 驱动的组合输出在波形里就只有 t=0 这一条记录。
    # 如果把它算进 changed，一个从头到尾没动过的信号会显示成"动过"——
    # 而"这个信号压根没变过"恰恰是最值得看的诊断之一，不能被这点差别盖掉。
    real_changes = [t for t in st.times if t > 0]

    facts: dict = {
        "path": sig.path,
        "scope": sig.scope,
        "width": sig.width,
        "n_changes": st.n_changes,
        "changed": bool(real_changes),
    }

    if st.n_changes == 0:
        facts["has_unknown"] = None
        facts["all_unknown"] = None
        facts["initial_value"] = None
    else:
        # format_value 是 VCD 的**方法**，不是 vcd 模块的函数，别写成 vcdmod.
        facts["initial_value"] = v.format_value(sig, st.values[0])
        facts["has_unknown"] = any(_has_unknown(x) for x in st.values)
        # 全程未知 ≈ 只声明了从来没被赋值过（或者复位根本没生效）
        facts["all_unknown"] = all(_has_unknown(x) for x in st.values)
        facts["first_change_text"] = vcdmod.format_time(v, st.times[0])
        facts["last_change_text"] = vcdmod.format_time(v, st.times[-1])

    # ---- 与时钟对齐的量 ----
    if clock is not None:
        # 这里刻意**不**给"每个时钟沿变化多少次"这类归一化比值。
        # 试过一版，分母取"上升沿个数"——而仿真窗口末端常常截在半周期中间，
        # 时钟自己的比值算出来是 1.75 而不是 2，纯粹是窗口造成的，
        # 却看着像个有意义的数。要比较"动得多少"直接用 n_changes 就够了。
        half = clock.period_ticks // 2
        glitches = [t for t, nxt in zip(st.times, st.times[1:]) if nxt - t < half]
        facts["n_glitches"] = len(glitches)
        if glitches:
            # 只在真有毛刺时给时刻，避免 JSON 里堆一串没用的 null
            facts["first_glitch_text"] = vcdmod.format_time(v, glitches[0])
            facts["glitch_note_zh"] = (
                f"有 {len(glitches)} 处脉冲短于半个时钟周期"
                f"（门槛 {vcdmod.format_time(v, half)}）——组合逻辑的竞争/冒险，"
                "也可能是测试台把激励直接施加在时钟沿上造成的"
            )

    # ---- 窗口内的跳变明细 ----
    # 给 agent 一串原始时刻，而不是"它早了一拍"这种结论。
    # 上限 max_edges：教学用的波形远到不了，到了也没人看得完。
    if st.n_changes:
        shown = list(zip(st.times, st.values))[:max_edges]
        facts["timeline"] = [
            {"time_text": vcdmod.format_time(v, t), "value": val}
            for t, val in shown
        ]
        if st.n_changes > max_edges:
            facts["timeline_truncated"] = True

    return facts


# ============================================================ 首次分歧

def _first_divergence(tests: list[dict], v: VCD, clock: Clock | None) -> dict | None:
    """最早那个失败用例的时间点，并把时间换算成"第几个时钟沿"。

    为什么是最早的那个：
        后面所有失败几乎都是第一个错误的**后果**。先修第一个，
        前面几条常常一起变绿。按 tests 里的顺序报会让人从症状下手。

    同一时刻有多个失败时取 tests 里靠前的那个（测试台的书写顺序）。
    """
    fails = [t for t in tests if t.get("status") == "fail"]
    if not fails:
        return None

    def when(t: dict) -> int:
        ticks = _ticks_from_text(t.get("time"), v)
        # 时间解析不出来的排到最后，但仍然会被报出来（它的 expected/got 有用）
        return ticks if ticks is not None else 1 << 62

    first = min(fails, key=when)
    ticks = _ticks_from_text(first.get("time"), v)

    out: dict = {
        "case": first.get("name"),
        "time_text": first.get("time"),
        "expected": first.get("expected"),
        "got": first.get("got"),
        "signal": first.get("signal"),
        "n_failed": len(fails),
        "failed_cases": [t.get("name") for t in fails],
    }
    if ticks is None:
        out["note_zh"] = (
            f"测试台打印的时刻 {first.get('time')!r} 解析不出单位，"
            "无法换算成时钟沿（时钟沿相关字段全部为 null）"
        )
        out["edge_index"] = None
        out["edge_time_text"] = None
        out["offset_from_edge_text"] = None
        return out

    out["time_ticks"] = ticks
    if clock is None:
        out["edge_index"] = None
        out["edge_time_text"] = None
        out["offset_from_edge_text"] = None
        out["note_zh"] = "没找到时钟信号，无法把时刻换算成时钟沿"
        return out

    out["edge_index"] = clock.edges_at_or_before(ticks)
    edge = clock.last_edge_at_or_before(ticks)
    if edge is None:
        out["edge_time_text"] = None
        out["offset_from_edge_text"] = None
        out["note_zh"] = "这次判定发生在第一个时钟上升沿之前"
        return out
    out["edge_time_text"] = vcdmod.format_time(v, edge)
    out["offset_from_edge_text"] = vcdmod.format_time(v, ticks - edge)
    return out


# ============================================================ 主体

def _representatives(v: VCD) -> list[Signal]:
    """按跳变流去重。

    一个 ID 可能挂着多个名字（测试台的 tb.clk 和它驱动的 dut.clk 在 VCD 里
    是同一个 ID）。它们本来就同步变化，列两遍只是噪声。
    保留**第一个出现**的名字（iverilog 先写顶层作用域，所以通常是测试台那侧，
    对学习者更眼熟）。
    """
    seen: set[int] = set()
    out: list[Signal] = []
    for s in v.signals:
        if id(s.stream) in seen:
            continue
        seen.add(id(s.stream))
        out.append(s)
    return out


def _priority(f: dict) -> tuple:
    """排序键：越"值得看"的排越前。

    顺序是有讲究的，不是随便排：
      1. 全程未知（all_unknown）—— 最高优先级。它意味着信号从来没被赋值过，
         测试台却可能在别的信号上判定通过，只看失败列表完全发现不了。
      2. 一次都没动过 —— 输出恒定，通常是没接上或条件永远不成立。
      3. 有毛刺 —— 组合逻辑的竞争冒险，仿真波形上最容易被忽略的一类。
      4. 其余按跳变次数降序：动得越多越有信息量。
    """
    if f.get("all_unknown"):
        rank = 0
    elif not f.get("changed"):
        rank = 1
    elif f.get("n_glitches"):
        rank = 2
    else:
        rank = 3
    return (rank, -f.get("n_changes", 0))


def observe(
    vcd_path: Path,
    tests: list[dict] | None = None,
    *,
    max_signals: int = DEFAULT_MAX_SIGNALS,
    max_edges_per_signal: int = 12,
) -> dict:
    """读一个 VCD，返回结构化的观察块。

    任何失败都以 `ok: False` + `reason` 返回，**不抛异常**：
    观察不该能把判定结果带崩 —— 仿真和判定已经跑完了，
    波形读不出来只是少了一层辅助信息。
    """
    tests = tests or []
    try:
        v = vcdmod.parse(Path(vcd_path))
    except Exception as e:                      # noqa: BLE001 —— 见上面的说明
        return {"ok": False, "reason": f"波形解析失败：{e}", "vcd": str(vcd_path)}

    # 一条信号定义都没有，说明这根本不是个能用的 VCD（文件坏了、被截断了、
    # 或者测试台只调了 $dumpfile 却忘了 $dumpvars）。
    #
    # 这里必须是 ok=False，不能返回一个 n_signals=0 的"成功"结果：
    # 后者读起来是「波形读到了，里面没有信号」——一句听起来很像事实、
    # 实际什么都没验过的话。观察层的全部价值就在于数字是量出来的，
    # 一旦允许它在"其实什么都没读到"时报成功，这个保证就没了。
    #
    # （vcd.parse 对这种情况不报错，那是它作为渲染用的解析器该有的宽容，
    #   所以这道判断放在这里，而不是去改 vcd.py 的行为、连累 wave.py。）
    if not v.signals:
        return {
            "ok": False,
            "vcd": str(vcd_path),
            "reason": "波形里一条信号定义都没有（$var 零条）。"
                      "常见原因：文件不是 VCD / 被截断了，"
                      "或测试台只写了 $dumpfile 而没写 $dumpvars",
            "notes": list(v.notes),
        }

    reps = _representatives(v)
    clock = _find_clock(reps)

    facts = [_signal_facts(s, v, clock, max_edges_per_signal) for s in reps]

    # 同一根线在测试台和待测模块两侧**各有一份 VCD 记录**，这在 iverilog 里
    # 不一定合并成一个 ID（实测：1 位的端口会合并，带 [3:0] 范围的不会）。
    # 于是 signals 里会出现两条同名同值的记录。
    #
    # 这里**只标注、不合并**：合并要靠"值和时刻完全一致"来判定，
    # 而两个碰巧同步的独立信号（比如两个初值相同的计数器）会被错并成一条——
    # 那比多列一条记录坏得多（同 _lint.py 的「宁可漏报，不可冤枉」）。
    by_name: dict[str, list[str]] = {}
    for s in reps:
        by_name.setdefault(s.name, []).append(s.path)
    for s, f in zip(reps, facts):
        others = [p for p in by_name.get(s.name, []) if p != s.path]
        if others:
            f["also_in"] = others

    facts.sort(key=_priority)
    shown = facts[:max_signals]

    notes = list(v.notes)
    if clock is None:
        notes.append(
            f"没找到稳定的时钟信号（门槛：只取 0/1、跳变 ≥ {_CLK_MIN_CHANGES} 次、"
            f"跳变间隔 {int(_CLK_MIN_REGULARITY * 100)}% 以上一致）。"
            "纯组合逻辑本来就没有时钟；有时钟却没认出来，是 _observe.py 的问题，请报告"
        )
    if len(facts) > max_signals:
        # 静默截断会让人以为"信号就这些"，所以明说
        notes.append(
            f"信号共 {len(facts)} 个，按「最值得看」的次序只列了前 {max_signals} 个"
        )

    unit = vcdmod.display_unit(v)
    blk: dict = {
        "ok": True,
        "vcd": str(vcd_path),
        "unit": unit,
        "end_time_text": vcdmod.format_time(v, v.end_time),
        "n_signals": len(facts),
        # 作用域清单（不去猜哪个是待测模块 —— 见 AGENT.md 的说明）
        "scopes": [
            {"name": scope, "n_signals": len(sigs), "is_top": i == 0}
            for i, (scope, sigs) in enumerate(v.groups().items())
        ],
        "clock": None,
        "first_divergence": _first_divergence(tests, v, clock),
        "signals": shown,
        "signals_truncated": len(facts) > max_signals,
        "notes": notes,
    }

    if clock is not None:
        blk["clock"] = {
            "signal": clock.signal,
            "period_ticks": clock.period_ticks,
            "period_text": vcdmod.format_time(v, clock.period_ticks),
            "toggle_interval_text": vcdmod.format_time(v, clock.toggle_interval_ticks),
            "n_rising_edges": clock.edges_at_or_before(v.end_time),
            "first_edge_text": vcdmod.format_time(v, clock.rising_edges[0]),
            "regularity": clock.regularity,
            # 给 agent 的提醒：这是**推断**出来的，不是声明出来的
            "detected_by": "跳变规律性（非信号名）",
        }
    blk["note_zh"] = (
        "本块只含从波形里量得到的事实，不含任何对错判断。"
        "时钟是推断的（见 clock.detected_by），信号作用域未做待测模块猜测。"
    )
    return blk
