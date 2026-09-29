"""逐位翻转计数：把状态寄存器「一共翻了多少位」从波形里数出来。

    python tools/toggle.py build/cnt_bin.vcd --signal st
    python tools/toggle.py build/b.txt.vcd build/g.txt.vcd --signal st \\
        --label 二进制 --label 格雷

**这个脚本存在的理由**：状态编码的代价分两半，而 `synth.py` 只能看见一半。

- **面积那一半**：状态寄存器几个触发器、次态逻辑多少门 → `sizer` 给得出。
- **翻转那一半（动态功耗）**：状态每变一次，寄存器里翻几位 → **综合器量不出来**。

第二条不像看起来的那么显然：yosys 之类的综合工具**不做活动性分析**，
它不知道哪些跳变是"热的"。所以哪怕装上 yosys，它也只能告诉你
"二进制和格雷都是 2 个触发器"，而说不出"格雷每次只翻 1 位、二进制平均翻 2 位"。
那件事只有仿真知道 —— 就是本脚本。

## 三个必须分开报的数（同 `lint.ran` / `n_warnings` 的教训）

数翻转最容易犯的错，是把三个不同的东西叫成"翻转次数"：

| 字段 | 是什么 | 用途 |
|---|---|---|
| `n_changes` | 波形里这个信号**值变了**几次（含 x/z） | 事件数 |
| `n_defined_changes` | 变化前后**都是 0/1** 的次数 | 真正的状态转换次数 |
| `total_bit_toggles` | **按位求和**：一共翻了几「位次」 | **功耗看这个** |
| `n_x_bit_edges` | 涉及 x/z、**被排除**的位次 | 复位瞬间的污染量 |

「状态变了多少次」和「一共翻了多少位」是两回事：状态机从 01 跳到 10，
**一次**变化、**两位**翻转。只看 `n_changes` 会得出「二进制和格雷一样」——
它们的状态转换次数确实一样（同一套激励），差的是每次翻几位。

`n_x_bit_edges` 必须单列而**不是并进 `total`**：复位释放时寄存器从 x 变成 0，
按"位变了就算翻"会把整条寄存器算成翻了一遍，一个纯粹的仿真伪影
也会被算成功耗。本脚本**只数 0↔1**，x/z 的一律排除并如实报出排除了多少 ——
这样从 0 时刻开始计时也不会得到一个虚高的数。

## 两个输入（`--signal` 的两种给法）

- **同一个信号**（默认）：各份波形数的是各自的 `st`。
- 编码不同时寄存器名字常常一样（都是 `st`），正好。

## 唯一能挡住"拿两份不同激励硬比"的东西：可比性

**翻转数完全由激励决定**，它是激励的属性，不是设计的属性。
换一套激励，排名就可能翻过来 —— 这是本脚本最容易被误用的地方。
所以多份输入时，本脚本会做一个**与编码无关**的硬校验：

> 同一套激励下，状态寄存器**在哪些时刻变**与编码方式无关
> （编码是状态到码字的一一映射，状态序列一样 ⇒ 变化的时刻序列一样）。

于是：**各份波形的变化时刻列表必须逐项相等**，才认为这组比较成立。
不相等 → `comparable: false`，报出第一处对不上的时刻，并**拒绝给结论**
（退出码非 0）。这不是"算不出来"，是"算出来也没有意义"。

反过来说：**逐项相等却得到相同的翻转数，那是真结论，不是工具坏了。**
某些状态图下二进制和格雷本来就打平（见 `tests/_smoke/cnt_enc.v` 的说明），
本脚本会把它如实报成"无差异"，而不是判失败。

## 判定与这里无关

和 `sim.py` 的 `observe` / `cover` 块一样，`affects_verdict` 恒为 False。
本脚本是**感知层**：只报量出来的事实（每个数都来自 VCD），不下"这个设计对不对"
的判断，也不翻任何判定。它也不负责跑仿真 —— 它读 `sim.py` 已经产出的波形，
不重跑、不编译，和 `wave.py` / `vcd.py` 是同一个位置。

## 它不是什么

- **不是功耗报告。** 翻转只是动态功耗 P ∝ C·V²·f 里的 f（活动因子）。
  电容 C 里差异很大 —— 独热用 4 个触发器、二进制 2 个，**同样翻 1 位，
  独热的代价也是两倍**（多出来那几位即使不翻，漏电和时钟树也在那儿）。
  所以本脚本只能告诉你"翻了多少位"，不能告诉你"省了多少电"。
- **不是编码好坏的普遍结论。** 只对这一套激励成立。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import vcd as vcdmod                # noqa: E402
from _console import init_console   # noqa: E402
from _toolchain import repo_root    # noqa: E402

SCHEMA = "vlearn.toggle/1"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 64

# 时间窗的三种写法都收：带单位（100ns）、`#` 开头的裸刻度（#5000）、纯数字。
_TIME_UNITS = vcdmod.UNIT_FS


class _TimeError(ValueError):
    pass


def parse_time(text: str, timescale_fs: int) -> int:
    """把 `100ns` / `#5000` / `5000` 换算成 VCD 内部刻度。

    VCD 里的时间戳是**以 $timescale 为单位的整数**（见 `vcd.py` 头部第 2 点），
    而人嘴里说的总是 `100ns`。两者之间必须显式换算 —— 直接拿 100 当刻度用，
    在 1ps 时间精度下会把 100ns 当成 0.1ns，窗口整个错位而**没有任何报错**。
    `#` 前缀是"这就是内部刻度"的显式写法，给需要对波形工具核对时用。
    """
    s = text.strip()
    if s.startswith("#"):
        s = s[1:]
        try:
            return int(s)
        except ValueError as e:
            raise _TimeError(f"`{text}` 不是合法的刻度数") from e

    low = s.lower()
    for unit, fs in _TIME_UNITS.items():
        if low.endswith(unit):
            num = low[: -len(unit)].strip()
            try:
                val = float(num)
            except ValueError as e:
                raise _TimeError(f"`{text}` 里的数字看不懂") from e
            return int(round(val * fs / timescale_fs))
    # 裸数字：按内部刻度解释（并已在 --help / 文档里写明）
    try:
        return int(s)
    except ValueError as e:
        raise _TimeError(
            f"`{text}` 看不懂。带单位的写 `100ns`，要直接给内部刻度就写 `#5000`"
        ) from e


# ---------------------------------------------------------------- 计数


def count_signal(sig: "vcdmod.Signal", t0: int | None, t1: int | None) -> dict:
    """数一个信号在时间窗内的逐位翻转。

    ★ 为什么不是"先取窗口内的变化，再两两比较"：
      那样窗口里**第一次**变化没有前一个值可比，会被默默丢掉一格。
      这里从头遍历、只筛时间，前一格永远拿得到 —— 窗口边界处的那个翻转
      （往往正是复位后第一次状态转换）才不会被吞掉。
    """
    st = sig.stream
    width = sig.width

    bits = [0] * width
    n_changes = 0
    n_defined_changes = 0
    n_x_bit_edges = 0
    total = 0
    first_t: int | None = None
    last_t: int | None = None
    times_in_window: list[int] = []

    for i, t in enumerate(st.times):
        if t0 is not None and t < t0:
            continue
        if t1 is not None:
            if t > t1:
                break
        prev = st.values[i - 1] if i > 0 else None
        if prev is None:
            # 窗口之前一次都没记录过 —— 没有"前一个值"，不算一次变化。
            # 这不是数据缺失，是信号本来就在这个时刻才开始有值。
            continue

        times_in_window.append(t)
        n_changes += 1
        if first_t is None:
            first_t = t
        last_t = t

        cur = st.values[i]
        defined = True
        for b in range(width):
            # vcd.py 已把值补位到 width，两个串等长。
            # 字符串下标 0 是**最高位**，所以 bit index = width - 1 - b。
            a_ch, c_ch = prev[b], cur[b]
            if a_ch == c_ch:
                continue
            if a_ch in "xz" or c_ch in "xz":
                n_x_bit_edges += 1
                defined = False
            else:
                bits[width - 1 - b] += 1
                total += 1
        if defined:
            n_defined_changes += 1

    return {
        "signal": sig.path,
        "name": sig.name,
        "scope": sig.scope,
        "width": width,
        # 高位在前，和 Verilog 的 `[width-1:0]` 一致，读起来能直接对上代码
        "bit_toggles": bits,
        "n_changes": n_changes,
        "n_defined_changes": n_defined_changes,
        "total_bit_toggles": total,
        "n_x_bit_edges": n_x_bit_edges,
        # 每换一次状态翻几位 —— 编码质量的核心指标。
        # 分母用 n_defined_changes 而不是 n_changes：复位那一下翻了几位
        # 不是这台机器工作时的代价，混进来会把比值拉偏。
        "bits_per_change": (round(total / n_defined_changes, 3)
                            if n_defined_changes else None),
        # 窗口只记内部刻度。给人看的 `100ns` 文案要 VCD 的 timescale 才算得出来，
        # 而 count_signal 手上只有 Signal —— 由 _fill_window_text 补。
        # （不在这里塞个全局的 dump 引用：计数核心不该依赖"必须先 parse 过"。）
        "window": {"t0": t0, "t1": t1},
        "first_change_t": first_t,
        "last_change_t": last_t,
        "change_times": times_in_window,
    }


def _fill_window_text(run: dict, dump: "vcdmod.VCD") -> None:
    w = run["window"]
    w["from_text"] = (vcdmod.format_time(dump, w["t0"])
                      if w["t0"] is not None else None)
    w["to_text"] = (vcdmod.format_time(dump, w["t1"])
                    if w["t1"] is not None else None)
    w["unit"] = vcdmod.display_unit(dump)
    w["whole_file"] = w["t0"] is None and w["t1"] is None


def _first_divergence(a: list[int], b: list[int]) -> dict:
    """两份变化时刻列表第一处对不上的位置。"""
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return {"index": i, "a": a[i], "b": b[i]}
    if len(a) != len(b):
        longer = "a" if len(a) > len(b) else "b"
        idx = min(len(a), len(b))
        val = (a if longer == "a" else b)[idx]
        return {"index": idx, "a": val if longer == "a" else None,
                "b": val if longer == "b" else None, "only_in": longer}
    return {}


# ---------------------------------------------------------------- 输入


def _resolve_vcd(p: Path, root: Path) -> tuple[Path | None, str | None]:
    """输入可以是 .vcd，也可以是 sim.py 的结果 JSON（取它的 artifacts.vcd）。

    仿真产物路径在 JSON 里是**相对仓库根**记的（`build/xxx/x.vcd`），
    所以这里要按 root 解析一次 —— 直接当相对当前目录用，换个 cwd 就找不到。
    """
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as e:
            return None, f"{p} 读不出 JSON：{e}"
        if not isinstance(data, dict):
            return None, f"{p} 顶层不是对象，不是 sim.py 的结果 JSON"
        v = (data.get("artifacts") or {}).get("vcd")
        if not v:
            return None, (f"{p} 的 artifacts.vcd 是空的 —— 那一次仿真没产生波形。"
                          "测试台里要有 $dumpfile + $dumpvars，"
                          "并且 sim.py 不能带 --no-vcd。")
        vp = Path(v)
        if not vp.is_absolute():
            vp = root / vp
        if not vp.is_file():
            return None, f"{p} 记的波形不存在：{vp}（结果 JSON 和波形要对得上）"
        return vp, None

    if not p.is_file():
        return None, f"找不到输入文件：{p}"
    return p, None


# ---------------------------------------------------------------- 比较


def compare(runs: list[dict]) -> dict:
    """多份波形横向比。核心是**先验可比性，再谈结论**。

    可比性的硬判据：状态寄存器的**变化时刻列表逐项相等**。
    理由见文件头 —— 编码是状态到码字的一一映射，同一套激励下
    状态序列一样，变化的时刻序列就与编码无关。
    """
    if len(runs) < 2:
        return {}

    base = runs[0]
    base_times = base["change_times"]
    others = []

    for r in runs[1:]:
        d = _first_divergence(base_times, r["change_times"])
        others.append({
            "name": r["name"],
            "total_bit_toggles": r["total_bit_toggles"],
            "bits_per_change": r["bits_per_change"],
            "same_change_times": not d,
            "divergence": d or None,
            # 相对基准的比值：<1 就是比基准翻得少。
            # 只在可比时才给 —— 拿两份不同激励算出来的比值是纯粹的误导。
            "ratio_vs_base": (round(r["total_bit_toggles"] / base["total_bit_toggles"], 3)
                              if not d and base["total_bit_toggles"] else None),
        })

    comparable = all(o["same_change_times"] for o in others)
    diverged = [o for o in others if not o["same_change_times"]]
    # ★ 两个计数分开报（同 lint.ran / n_warnings 的教训）：
    #   `n_runs` 是"给了几份波形"，`n_runs_comparable` 是"其中几份和基准
    #   跑的是同一套激励"。只报前者，一份对不上的波形就会被当成有效对比。
    n_comparable = 1 + sum(1 for o in others if o["same_change_times"])

    totals = [r["total_bit_toggles"] for r in runs]
    all_zero = all(t == 0 for t in totals)
    no_diff = len(set(totals)) == 1

    # 同名同大小同哈希 = 拿同一个文件跟自己比。此时"变化时刻逐项相等"**必然**
    # 成立（可比性检查自己抓不到这种情况），而结论毫无意义。
    # 更常见、也更危险的一种：`--param` 没接上，两次仿真产出**字节相同**的波形 ——
    # 于是报出"打平"这个看着挺像结论的假结论。所以它必须判失败。
    same_file = all(r.get("vcd_digest") == base.get("vcd_digest") for r in runs[1:])

    # 全 0 是工具没做成事（和 regress "扫了一维毫无差异" 同一类：
    # 看着绿，其实没在工作）。**有差异却是 0 不是失败** —— 打平是真结论。
    if not comparable:
        status, ok = "not_comparable", False
    elif same_file:
        status, ok = "identical_inputs", False
    elif all_zero:
        status, ok = "no_toggles", False
    else:
        status, ok = "toggle_ok", True

    return {
        "ok": ok,
        "status": status,
        "comparable": comparable,
        "n_runs": len(runs),
        "n_runs_comparable": n_comparable,
        "baseline": base["name"],
        "all_same_total": no_diff,
        "identical_inputs": same_file,
        "runs": others,
        "diverged": diverged,
        "spread": (max(totals) - min(totals)) if not all_zero else 0,
        "best": (min(runs, key=lambda r: r["total_bit_toggles"])["name"]
                 if not all_zero else None),
        "worst": (max(runs, key=lambda r: r["total_bit_toggles"])["name"]
                  if not all_zero else None),
    }


# ---------------------------------------------------------------- 输出


def _print_summary(rep: dict) -> None:
    """人类可读摘要。**走 stderr**，和 sim.py / cov.py / synth.py 同一个约定。"""
    w = sys.stderr.write

    # 错误分支放在最前面：输入有问题时**没有表格可打**，
    # 硬走下面的表格化代码会因为缺键抛 KeyError —— 那会把"输入有问题"
    # 显示成"工具崩了"，正好盖住真正要传达的那句话。
    if rep.get("error"):
        w("=" * 60 + "\n")
        w(f"逐位翻转计数未执行：{rep.get('status')}\n")
        w("-" * 60 + "\n")
        w(f"  ✗ {(rep['error'] or {}).get('message_zh')}\n")
        w("=" * 60 + "\n")
        return

    w("=" * 60 + "\n")
    w(f"逐位翻转计数：{rep['n_runs']} 份波形，信号 `{rep['signal_query']}`\n")
    w("-" * 60 + "\n")
    w(f"  {'波形':<24}{'位宽':>5}{'变化':>6}{'翻位':>7}{'位/次':>8}   逐位\n")
    for r in rep["runs"]:
        bpc = r["bits_per_change"]
        bits = " ".join(str(b) for b in r["bit_toggles"])
        w(f"  {r['name']:<24}{r['width']:>5}{r['n_changes']:>6}"
          f"{r['total_bit_toggles']:>7}{(f'{bpc:.2f}' if bpc is not None else '—'):>8}"
          f"   [{bits}]\n")
    w("      （逐位那列高位在前，和代码里的 `[width-1:0]` 对得上）\n")

    if rep["n_x_bit_edges_any"]:
        w("-" * 60 + "\n")
        w("  ★ 这些位次涉及 x/z、**没有计进翻位数**：\n")
        for r in rep["runs"]:
            if r["n_x_bit_edges"]:
                w(f"    · {r['name']:<22}排除了 {r['n_x_bit_edges']} 个位次"
                  f"（复位释放时寄存器从 x 变到确定值，"
                  f"算进去会把整条寄存器算成翻了一遍）\n")

    for r in rep["runs"]:
        if not r["window"]["whole_file"]:
            w(f"  窗口 {r['name']}："
              f"{r['window']['from_text'] or '波形开头'} → "
              f"{r['window']['to_text'] or '波形结尾'}\n")

    cmp_ = rep.get("comparison") or {}
    if cmp_:
        w("-" * 60 + "\n")
        if not cmp_["comparable"]:
            w("  ★ 这组**不可比**，不给结论（退出码非 0）：\n")
            for d in cmp_["diverged"]:
                dv = d["divergence"] or {}
                w(f"    · {d['name']} 和基准 {cmp_['baseline']} 在第 "
                  f"{dv.get('index')} 处对不上："
                  f"{dv.get('a')} vs {dv.get('b')}\n")
            w("      状态寄存器的**变化时刻**与编码方式无关 —— 对不上就说明\n"
              "      这几份跑的不是同一套激励，翻转数没有可比性。\n"
              "      换一套同样的激励重跑，别拿两个不同 workload 硬比。\n")
        elif cmp_["identical_inputs"]:
            w("  ★ 这几份是**同一个波形文件**（内容哈希相同）—— 拿它跟自己比，\n"
              "    结果必然相同，不构成任何结论。\n")
        elif cmp_["all_same_total"] and rep["runs"][0]["total_bit_toggles"]:
            w(f"  这几份的翻位数**完全相同**（都是 "
              f"{rep['runs'][0]['total_bit_toggles']} 位）—— 这是真结论，不是工具坏了。\n")
        elif cmp_["all_same_total"]:
            # 全是 0：这**不是**"打平"，是压根没数到东西。必须和打平分得清 ——
            # 上面那句"打平是真结论"套在 0 上，会把"工具没在工作"说成结论。
            w(f"  两份都是 0 位 —— 这个窗口里 `{rep['signal_query']}` 一次都没变过。\n"
              f"      这不是「打平」，是**没数到翻转**：先确认窗口盖住了活动区。\n")
        else:
            w(f"  {cmp_['n_runs_comparable']}/{cmp_['n_runs']} 份与基准"
              f" {cmp_['baseline']} 跑的是同一套激励（变化时刻逐项相等）\n")
            w(f"  翻位数（相对基准 {cmp_['baseline']}）：\n")
            for d in cmp_["runs"]:
                if d.get("ratio_vs_base") is None:
                    w(f"    · {d['name']:<22}不可比\n")
                    continue
                pct = (1 - d["ratio_vs_base"]) * 100
                arrow = "省" if pct > 0 else ("多" if pct < 0 else "平")
                w(f"    · {d['name']:<22}{d['total_bit_toggles']:>5} 位"
                  f"（{d['bits_per_change']:.2f} 位/次）"
                  f"  相对基准{arrow} {abs(pct):.0f}%\n")
            w(f"  最少的是 {cmp_['best']}，最多的是 {cmp_['worst']}\n")

    w("-" * 60 + "\n")
    if rep["status"] == "not_comparable":
        w("\n结果：不可比，未给结论 ✗\n")
    elif rep["status"] == "identical_inputs":
        w("\n结果：几份输入其实是**同一个波形**，比较不成立 ✗\n")
    elif rep["status"] == "no_toggles":
        w("\n结果：所有波形里这个信号**一次都没翻过** ✗\n"
          "      （工具被叫来数翻转，却没有翻转可数 —— 和「比出来打平」是两回事）\n")
    else:
        w(f"\n结果：{len(rep['runs'])} 份波形都数出来了 ✓\n"
          f"      翻位数**完全由激励决定**，换一套激励排名可能就变 —— \n"
          f"      它说明的是「在这套激励下谁翻得少」，不是「谁更好」。\n")
    w("=" * 60 + "\n")


def _next_actions(rep: dict) -> list[str]:
    out: list[str] = []
    if rep["status"] == "not_comparable":
        out.append("先统一激励再比：几份波形必须是**同一套测试台、同一组输入**跑出来的，"
                   "只有状态编码不同。现在状态寄存器的变化时刻对不上，"
                   "说明它们跑的根本不是同一件事。")
    if rep["status"] == "no_toggles":
        out.append("这个信号一次都没翻：确认 --signal 选中的是**状态寄存器**"
                   "（不是复位后就不动的某个寄存器），以及仿真确实跑到了状态转换。")
    if rep["status"] == "identical_inputs":
        out.append("几份波形的**内容完全相同**，说明它们跑的根本是同一个东西。"
                   "常见原因有两个：(1) 后一次仿真把前一次的波形覆盖了 —— "
                   "每次仿真给不同的 --workdir / $dumpfile；"
                   "(2) 本该区分两份实现的开关（--param / --define）没接上，"
                   "两次跑出来是同一份电路。")
    if rep.get("n_x_bit_edges_any"):
        out.append("有 x/z 跳变被排除。要在复位之后再开始计数就用 `--from`，"
                   "例如 `--from 100ns`；不排除的话复位那一下会把结果撑高。")
    if not out:
        out.append("翻位数要和 `python tools/synth.py` 的触发器/门数**一起看**："
                   "面积和翻转是状态编码的两半代价，只看一个会得出片面的结论。")
    return out


def _emit(rep: dict, args) -> None:
    if not args.quiet:
        _print_summary(rep)
    text = json.dumps(rep, ensure_ascii=False, indent=2 if args.pretty else None)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text + "\n", encoding="utf-8")
    sys.stdout.write(text + "\n")


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    init_console()
    root = repo_root()

    ap = argparse.ArgumentParser(
        prog="toggle.py",
        description="从波形里数状态寄存器的逐位翻转（动态功耗的代理量；"
                    "综合器量不出这个，只有仿真知道）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("## 三个必须分开报的数", 1)[0].split("\n", 1)[1].strip(),
    )
    ap.add_argument("inputs", nargs="+", type=Path,
                    help="VCD 文件，或 sim.py 的结果 JSON（取其中记的波形）")
    ap.add_argument("--signal", required=True,
                    help="要数的信号（状态寄存器）。可以写 `st` 或 `dut.st`，"
                         "按子串/末尾匹配")
    ap.add_argument("--from", dest="t_from", metavar="T",
                    help="计数起点，如 `100ns` 或 `#5000`（裸数字按内部刻度解释）")
    ap.add_argument("--to", dest="t_to", metavar="T", help="计数终点，写法同 --from")
    ap.add_argument("--label", action="append", default=[], metavar="NAME",
                    help="给对应位置的输入起个可读名字（可重复，按顺序对应）")
    ap.add_argument("--json", type=Path, help="把 JSON 结果同时写入该文件")
    ap.add_argument("--pretty", action="store_true", help="JSON 缩进输出")
    ap.add_argument("--quiet", action="store_true", help="不输出人类可读摘要")
    args = ap.parse_args(argv)

    def bail(kind: str, msg: str, actions: list[str]) -> int:
        # n_runs / n_x_bit_edges_any 在这里必须给上：摘要函数要先打印表头再打印
        # 错误，缺了这两个键会变成 KeyError —— 于是"输入有问题"被显示成
        # "工具崩了"（带一整片 traceback），正好盖住真正要传达的那句话。
        # 同 cov.py 里那个 `if rep.get("error"):` 早退的分支，是同一个教训。
        rep = {"schema": SCHEMA, "ok": False, "status": kind,
               "signal_query": args.signal,
               "n_runs": 0,
               "n_x_bit_edges_any": False,
               "error": {"kind": kind, "message_zh": msg},
               "affects_verdict": False,
               "next_actions_zh": actions}
        _emit(rep, args)
        return EXIT_USAGE

    if len(args.label) > len(args.inputs):
        return bail("bad_label", f"--label 给了 {len(args.label)} 个，"
                                 f"输入只有 {len(args.inputs)} 个。",
                    ["一个 --label 对应一个输入，按顺序排。"])

    runs: list[dict] = []
    x_any = False

    for i, p in enumerate(args.inputs):
        vp, why = _resolve_vcd(p, root)
        if why:
            return bail("bad_input", why,
                        ["输入要么是 .vcd，要么是 sim.py 的 --json 结果。"
                         "要产生波形：测试台里写 $dumpfile + $dumpvars，"
                         "然后用 python tools/sim.py 跑一遍。"])
        try:
            dump = vcdmod.parse(vp)
        except vcdmod.VCDError as e:
            return bail("vcd_error", f"{vp} 解析失败：{e}",
                        ["确认这是 iverilog 产出的 VCD，且文件没被截断。"])

        hits = dump.find(args.signal)
        if not hits:
            names = "、".join(s.path for s in dump.signals[:8]) or "（一个信号都没有）"
            return bail("no_signal",
                        f"{vp} 里找不到信号 `{args.signal}`。"
                        f"波形里有：{names}",
                        ["状态寄存器常常在 dut 里，试试 `--signal dut."
                         + args.signal + "`。"])

        # 一个模式命中多个信号是常态：`st` 会同时命中 `dut.st` 和 `dut.st_raw`
        # （子串匹配），设计里也可能有好几个同名寄存器。
        # **取一个，但如实报出还有几个** —— 静默挑一个会让人拿错信号数了都不知道。
        # 挑的时候按 (路径长度, 路径) 排：短路径优先 = 最靠外的那个，
        # 顺序是确定的、可解释的，不依赖 VCD 里 $var 的书写顺序。
        sig = sorted(hits, key=lambda s: (len(s.path), s.path))[0]

        try:
            t0 = parse_time(args.t_from, dump.timescale_fs) if args.t_from else None
            t1 = parse_time(args.t_to, dump.timescale_fs) if args.t_to else None
        except _TimeError as e:
            return bail("bad_time", str(e),
                        ["时间写成 `100ns` / `1us`，或 `#5000` 表示内部刻度。"])

        run = count_signal(sig, t0, t1)
        _fill_window_text(run, dump)
        run["name"] = (args.label[i] if i < len(args.label) else vp.stem)
        run["origin"] = p.as_posix()
        run["vcd"] = vp.as_posix()
        run["vcd_digest"] = hashlib.sha256(vp.read_bytes()).hexdigest()[:16]
        run["n_matched_signals"] = len(hits)
        run["matched_signals"] = [s.path for s in hits[:8]]
        run["vcd_notes"] = dump.notes
        runs.append(run)
        if run["n_x_bit_edges"]:
            x_any = True

    # 先做比较（要用 change_times），再把原料剔掉：大波形下它能有几十万项，
    # 而对外只需要"是否相等 / 第一处对不上在哪"这个结论。
    comparison = compare(runs)
    for r in runs:
        r.pop("change_times", None)

    rep = {
        "schema": SCHEMA,
        "ok": True,
        "status": "toggle_ok",
        "handler": "toggle.py（逐位翻转计数，数据来自 VCD）",
        "signal_query": args.signal,
        "n_runs": len(runs),
        "n_x_bit_edges_any": x_any,
        # ★ 和 observe / cover 块同一个口径：这是感知层，
        #   永远不许有机会翻转 sim.py 的判定。
        "affects_verdict": False,
        "runs": runs,
        "comparison": comparison,
        "limits_zh": [
            "翻转数**完全由激励决定**：它是这次仿真的属性，不是设计的属性。"
            "换一套激励，排名可能就变 —— 所以本脚本报的是"
            "「在这套激励下谁翻得少」，不是「谁更好」。",
            "**不是功耗报告**：翻转只是 P ∝ C·V²·f 里的活动因子。"
            "电容那半边差异很大 —— 独热用 4 个触发器、二进制 2 个，"
            "同样翻 1 位，独热承担这些翻转的位数是两倍。"
            "要和 synth.py 的触发器数一起看。",
            "只数 0↔1。涉及 x/z 的位次一律排除并单列在 n_x_bit_edges 里，"
            "免得复位那一下把整条寄存器算成翻了一遍。",
        ],
        "notes_zh": [
            "`n_changes`（状态变了几次）和 `total_bit_toggles`（一共翻了几位）"
            "是两个数：一次变化可以翻多位。功耗看后者。",
            "多份输入时会先验**可比性**（状态寄存器的变化时刻列表必须逐项相等）。"
            "对不上就拒绝给结论 —— 拿两份不同激励算出的比值是纯粹的误导。",
            "本脚本不跑仿真、不编译：它读 sim.py 已经产出的波形，"
            "和 wave.py / vcd.py 一样属于感知层。",
        ],
    }
    # ★ 顺序要紧：先把比较的结论并进来，**再**算 next_actions。
    #   反过来的话，_next_actions 看到的是"单份计数"时的 status（toggle_ok），
    #   「不可比」「同一个文件」这些情况该给的提示一条都不会出现 ——
    #   而报告里 status 却是对的，看着很正常。
    if comparison:
        rep.update({"ok": comparison["ok"], "status": comparison["status"]})
    rep["next_actions_zh"] = _next_actions(rep)

    _emit(rep, args)
    return EXIT_OK if rep["ok"] else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
