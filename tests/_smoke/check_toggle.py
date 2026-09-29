"""toggle.py 的检查：**「变化了几次」和「翻了几位」必须分开报**。

    python tests/_smoke/check_toggle.py

这个脚本大部分篇幅在钉一件事：**数翻转最容易犯的错，是把"值变了几次"
和"一共翻了几位"叫成同一个东西。**

夹具是同一台 4 位计数器，只把状态编码换掉（`cnt_enc.v` 的 `GRAY` 参数），
激励完全相同。实测：

    二进制 st：变化 34 次，翻 63 位（1.85 位/次）
    格雷   st：变化 34 次，翻 34 位（1.00 位/次）

**变化次数一模一样、翻位数差将近一倍。** 只报一个数，无论报哪个，
都会得出片面的结论：报「变化次数」→ 两种编码没差别；报「翻位数」→
看不出"它们其实每个时钟沿都在动"。所以第 1、2 节把两个数都钉住，
并**显式断言 n_changes 相等而 total 不等** —— 一个把两者混为一谈的
实现会给出一致的数，只有把"相等"和"不等"同时钉住，将来有人改错才会红。

第 4 节钉的是本工具唯一能挡住误用的东西：**可比性**。
翻转数完全由激励决定，拿两份不同 workload 算出的比值是纯粹的误导。
判据是状态寄存器的变化时刻列表逐项相等（编码是状态到码字的一一映射，
同一套激励下状态序列一样 ⇒ 变化时刻一样）。
第 5 节钉一个可比性检查自己抓不到的情况：两份输入其实是同一个波形文件。
它会导致一个**看着很像结论**的假结果（"打平"），必须判失败。

第 6 节钉"全是 0"和"打平"的区别 —— 和 `cov.py` 里
「没有覆盖数据」vs「覆盖 0 个点」、`regress.py` 里「扫了但没扫到差异」
是同一条教训：看着绿，其实没在工作。
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
WORK = ROOT / "build" / "_smoke_toggle"

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        fails.append(msg)


def run(*args: str) -> tuple[int, dict, str]:
    """跑一次 toggle.py，返回 (退出码, stdout 的 JSON, stderr 的摘要)。"""
    p = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "toggle.py"), *args],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    try:
        return p.returncode, json.loads(p.stdout), p.stderr
    except (json.JSONDecodeError, ValueError):
        return p.returncode, {}, p.stderr


def sim(tag: str, *params: str) -> Path:
    """跑一遍计数器测试台，把 VCD **拷出来**（测试台固定写同一个文件名，
    第二次跑会覆盖第一次的 —— 这正是 toggle.py 的 identical_inputs 要抓的东西，
    在造夹具时自己得先躲开）。"""
    out = WORK / f"{tag}.vcd"
    if out.exists():
        return out
    p = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "sim.py"),
         str(SMOKE / "cnt_enc.v"), "--tb", str(SMOKE / "tb_cnt_enc.v"),
         "--top", "tb_cnt_enc", *params, "--name", tag,
         "--workdir", str(WORK / tag), "--json", str(WORK / f"{tag}.json"),
         "--quiet"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    check(p.returncode == 0, f"夹具 {tag} 该跑通，实际退出 {p.returncode}\n{p.stderr[-800:]}")
    src = ROOT / "build" / "_smoke_cnt_enc.vcd"
    if src.exists():
        shutil.copyfile(src, out)
    return out


def main() -> int:
    if not detect(None).ok:
        print("⊘ 没有 iverilog，跳过 toggle 检查（先跑 setup_toolchain.py）")
        return 0

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    v_bin = sim("bin", "--param", "GRAY=0")
    v_gray = sim("gray", "--param", "GRAY=1")
    v_short = sim("short", "--param", "GRAY=0", "--param", "N_CYCLE=10")

    # ============ 1. ★ 正例：两份编码可比，翻位数差将近一倍 ============
    print("--- ★ 正例：二进制 / 格雷，同一套激励 ---")
    code, rep, err = run(str(v_bin), str(v_gray),
                         "--signal", "st", "--label", "二进制", "--label", "格雷")
    check(code == 0, f"可比的比较该退出 0，实际 {code}")
    check(rep.get("ok") is True and rep.get("status") == "toggle_ok",
          f"应为 toggle_ok，实际 {rep.get('status')}")
    check(rep.get("affects_verdict") is False,
          "★ affects_verdict 必须恒为 False —— 这是感知层，不许有机会翻判定")
    check(rep.get("n_runs") == 2, f"n_runs 应为 2，实际 {rep.get('n_runs')}")

    runs = rep.get("runs") or []
    b = runs[0] if len(runs) > 0 else {}
    g = runs[1] if len(runs) > 1 else {}
    check(b.get("signal") == "tb_cnt_enc.dut.st",
          f"应选中 dut 里的状态寄存器，实际 {b.get('signal')}")
    check(b.get("n_matched_signals") == 1,
          f"`st` 只该命中一个确切同名的信号，实际 {b.get('n_matched_signals')}")
    check(b.get("bit_toggles") == [34, 17, 8, 4],
          f"★ 二进制逐位翻转应为 [34,17,8,4]，实际 {b.get('bit_toggles')}")
    check(g.get("bit_toggles") == [17, 9, 4, 4],
          f"★ 格雷逐位翻转应为 [17,9,4,4]，实际 {g.get('bit_toggles')}")
    check(b.get("total_bit_toggles") == 63,
          f"★ 二进制翻位数应为 63，实际 {b.get('total_bit_toggles')}")
    check(g.get("total_bit_toggles") == 34,
          f"★ 格雷翻位数应为 34，实际 {g.get('total_bit_toggles')}")
    check(g.get("bits_per_change") == 1.0,
          f"★ 格雷每次进位**恰好翻 1 位**，实际 {g.get('bits_per_change')}")
    check(b.get("bits_per_change") == 1.853,
          f"二进制平均 1.853 位/次，实际 {b.get('bits_per_change')}")
    # 逐位之和必须等于总数：两处分别统计，对不上就是实现里漏算了某一位
    for r in runs:
        check(sum(r.get("bit_toggles") or []) == r.get("total_bit_toggles"),
              f"{r.get('name')}：逐位之和与翻位数对不上 —— "
              f"{sum(r.get('bit_toggles') or [])} vs {r.get('total_bit_toggles')}")
    print(f"  ✓ 二进制 63 位（[34,17,8,4]）/ 格雷 34 位（[17,9,4,4]）")

    # ============ 2. ★★ 变化次数相同、翻位数不同 ============
    print("\n--- ★★ 「变化了几次」和「翻了几位」是两个数 ---")
    check(b.get("n_changes") == 34 and g.get("n_changes") == 34,
          f"★ 两份的变化次数必须都是 34（同一套激励、每个时钟沿都动一次），"
          f"实际 {b.get('n_changes')} / {g.get('n_changes')}")
    check(b.get("total_bit_toggles") != g.get("total_bit_toggles"),
          "★ 变化次数相同的前提下，翻位数必须不同 —— "
          "两者相等就说明实现把「变化次数」当成了「翻位数」")
    check(b.get("n_changes") != b.get("total_bit_toggles") or
          g.get("n_changes") != g.get("total_bit_toggles"),
          "同一次计数里，n_changes 和 total_bit_toggles 至少有一份必须不同")
    # 二进制这份最能说明问题：34 次变化、63 次位翻转
    check(b.get("n_changes") == 34 and b.get("total_bit_toggles") == 63,
          "★ 二进制：34 次变化、63 位翻转 —— 只看 34 会得出"
          "「两种编码一样」的错结论")
    print("  ✓ 两份都是 34 次变化；翻位数一个 63、一个 34 —— "
          "只报一个数必然片面")

    # 比较块：基准、比值、结论
    cmp_ = rep.get("comparison") or {}
    check(cmp_.get("comparable") is True, "两份该被判定为可比")
    check(cmp_.get("n_runs_comparable") == 2,
          f"★ 两个计数分开报（同 lint.ran/n_warnings）："
          f"n_runs_comparable 应为 2，实际 {cmp_.get('n_runs_comparable')}")
    check(cmp_.get("baseline") == "二进制", f"基准应是第一份，实际 {cmp_.get('baseline')}")
    o = (cmp_.get("runs") or [{}])[0]
    check(o.get("ratio_vs_base") == 0.54,
          f"★ 格雷相对基准应为 0.54，实际 {o.get('ratio_vs_base')}")
    check(cmp_.get("best") == "格雷" and cmp_.get("worst") == "二进制",
          f"最少/最多应为格雷/二进制，实际 {cmp_.get('best')}/{cmp_.get('worst')}")
    check(cmp_.get("all_same_total") is False and cmp_.get("identical_inputs") is False,
          "这两份既不是打平、也不是同一个文件")
    print(f"  ✓ 可比 2/2，基准=二进制，格雷比值 0.54（省 46%）")

    # ============ 3. 摘要走 stderr、JSON 走 stdout ============
    print("\n--- 输出约定：JSON 走 stdout、摘要走 stderr ---")
    check("翻位" in err and "同一套激励" in err,
          "人类可读摘要该在 stderr（和 sim.py / cov.py 同一个约定）")
    jf = WORK / "out.json"
    code, rep2, _ = run(str(v_bin), str(v_gray), "--signal", "st", "--json", str(jf))
    check(jf.is_file(), "--json 该写出文件")
    check(json.loads(jf.read_text(encoding="utf-8")).get("schema") == "vlearn.toggle/1",
          "--json 写出的该是完整结果")
    print("  ✓ 摘要/JSON 分流正常，--json 落盘")

    # ============ 4. ★ 负例：两份不同激励 → 不可比 ============
    print("\n--- ★ 负例：34 拍 vs 10 拍（激励不同）---")
    code, rep, err = run(str(v_bin), str(v_short),
                         "--signal", "st", "--label", "34拍", "--label", "10拍")
    check(code == 1, f"★ 不可比必须退出非 0（拒绝给结论），实际 {code}")
    check(rep.get("status") == "not_comparable",
          f"status 应为 not_comparable，实际 {rep.get('status')}")
    cmp_ = rep.get("comparison") or {}
    check(cmp_.get("comparable") is False, "comparable 应为 False")
    check(cmp_.get("n_runs") == 2 and cmp_.get("n_runs_comparable") == 1,
          f"★ 跑了 2 份、其中 1 份可比 —— 两个数都要如实报，"
          f"实际 {cmp_.get('n_runs')}/{cmp_.get('n_runs_comparable')}")
    dv = (cmp_.get("diverged") or [{}])[0].get("divergence") or {}
    check(dv.get("index") == 10, f"★ 第一处对不上应报出位置 10，实际 {dv.get('index')}")
    check(dv.get("a") is not None and dv.get("b") is None,
          f"★ 要指出是哪一份先没了（10 拍那份在第 10 次变化后就没有了），实际 {dv}")
    check((cmp_.get("runs") or [{}])[0].get("ratio_vs_base") is None,
          "★ 不可比时**不许**给出比值 —— 那个数看着挺合理，却是纯粹的误导")
    # 数字本身照样要报：测量是真的，"结论"才是被拒绝的
    check((rep.get("runs") or [{}])[0].get("total_bit_toggles") == 63,
          "★ 拒绝给结论 ≠ 不给测量值：两份的翻位数仍要如实报出来")
    print("  ✓ 退出 1、not_comparable、第 10 处对不上、比值为 null，"
          "但两份的测量值都还在")

    # ============ 5. ★ 负例：两份输入其实是同一个波形 ============
    print("\n--- ★ 负例：同一个波形文件比两次 ---")
    code, rep, err = run(str(v_bin), str(v_bin), "--signal", "st")
    check(code == 1, f"★ 拿同一个文件跟自己比必须判失败，实际 {code}")
    check(rep.get("status") == "identical_inputs",
          f"status 应为 identical_inputs，实际 {rep.get('status')}")
    cmp_ = rep.get("comparison") or {}
    check(cmp_.get("comparable") is True and cmp_.get("identical_inputs") is True,
          "★ 这种情况可比性检查**抓不到**（列表当然逐项相等）—— 必须单独挡")
    check(cmp_.get("all_same_total") is True,
          "翻位数确实相同 —— 正因为如此，报出来的『打平』才是个假结论")
    check(any("--param" in a or "覆盖" in a for a in (rep.get("next_actions_zh") or [])),
          "★ 提示要给出路：要么是产物被覆盖了，要么是区分两份实现的开关没接上")
    print("  ✓ 退出 1、identical_inputs，且提示指出「打平」可能是假的")

    # ============ 6. ★ 「全是 0」不是「打平」============
    print("\n--- ★ 负例：窗口里一次都没翻（≠ 打平）---")
    code, rep, err = run(str(v_bin), str(v_gray), "--signal", "st",
                         "--from", "#400000", "--to", "#500000")
    check(code == 1, f"★ 全 0 是工具没做成事，必须退出非 0，实际 {code}")
    check(rep.get("status") == "no_toggles",
          f"status 应为 no_toggles，实际 {rep.get('status')}")
    check(rep.get("comparison", {}).get("all_same_total") is True,
          "全是 0 时 all_same_total 也是 True —— 所以光看这个标记分不出"
          "『打平』和『没数到』，必须另判")
    check("打平是真结论" not in err,
          "★ 全是 0 时**不许**说『打平是真结论』—— 那是把『工具没在工作』"
          "说成了结论（同 cov.py 的『没数据』vs『覆盖 0 个点』）")
    check("没数到翻转" in err,
          "要把话说明白：这不是打平，是没数到翻转")
    # 对照：单份输入时窗口内零翻转是**合法测量**（0 就是答案），不该判失败
    code, rep, err = run(str(v_bin), "--signal", "st",
                         "--from", "#400000", "--to", "#500000")
    check(code == 0 and rep.get("status") == "toggle_ok",
          f"★ 单份时 0 就是答案，不该判失败，实际 {code}/{rep.get('status')}")
    check(rep.get("comparison") == {}, "单份没有比较块")
    print("  ✓ 两份全 0 → 退出 1、no_toggles；单份 0 → 退出 0（0 就是答案）")

    # ============ 7. ★ x/z 排除，并且要**报出来排除了多少** ============
    print("\n--- ★ x/z 位次被排除且单列（不并进翻位数）---")
    code, rep, err = run(str(v_bin), "--signal", "exp_q")
    r0 = (rep.get("runs") or [{}])[0]
    check(code == 0, f"该退出 0，实际 {code}")
    check(r0.get("n_x_bit_edges") == 4,
          f"★ exp_q 在复位前是 xxxx，那次到 0000 的位次应被记成 4 个，"
          f"实际 {r0.get('n_x_bit_edges')}")
    check(r0.get("n_defined_changes") == r0.get("n_changes") - 1,
          f"★ 34 次变化里有 1 次涉及 x —— 两个计数要分开，"
          f"实际 {r0.get('n_defined_changes')}/{r0.get('n_changes')}")
    check(r0.get("total_bit_toggles") == sum(r0.get("bit_toggles") or []),
          "逐位之和仍要等于翻位数")
    # ★ 关键断言：x 那 4 个位次**没有**被算进翻位数。
    #   算进去的话 exp_q 会变成 66（62 + 4）而不是 62
    check(r0.get("total_bit_toggles") == 62,
          f"★ 翻位数必须是 62（x 那 4 个位次被排除），"
          f"算进去会变成 66，实际 {r0.get('total_bit_toggles')}")
    check(r0.get("bits_per_change") == 1.879,
          f"★ 分母用 n_defined_changes(33) 而不是 n_changes(34)，"
          f"实际 {r0.get('bits_per_change')}")
    check(rep.get("n_x_bit_edges_any") is True, "顶层要标记有过 x 跳变")
    check("x/z" in err or "排除" in err, "摘要里要说清排除了多少")
    print("  ✓ 4 个 x 位次被排除并单列；翻位数 62 而不是 66；分母是 33")

    # ============ 8. --from 时间窗 ============
    print("\n--- 时间窗：--from 100ns ---")
    code, rep, err = run(str(v_bin), "--signal", "st", "--from", "100ns")
    r0 = (rep.get("runs") or [{}])[0]
    check(code == 0, f"该退出 0，实际 {code}")
    check(r0.get("n_changes") == 27,
          f"★ 100ns 之后还剩 27 次变化（首次变化在 35ns），实际 {r0.get('n_changes')}")
    check(r0["window"]["from_text"] == "100ns",
          f"窗口文案该是 100ns，实际 {r0['window'].get('from_text')}")
    check(r0["window"]["whole_file"] is False, "给了 --from 就不是全文件窗口")
    check(r0["window"]["unit"] == "ns", "1ps 精度的时间戳该按 ns 显示")
    # 边界语义：窗口起点正好落在一次变化上时，**那一次算进去**
    # （闭区间）。写清楚是因为两种约定都有人用，而差一次翻转在
    # "两种编码只差几十次"的量级上不是个可以含糊的事。
    code, rep, _ = run(str(v_bin), "--signal", "st", "--from", "#105000")
    check((rep.get("runs") or [{}])[0].get("n_changes") == 27,
          f"★ 起点正好压在变化上（#105000）时该算进去，仍是 27，"
          f"实际 {(rep.get('runs') or [{}])[0].get('n_changes')}")
    code, rep, _ = run(str(v_bin), "--signal", "st", "--from", "#105001")
    check((rep.get("runs") or [{}])[0].get("n_changes") == 26,
          f"★ 起点比那次变化晚 1 个刻度就只剩 26，"
          f"实际 {(rep.get('runs') or [{}])[0].get('n_changes')}")
    # 起点之前的值仍是"前一个值"：窗口里第一次变化的翻转位数要算对。
    # 这就是 count_signal **从头遍历、只筛时间**而不是先切窗口的原因 ——
    # 切窗口的话第一次变化没有前值可比，会被默默丢掉一格。
    #
    # 手算核对：100ns 时 st 停在 7（上次变化在 95ns），窗口里第一次变化
    # 是 105ns 的 7→8 —— **4 位全翻**（0111→1000）。这正是最容易丢的那一格。
    # 之后走到 34，逐位：bit0 每拍翻 = 27；bit1 在 8,10,…,34 = 14；
    # bit2 在 8,12,…,32 = 7；bit3 在 8,16,24,32 = 4。合计 52。
    code, rep, _ = run(str(v_bin), "--signal", "st", "--from", "#100000")
    r0 = (rep.get("runs") or [{}])[0]
    check(r0.get("bit_toggles") == [27, 14, 7, 4],
          f"★ 窗口首格要算对（100ns 时 st=7，下一拍 7→8 四位全翻），"
          f"实际 {r0.get('bit_toggles')}")
    check(r0.get("total_bit_toggles") == 52,
          f"★ 该是 52，实际 {r0.get('total_bit_toggles')}")
    check(r0.get("n_defined_changes") == 27,
          "27 次变化、52 位翻转 —— 又一次说明这两个数不是一回事")
    print(f"  ✓ --from 100ns 后剩 {r0.get('n_changes')} 次变化，窗口文案 100ns；"
          f"起点压在变化上算进去、晚一刻度就不算")

    # ============ 9. 用法错误：一律 64，且不冒充成功 ============
    print("\n--- 负例：用法 / 输入错误 ---")
    bad_cases = [
        ("没有这个信号", [str(v_bin), "--signal", "nope"]),
        ("时间看不懂", [str(v_bin), "--signal", "st", "--from", "10furlongs"]),
        ("输入文件不存在", [str(WORK / "nope.vcd"), "--signal", "st"]),
        ("--label 比输入多", [str(v_bin), str(v_gray), "--signal", "st",
                          "--label", "a", "--label", "b", "--label", "c"]),
        ("JSON 里没有波形记录", [str(WORK / "novcd.json"), "--signal", "st"]),
    ]
    (WORK / "novcd.json").write_text(
        json.dumps({"schema": "vlearn.sim/1", "status": "pass",
                    "artifacts": {"vcd": None}}), encoding="utf-8")
    for desc, args_ in bad_cases:
        code, rep, err = run(*args_)
        check(code == 64, f"{desc}：应退出 64（用法/输入错误），实际 {code}")
        check(rep.get("ok") is False, f"{desc}：不该报成功")
        check("Traceback" not in err,
              f"{desc}：错误路径不许抛 traceback —— 那会把『输入有问题』"
              f"显示成『工具崩了』")
    # JSON 那条要给出去路：说清为什么没有波形
    code, rep, err = run(str(WORK / "novcd.json"), "--signal", "st")
    check("dumpfile" in err or "no-vcd" in err,
          f"★ JSON 里没有波形要说清原因（$dumpfile / --no-vcd），实际：{err[-300:]}")
    # 手表 VCD 头信息
    print("  ✓ 五类都是 64、不冒充成功、不抛 traceback，且给出路")

    # ============ 10. 从 sim.py 的结果 JSON 取波形 ============
    # 这里用**手写的** JSON 指向刚跑出来的真波形：仿真自己的 JSON 记的
    # artifacts.vcd 是 null（测试台把 $dumpfile 写到了 workdir 之外，
    # 见 sim.py 里 _snapshot_vcd 的说明）—— 那条路单独用第 9 节验。
    # 这一段要验的是**路径解析**：JSON 里的路径是相对仓库根记的，
    # 换个 cwd 就跑不通，所以必须按 root 解析。
    print("\n--- 从 sim.py 结果 JSON 取波形（路径按仓库根解析）---")
    (WORK / "ptr.json").write_text(json.dumps({
        "schema": "vlearn.sim/1", "status": "pass",
        "artifacts": {"vcd": str(v_bin.relative_to(ROOT).as_posix())},
    }), encoding="utf-8")
    code, rep, err = run(str(WORK / "ptr.json"), "--signal", "st")
    check(code == 0, f"该能从 JSON 取到波形并数出来，实际退出 {code}；{err[-300:]}")
    check((rep.get("runs") or [{}])[0].get("total_bit_toggles") == 63,
          "★ 取到的该是二进制那一份（63 位）")
    check((rep.get("runs") or [{}])[0].get("origin", "").endswith("ptr.json"),
          "origin 该记的是用户给的那个 JSON，不是解析出来的 VCD")
    print("  ✓ 从 JSON 取到波形，数值与直接给 VCD 一致")

    if fails:
        print(f"\ntoggle 检查未通过：{len(fails)} 条断言失败")
        for f in fails:
            print(f"  ✗ {f}")
        return 1
    print("\ntoggle 检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
