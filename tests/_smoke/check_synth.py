"""synth.py 的检查：综合产物的**翻译**能力，以及它有没有报绿。

    python tests/_smoke/check_synth.py

`synth.py` 拿的是 iverilog 的 `-t sizer`（资源估算）和 `-t blif`（门级网表），
这两个后端**都会用退出码 0 之外的沉默方式骗人**，所以这个脚本里最重的不是
"跑得通"，而是三类具体的沉默失败有没有被挡住：

    1. 两条腿能接受的 RTL 不一样
       `always @(posedge clk or negedge rst_n)` —— sizer 出数字，blif 直接拒绝。
       工具若不分开报，用户会以为"综合不了 = 我写法错"，
       而异步复位在 yosys/Vivado/DC 里都是标准写法。**这是假话，必须挡住。**
    2. 用户显式要了网表、一份没出 → 工具还报 pass
       "跑绿了但你要的东西不在"。`ok` 必须跟着翻 false。
    3. `**** TOTALS` 被当成一份报告的结束标志
       单顶层文件被切成两份报告 —— **数字一个没丢，结论全错**。
       这条端到端不容易逼出来，所以在第 7 节直接对 `parse_sizer()` 做单元断言。

第 3、6 节的写法照抄 `check_regress.py`：正例旁边永远放一条只差一点点的负例，
**并且负例必须真的判失败** —— 一个只会报绿的脚本在这里和"完全没工作"长得一样。
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
from _toolchain import detect      # noqa: E402

init_console()

SMOKE = ROOT / "tests" / "_smoke"
WORK = ROOT / "build" / "_smoke_synth"

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        fails.append(msg)


_n = [0]


def run(*args: str) -> tuple[int, dict]:
    """跑一次 synth.py，返回 (退出码, JSON)。

    JSON 从 **stdout** 取 —— 那是工具的契约（人类摘要走 stderr）。
    解析失败返回空 dict，由调用方断言报错，不在这里抛。
    """
    _n[0] += 1
    p = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "synth.py"), *args,
         "--quiet", "--workdir", str(WORK / f"c{_n[0]:02d}")],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    try:
        return p.returncode, json.loads(p.stdout)
    except (json.JSONDecodeError, ValueError):
        return p.returncode, {}


def _d(rep: dict, i: int = 0) -> dict:
    return (rep.get("designs") or [{}])[i]


def _kind(d: dict) -> str | None:
    return ((d.get("error") or d.get("netlist_error")) or {}).get("kind")


def _totals(rep: dict, i: int = 0) -> dict:
    return _d(rep, i).get("totals") or {}


def main() -> int:
    if not detect(None).ok:
        print("⊘ 没有 iverilog，跳过 synth 检查（先跑 setup_toolchain.py）")
        return 0

    if WORK.exists():
        shutil.rmtree(WORK)

    # ================= 1. 正例：三种状态编码横向对比 =================
    print("--- 正例：同一台状态机，三种编码横向比 ---")
    code, rep = run(str(SMOKE / "fsm_bin.v"), str(SMOKE / "fsm_onehot.v"),
                    str(SMOKE / "fsm_gray.v"))
    check(code == 0, f"应退出 0，实际 {code}（kinds={[_kind(d) for d in rep.get('designs') or []]}）")
    check(rep.get("ok") is True, "ok 应为 true")
    check(rep.get("n_ok") == 3, f"3 份都该成功，实际 {rep.get('n_ok')}")
    check(rep.get("n_sizer_ok") == 3, f"sizer 该有 3 份数字，实际 {rep.get('n_sizer_ok')}")
    # 没要网表时 wanted_netlist 必须是 false —— 否则"网表 0/3"会显得像失败
    check(rep.get("wanted_netlist") is False,
          "没加 --netlist 时 wanted_netlist 应为 false")

    ff = [_totals(rep, i).get("ff") for i in range(3)]
    check(ff == [2, 4, 2], f"FF 应为 2/4/2（bin/onehot/gray），实际 {ff}")
    # ★ 用 isinstance 兜一下再比大小，不是洁癖：`parse_sizer` 一旦退回
    #   "TOTALS 当结束标志"那个 bug，`totals` 会变成 None，
    #   裸写 `g_one > g_bin` 会让**本检查脚本自己抛 TypeError** ——
    #   护栏是响了，但响成了一片 traceback，没人看得出是哪一条规矩破了。
    #   一份结构回归必须让**带名字的断言**红。
    g_bin = _totals(rep, 0).get("gates")
    g_one = _totals(rep, 1).get("gates")
    check(isinstance(g_bin, int) and isinstance(g_one, int) and g_one > g_bin,
          f"独热的门数应比二进制高（拿触发器换组合逻辑），实际 {g_bin} vs {g_one}")
    # ★ 这条是**如实记录工具的边界**，不是在夸它：sizer 不做优化，
    #   它对"状态寄存器变宽"敏感，对"同宽度下换编码"不敏感。
    #   哪天有人给它加了"能看出格雷的好处"这种幻觉，这条会红。
    check(_totals(rep, 2) == _totals(rep, 0),
          "sizer 对 bin 和 gray 应给出一模一样的数字（它不做优化，"
          "只是把同一份 case 结构翻译了一遍）—— 若这里变了，"
          "要么 iverilog 升级了，要么有人在报告里编了数字")
    check(all(_d(rep, i).get("n_roots") == 1 for i in range(3)),
          "三份设计各自都只有一个顶层")
    print(f"  ✓ FF 2/4/2，独热 {g_one} 门 > 二进制 {g_bin} 门；"
          f"bin 与 gray 数字相同（如实反映 sizer 只看结构不看编码）")

    # ================= 2. 正例：参数 + 网表真的跟着变 =================
    print("\n--- 正例：参数化设计 + --netlist ---")
    got = {}
    for w in (3, 4, 8):
        code, rep = run(str(SMOKE / "dut_counterN.v"), "--top", "counterN",
                        "--param", f"WIDTH={w}", "--netlist")
        check(code == 0, f"WIDTH={w} 应退出 0，实际 {code}")
        d = _d(rep)
        check(d.get("params") == [f"WIDTH={w}"], f"params 应记录 WIDTH={w}")
        nl = d.get("netlist") or {}
        check(bool(nl), f"WIDTH={w} 该有网表，实际 {nl}")
        check(isinstance(nl.get("n_lut"), int) and nl["n_lut"] > 0,
              f"WIDTH={w} 该有 LUT，实际 {nl.get('n_lut')}")
        # 触发器数必须等于位宽 —— 网表和 sizer 两边对得上账
        check(nl.get("n_latch") == _totals(rep).get("ff"),
              f"WIDTH={w}：网表里的 FF={nl.get('n_latch')} 应等于 sizer 的 "
              f"FF={_totals(rep).get('ff')}")
        got[w] = (nl.get("n_lut"), _totals(rep).get("ff"), nl.get("inlined") or [])
    luts = [got[w][0] for w in (3, 4, 8)]
    check(all(isinstance(v, int) for v in luts) and sorted(set(luts)) == luts,
          f"位宽越大 LUT 应单调变多且三档互不相同，实际 {luts}")
    add = got[4][2]
    check(len(add) == 1 and add[0]["kind"] == "IVL_LPM_ADD",
          f"WIDTH=4 应识别出加法宏，实际 {add}")
    check("width=4" in (add[0].get("detail") or ""),
          f"加法宏应带上位宽，实际 {add[0].get('detail')!r}")
    print(f"  ✓ WIDTH 3/4/8 → LUT {luts}，FF 3/4/8，"
          f"`q+1` 被认出变成 {add[0]['kind']} {add[0].get('detail')}")

    # ========= 3. ★ 负例：异步复位 —— 必须和"合不了"分开，且不骂写法 =========
    print("\n--- ★ 负例：异步复位（RTL 没问题，是 blif 后端的限制）---")
    code, rep = run(str(SMOKE / "fsm_bin.v"), str(SMOKE / "fsm_onehot.v"),
                    str(SMOKE / "fsm_gray.v"), "--netlist")
    check(code != 0, "★ 要了网表一份没出，必须判失败")
    check(rep.get("ok") is False, "ok 应为 false")
    check(rep.get("wanted_netlist") is True, "wanted_netlist 应为 true")
    # ★ 核心：两条腿分开报。数字拿到了，网表没拿到，两件事都必须说出来。
    check(rep.get("n_sizer_ok") == 3,
          f"★ sizer 的数字照常该有 3 份，实际 {rep.get('n_sizer_ok')}")
    check(rep.get("n_ok") == 0, "整体成功数应为 0")
    kinds = {_kind(d) for d in rep["designs"]}
    check(kinds == {"async_reset_unsupported"},
          f"★ 应全部归到 async_reset_unsupported，实际 {kinds}")
    check("not_synthesizable" not in kinds,
          "★ 绝不能混进 not_synthesizable —— 那等于说'你写法有问题'，是假话")
    for i in range(3):
        d = _d(rep, i)
        check(bool(d.get("totals")), f"第 {i} 份该仍有 sizer 数字")
        check(d.get("netlist_error") is not None, f"第 {i} 份该报网表失败")
        check(d.get("error") is None, f"第 {i} 份 sizer 没失败，不该有 error")
        msg = _kind(d) and (d["netlist_error"].get("message_zh") or "")
        check("不是你的写法问题" in msg,
              f"第 {i} 份的提示里必须明说不是写法问题，实际 {msg[:60]!r}")
    check(any("异步复位" in a for a in (rep.get("next_actions_zh") or [])),
          "next_actions 该给出异步复位这一条")
    print("  ✓ 3/3 拿到 sizer 数字、0/3 出网表，整体判失败；"
          "归类为 async_reset_unsupported 而非 not_synthesizable")

    # 反过来：不 --netlist 时同样的文件必须**全绿** —— 否则第 3 节的修复就成了误伤
    code2, rep2 = run(str(SMOKE / "fsm_bin.v"), str(SMOKE / "fsm_onehot.v"),
                      str(SMOKE / "fsm_gray.v"))
    check(code2 == 0 and rep2.get("ok") is True,
          "没要网表时异步复位不该影响判定（sizer 这条路它是能合的）")
    print("  ✓ 不加 --netlist 时同一批文件全绿（加固没有误伤）")

    # ================= 4. 负例：四种失败各自归位 =================
    print("\n--- 负例：四类失败各自的归类 ---")
    cases = [
        ("synth_unsynth.v", "not_synthesizable", "#延迟/initial/$display"),
        ("synth_latch.v", "iverilog_crash", "锁存器断言崩溃"),
        ("synth_empty.v", "no_top", "没有模块"),
        ("synth_syntax.v", "compile_error", "前端语法错"),
    ]
    for fn, want, desc in cases:
        code, rep = run(str(SMOKE / fn), "--json", str(WORK / f"{fn}.json"))
        check(code != 0, f"{desc}：应判失败，实际退出 {code}")
        check(rep.get("ok") is False, f"{desc}：ok 应为 false")
        check(rep.get("n_sizer_ok") == 0, f"{desc}：不该有 sizer 数字")
        k = _kind(_d(rep))
        check(k == want, f"{desc}：应归到 {want}，实际 {k}")
        e = _d(rep).get("error") or {}
        check(bool(e.get("raw_stderr")), f"{desc}：原始 stderr 必须留着，不许只给转述")
        check(e.get("exit_code") is not None, f"{desc}：退出码要记下来")
    print("  ✓ 四类各自归位，且都保留了 iverilog 原文与退出码")

    # 崩溃那一条要单独再钉一次：退出码是 Windows 的断言码，不是 1/2
    code, rep = run(str(SMOKE / "synth_latch.v"))
    e = _d(rep).get("error") or {}
    check(e.get("exit_code") == 0xC0000409,
          f"锁存器崩溃的退出码应是 0xC0000409，实际 {hex(e.get('exit_code') or 0)}")
    check("latch_inference" in (e.get("message_zh") or ""),
          "提示里该把人引到 lint 的 latch_inference（那里更早也更直白）")
    check(any("latch_inference" in a or "断言" in a
              for a in (rep.get("next_actions_zh") or [])),
          "next_actions 该给出补全分支这一条")
    print("  ✓ 崩溃退出码 0xC0000409 记录在案，并把入口指向 _lint.py 的 latch_inference")

    # ========= 5. 负例：多顶层 —— 不给合并数字，但要分别列出来 =========
    print("\n--- 负例：一个文件两棵不相干的层次 ---")
    code, rep = run(str(SMOKE / "synth_tworoot.v"))
    check(code == 0, "多顶层本身不是失败：能综合，只是没法给一个总数")
    d = _d(rep)
    check(d.get("n_roots") == 2, f"应认出 2 个顶层，实际 {d.get('n_roots')}")
    check(d.get("totals") is None,
          "★ 多顶层时 totals 必须**故意为空** —— 把两棵不相干层次的门数加起来没有意义")
    check(d.get("scopes") is None, "多顶层时 scopes 也不该给")
    reps = d.get("reports") or []
    check(len(reps) == 2, f"★ 应有 2 份报告（各 2 个顶层各一份），实际 {len(reps)}")
    names = [[s["scope"] for s in r["scopes"]] for r in reps]
    check(names == [["a1"], ["b1"]],
          f"★ 两份报告必须各含一个顶层、不能串，实际 {names}")
    check(all(r.get("totals") for r in reps), "每份报告各自要有合计")
    check(any("没有合并" in n for n in (d.get("notes_zh") or [])),
          "要说明为什么故意不合并")
    print(f"  ✓ n_roots=2、totals 为空（刻意），但 {names} 各自列了出来")

    # ================= 6. 负例：用法错误 =================
    print("\n--- 负例：用法错误 ---")
    for desc, bad in [
        ("传了测试台", [str(SMOKE / "tb_counterN.v")]),
        ("给了 --param 却没给 --top", [str(SMOKE / "dut_counterN.v"),
                                 "--param", "WIDTH=3"]),
        ("文件不存在", [str(SMOKE / "no_such_file.v")]),
    ]:
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "synth.py"), *bad],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        check(p.returncode == 64, f"{desc}：应退出 64（用法错误），实际 {p.returncode}")
        check(not p.stdout.strip().startswith("{"),
              f"{desc}：用法错误时不该往 stdout 吐 JSON")
    print("  ✓ 三类用法错误都是 64，且不污染 stdout")

    # ========== 7. 单元：parse_sizer 的 `**** TOTALS`（端到端逼不出来） ==========
    #
    # 这是**单元级**的。端到端下要逼出"一份被切成两份"需要正好一份单顶层文件、
    # 且报告数目被断言到 1 —— 第 1、2 节其实已经覆盖了这个方向（都断言
    # n_roots == 1）。但两顶层那份（第 5 节）只能证明"有两份"，
    # 不能证明"该一份的时候只有一份"。与其假装覆盖了，不如直接对着
    # parse_sizer 断言。拆掉 TOTALS 的 close() 逻辑做过实验，这两条会红。
    print("\n--- parse_sizer 单元（TOTALS 是切换目标，不是结束标志）---")
    import synth as synthmod

    one = ("**** module/scope: only\n"
           "     Flip-Flops   : 2\n     Logic Gates  : 21\n"
           "**** TOTALS\n"
           "     Flip-Flops   : 2\n     Logic Gates  : 21\n")
    reps, unparsed = synthmod.parse_sizer(one)
    check(len(reps) == 1,
          f"★ 单顶层文件必须解析成**1 份**报告，实际 {len(reps)}"
          "（第一版把 TOTALS 当结束标志，切成了两份 —— 数字没丢但结论全错）")
    # 一律用 .get：这条在旧 bug 下 `totals` 是空 dict，裸下标会 KeyError，
    # 那时又变成 traceback 而不是断言
    check(bool(reps) and (reps[0].get("totals") or {}).get("ff") == 2,
          f"单顶层这份要同时有 scopes 和 totals，实际 {reps}")
    check(len(reps[0]["scopes"]) == 1, "单顶层这份的 scopes 长度应为 1")
    check(unparsed == [], f"标准格式不该有解析不了的行，实际 {unparsed}")

    two = one + ("**** module/scope: second\n"
                 "     Flip-Flops   : 3\n     Logic Gates  : 7\n"
                 "**** TOTALS\n"
                 "     Flip-Flops   : 3\n     Logic Gates  : 7\n")
    reps2, _ = synthmod.parse_sizer(two)
    check(len(reps2) == 2, f"两个顶层应解析成 2 份，实际 {len(reps2)}")
    ffs = [(r.get("totals") or {}).get("ff") for r in reps2]
    check(ffs == [2, 3], f"两份的合计应各归各（2、3），实际 {ffs}")
    print("  ✓ 单顶层 1 份、两顶层 2 份，合计各归各，无残留未解析行")

    # ============ 8. yosys 引擎（--engine yosys）—— 它自己特有的沉默失败 ============
    #
    # 前面七节覆盖的全是 iverilog 那条腿。这一节补的是 yosys 那条腿**独有**的坑：
    #   ① 非硬件 cell 会活到 abc 之后、退出码还是 0（直接报"96 个门"就是假话）
    #   ② 「退出码 0」和「有输出」都不是跑过的证据（空文件 rc=0 且日志正常）
    #   ③ 「没有状态机」和「编码没影响」在下游都是一张相同的表，含义相反
    #   ④ yosys 备齐了、引擎却没生效 —— 和"引擎正常工作"在下游长得一样
    # 每条都要能变红，变异方式写在各自的注释里。
    print("\n--- yosys 引擎 ---")
    import _yosys

    _yos = _yosys.locate()
    if _yos is None:
        # 「没跑」≠「跑了没问题」。这里必须**明说跳过了**，
        # 不能让整节静默消失 —— 那和"全绿"长得一样（同 lint.ran 的教训）。
        print("  ⊘ 没找到 yosys，跳过这一节（装法："
              "python tools/setup_tools.py --install yosys --lock）")
    else:
        # ---- 8a. 正例对照：引擎真的生效了吗 ----
        code, rep = run(str(SMOKE / "fsm_bin.v"), "--engine", "yosys",
                        "--encoding-compare")
        check(code == 0, f"yosys 引擎应退出 0，实际 {code}")
        d = _d(rep)
        check(rep.get("engine") == "yosys", "engine 字段要如实写 yosys")
        check(rep.get("n_yosys_ok") == 1, f"n_yosys_ok 应为 1，实际 {rep.get('n_yosys_ok')}")
        # 两个计数必须分开：iverilog 那条腿的 n_ok / n_sizer_ok 含义不变
        check(rep.get("n_ok") == 1 and rep.get("n_sizer_ok") == 1,
              "★ 加了 yosys 那条腿之后，iverilog 那两个计数必须照旧")
        y = d.get("yosys") or {}
        check(y.get("ran") is True, f"yosys 该跑成，实际 {y.get('skipped_reason_zh')}")
        check(y.get("fsm_detected") is True, "fsm_bin 里该认出状态机")

        rows = {r["encoding"]: r for r in ((d.get("encoding_compare") or {}).get("rows") or [])}
        check(set(rows) == {"auto", "one-hot", "binary"},
              f"三种编码各要一行，实际 {sorted(rows)}")
        # ★★ 这一条是整节的锚：**编码真的换出不同硬件**。
        #   少了它，"引擎压根没生效"和"引擎正常工作"在测试里长得一模一样
        #   —— 三种编码全给同一份数字，断言照样过。
        one, bina = rows["one-hot"], rows["binary"]
        check(one["n_state_bits"] == 4 and bina["n_state_bits"] == 2,
              f"★ one-hot 该 4 位状态寄存器、binary 该 2 位，实际 "
              f"{one['n_state_bits']} / {bina['n_state_bits']}")
        check(isinstance(one["n_cells"], int) and isinstance(bina["n_cells"], int)
              and one["n_cells"] != bina["n_cells"],
              f"★ 两种编码的门数必须不同，实际 {one['n_cells']} / {bina['n_cells']}")
        # 变异：把 build_script 里 `fsm` 那一阶段整段删掉 → 上面两条全红。
        if rows["auto"]["encoding_used"] != one["encoding_used"] and \
           rows["auto"]["encoding_used"] != bina["encoding_used"]:
            check(False, f"auto 的 encoding_used 得是 yosys 报出来那个，"
                         f"实际 {rows['auto']['encoding_used']!r}")
        cmp_ = d.get("encoding_compare") or {}
        check(cmp_.get("comparable") is True and cmp_.get("identical") is False,
              "状态机认出来了、数字也不同，comparable 该 true、identical 该 false")
        print(f"  ✓ one-hot {one['n_state_bits']} 位/{one['n_cells']} 门 vs "
              f"binary {bina['n_state_bits']} 位/{bina['n_cells']} 门，"
              f"auto 实际挑了 {rows['auto']['encoding_used']}")

        # ---- 8b. ★ 负例：没有状态机时**不许给一张看着像结论的表** ----
        code, rep = run(str(SMOKE / "dut_counter4.v"), "--engine", "yosys",
                        "--encoding-compare")
        d = _d(rep)
        c = d.get("encoding_compare") or {}
        check(c.get("comparable") is False,
              "★ 没有状态机时必须报 comparable=False —— "
              "三种编码数字相同不是结论，是没比较到东西")
        # ★ 和 comparable 分开的两位：认出来了但数字相同是**真结论**，
        #   认不出来是**没法比较**。挤成一位下游就分不清，两者给学习者的
        #   下一步完全相反。
        check(c.get("identical") is True,
              "★ 认不出来时 identical 仍该如实报 true（三次确实一样），"
              "但它的含义由 comparable 决定 —— 两位不能合并")
        check(any("没能比较" in n for n in (c.get("notes_zh") or [])),
              "提示里必须明说「本次没能比较」，而不是把三次相同摆成结论")
        check(rep.get("n_comparable") == 0, "n_comparable 该是 0")
        # 用户显式要了对比、一份没比成 → 翻失败（沿用 --netlist 那条先例）
        check(code != 0, "★ 要了编码对比却一份都没比成，必须判失败")
        print("  ✓ 纯计数器：comparable=false / identical=true，"
              "明说「没能比较」，整体判失败")

        # ---- 8c. ★ 负例：非硬件 cell 在场时不许给门数结论 ----
        code, rep = run(str(SMOKE / "synth_unsynth.v"), "--engine", "yosys")
        d = _d(rep)
        y = d.get("yosys") or {}
        check(code != 0, "★ 含 $display/#延迟 的设计，yosys 引擎该判失败")
        check(y.get("ran") is True,
              "★ yosys 会照常跑完（rc=0），所以 ran 该是 true —— "
              "问题不在「没跑」，在「跑出来的不是硬件」")
        check("$print" in (y.get("non_hw_cells") or {}),
              f"★ 该认出 $print，实际 {y.get('non_hw_cells')}")
        check(y.get("gate_count_meaningful") is False,
              "★ 非硬件 cell 在场时 gate_count_meaningful 必须 false")
        check(y.get("ok") is False, "★ 不许判 ok")
        # 数字照给（它是事实）——但它**必须**带着"不是门数"的标记
        nd = (y.get("modules") or [{}])[0]
        check(nd.get("counts", {}).get("cells") == 96,
              f"实测是 96 个 cell，要如实记下来，实际 "
              f"{nd.get('counts', {}).get('cells')}")
        check(any("不能当门数" in n for n in (y.get("notes_zh") or [])),
              "必须明说这个数字不能当门数")
        # 变异：把 _NON_HW_PREFIXES 置空 → non_hw_cells 变 {}、上面四条全红。
        print("  ✓ 96 个 cell 如实记下但标为不可用（non_hw_cells=$print），"
              "gate_count_meaningful=false")

        # ---- 8d. ★ 两条腿的分歧要挑明，不能只报乐观的那个 ----
        cc = d.get("cross_check") or {}
        check(cc.get("kind") == "disagreement",
              f"★ iverilog 说合不了、yosys 给出数字 —— 必须报成分歧，实际 {cc.get('kind')}")
        check("gate_count_meaningful" in (cc.get("message_zh") or ""),
              "分歧说明里要把人引到 yosys 那一侧的可信度字段上")
        # 反过来：异步复位不是"打架"，是预料之中的边界差异，措辞不能混
        code, rep = run(str(SMOKE / "fsm_bin.v"), "--engine", "yosys", "--netlist")
        cc2 = _d(rep).get("cross_check") or {}
        check(cc2.get("kind") == "yosys_handles_it",
              f"★ 异步复位该归到 yosys_handles_it（不是 disagreement），实际 {cc2.get('kind')}")
        check("你的 RTL 没问题" in (cc2.get("message_zh") or ""),
              "异步复位那一条必须明说 RTL 没问题（和 iverilog 那条腿同一口径）")
        print("  ✓ 分歧归类：not_synthesizable 对 yosys → disagreement；"
              "异步复位 → yosys_handles_it（明说 RTL 没问题）")

        # ---- 8e. ★ 负例：「没跑」≠「跑了没报」，且不许动 iverilog 那条腿的结论 ----
        bad = _yosys.run([SMOKE / "fsm_bin.v"], top="fsm_bin",
                         exe_hint=WORK / "no_such_dir",
                         workdir=WORK / "y_badexe")
        check(bad.ran is False and bad.started is False,
              "★ exe 指到不存在的路径：ran 和 started 都该是 false")
        check(bool(bad.skipped_reason_zh) and "setup_tools" in bad.skipped_reason_zh,
              f"要如实报缺并给出装法，实际 {bad.skipped_reason_zh!r}")
        # 变异：把 `run()` 里的 `ran` 恒置 True → 上面两条红。
        # 而**判定一个字不变** —— 和 lint 那条铁律同理：
        # yosys 没跑成，不能把 iverilog 那条腿已经拿到的数字一起打成失败。
        code_ref, rep_ref = run(str(SMOKE / "fsm_bin.v"))
        code_y, rep_y = run(str(SMOKE / "fsm_bin.v"), "--engine", "yosys")
        check(_totals(rep_ref) == _totals(rep_y),
              f"★ yosys 那条腿不许改动 iverilog 的数字："
              f"{_totals(rep_ref)} vs {_totals(rep_y)}")
        check(rep_ref.get("n_sizer_ok") == rep_y.get("n_sizer_ok"),
              "n_sizer_ok 逐字段不变")
        print("  ✓ yosys 缺席时 ran=false + 报缺，iverilog 那条腿的数字逐字段不变")

        # ---- 8f. ★ 硬事实①：空文件 rc=0、日志正常，但一个模块都没有 ----
        empty = _yosys.run([SMOKE / "synth_empty.v"], workdir=WORK / "y_empty")
        check(empty.started is True,
              "★ 空文件时 yosys **确实起来了**（有横幅）—— 不能和「缺 DLL 起不来」混")
        check(empty.reached_stat is True,
              "★ 空文件时流水线**确实跑到了 stat**（tee 文件在）")
        check(empty.ran is False and empty.modules == [],
              "★ 但没有模块，所以 ran 该是 false、modules 该是空")
        check("退出码是 0" in (empty.skipped_reason_zh or ""),
              "理由里必须点破「退出码 0 不算证据」，否则读者会去信 rc")
        # 变异：把 `ran` 改成只看 stat 文件在不在 → 上面 ran/modules 两条红。
        print("  ✓ 空文件：started=true / reached_stat=true / ran=false，"
              "并点破「退出码 0 不算证据」")

        # ---- 8g. gray 必须被明确挡住（不是静默当成 auto）----
        code, rep = run(str(SMOKE / "fsm_bin.v"), "--engine", "yosys",
                        "--fsm-encoding", "gray")
        check(code == 64, f"gray 是非法取值，该退出 64，实际 {code}")
        print("  ✓ --fsm-encoding gray 被挡下（yosys 的 fsm_recode 只支持 one-hot/binary）")

    # ---- 8h. 用法错误：编码相关开关不能脱离 --engine yosys ----
    for desc, bad_args in [
        ("--encoding-compare 没配 --engine yosys",
         [str(SMOKE / "dut_counter4.v"), "--encoding-compare"]),
        ("--fsm-encoding 没配 --engine yosys",
         [str(SMOKE / "dut_counter4.v"), "--fsm-encoding", "binary"]),
    ]:
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "synth.py"), *bad_args],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        check(p.returncode == 64, f"{desc}：应退出 64，实际 {p.returncode}")
        check(not p.stdout.strip().startswith("{"),
              f"{desc}：用法错误时不该往 stdout 吐 JSON")
    print("\n--- 负例：编码开关脱离 --engine yosys ---")
    print("  ✓ 两条都是 64，且不污染 stdout（不静默换引擎）")

    # ---- 8i. 单元：parse_stat / cell 分类（端到端逼不出边界）----
    print("\n--- parse_stat / cell 分类 单元 ---")
    check(_yosys.parse_stat("") == ([], []),
          "空输入该返回空、不抛异常")
    check(_yosys.parse_stat("noise\nmore noise\n") == ([], ["noise", "more noise"]),
          "没有 === 分节时，非空行该进 outside 而不是被静默丢掉")
    mods, _ = _yosys.parse_stat(
        "=== m ===\n\n        5 wires\n        3 cells\n"
        "        2   $_DFF_P_\n        1   $print\n"
        "   这里是不认识的一行\n"
    )
    check(len(mods) == 1 and mods[0].name == "m", f"该解析出 1 个模块，实际 {mods}")
    check(mods[0].n_cells == 3, f"cells 该是 3，实际 {mods[0].n_cells}")
    # ★ 寄存器位数取 `$_DFF_*` 的**个数和**，不是 `stat` 的 cell 总数：
    #   `$adff` 无论状态寄存器多宽都只算一个 cell，拿总数比编码等于没比。
    check(mods[0].n_state_bits == 2,
          f"★ 寄存器位该是 $\\_DFF_* 的个数和 = 2，实际 {mods[0].n_state_bits}")
    check("$print" not in mods[0].seq_cells, "非硬件 cell 不许算进寄存器位")
    check(_yosys._is_non_hw("$print") and not _yosys._is_non_hw("$_DFF_P_"),
          "非硬件 cell 的判据要能分开 $print 和 $_DFF_P_")
    check(_yosys._is_mapped_gate("$_AND_") and not _yosys._is_mapped_gate("$mem"),
          "门级 cell 的判据：$_XXX_ 才算映射过的门")
    check(mods[0].unparsed == ["   这里是不认识的一行"],
          f"不认识的行要留下来，静默丢行和静默丢结构是一类错，实际 {mods[0].unparsed}")
    print("  ✓ 空输入不抛、未知行进 unparsed、寄存器位只数 $\\_DFF_*")

    if fails:
        print(f"\nsynth 检查未通过：{len(fails)} 条断言失败")
        for f in fails:
            print(f"  ✗ {f}")
        return 1
    print("\nsynth 检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
