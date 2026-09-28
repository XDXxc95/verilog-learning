"""_observe.py 的检查。

    python tests/_smoke/check_observe.py

为什么不全部塞进 run_smoke.py 的 CASES：
    那边只能对 sim.py 吐出来的 JSON 做**点号路径取值比对**，表达不了
    「按时间选最早的分歧而不是按列表顺序」「时间字符串的单位换算」
    「时钟不靠名字认」这些。这些恰恰是最容易写错、错了又最致命的地方
    ——观察层的数字会被上层当成事实讲给学习者听。

为什么波形是**手写**的，不是跑 iverilog 生成的：
    手写才能精确控制到"某个信号只在 t=0 有一条记录""时钟叫 tick 不叫 clk"
    这种形状。靠仿真去凑这些形状，要么凑不出来，要么凑出来的东西哪天
    iverilog 换个版本就变了。这里要验的是**解析与提炼的规则**，
    不是 iverilog 的转储格式，所以不该依赖 iverilog。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from _console import init_console  # noqa: E402
from _observe import observe  # noqa: E402

init_console()

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}")
    if not cond:
        if detail:
            print(f"      ↳ {detail}")
        _failures.append(name)


# ---------------------------------------------------------------- VCD 生成

def _vcd(defs, body, timescale="1ps") -> str:
    """拼一个最小可用的 VCD。

    defs: [(scope, vtype, width, vid, name)]
    body: [(t, vid, value)]   —— value 为 "0"/"1"/"x"，或宽度 >1 时的位串
    """
    out = ["$timescale", f"\t{timescale}", "$end"]
    cur: list[str] = []
    for scope, vtype, width, vid, name in defs:
        target = [p for p in scope.split(".") if p]
        common = 0
        while common < len(cur) and common < len(target) and cur[common] == target[common]:
            common += 1
        out += ["$upscope $end"] * (len(cur) - common)
        out += [f"$scope module {p} $end" for p in target[common:]]
        cur = target
        out.append(f"$var {vtype} {width} {vid} {name} $end")
    out += ["$upscope $end"] * len(cur)
    out.append("$enddefinitions $end")

    widths = {d[3]: d[2] for d in defs}
    by_time: dict[int, list[tuple[str, str]]] = {}
    for t, vid, val in body:
        by_time.setdefault(t, []).append((vid, val))
    for t in sorted(by_time):
        out.append(f"#{t}")
        for vid, val in by_time[t]:
            if widths.get(vid, 1) > 1 and val not in ("x", "z"):
                out.append(f"b{val} {vid}")
            else:
                out.append(f"{val}{vid}")
    return "\n".join(out) + "\n"


def _write(text: str, tmp: Path, name: str) -> Path:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return p


def _toggle(vid: str, times: list[int], start: str = "0") -> list[tuple]:
    """交替 0/1 的跳变序列，用来造规整的时钟。"""
    out = []
    val = start
    for t in times:
        out.append((t, vid, val))
        val = "1" if val == "0" else "0"
    return out


# ---------------------------------------------------------------- 用例

def main() -> int:
    print("_observe.py 检查")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ---- 1. 首次分歧按**时间**选，不是按测试台里的顺序 ----
        # 测试台把后面的失败写在前面是常事（比如故意先报总量再报细节）。
        # 如果实现里写成"取列表第一个 fail"，这里就会选错。
        # 尺度对齐真实波形：`timescale 1ps（iverilog 对 1ns/1ps 写出来的
        # 就是 1ps），时钟每 5000 个刻度翻一次，即半周期 5ns、周期 10ns。
        # 时间戳一直铺到 60000，好让 0.026us（=26000 刻度）落在一段
        # **还没结束**的波形中间 —— 否则"第几个沿"算出来必然是最后一个，
        # 断言就验不出边界。
        clk_times = [i * 5000 for i in range(13)]      # 0..60000
        v = _write(_vcd(
            [("tb", "reg", 1, "!", "clk")],
            _toggle("!", clk_times),
        ), tmp, "order.vcd")
        blk = observe(v, [
            {"name": "later", "status": "fail", "time": "80.000ns", "expected": "1", "got": "0"},
            {"name": "earliest", "status": "fail", "time": "30.000ns", "expected": "0", "got": "1"},
            {"name": "middle", "status": "fail", "time": "50.000ns", "expected": "1", "got": "0"},
        ])
        fd = blk["first_divergence"]
        check("首次分歧按时间取最早，而不是取列表第一个",
              fd["case"] == "earliest",
              f"取到了 {fd['case']!r}，应是 'earliest'")
        check("分歧同时列出全部失败用例（顺序保留测试台原顺序）",
              fd["failed_cases"] == ["later", "earliest", "middle"], f"{fd['failed_cases']}")
        check("分歧带上失败总数", fd["n_failed"] == 3, f"{fd['n_failed']}")

        # ---- 2. 时间单位换算按**字符串里的单位**，不是 VCD 的显示单位 ----
        # VCD 是 1ps 刻度、显示单位是 ns；测试台打的是 us。
        # 拿 VCD 的显示单位 1ns 去套 "0.026us"，会算成 0ns ≈ 0 刻度，
        # 于是"第几个时钟沿"直接变成第 1 个 —— 错得很安静。
        blk = observe(v, [
            {"name": "x", "status": "fail", "time": "0.026us", "expected": "0", "got": "1"},
        ])
        fd = blk["first_divergence"]
        check("0.026us 在 1ps 刻度下换算成 26000 刻度",
              fd["time_ticks"] == 26000, f"算出 {fd['time_ticks']}")
        check("时钟上升沿在 5/15/25/35...ns，26ns 落在第 3 个沿",
              fd["edge_index"] == 3, f"算出 {fd['edge_index']}")
        check("回填该沿的时刻", fd["edge_time_text"] == "25ns", f"{fd['edge_time_text']!r}")
        check("给出与沿的偏移量", fd["offset_from_edge_text"] == "1ns", f"{fd['offset_from_edge_text']!r}")

        # ---- 3. 时钟靠**跳变间隔**认，不靠名字 ----
        # 三个信号是精心配的，各挡一种错误实现：
        #   clk   —— 名字叫时钟，跳得也**很规整**，但慢（间隔 30）。名字党会选中它。
        #   tick  —— 真正最快最规整的那个（间隔 10）。正确答案。
        #   noisy —— 跳得够多但**不规整**。用来验证"规整度"这道门槛真的在起作用。
        # （第一版这里只放了 clk + tick，而 clk 是不规整的 —— 结果它早在
        #   规整度那道门槛就被筛掉了，于是拆掉"取间隔最小"这条规则，断言
        #   居然还是绿的。教训：对拍用的诱饵必须能活着走到被测的那条分支。）
        v2 = _write(_vcd(
            [("tb", "reg", 1, "!", "clk"), ("tb", "reg", 1, '"', "tick"),
             ("tb", "reg", 1, "#", "noisy")],
            _toggle("!", [i * 30 for i in range(9)])          # 规整，间隔 30
            + _toggle('"', [i * 10 for i in range(19)])       # 规整，间隔 10 ← 正确
            + [(t, "#", v) for t, v in zip(
                [0, 3, 7, 20, 21, 50, 51, 52, 90],
                ["0", "1", "0", "1", "0", "1", "0", "1", "0"])],  # 不规整
        ), tmp, "byname.vcd")
        blk = observe(v2, [])
        check("时钟取跳变间隔最小的规整信号，不按信号名",
              (blk["clock"] or {}).get("signal") == "tb.tick",
              f"选到了 {(blk['clock'] or {}).get('signal')!r}，应是 'tb.tick'")

        # ---- 3b. 只认 0/1：总线跳得再规整也不是时钟 ----
        # 这条 fixture 专门让总线**跳得比时钟还快**（间隔 5 < 时钟的 10）。
        # 若"只取 0/1"这道门槛被拆掉，总线会凭"间隔最小"直接当选 ——
        # 而一条 4 位数据线在数值来回切换时凑出规整间隔是很常见的。
        v2b = _write(_vcd(
            [("tb", "reg", 1, "!", "clk"), ("tb", "wire", 4, '"', "bus")],
            _toggle("!", [i * 10 for i in range(19)])
            + [(t, '"', v) for t, v in zip(
                [i * 5 for i in range(19)],
                ["0000", "1111"] * 10)][:19],
        ), tmp, "bus.vcd")
        blk = observe(v2b, [])
        check("跳得再规整的多位总线也不算时钟（只认 0/1）",
              (blk["clock"] or {}).get("signal") == "tb.clk",
              f"选到了 {(blk['clock'] or {}).get('signal')!r}，应是 'tb.clk'")

        # ---- 4. 唯一够格的候选不规整时，宁可给 None ----
        # 上一条里 noisy 被 tick 压过去了，看不出"规整度门槛"本身有没有用。
        # 这里把它单独放出来：跳变有 8 次、值都是 0/1，只是间隔不规整。
        # 门槛若被拆掉，它就会被当成时钟报出去 —— 而它根本不是时钟。
        v3 = _write(_vcd(
            [("tb", "reg", 1, "!", "noisy")],
            [(t, "!", v) for t, v in zip(
                [0, 3, 7, 20, 21, 50, 51, 52, 90],
                ["0", "1", "0", "1", "0", "1", "0", "1", "0"])],
        ), tmp, "noclk.vcd")
        blk = observe(v3, [])
        check("跳变够多但不规整 → 不算时钟，clock 为 None",
              blk["clock"] is None, f"{blk['clock']}")
        # 次数门槛要单独验：a **非常规整**（每 5 个刻度翻一次，间隔完全一致），
        # 只是翻身次数太少（4 次 = 两个完整周期），不足以断定"它是个周期时钟"。
        #
        # 这条 fixture 是改过的：第一版 a 只有 2 次跳变，拆掉次数门槛后
        # 它仍然会被后面 `len(rising) < 2`（上升沿不足两个）挡掉 —— 于是
        # 门槛拆了断言照样绿，等于没验。换成 4 次跳变（上升沿正好两个）
        # 才真的只剩次数门槛这一道防线。
        v3b = _write(_vcd(
            [("tb", "reg", 1, "!", "a"), ("tb", "reg", 1, '"', "b")],
            _toggle("!", [0, 5, 10, 15, 20])
            + [(0, '"', "1"), (17, '"', "0")],
        ), tmp, "few.vcd")
        blk = observe(v3b, [])
        check("跳变次数不够的也不认（纯组合逻辑的典型形状）",
              blk["clock"] is None, f"{blk['clock']}")
        check("并且 notes 里说明了门槛、也提到纯组合逻辑没有时钟",
              any("纯组合逻辑" in n for n in blk["notes"]), f"{blk['notes']}")

        # ---- 5. t=0 那条是初值，不算"变过" ----
        # 实测形状：assign 驱动的组合输出在波形里就只有 t=0 这一条记录。
        # 把它算成"动过"，"这个信号从头到尾没变"这个关键诊断就没了。
        v4 = _write(_vcd(
            [("tb", "wire", 1, "!", "const"), ("tb", "reg", 1, '"', "moves")],
            [(0, "!", "0"), (0, '"', "0"), (10, '"', "1")],
        ), tmp, "init.vcd")
        blk = observe(v4, [])
        facts = {f["path"]: f for f in blk["signals"]}
        check("只有 t=0 一条记录的信号：changed 为 False",
              facts["tb.const"]["changed"] is False,
              f"{facts['tb.const']['changed']}")
        check("同上，但仍保留 n_changes 与初值",
              facts["tb.const"]["n_changes"] == 1 and facts["tb.const"]["initial_value"] == "0",
              f"{facts['tb.const']}")
        check("真的动过的信号：changed 为 True",
              facts["tb.moves"]["changed"] is True, f"{facts['tb.moves']['changed']}")
        check("全程没动过的信号排序靠前（优先展示）",
              [f["path"] for f in blk["signals"]][0] == "tb.const",
              f"{[f['path'] for f in blk['signals']]}")

        # ---- 6. 全程未知的信号排最前 ----
        v5 = _write(_vcd(
            [("tb", "reg", 1, "!", "dead"), ("tb", "wire", 1, '"', "alive"),
             ("tb", "reg", 1, "#", "held")],
            [(0, "!", "x"), (0, '"', "0"), (0, "#", "1"),
             (10, '"', "1"), (20, '"', "0")],
        ), tmp, "unknown.vcd")
        blk = observe(v5, [])
        order = [f["path"] for f in blk["signals"]]
        check("全程未知的信号排在最前", order[0] == "tb.dead", f"{order}")
        check("未知信号被标成 all_unknown",
              blk["signals"][0]["all_unknown"] is True, f"{blk['signals'][0]}")

        # ---- 7. 毛刺：脉冲短于半个时钟周期 ----
        # 时钟周期 20（每 10 一跳）。alive 在 t=45 冒出 2 个刻度的窄脉冲，
        # 远小于半周期 10 —— 组合逻辑竞争冒险的典型形状。
        v6 = _write(_vcd(
            [("tb", "reg", 1, "!", "clk"), ("tb", "wire", 1, '"', "y")],
            _toggle("!", [0, 10, 20, 30, 40, 50, 60, 70, 80, 90])
            + [(0, '"', "0"), (45, '"', "1"), (47, '"', "0")],
        ), tmp, "glitch.vcd")
        blk = observe(v6, [])
        facts = {f["path"]: f for f in blk["signals"]}
        check("识别出窄脉冲并计数", facts["tb.y"]["n_glitches"] == 1,
              f"{facts['tb.y']}")
        check("毛刺不在时钟自身上误报", facts["tb.clk"]["n_glitches"] == 0,
              f"{facts['tb.clk']}")

        # ---- 8. 信号太多时截断，并且**明说** ----
        # 静默截断会让人以为"信号就这些"，比不列更危险。
        defs = [("tb", "reg", 1, chr(ord("!") + i), f"s{i}") for i in range(6)]
        body = [(0, d[3], "0") for d in defs] + [(i * 10 + 5, d[3], "1") for i, d in enumerate(defs)]
        v7 = _write(_vcd(defs, body), tmp, "many.vcd")
        blk = observe(v7, [], max_signals=2)
        check("超过上限时只列前 N 个", len(blk["signals"]) == 2, f"{len(blk['signals'])}")
        check("截断了就明确标出来，不静默",
              blk["signals_truncated"] is True and any("只列了前" in n for n in blk["notes"]),
              f"truncated={blk['signals_truncated']} notes={blk['notes']}")

        # ---- 9. 波形读不出来时降级，不抛异常 ----
        bad = _write("这不是一个 VCD 文件\n随便写点什么\n", tmp, "bad.vcd")
        try:
            blk = observe(bad, [])
            ok = blk.get("ok") is False and bool(blk.get("reason"))
        except Exception as e:  # noqa: BLE001
            ok = False
            blk = {"exception": repr(e)}
        check("波形解析失败 → ok=False + reason，不抛异常（观察层不许带崩判定）",
              ok, f"{blk}")

        # ---- 10. 同名信号跨作用域出现时只标注、不合并 ----
        # iverilog 不一定把 tb 的线和待测模块的端口合并成同一个 ID
        # （实测：1 位会合并，带 [3:0] 范围的不会）。合并要靠"值和时刻完全
        # 一致"来判，两个碰巧同步的独立信号会被错并 —— 所以只标注。
        v8 = _write(_vcd(
            [("tb", "wire", 1, "!", "q"), ("tb.dut", "reg", 1, '"', "q")],
            [(0, "!", "0"), (0, '"', "0"), (10, "!", "1"), (10, '"', "1")],
        ), tmp, "dup.vcd")
        blk = observe(v8, [])
        facts = {f["path"]: f for f in blk["signals"]}
        check("同名信号跨作用域时各留一条（不合并）", len(blk["signals"]) == 2,
              f"{[f['path'] for f in blk['signals']]}")
        check("并用 also_in 指出另一处的路径",
              facts["tb.q"].get("also_in") == ["tb.dut.q"],
              f"{facts['tb.q'].get('also_in')}")

    print()
    if _failures:
        print(f"_observe.py 检查未通过：{len(_failures)} 项失败")
        return 1
    print("_observe.py 检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
