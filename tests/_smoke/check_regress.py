"""regress.py 的检查：批量回归的编排、核对与**证伪能力**。

    python tests/_smoke/check_regress.py

这个脚本里最重要的不是"能跑通"，而是**负例真的会被拦下来**。

难点在于：批量回归最典型的失败是"扫了一堆取值，其实每一轮跑的是同一个电路"，
而它的表现是 —— 所有组合全绿、退出码 0、没有任何异常。
一个只能报绿的脚本在这里和"完全没工作"长得一模一样。
所以这里的每一条正例都配了一条只差一点点的负例：

    正例 tb_counterN.v        参数透传了 + 自报生效值  → 通过，且报"有差异"
    负例 tb_counterN_inert.v  参数没透传 + 不自报      → **必须判失败**
    正例 tb_macrow.v          WIDTH 有差异 / TRACE 无差异但自报了 → 都通过

其中 tb_macrow 的 TRACE 那一轴是刻意留的分界线：同样"扫下来结果一样"，
因为**有生效值证据**，结论就从"失败"变成"信息"。两个夹具合起来才钉死这条线。
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
WORK = ROOT / "build" / "_smoke_regress"

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        fails.append(msg)


def run(*args: str) -> tuple[int, dict]:
    """跑一次 regress.py，返回 (退出码, JSON)。

    JSON 从 **stdout** 取 —— 那是工具的契约（人类摘要走 stderr）。
    解析失败时返回空 dict，由调用方断言报错，不在这里抛。
    """
    p = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "regress.py"), *args,
         "--quiet", "--workdir", str(WORK / _tag(args))],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    try:
        return p.returncode, json.loads(p.stdout)
    except (json.JSONDecodeError, ValueError):
        return p.returncode, {}


_TAGN = [0]


def _tag(args) -> str:
    _TAGN[0] += 1
    return f"c{_TAGN[0]:02d}"


def _kinds(rep: dict) -> set[str]:
    return {p.get("kind") for p in (rep.get("problems") or [])}


def main() -> int:
    if not detect(None).ok:
        print("⊘ 没有 iverilog，跳过 regress 检查（先跑 setup_toolchain.py）")
        return 0

    if WORK.exists():
        shutil.rmtree(WORK)

    # ================= 1. 正例：参数真的扫出了差异 =================
    print("--- 正例：参数化计数器扫 WIDTH ---")
    code, rep = run(str(SMOKE / "dut_counterN.v"), "--tb", str(SMOKE / "tb_counterN.v"),
                    "--top", "tb_counterN", "--param", "WIDTH=3:5")
    check(code == 0, f"应退出 0，实际 {code}（problems={_kinds(rep)}）")
    check(rep.get("ok") is True, "ok 应为 true")
    check(rep.get("n_combos") == 3, f"应跑 3 个组合，实际 {rep.get('n_combos')}")
    check(rep.get("n_combos_planned") == 3, "计划组合数应为 3")

    ax = (rep.get("axes") or [{}])[0]
    check(ax.get("name") == "WIDTH", "轴名应为 WIDTH")
    # ★ 核心断言：这一维必须被判定为"真的起了作用"
    check(ax.get("n_varying_groups", 0) >= 1,
          "WIDTH 应至少在一组对照里造成差异（否则等于没扫）")
    check(ax.get("inert") is False, "WIDTH 不该被判成 inert")
    check(ax.get("effective_ok") is True,
          "测试台自报了 EFFECTIVE_WIDTH，生效值核对应通过")
    check(ax.get("effective") == {"3": "3", "4": "4", "5": "5"},
          f"生效值应逐档对上，实际 {ax.get('effective')}")
    check(not ax.get("mismatches"), "不该有生效值不符")

    combos = rep.get("combos") or []
    check(len(combos) == 3, "combos 长度应为 3")
    check(all(c.get("status") == "pass" for c in combos),
          "三个组合都该判定通过")
    # 覆盖有洞必须是 info，不能把 ok 翻成 false —— 覆盖不进判定，这条对齐内核
    cov_problems = [p for p in (rep.get("problems") or [])
                    if p.get("kind") == "coverage_hole"]
    check(bool(cov_problems), "WIDTH=4/5 打不到 count_wraps，应报出覆盖洞")
    check(all(p.get("severity") == "info" for p in cov_problems),
          "覆盖洞必须是 info —— 它说明测得浅，不是判定失败")
    check("count_wraps" in json.dumps(rep, ensure_ascii=False),
          "覆盖洞的细节里应点名 count_wraps")
    print(f"  ✓ 3 个组合全过；WIDTH 在 {ax.get('n_groups')} 组对照里"
          f" {ax.get('n_varying_groups')} 组有差异；生效值 3/4/5 逐档核对一致")

    # ================= 2. 正例：宏扫描 + "无差异但有证据" =================
    print("\n--- 正例：宏扫描（WIDTH 有差异 / TRACE 无差异但自报） ---")
    code, rep = run(str(SMOKE / "dut_macrow.v"), "--tb", str(SMOKE / "tb_macrow.v"),
                    "--top", "tb_macrow",
                    "--define", "WIDTH=3,4", "--define", "TRACE=0,1")
    check(code == 0, f"应退出 0，实际 {code}（problems={_kinds(rep)}）")
    check(rep.get("n_combos") == 4, f"2×2 应得 4 个组合，实际 {rep.get('n_combos')}")
    by_name = {a["name"]: a for a in (rep.get("axes") or [])}
    check(set(by_name) == {"WIDTH", "TRACE"}, f"应有两个轴，实际 {set(by_name)}")

    w, t = by_name.get("WIDTH", {}), by_name.get("TRACE", {})
    # WIDTH 换位宽 → 回绕与否变了 → 覆盖命中集变了 → 必须看出差异
    check(w.get("n_varying_groups", 0) == 2,
          f"WIDTH 应在 2 组对照（TRACE=0/1）里都有差异，实际 {w.get('n_varying_groups')}")
    check(w.get("inert") is False, "WIDTH 不该是 inert")
    # TRACE 只控制一行调试打印，判定/覆盖都不变 —— 但自报了生效值，
    # 所以结论是"确实传下去了，只是观察不到"，是 info 而不是失败
    check(t.get("inert") is True, "TRACE 扫下来结果一样，应被判成 inert")
    check(t.get("effective_ok") is True, "TRACE 自报了生效值，核对应通过")
    check("inert_but_verified" in _kinds(rep),
          f"应报 inert_but_verified，实际 {_kinds(rep)}")
    check("sweep_looks_inert" not in _kinds(rep),
          "有生效值证据时**不该**报成失败 —— 这条正是本夹具存在的理由")
    check(all(p.get("severity") == "info" for p in (rep.get("problems") or [])
              if p.get("kind") == "inert_but_verified"),
          "inert_but_verified 必须是 info（不翻 status）")
    print(f"  ✓ WIDTH {w.get('n_varying_groups')}/2 组有差异；"
          f"TRACE 0 差异但有生效值证据 → info，不判失败")

    # ================= 3. 负例：参数没透传，扫了等于没扫 =================
    print("\n--- 负例：参数没透传到待测模块 ---")
    code, rep = run(str(SMOKE / "dut_counterN.v"),
                    "--tb", str(SMOKE / "tb_counterN_inert.v"),
                    "--top", "tb_counterN_inert", "--param", "WIDTH=3:5")
    check(code != 0, "★ 参数没生效却全绿，必须判失败（这是本工具最核心的一条）")
    check(rep.get("ok") is False, "ok 应为 false")
    check("sweep_looks_inert" in _kinds(rep),
          f"应报 sweep_looks_inert，实际 {_kinds(rep)}")
    ax = (rep.get("axes") or [{}])[0]
    check(ax.get("inert") is True, "应判成 inert")
    check(ax.get("evidence") == "none", "该夹具不打印生效值，证据应是 none")
    # 每一轮自己是判 pass 的 —— 拦下来靠的是核对，不是判定
    check(all(c.get("status") == "pass" for c in (rep.get("combos") or [])),
          "三轮自身的判定都该是 pass（所以只看单轮永远发现不了这个 bug）")
    msg = json.dumps(rep, ensure_ascii=False)
    check("EFFECTIVE_WIDTH" in msg,
          "提示里应告诉用户怎么补上生效值自报")
    check("透传" in msg, "提示里应点出'透传'这个动作")
    print("  ✓ 三轮各自 pass，但整体判失败并指明缺少生效值证据")

    # 同一条负例加 --allow-inert 之后必须放行，否则这个开关是假的
    code2, rep2 = run(str(SMOKE / "dut_counterN.v"),
                      "--tb", str(SMOKE / "tb_counterN_inert.v"),
                      "--top", "tb_counterN_inert", "--param", "WIDTH=3:5",
                      "--allow-inert")
    check(code2 == 0, f"--allow-inert 应放行，实际退出 {code2}")
    check("sweep_looks_inert" not in _kinds(rep2),
          "--allow-inert 后不该再报 sweep_looks_inert")
    print("  ✓ --allow-inert 能放行（且必须显式加）")

    # ================= 4. 负例：参数名不在测试台上 =================
    print("\n--- 负例：参数名压根不在测试台上 ---")
    code, rep = run(str(SMOKE / "dut_counterN.v"), "--tb", str(SMOKE / "tb_counterN.v"),
                    "--top", "tb_counterN", "--param", "NOPE=1,2",
                    "--param", "WIDTH=3,4")
    check(code != 0, "参数名不存在应判失败")
    check(rep.get("status") == "aborted", f"应中断，实际 {rep.get('status')}")
    check(rep.get("n_combos") == 1,
          f"应在第一轮就停下，实际跑了 {rep.get('n_combos')} 轮")
    check(rep.get("n_combos_planned") == 4, "计划组合数仍应是 4")
    check("param_not_found" in json.dumps(rep, ensure_ascii=False),
          "应带上 iverilog 的 param_not_found 判据")
    print("  ✓ 第一轮就中断，没有把同一条错误重复 4 遍")

    # ================= 5. 负例：用法错误 =================
    print("\n--- 负例：用法错误 ---")
    for desc, bad in [
        ("没有给任何维度", [str(SMOKE / "dut_counterN.v"), "--tb",
                       str(SMOKE / "tb_counterN.v")]),
        ("维度写法不对", [str(SMOKE / "dut_counterN.v"), "--tb",
                     str(SMOKE / "tb_counterN.v"), "--param", "WIDTH"]),
        ("区间写反了", [str(SMOKE / "dut_counterN.v"), "--tb",
                    str(SMOKE / "tb_counterN.v"), "--param", "WIDTH=9:2"]),
        ("组合数超上限", [str(SMOKE / "dut_counterN.v"), "--tb",
                     str(SMOKE / "tb_counterN.v"), "--param", "WIDTH=1:9999"]),
    ]:
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "regress.py"), *bad],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        check(p.returncode == 64, f"{desc}：应退出 64（用法错误），实际 {p.returncode}")
        check(not p.stdout.strip().startswith("{"),
              f"{desc}：用法错误时不该往 stdout 吐 JSON")
    print("  ✓ 四类用法错误都是 64，且不污染 stdout")

    # ================= 6. 逐档结果确实不同（真的换了电路） =================
    print("\n--- 逐档结果 ---")
    code, rep = run(str(SMOKE / "dut_counterN.v"), "--tb", str(SMOKE / "tb_counterN.v"),
                    "--top", "tb_counterN", "--param", "WIDTH=3,4")
    fps = {tuple(sorted(c["params"].items())): c["fingerprint"]
           for c in (rep.get("combos") or [])}
    check(len(set(fps.values())) == 2,
          "WIDTH=3 与 WIDTH=4 的指纹必须不同（覆盖命中集不一样）")
    check(all(c.get("effective", {}).get("WIDTH")
              for c in (rep.get("combos") or [])),
          "每个组合都应取到自报的生效值")
    print("  ✓ 两档指纹不同，说明扫的确实是两个不同的电路")

    # ================= 7. 指纹的各个分量 =================
    #
    # 这一节是**单元级**的，不是端到端：现有两个夹具里，判定状态和覆盖命中集
    # 已经足以区分各档参数，所以"用例逐条结果"和"错误标签"这两个分量
    # 在端到端用例里**没有被单独逼出来过**。与其假装它们被覆盖了，
    # 不如直接对着 _fingerprint 断言：分量不同 → 指纹必须不同。
    # （拆掉这两个分量的端到端实验做过：check 全绿 —— 也就是说，
    #   它们目前是冗余的防御，不是端到端里唯一起作用的那一环。）
    print("\n--- 指纹分量（单元） ---")
    import regress as regressmod

    base = {"status": "pass",
            "tests": [{"name": "t1", "status": "pass"}, {"name": "t2", "status": "pass"}],
            "cover": {"hit": ["a"], "missed": []},
            "error_tags": []}
    f0 = regressmod._fingerprint(base)

    v = json.loads(json.dumps(base))
    v["tests"][1]["status"] = "fail"
    check(regressmod._fingerprint(v) != f0,
          "某一用例由 pass 变 fail，指纹必须变（否则'哪一档挂了哪条用例'看不出来）")
    # 状态都是 fail、但挂在不同的用例上 —— 只看总状态是分不出来的
    a = json.loads(json.dumps(base))
    a["status"] = a["tests"][0]["status"] = "fail"
    b = json.loads(json.dumps(base))
    b["status"] = b["tests"][1]["status"] = "fail"
    check(regressmod._fingerprint(a) != regressmod._fingerprint(b),
          "两档都判 fail、但挂的用例不同，指纹必须不同")

    v = json.loads(json.dumps(base))
    v["error_tags"] = ["latch_inference"]
    check(regressmod._fingerprint(v) != f0, "error_tags 变了，指纹必须变")

    # 反过来：判定/用例/覆盖/标签都没变时，指纹必须稳定 ——
    # 掺进时间戳之类的噪声，指纹就永远"有差异"，inert 再也报不出来
    check(regressmod._fingerprint(json.loads(json.dumps(base))) == f0,
          "内容完全相同的两轮，指纹必须一样")
    print("  ✓ 用例逐条结果 / error_tags / 稳定性，四个分量各自成立")

    if fails:
        print(f"\nregress 检查未通过：{len(fails)} 条断言失败")
        for f in fails:
            print(f"  ✗ {f}")
        return 1
    print("\nregress 检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
