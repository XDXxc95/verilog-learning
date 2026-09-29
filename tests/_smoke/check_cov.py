"""cov.py 的检查：**合并 ≠ 平均**，以及三个计数不许混。

    python tests/_smoke/check_cov.py

这个脚本大部分篇幅在钉一件事：**覆盖率的合并必须按点的并集算，不能把各份的
比值平均一下。**平均在两个方向上都会骗人，而且骗得很像真的：

    tb_cov_a 4/4 = 100%，tb_cov_b 1/3 = 33%
    平均 → 66.5%（听着像"测了三分之二"）
    并集 → 4/5 = 80%（真值：B 够不着的那个洞，A 打到了）

所以第 1 节的断言不是"等于 0.8"这么简单，还要**显式断言它不等于平均**——
一个把两者搞混的实现完全可能碰巧给出 0.8，只有把平均那个数也钉住，
才能确保将来有人"顺手改成平均"时这里会红。

第 9 节的单元用例钉的是文档里承诺的那两组数字（91% vs 50%、9% vs 50%）：
端到端夹具凑不出那么极端的两个分母，与其假装覆盖了，不如直接对着
`merge()` 断言 —— 和 check_regress.py 第 7 节同一个做法。

覆盖永远不翻判定（`affects_verdict` 恒为 False），所以这里也要验
"有洞不翻退出码"。**但"压根没有覆盖数据"要翻** —— 那是工具自己没做成事，
和 `regress.py` 里"扫了一维却毫无差异"同一类：看着绿，其实没在工作。
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
WORK = ROOT / "build" / "_smoke_cov"

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        fails.append(msg)


_n = [0]


def run(*args: str) -> tuple[int, dict]:
    """跑一次 cov.py，返回 (退出码, JSON)。JSON 走 stdout，摘要走 stderr。"""
    _n[0] += 1
    p = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "cov.py"), *args,
         "--quiet", "--workdir", str(WORK / f"c{_n[0]:02d}")],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    try:
        return p.returncode, json.loads(p.stdout)
    except (json.JSONDecodeError, ValueError):
        return p.returncode, {}


def _pnt(rep: dict, name: str) -> dict:
    return next((p for p in (rep.get("points") or []) if p["name"] == name), {})


def _num(x) -> float:
    """把可能是 None / 缺字段的东西变成"能比较的数"。

    ★ 不是洁癖：`cov.py` 一旦有结构回归，报告里的字段会缺，
    裸写 `rep["ratio"] - avg` 会让**本检查脚本自己抛 TypeError** ——
    护栏是响了，却响成一片 traceback，没人看得出是哪一条规矩破了。
    带上这个名字的断言红，才是可读的失败。
    """
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else float("nan")


def main() -> int:
    if not detect(None).ok:
        print("⊘ 没有 iverilog，跳过 cov 检查（先跑 setup_toolchain.py）")
        return 0

    if WORK.exists():
        shutil.rmtree(WORK)

    # ================= 1. ★ 正例：并集，不是平均 =================
    print("--- ★ 正例：两份测试台合并（并集 ≠ 平均）---")
    code, rep = run(str(SMOKE / "dut_counterN.v"),
                    "--tb", str(SMOKE / "tb_cov_a.v"),
                    "--tb", str(SMOKE / "tb_cov_b.v"))
    check(code == 0, f"有洞不该翻退出码（覆盖不进判定），实际退出 {code}")
    check(rep.get("ok") is True, "ok 应为 true")
    check(rep.get("affects_verdict") is False,
          "★ affects_verdict 必须恒为 False —— 覆盖不许有机会翻判定")
    check(rep.get("n_runs") == 2, f"n_runs 应为 2，实际 {rep.get('n_runs')}")
    check(rep.get("n_runs_with_cover") == 2, "两份都该有覆盖数据")
    check(rep.get("n_points") == 5,
          f"并集应有 5 个点（A 4 + B 独有的 1），实际 {rep.get('n_points')}")
    check(rep.get("n_covered") == 4, f"应覆盖 4 个点，实际 {rep.get('n_covered')}")
    check(rep.get("ratio") == 0.8, f"★ 合并比值应为 0.8，实际 {rep.get('ratio')}")

    # ★ 核心：把"平均"那个数也算出来，钉住"不许拿它当结论"
    runs = rep.get("runs") or []
    # 0.333 是 sim.py 那边 `round(1/3, 3)` 的结果，照抄实测值
    r_a = _num(runs[0].get("ratio")) if len(runs) > 0 else float("nan")
    r_b = _num(runs[1].get("ratio")) if len(runs) > 1 else float("nan")
    check(r_a == 1.0 and r_b == 0.333,
          f"两份各自的比值应为 1.0 和 0.333，实际 {r_a} 和 {r_b}")
    ratio = _num(rep.get("ratio"))
    avg = (r_a + r_b) / 2
    check(abs(ratio - avg) > 0.1,
          f"★ 合并值({ratio}) 必须和平均({avg:.3f})明显不同 —— "
          "若相等，很可能是实现里偷偷平均了")
    check(not rep.get("complete"),
          "★ B 拼错的那个点谁都没打到，complete 必须是 False")
    print(f"  ✓ 合并 4/5 = {ratio:.0%}，而两份比值平均是 {avg:.1%}"
          f"（差 {abs(ratio - avg) * 100:.1f} 个点）；affects_verdict=False")

    # ================= 2. 并集语义：洞的下场各不相同 =================
    print("\n--- 并集语义：一个洞被补上、一个仍是洞 ---")
    # count_wraps：B 声明了但够不着（WIDTH=4），A 打到了 → 合并后**不是洞**
    cw = _pnt(rep, "count_wraps")
    check("count_wraps" not in (rep.get("holes") or []),
          "★ count_wraps 在 B 是洞、在 A 打到了 —— 合并后不该再算洞（这正是合并的意义）")
    check(cw.get("covered_by") == ["tb_cov_a"],
          f"count_wraps 应只有 tb_cov_a 打到，实际 {cw.get('covered_by')}")
    # reset_during_count：谁都没打到 → 合并后仍然是洞
    check((rep.get("holes") or []) == ["reset_during_count"],
          f"★ 只剩一个洞（B 拼错的那个），实际 {rep.get('holes')}")
    rd = _pnt(rep, "reset_during_count")
    check(rd.get("declared_by") == ["tb_cov_b"],
          f"声明者应只有 tb_cov_b，实际 {rd.get('declared_by')}")
    check(rd.get("n_covered_by") == 0, "它该被 0 份打到")
    # hold_when_disabled：两份都打到 → "厚"
    check(_pnt(rep, "hold_when_disabled").get("n_covered_by") == 2,
          "hold_when_disabled 两份都打到了，n_covered_by 应为 2")
    print("  ✓ count_wraps 被 A 补上（不再是洞）；reset_during_count 仍是洞；"
          "hold_when_disabled 被两份打到")

    # ================= 3. undeclared_hits 不许进分子 =================
    print("\n--- ★ 拼错的命中：只进 undeclared_hits，不进分子 ---")
    uh = rep.get("undeclared_hits") or []
    check([u["name"] for u in uh] == ["reset_during_cnt"],
          f"应报出拼错的那一个命中，实际 {[u['name'] for u in uh]}")
    check(uh and uh[0]["hit_by"] == ["tb_cov_b"], "该说明是在哪份里打到的")
    check("reset_during_cnt" not in [p["name"] for p in (rep.get("points") or [])],
          "★ 无声明的命中**不是覆盖点** —— 它连分母都没有，不许进 points")
    # 反证：若把它错算成覆盖，分子会变成 5、比值变成 1.0
    check(rep.get("n_covered") == 4 and rep.get("ratio") == 0.8,
          "★ 分子必须是 4 —— 把无声明命中算进分子就会虚高成 5/5")
    check(any("VLEARN_COVER_DECL" in a for a in (rep.get("next_actions_zh") or [])),
          "next_actions 该告诉人补声明")
    print("  ✓ reset_during_cnt 只出现在 undeclared_hits，分子仍是 4（没有虚高）")

    # ================= 4. 「薄」的识别 =================
    print("\n--- 「薄」：不是洞，但最省力的加深方向 ---")
    thin = {t["name"]: t["n_covered_by"] for t in (rep.get("thin") or [])}
    check(set(thin) == {"reset_clears_q", "count_up_sequence", "count_wraps"},
          f"应识别出 A 独有的 3 个点，实际 {set(thin)}")
    check("hold_when_disabled" not in thin, "两份都打到的点不该算薄")
    check(all(v == 1 for v in thin.values()), f"这几个都该是 1/2，实际 {thin}")
    print(f"  ✓ 薄的点 {sorted(thin)}（各被 1/2 份打到）")

    # ========= 5. ★ 三个计数分开：跑了 ≠ 带覆盖数据 =========
    print("\n--- ★ 混合：一份有覆盖 + 一份只有判定 ---")
    code, rep = run(str(SMOKE / "dut_counterN.v"),
                    "--tb", str(SMOKE / "tb_cov_a.v"),
                    "--tb", str(SMOKE / "tb_nocov.v"))
    check(code == 0, f"应退出 0，实际 {code}")
    check(rep.get("n_runs") == 2,
          f"★ n_runs 要把没写覆盖点的那份也算进去，实际 {rep.get('n_runs')}")
    check(rep.get("n_runs_with_cover") == 1,
          f"★ n_runs_with_cover 应为 1 —— 两个数分开报，"
          f"只看一个数就说不清情况，实际 {rep.get('n_runs_with_cover')}")
    # 没声明覆盖点的那份**既不进分子也不进分母**
    check(rep.get("n_points") == 4,
          f"★ 无覆盖数据的那份不该贡献分母，应仍是 4，实际 {rep.get('n_points')}")
    check(rep.get("ratio") == 1.0,
          f"★ 它也不是「覆盖了 0 个点」—— 比值应仍是 A 的 1.0，"
          f"若被算成 4/8=0.5 就是把两种情况混了，实际 {rep.get('ratio')}")
    check(rep.get("failed_runs") == [],
          "★ 它**跑了、判定也过了**，不许归进 failed_runs（那是给没跑出结果的）")
    print(f"  ✓ n_runs=2 / n_runs_with_cover=1；分母仍是 4、比值仍是 100%，"
          f"failed_runs 为空")

    # ================= 6. 负例：一份覆盖数据都没有 =================
    print("\n--- 负例：没有任何一份带覆盖数据 ---")
    code, rep = run(str(SMOKE / "dut_counterN.v"), "--tb", str(SMOKE / "tb_nocov.v"))
    check(code == 1, f"★ 工具被叫来做覆盖合并却没有数据，必须判失败，实际退出 {code}")
    check(rep.get("ok") is False, "ok 应为 false")
    check(rep.get("status") == "no_cover", f"应中断为 no_cover，实际 {rep.get('status')}")
    check(rep.get("n_runs") == 1 and rep.get("n_runs_with_cover") == 0,
          "★ 两个计数仍要如实报（跑了 1 份、0 份有数据），不能因为失败就丢掉")
    check(rep.get("n_points") == 0 and rep.get("ratio") is None,
          "★ 没有分母时 ratio 必须是 None，**不能是 0.0** —— "
          "0.0 会被读成『覆盖了 0%』，和『没统计』是相反的两件事")
    check(rep.get("complete") is False,
          "★ 没有分母时 complete 必须是 False，绝不能取 True")
    check(rep.get("failed_runs") == [],
          "没写覆盖点 ≠ 没跑出结果，不该混进 failed_runs")
    print("  ✓ 退出 1、status=no_cover、ratio=None、complete=False，"
          "且和 failed_runs 分得清")

    # 对照：有洞**不**翻退出码（第 1 节已经验过 0，这里再点一次名）
    code, rep = run(str(SMOKE / "dut_counterN.v"),
                    "--tb", str(SMOKE / "tb_cov_a.v"),
                    "--tb", str(SMOKE / "tb_cov_b.v"))
    check(code == 0 and rep.get("n_missed", 0) == 1,
          "★ 有洞（n_missed=1）但退出码必须是 0 —— 洞是信息不是失败")
    print("  ✓ 有 1 个洞时退出码仍是 0（洞不翻判定，也不翻退出码）")

    # ================= 7. --from：取已有结果，不重跑 =================
    print("\n--- --from：从已有的结果 JSON 取覆盖数据 ---")
    sim_json = WORK / "sim_a.json"
    p = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "sim.py"),
         str(SMOKE / "dut_counterN.v"), "--tb", str(SMOKE / "tb_cov_a.v"),
         "--json", str(sim_json), "--quiet",
         "--workdir", str(WORK / "sim_a")],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    check(p.returncode == 0, f"配合用的 sim.py 该跑通，实际 {p.returncode}")
    _n[0] += 1
    p2 = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "cov.py"), "--from", str(sim_json),
         "--quiet", "--json", str(WORK / "cov_from_sim.json")],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    check(p2.returncode == 0, f"--from sim JSON 应退出 0，实际 {p2.returncode}")
    try:
        rep = json.loads(p2.stdout)
    except ValueError:
        rep = {}
    check(rep.get("n_runs") == 1 and rep.get("n_points") == 4,
          f"★ 应取到 A 声明的那 4 个点，实际 {rep.get('n_points')}")
    check(rep.get("ratio") == 1.0, f"A 是 4/4，应得 1.0，实际 {rep.get('ratio')}")

    # 负例：regress.py 的**摘要**结果缺 hit/declared，必须拒绝而不是猜
    _n[0] += 1
    dig = WORK / "regress_digest.json"
    p3 = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "regress.py"),
         str(SMOKE / "dut_counterN.v"), "--tb", str(SMOKE / "tb_counterN.v"),
         "--top", "tb_counterN", "--param", "WIDTH=3,4",
         "--json", str(dig), "--quiet", "--workdir", str(WORK / "rg_dig")],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    check(p3.returncode == 0, f"配合用的 regress.py 该跑通，实际 {p3.returncode}")
    p4 = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "cov.py"), "--from", str(dig), "--quiet"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    check(p4.returncode == 64, f"★ 摘要形式必须拒绝，实际退出 {p4.returncode}")
    check("full" in p4.stdout and "hit" in p4.stdout,
          "★ 拒绝的理由要给出路：提示 --full 重跑、并说清缺的是 hit/declared")
    check(not p4.stdout.strip().startswith("{\"schema\": \"vlearn.cov/1\", \"n_points\""),
          "拒绝时不该吐出一份看着像结果的报告")

    # 正例：regress.py --full 的完整结果（在顶层 full_results 里）能合并
    _n[0] += 1
    full = WORK / "regress_full.json"
    p5 = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "regress.py"),
         str(SMOKE / "dut_counterN.v"), "--tb", str(SMOKE / "tb_counterN.v"),
         "--top", "tb_counterN", "--param", "WIDTH=3,4", "--full",
         "--json", str(full), "--quiet", "--workdir", str(WORK / "rg_full")],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    check(p5.returncode == 0, f"配合用的 regress.py --full 该跑通，实际 {p5.returncode}")
    p6 = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "cov.py"), "--from", str(full), "--quiet"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    check(p6.returncode == 0, f"★ --full 的结果应能合并，实际退出 {p6.returncode}")
    try:
        rep6 = json.loads(p6.stdout)
    except ValueError:
        rep6 = {}
    check(rep6.get("n_runs") == 2, f"应取到 2 个组合，实际 {rep6.get('n_runs')}")
    # WIDTH=3 打满 3/3、WIDTH=4 只有 2/3 → 并集 3/3（平均会是 83.5%）
    check(rep6.get("ratio") == 1.0,
          f"★ WIDTH=4 够不着的那个点被 WIDTH=3 补上，并集应 100%，"
          f"实际 {rep6.get('ratio')}（若是 0.835 就是平均了）")
    check(rep6.get("n_covered") == 3 and rep6.get("n_missed") == 0,
          "并集后不该还有洞")
    print("  ✓ sim JSON 直接可合；regress 摘要被拒（提示 --full）；"
          "--full 的两个组合并成 100%（平均只有 83.5%）")

    # ================= 8. 负例：用法错误 =================
    print("\n--- 负例：用法错误 ---")
    for desc, bad in [
        ("没有任何来源", []),
        ("给了 --tb 却没给设计源文件", ["--tb", str(SMOKE / "tb_cov_a.v")]),
        ("测试台不存在", [str(SMOKE / "dut_counterN.v"),
                    "--tb", str(SMOKE / "no_such_tb.v")]),
        ("--from 的文件不存在", ["--from", str(WORK / "nope.json")]),
    ]:
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "cov.py"), *bad,
                            "--quiet"], cwd=str(ROOT), capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        check(p.returncode == 64, f"{desc}：应退出 64（用法/输入错误），实际 {p.returncode}")
        check(not p.stdout.strip().startswith('{"schema": "vlearn.cov/1", "ok": true'),
              f"{desc}：错误时不该吐一份「成功」的 JSON")
    print("  ✓ 四类都是 64，且不冒充成功")

    # ========== 9. 单元：merge() 的极端构造（端到端凑不出来） ==========
    #
    # 文档里承诺了两组数字，端到端夹具凑不出这么极端的两个分母。
    # 与其假装覆盖了，不如直接对着 merge() 断言 —— 和 check_regress.py
    # 第 7 节同一个做法：**分量撑不出来的地方就在单元级钉住，并说清楚边界。**
    print("\n--- merge() 单元：平均在两个方向上都会骗人 ---")
    import cov as covmod

    def mk(name, declared, hit, status="pass"):
        return covmod._norm_run(
            name=name, origin="unit", status=status,
            cov={"ok": bool(declared), "declared": list(declared),
                 "hit": list(hit),
                 "missed": [p for p in declared if p not in set(hit)],
                 "undeclared_hits": [p for p in hit if p not in set(declared)],
                 "n_declared": len(declared),
                 "n_covered": len([p for p in declared if p in set(hit)]),
                 "ratio": (round(len([p for p in declared if p in set(hit)])
                                 / len(declared), 3) if declared else None),
                 "reason": None},
            exit_code=0)

    # 低报方向：A 10 个点打满、B 1 个点一个没打到
    a = mk("A", [f"p{i}" for i in range(10)], [f"p{i}" for i in range(10)])
    b = mk("B", ["q0"], [])
    m = covmod.merge([a, b])
    avg = (1.0 + 0.0) / 2
    check(m["n_points"] == 11 and m["n_covered"] == 10,
          f"并集应 10/11，实际 {m['n_covered']}/{m['n_points']}")
    check(m["ratio"] == 0.909,
          f"★ 低报方向：真值应是 0.909（=10/11），实际 {m['ratio']}")
    check(abs(m["ratio"] - avg) > 0.4,
          f"★ 平均只有 {avg:.0%}、真值 {m['ratio']:.0%}，差得远 —— "
          "这正是「平均在两个方向上都会骗人」里低报的那一半")

    # 高报方向：A 1 个点打满、B 10 个点一个没打到
    c = mk("C", ["q0"], ["q0"])
    d = mk("D", [f"p{i}" for i in range(10)], [])
    m2 = covmod.merge([c, d])
    check(m2["n_points"] == 11 and m2["n_covered"] == 1,
          f"并集应 1/11，实际 {m2['n_covered']}/{m2['n_points']}")
    check(m2["ratio"] == 0.091,
          f"★ 高报方向：真值应是 0.091（=1/11），实际 {m2['ratio']}")
    check(abs(m2["ratio"] - avg) > 0.4,
          f"★ 平均是 {avg:.0%}，把 {m2['ratio']:.1%} 说成了 50% —— 高报，"
          "而且高报的正好是「看起来很努力」的那一侧")
    # 两组的分母一样、分子一样，**只是谁打满反过来了**，
    # 平均给出的结论完全相同（都是 50%），而真值一个 91% 一个 9%。
    check(abs(m["ratio"] - m2["ratio"]) > 0.8,
          "★ 两个构造的平均值一模一样，真值却差 80 个点 —— 这是本脚本存在的理由")
    print(f"  ✓ 同样的两份分母(1 和 10)，平均都答 50%；"
          f"真值一个 {m['ratio']:.1%}、一个 {m2['ratio']:.1%}")

    # 没有覆盖数据的那份：既不进分子也不进分母，但要计入 n_runs
    e = mk("E", [], [], status="pass")
    m3 = covmod.merge([a, e])
    check(m3["n_runs"] == 2 and m3["n_runs_with_cover"] == 1,
          f"★ 两个计数要分开，实际 {m3['n_runs']}/{m3['n_runs_with_cover']}")
    check(m3["n_points"] == 10 and m3["ratio"] == 1.0,
          f"★ 无覆盖数据的那份不许改动分母和比值，实际 "
          f"{m3['n_covered']}/{m3['n_points']} = {m3['ratio']}")
    check(m3["failed_runs"] == [], "它跑了、判定过了，不该进 failed_runs")
    # 一份数据都没有时 status/ratio 的形状
    m4 = covmod.merge([e])
    check(m4["status"] == "no_cover" and m4["ok"] is False,
          f"完全没有数据时该是 no_cover/ok=False，实际 {m4['status']}/{m4['ok']}")
    check(m4["ratio"] is None and m4["complete"] is False,
          "★ 没有分母时 ratio 必须 None、complete 必须 False")
    # 跑了但没出结果的，要单独归到 failed_runs
    f = mk("F", [], [], status="compile_error")
    m5 = covmod.merge([a, f])
    check(m5["status"] == "run_failed" and len(m5["failed_runs"]) == 1,
          f"★ 没跑出结果的那份该进 failed_runs 并翻 status，实际 {m5['status']}")
    check(m5["n_points"] == 10,
          "它的缺席不该改动另一些 run 的分母")
    print("  ✓ 无数据的那份不进分子分母但仍计入 n_runs；"
          "没跑出结果的单独归 failed_runs 并翻 status")

    if fails:
        print(f"\ncov 检查未通过：{len(fails)} 条断言失败")
        for f_ in fails:
            print(f"  ✗ {f_}")
        return 1
    print("\ncov 检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
