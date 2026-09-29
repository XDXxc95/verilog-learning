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

    if fails:
        print(f"\nsynth 检查未通过：{len(fails)} 条断言失败")
        for f in fails:
            print(f"  ✗ {f}")
        return 1
    print("\nsynth 检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
