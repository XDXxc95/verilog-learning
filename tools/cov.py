"""把多份测试台的功能覆盖点**合并**成一张表。

    python tools/cov.py solutions/L2-03/fifo.v \
        --tb tests/L2-03/tb_fifo_basic.v --tb tests/L2-03/tb_fifo_full.v

`sim.py` 报的是一**份**测试台自己的覆盖情况；`cov.py` 回答的是另一个问题：
**这一组测试台合起来，把设计该验的场景验全了没有、哪些死角一次都没碰过。**

## 为什么不能只是"把各份的覆盖率平均一下"

这是本脚本存在的唯一理由，也是最容易写错的一行。**平均比值在两个方向上都会骗人**：

    两份测试台，A 声明了 10 个点、打满 10 个；B 声明了 1 个点、一个没打到。
      · 平均比值 = (100% + 0%) / 2 = **50%** —— 听着像"测了一半"
      · 真相：合并分母 11 个点、打中 10 个 = **91%**，只有 B 那 1 个是洞
      平均**低报**了。

    反过来，A 声明 1 个点、打中 1 个；B 声明 10 个点、打中 0 个。
      · 平均比值 = (100% + 0%) / 2 = **50%**
      · 真相：合并 11 个点打中 1 个 = **9%**
      平均**高报**了，而且高报的正好是"看起来很努力"的那一侧。

所以这里**只做分母的并集**：一个点只要**任何一份**测试台打到过，就算覆盖到了
（本来就是"这个场景被验过没有"）。比值由合并后的分子分母现算，绝不拿几份的
比值去平均。

## 三个必须分开报的数（同 `lint.ran` / `lint.n_warnings` 的教训）

- `n_runs` 与 `n_runs_with_cover`：**跑了 5 份**和**有 5 份带覆盖数据**是两回事。
  没写覆盖标记的那份不是"覆盖 0 个点"，它是**没有分母**，不能混进合并里。
- **合并后的总比值**和**每一份自己的比值**：只在摘要里给一个总比值，
  会掩盖"5 份里 4 份其实是空的"。两个都要给。
- `undeclared_hits`：打了 `TEST_COVER_HIT` 却没有 `TEST_COVER_DECL` 的点。
  **这不是覆盖，是分母错了** —— 它连一个点都不算进 `n_points`，
  但必须大声报出来（少了声明，覆盖率算出来就是自欺）。

## 判定与这里无关

和 `sim.py` 的 cover 块一样，`affects_verdict` 恒为 False，覆盖**永远不翻判定**。
本脚本的退出码也不受"有洞"影响 —— 有洞是信息，不是失败。

**但"压根没有覆盖数据"是工具自己的失败**（退出码非 0）。这条线要划清楚：
洞是"测得不全"，工具照常完成了任务；没有数据是"工具没做成它被叫来做的事"，
和 `regress.py` 里"某一维扫下来毫无差异"属于同一类 —— 看着全绿，其实没在工作。

## 输入来源

- 不给 `--from`：`cov.py` 自己按 `--tb` 逐份跑一遍（每份一个独立的 sim 判定）。
- 给 `--from`：直接从已有的 sim.py JSON 里取 cover 块，**不重跑**。
  regress.py 的 JSON 只有**带 `--full` 时**才有 cover 明细；摘要形式缺
  `hit` / `declared`，本脚本会明确拒绝而不是猜。

覆盖点协议（`TEST_COVER_DECL` / `TEST_COVER_HIT`）见 `tb/tb_common.vh`。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import sim as simmod               # noqa: E402
from _console import init_console  # noqa: E402
from _toolchain import detect, repo_root  # noqa: E402

SCHEMA = "vlearn.cov/1"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ENV = 2
EXIT_USAGE = 64

# 覆盖点的名字来自测试台里的字符串，理论上可能重复声明。去重时**保序**，
# 让报告里的点按"第一次被声明的顺序"排，读起来和测试台对得上。
_MAX_JSON_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------- 一趟覆盖


def _run_one(root: Path, designs: list[Path], tb: Path, top: str | None,
             workdir: Path, timeout: float, no_lint: bool) -> dict:
    """跑一份测试台，只取它那趟的 cover 块。判定由 sim.py 内核完成。"""
    name = tb.stem
    cfg = simmod.SimConfig(
        designs=designs,
        tb=tb,
        top=top,
        workdir=workdir / name,
        name=name,
        timeout=timeout,
        gflags=[],
        includes=[],
        defines=[],
        syntax_only=False,
        no_vcd=True,
        no_observe=True,
        no_lint=no_lint,
        iverilog_path=None,
        verilator_path=None,
        params=[],
    )
    result, code = simmod._run_sim(cfg)
    cov = result.get("cover") or {}
    return _norm_run(
        name=name, origin=tb.as_posix(), status=result.get("status"),
        cov=cov, exit_code=code, workdir=str(cfg.workdir),
        verdict=result.get("verdict") or {},
    )


def _norm_run(name: str, origin: str, status: str | None, cov: dict,
              exit_code: int, workdir: str | None = None,
              verdict: dict | None = None) -> dict:
    """把一份 cover 块归一成合并器认的形状。

    ★ `has_cover` 取的是 `cov["ok"]` **而不是** `n_declared > 0`：
      sim.py 里没有声明时 ok=False、n_declared=0；两者现在一致，
      但"这份测试台带覆盖数据"这个判断的**权威口径在 sim.py 那边**，
      这里跟着它走，免得将来 sim.py 改了判据而这里悄悄用旧口径。
    """
    v = verdict or {}
    return {
        "name": name,
        "origin": origin,
        "status": status,
        "exit_code": exit_code,
        "workdir": workdir,
        "has_cover": bool(cov.get("ok")),
        "declared": list(cov.get("declared") or []),
        "hit": sorted(set(cov.get("hit") or [])),
        "missed": list(cov.get("missed") or []),
        "undeclared_hits": sorted(set(cov.get("undeclared_hits") or [])),
        "n_declared": int(cov.get("n_declared") or 0),
        "n_covered": int(cov.get("n_covered") or 0),
        "ratio": cov.get("ratio"),
        "reason": cov.get("reason"),
        "passed": v.get("passed"),
        "failed": v.get("failed"),
        "total": v.get("total"),
    }


def _runs_from_json(path: Path) -> tuple[list[dict], str | None]:
    """从已有的 JSON 里取出 cover 块。返回 (runs, 拒绝的原因)。"""
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return [], f"{path} 太大了（超过 {_MAX_JSON_BYTES // 1024 // 1024} MB），不像是结果 JSON。"
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as e:
        return [], f"{path} 读不出 JSON：{e}"

    if not isinstance(data, dict):
        return [], f"{path} 顶层不是对象，不是本仓库的结果 JSON。"

    # ---- sim.py 的结果：顶层直接有 cover 块 ----
    if isinstance(data.get("cover"), dict):
        cov = data["cover"]
        if cov.get("declared") is None and cov.get("hit") is None:
            return [], (f"{path} 的 cover 块缺 declared/hit 明细，合并不了。")
        return [_norm_run(
            name=path.stem, origin=path.as_posix(), status=data.get("status"),
            cov=cov, exit_code=EXIT_OK,
            verdict=data.get("verdict") or {},
        )], None

    # ---- regress.py 的结果 ----
    #
    # ★ `--full` 时完整的 sim 结果在**顶层 `full_results`** 里（和 `combos`
    #   平行的一个列表），**不在** `combos[].result` 里。这一点是实测确认的：
    #   第一版按 `combos[].result` 找，于是连 --full 的 JSON 也被判成摘要、
    #   被拒绝 —— 报错话说得挺对，只是找错了地方。
    full = data.get("full_results")
    if isinstance(full, list) and full:
        runs = []
        for i, r in enumerate(full, 1):
            if not isinstance(r, dict):
                continue
            combos = data.get("combos") or []
            label = ""
            if i - 1 < len(combos) and isinstance(combos[i - 1], dict):
                label = _combo_label(combos[i - 1])
            runs.append(_norm_run(
                name=label or f"combo{i}", origin=f"{path.as_posix()}#{i}",
                status=r.get("status"), cov=r.get("cover") or {},
                exit_code=EXIT_OK, verdict=r.get("verdict") or {},
            ))
        if runs:
            return runs, None

    if isinstance(data.get("combos"), list):
        return [], (
            f"{path} 像是 regress.py 的**摘要**结果：每条 combo 只有计数、"
            "没有 hit/declared 明细，合并出来的覆盖率会是错的。"
            "用 `--full` 重跑一次 regress.py（完整结果在顶层 full_results 里），"
            "或者直接给 cov.py 传 --tb 让它自己跑。"
        )

    return [], f"{path} 认不出是哪种结果 JSON（既没有 cover，也没有 full_results / combos）。"


def _combo_label(combo: dict) -> str:
    merged = {**(combo.get("params") or {}), **(combo.get("defines") or {})}
    return ",".join(f"{k}={v}" for k, v in sorted(merged.items()))


# ---------------------------------------------------------------- 合并


def merge(runs: list[dict]) -> dict:
    """按**点的并集**合并，绝不平均比值。

    返回的 `points` 里每个点都带 `covered_by`（哪几份测试台打到过）——
    这是"测得深不深"唯一诚实的信号。只有一个总比值的话，
    "10 个点各被打到 1 次"和"10 个点被 5 份测试台反复打到"长得一模一样。
    """
    with_cover = [r for r in runs if r["has_cover"]]
    n_runs_cover = len(with_cover)

    # 声明顺序：按 run 的顺序、run 内按声明顺序，去重保序
    declared_order: list[str] = []
    declared_by: dict[str, list[str]] = {}
    for r in with_cover:
        for p in r["declared"]:
            if p not in declared_by:
                declared_by[p] = []
                declared_order.append(p)
            declared_by[p].append(r["name"])

    # 覆盖：每份 run 内部先按集合算（同一个点打中两次不算两次覆盖）
    covered_by: dict[str, list[str]] = {p: [] for p in declared_order}
    all_hits: dict[str, list[str]] = {}
    for r in with_cover:
        for p in r["hit"]:
            all_hits.setdefault(p, []).append(r["name"])
            if p in covered_by and r["name"] not in covered_by[p]:
                covered_by[p].append(r["name"])

    points = [{
        "name": p,
        "declared_by": declared_by[p],
        "covered_by": covered_by[p],
        "n_covered_by": len(covered_by[p]),
    } for p in declared_order]

    # ★ 有命中但**没有任何一份**声明过 —— 连分母都没有，不进 n_points。
    #   这不是覆盖，是测试台的声明漏了。
    undeclared = [{"name": p, "hit_by": rs} for p, rs in sorted(all_hits.items())
                  if p not in declared_by]

    holes = [p for p in points if p["n_covered_by"] == 0]
    # "薄"：被声明了、也打到了，但只有一部分测试台碰到。
    # 它不是洞，但正好是"再加一份测试台就能加深"的那一类。
    thin = [p for p in points
            if 0 < p["n_covered_by"] < n_runs_cover]

    n_points = len(points)
    n_covered = n_points - len(holes)

    # 跑了但没通过编译 / 超时的那些：它们的 has_cover 也是 False，
    # 但原因和"测试台没写覆盖点"完全不同，得单独说。
    failed_runs = [{"name": r["name"], "status": r["status"],
                    "reason": (r.get("reason") or "")[:200]}
                   for r in runs if not r["has_cover"] and r["status"] not in (None, "pass")]

    if failed_runs:
        status, ok = "run_failed", False
    elif n_runs_cover == 0:
        status, ok = "no_cover", False
    else:
        status, ok = "cov_ok", True

    return {
        "ok": ok,
        "status": status,
        # 覆盖不进判定 —— 和 sim.py 的 cover 块保持同一个口径
        "affects_verdict": False,
        "n_runs": len(runs),
        "n_runs_with_cover": n_runs_cover,
        "n_points": n_points,
        "n_covered": n_covered,
        "n_missed": len(holes),
        "ratio": round(n_covered / n_points, 3) if n_points else None,
        "complete": bool(n_points) and not holes,
        "points": points,
        "holes": [p["name"] for p in holes],
        "thin": [{"name": p["name"], "n_covered_by": p["n_covered_by"]}
                 for p in thin],
        "undeclared_hits": undeclared,
        "failed_runs": failed_runs,
        "runs": runs,
    }


# ---------------------------------------------------------------- 输出


def _print_summary(rep: dict) -> None:
    """人类可读摘要。**走 stderr**，和 sim.py / regress.py 同一个约定。"""
    w = sys.stderr.write

    # 中断 / 环境错误时**没有表格可打**（rep 里也没有 runs 这些键）。
    # 硬走下面的表格化代码会 KeyError —— 而那会把"输入有问题"
    # 显示成"工具崩了"，正好盖住真正要传达的那句话。
    if rep.get("error"):
        w("=" * 60 + "\n")
        w(f"覆盖合并未执行：{rep.get('status')}\n")
        w("-" * 60 + "\n")
        w(f"  ✗ {(rep['error'] or {}).get('message')}\n")
        w("=" * 60 + "\n")
        return

    w("=" * 60 + "\n")
    w(f"功能覆盖合并：{rep['n_runs']} 份测试台"
      f"（其中 {rep['n_runs_with_cover']} 份带覆盖数据）\n")
    w("-" * 60 + "\n")

    w(f"  {'测试台':<26}{'判定':<12}{'覆盖':<12}{'用例':<10}\n")
    for r in rep["runs"]:
        cov_s = (f"{r['n_covered']}/{r['n_declared']}"
                 if r["has_cover"] else "—")
        if r["missed"]:
            cov_s += " !"
        tests = (f"{r['passed']}/{r['total']}"
                 if r.get("passed") is not None else "—")
        w(f"  {r['name']:<26}{str(r['status']):<12}{cov_s:<12}{tests:<10}\n")

    w("-" * 60 + "\n")
    if rep["n_points"]:
        w(f"  合并：{rep['n_covered']}/{rep['n_points']} 个点"
          f"（{rep['ratio']:.0%}）"
          f"{'—— 全覆盖 ✓' if rep['complete'] else '，有 ' + str(rep['n_missed']) + ' 个洞'}\n")
        # ★ 两个比值都要给：只给合并值会掩盖"5 份里 4 份其实是空的"
        w(f"  注意这是**合并**后的比值（点的并集），不是各份比值的平均 ——"
          f"\n       各份自己的比值为：")
        per = [f"{r['name']} {r['ratio']:.0%}" for r in rep["runs"] if r["has_cover"]]
        w("、".join(per) + "\n" if per else "（无）\n")

    if rep["holes"]:
        w("-" * 60 + "\n")
        w(f"  一次都没碰到的点（{len(rep['holes'])}）：\n")
        for p in rep["holes"]:
            # 注意是 `next(gen, "?")` 而**不是** `join(next(gen, ["?"]))`：
            # 后者拿到的是一个字符串，`join` 会把它拆成一个个字符，
            # 打印出 `t、b、_、c、o、v…` 这种看着像但完全不是名字的东西。
            src = "、".join(r["name"] for r in rep["runs"] if p in r["declared"]) or "?"
            w(f"    · {p:<28}（由 {src} 声明，一次都没打到）\n")

    if rep["thin"]:
        w("-" * 60 + "\n")
        w(f"  只有部分测试台碰到的点（{len(rep['thin'])}）"
          f"—— 不是洞，但加深起来最省力：\n")
        for p in rep["thin"]:
            w(f"    · {p['name']:<28}被 {p['n_covered_by']}"
              f"/{rep['n_runs_with_cover']} 份打到\n")

    if rep["undeclared_hits"]:
        w("-" * 60 + "\n")
        w(f"  ★ 有命中却没有声明（{len(rep['undeclared_hits'])}）"
          f"—— 这不是覆盖，是**分母漏了**：\n")
        for u in rep["undeclared_hits"]:
            w(f"    · {u['name']:<28}在 {'、'.join(u['hit_by'])} 里打到了，"
              f"但没有任何一份声明过它\n")
        w("      补上 `VLEARN_COVER_DECL(\"<名字>\")`，否则覆盖率算出来是自欺。\n")

    if rep["failed_runs"]:
        w("-" * 60 + "\n")
        w(f"  ★ 这些测试台压根没跑出结果（{len(rep['failed_runs'])}）\n"
          f"    —— 它们不是「覆盖 0 个点」，是**没有分母**，不能混进合并：\n")
        for f in rep["failed_runs"]:
            w(f"    · {f['name']:<26}[{f['status']}] {f['reason']}\n")

    w("-" * 60 + "\n")
    if rep["status"] == "no_cover":
        w("\n结果：没有任何一份测试台使用了覆盖点标记 ✗\n"
          "      （工具被叫来做覆盖合并，却没有数据 —— 这和「有洞」是两回事）\n")
    elif rep["status"] == "run_failed":
        w(f"\n结果：有 {len(rep['failed_runs'])} 份测试台没跑出结果 ✗\n")
    elif rep["complete"]:
        w(f"\n结果：{rep['n_points']} 个覆盖点全部被验过 ✓\n")
    else:
        w(f"\n结果：合并 {rep['n_covered']}/{rep['n_points']} 个点"
          f"，{rep['n_missed']} 个洞（**覆盖不影响判定**，这是信息不是失败）\n")
    w("=" * 60 + "\n")


def _emit(rep: dict, args) -> None:
    if not args.quiet:
        _print_summary(rep)
    text = json.dumps(rep, ensure_ascii=False, indent=2 if args.pretty else None)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text + "\n", encoding="utf-8")
    sys.stdout.write(text + "\n")


def _next_actions(rep: dict) -> list[str]:
    out: list[str] = []
    if rep["status"] == "no_cover":
        out.append("测试台里一个 `VLEARN_COVER_DECL` 都没有 —— 覆盖点回答的是"
                   "「测试台测得全不全」，没有声明就没有分母。")
    if rep["failed_runs"]:
        out.append("先把没跑出结果的那几份测试台修好（python tools/sim.py "
                   "--tb <那个文件>），它们对覆盖率既贡献分子也不贡献分母。")
    if rep["undeclared_hits"]:
        out.append("有命中没声明：补 `VLEARN_COVER_DECL`，"
                   "否则覆盖率的分母是错的（和 `sim.py` 的 undeclared_hits 同一条）。")
    if rep["holes"]:
        out.append(f"这 {len(rep['holes'])} 个点一次都没碰到：先确认它们在"
                   "当前激励下打不打得到（打不到就是激励不够，不是代码错），"
                   "再决定加激励还是改声明。")
    if rep["thin"]:
        out.append("「只有部分测试台碰到」的那几个点加深起来最省力："
                   "把已有的一份测试台补两行激励通常就够了。")
    if not out:
        out.append("合并后全覆盖。注意覆盖点是你自己声明的 —— "
                   "声明漏了，100% 也是自欺。")
    return out


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    init_console()
    root = repo_root()

    ap = argparse.ArgumentParser(
        prog="cov.py",
        description="多份测试台的功能覆盖点合并（覆盖不影响判定，判定仍由 sim.py 完成）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("## 为什么不能只是", 1)[0].split("\n", 1)[1].strip(),
    )
    ap.add_argument("designs", nargs="*", type=Path, help="设计源文件（可多个）")
    ap.add_argument("--tb", action="append", default=[], type=Path,
                    help="测试台文件（可重复，一份一个 run）")
    ap.add_argument("--top", help="顶层模块名（默认取测试台里的 tb_* 自动识别）")
    ap.add_argument("--from", action="append", default=[], dest="from_json",
                    type=Path, metavar="RESULT.json",
                    help="从已有的 sim.py / regress.py --full JSON 里取覆盖数据，不重跑")
    ap.add_argument("--workdir", type=Path, help="产物根目录（默认 build/cov）")
    ap.add_argument("--timeout", type=float, default=30.0, help="每份测试台的超时秒数")
    ap.add_argument("--no-lint", action="store_true", help="不跑 verilator lint")
    ap.add_argument("--json", type=Path, help="把 JSON 结果同时写入该文件")
    ap.add_argument("--pretty", action="store_true", help="JSON 缩进输出")
    ap.add_argument("--quiet", action="store_true", help="不输出人类可读摘要")
    args = ap.parse_args(argv)

    if not args.tb and not args.from_json:
        print("没有覆盖数据来源：给至少一个 --tb（自己跑）或 --from（取已有结果）。",
              file=sys.stderr)
        print("（只看一份测试台的覆盖情况用 sim.py 就够了，不必绕道这里。）",
              file=sys.stderr)
        return EXIT_USAGE
    if args.from_json and not (args.tb and args.designs):
        # --from 单独用是合法的；但 --from 和 --tb 混用时 design 必须有，
        # 否则自己跑的那几份没有源文件。
        pass
    if args.tb and not args.designs:
        print("给了 --tb 就必须给设计源文件（cov.py 要自己跑它们）。", file=sys.stderr)
        return EXIT_USAGE

    runs: list[dict] = []
    aborted: str | None = None

    for p in args.from_json:
        got, why = _runs_from_json(p)
        if why:
            aborted = why
            break
        runs.extend(got)

    if aborted is None and args.tb:
        tc = detect(None)
        if not tc.ok:
            rep = {
                "schema": SCHEMA, "ok": False, "status": "env_error",
                "error": {"kind": "no_toolchain",
                          "message": "找不到 iverilog，无法自己跑测试台。"},
                "toolchain": tc.as_dict(),
                "next_actions_zh": ["先跑 python tools/setup_toolchain.py 装工具链"],
            }
            _emit(rep, args)
            return EXIT_ENV
        workdir = args.workdir or (root / "build" / "cov")
        for tb in args.tb:
            if not tb.exists():
                aborted = f"测试台不存在：{tb}"
                break
            if not args.quiet:
                sys.stderr.write(f"  跑 {tb.name} …\n")
                sys.stderr.flush()
            runs.append(_run_one(root, args.designs, tb, args.top,
                                 workdir, args.timeout, args.no_lint))

    if aborted is not None:
        rep = {"schema": SCHEMA, "ok": False, "status": "aborted",
               "error": {"kind": "aborted", "message": aborted},
               "next_actions_zh": ["按上面的说明修正输入后重跑。"]}
        _emit(rep, args)
        return EXIT_USAGE

    rep = {"schema": SCHEMA, "handler": "cov.py（覆盖数据来自 sim.py 的 cover 块）",
           **merge(runs)}
    rep["next_actions_zh"] = _next_actions(rep)
    rep["notes_zh"] = [
        "覆盖**不进判定**：affects_verdict 恒为 False，有洞也不会把 status 翻成 fail。"
        "覆盖点回答的是「测试台测得全不全」，不是「待测模块对不对」。",
        "合并的是**点的并集**，不是各份比值的平均 —— 平均在两个方向上都会骗人，"
        "见本文件头部。",
    ]

    _emit(rep, args)
    if rep["status"] in ("no_cover", "run_failed"):
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
