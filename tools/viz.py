"""一键可视化：编译 → 仿真 → 波形 + 结构图 → （可选）打开浏览器。

    python tools/viz.py exercises/03/mux4.v --tb tests/03/tb_mux4.v --top mux4 --open
    python tools/viz.py dut.v --wave                 # 只要波形
    python tools/viz.py dut.v --schematic            # 只画结构图（不仿真）
    python tools/viz.py --compare solutions/a.v exercises/a.v --tb tb.v --top a

它自己不解析任何东西，只是把已有的四块拼起来：

    sim.py       编译 + 仿真 + 判定    →  VCD
    vcd.py       VCD 解析
    wave.py      波形（终端 ASCII / 自包含 HTML）
    schematic.py 结构图 SVG

**结构图不需要仿真**，所以 `--schematic` 单独用时不跑 iverilog ——
改一行注释就想看一眼模块关系的时候，不必等一次完整的仿真。

输出统一放在 build/viz/<顶层模块名>/ 下，并且每次都打印完整路径 ——
"文件到底生成到哪去了"是这类工具最常见的困惑。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import schematic as sch          # noqa: E402
import sim as simmod             # noqa: E402
import vcd as vcdmod             # noqa: E402
import wave as wavemod           # noqa: E402
from _console import init_console  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 64


def _color_enabled() -> bool:
    return sys.stdout.isatty()


def _head(msg: str) -> None:
    print(f"\n=== {msg} ===")


def _status_line(result: dict) -> str:
    v = result.get("verdict") or {}
    s = result.get("status")
    tag = {"pass": "通过", "fail": "未通过", "inconclusive": "无法判定",
           "compile_error": "编译失败", "timeout": "仿真超时",
           "runtime_error": "运行错误", "env_error": "环境错误"}.get(s, s)
    if v.get("total"):
        return f"{tag}（{v.get('passed', 0)}/{v.get('total')} 个用例）"
    return tag


# ---------------------------------------------------------------- 仿真


def run_sim(designs: list[Path], tb: Path | None, top: str | None,
            workdir: Path, timeout: float | None) -> dict:
    """调用 sim.py 跑一轮，拿到结构化结果。

    直接复用 sim.py 的 SimConfig / _run_sim，而不是发一个子进程去调它的
    CLI：省掉一次 JSON 序列化，而且**判定逻辑保证是同一份**——
    如果这里另写一套，就会出现"sim.py 说失败、viz.py 说通过"这种
    最让人不信任的 bug。
    """
    cfg = simmod.SimConfig(
        designs=list(designs),
        tb=tb,
        top=top,
        workdir=workdir,
        name=top or (designs[0].stem if designs else "sim"),
        timeout=timeout if timeout is not None else 30.0,
        gflags=[],
        includes=[],
        defines=[],
        syntax_only=False,
        no_vcd=False,
        no_observe=False,
        no_lint=False,
        iverilog_path=None,
        verilator_path=None,
    )
    result, _code = simmod._run_sim(cfg)
    return result


# ---------------------------------------------------------------- 主流程


def _safe_name(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in s)


def do_schematic(designs: list[Path], out_dir: Path, top: str | None) -> list[Path]:
    """画结构图。

    designs 里**包含测试台**（调用方拼好的）：层次图从 tb 往下画才是完整画面
    —— `tb_counter4 → counter4` 恰好就是 L01 要建立的"例化"直觉。
    只看设计文件的话，层次图就只有一个孤零零的模块，什么关系都看不出来。
    """
    design = sch.parse_design(designs)
    if not design.modules:
        print("  ! 源码里没有找到任何 module，跳过结构图")
        return []
    if top and top not in design.modules:
        fallback = design.pick_top()
        print(f"  ! 指定的顶层 {top} 不在这些源文件里，改用 {fallback}")
        top = fallback
    made = sch.render_design(design, out_dir, top)
    for note in design.notes:
        print(f"  ! {note}")
    return made


def do_wave(vcd_path: Path, out_dir: Path, title: str,
            ascii_out: bool, html_out: bool, columns: int | None) -> list[Path]:
    try:
        dump = vcdmod.parse(vcd_path)
    except vcdmod.VCDError as e:
        print(f"  ! 无法解析 VCD：{e}")
        return []

    print(f"  VCD：{len(dump.signals)} 个信号，{dump.n_changes()} 次跳变")
    for n in dump.notes:
        print(f"  ! {n}")

    made: list[Path] = []
    if ascii_out:
        print()
        print(wavemod.render_ascii(dump, columns=columns))

    if html_out:
        out = out_dir / "wave.html"
        try:
            wavemod.render_html(dump, out, title=title)
        except wavemod.WaveTooLarge as e:
            print(f"  ! 波形太大，未生成 HTML：{e}")
        else:
            size_kb = out.stat().st_size / 1024
            print(f"  波形 HTML：{out}  ({size_kb:.0f} KB，完全自包含，可直接双击)")
            made.append(out)
    return made


def main(argv: list[str] | None = None) -> int:
    init_console()
    ap = argparse.ArgumentParser(
        prog="viz.py", description="一键：编译 → 仿真 → 波形 + 结构图",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("它自己不解析", 1)[0])
    ap.add_argument("design", nargs="+", type=Path, help="设计源文件（可多个）")
    ap.add_argument("--tb", type=Path, help="测试台文件")
    ap.add_argument("--top", help="顶层模块名（默认自动推断）")
    ap.add_argument("--out", type=Path, default=Path("build/viz"), help="输出目录")
    ap.add_argument("--wave", action="store_true", help="生成波形（默认开）")
    ap.add_argument("--schematic", action="store_true", help="生成结构图（默认开）")
    ap.add_argument("--no-wave", action="store_true", help="不生成波形")
    ap.add_argument("--no-schematic", action="store_true", help="不生成结构图")
    ap.add_argument("--ascii", action="store_true", help="同时在终端打印 ASCII 波形")
    ap.add_argument("--columns", type=int, help="ASCII 波形宽度（默认 96）")
    ap.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    ap.add_argument("--timeout", type=float, help="仿真超时秒数")
    ap.add_argument("--compare", action="store_true",
                    help="把每个 design 分别跑一遍并排对比（用 --top 指定同一个顶层）")
    args = ap.parse_args(argv)

    # 默认两个都做；--no-* 关掉。显式给了 --wave/--schematic 就只做指定的那些
    only = args.wave or args.schematic
    want_wave = args.wave or (not only and not args.no_wave)
    want_schem = args.schematic or (not only and not args.no_schematic)

    for p in args.design:
        if not p.exists():
            print(f"找不到源文件：{p}")
            return EXIT_USAGE

    if args.compare and len(args.design) < 2:
        print("--compare 需要至少两个 design 文件（正确版 / 待对比版）")
        return EXIT_USAGE

    opened: list[Path] = []
    failed: list[str] = []

    if args.compare:
        code = _run_compare(args, want_wave, want_schem, opened, failed)
    else:
        code = _run_one(args, args.design, want_wave, want_schem, opened, failed)

    if opened and args.open:
        _head("打开")
        for p in opened:
            print(f"  {p.name}")
            _open(p)
    elif opened:
        _head("生成结果")
        for p in opened:
            print(f"  {p}")

    if failed:
        _head("失败")
        for f in failed:
            print(f"  ✗ {f}")
        return EXIT_FAIL
    return code


def _run_one(args, designs: list[Path], want_wave: bool, want_schem: bool,
             opened: list[Path], failed: list[str]) -> int:
    inputs = list(designs) + ([args.tb] if args.tb else [])
    # ★ 结构图的顶层和仿真的顶层不是一回事，别用同一个变量（见 _guess_sim_top）。
    #   `--top` 用户显式给的时候照旧覆盖两者 —— README / AGENT.md 里举的就是
    #   `--top tb_xxx`，那本来就是仿真顶层。
    sch_top = args.top or _guess_top(inputs)
    sim_top = args.top or _guess_sim_top(designs, args.tb)
    out_dir = (args.out / _safe_name(sch_top or "design"))
    out_dir.mkdir(parents=True, exist_ok=True)

    if want_wave:
        _head("编译 + 仿真")
        result = run_sim(designs, args.tb, sim_top, Path("build"), args.timeout)
        print(f"  {_status_line(result)}")
        for d in (result.get("compile") or {}).get("diagnostics") or []:
            sev = d.get("severity")
            mark = "✗" if sev == "error" else "!"
            print(f"  {mark} {d.get('file')}:{d.get('line')} {d.get('message_zh')}")
        if result.get("status") in ("compile_error", "env_error"):
            failed.append(f"{sim_top} 编译未通过，没有波形可以看")
            want_wave = False
        else:
            for a in result.get("next_actions_zh") or []:
                print(f"  → {a}")
            vcd_rel = (result.get("artifacts") or {}).get("vcd")
            if not vcd_rel:
                # 把 sim.py 那句「找了哪里」原样带出来 —— 不许替它断言
                # 学习者没写 $dumpfile（文件可能只是落在了别的地方）。
                why = (result.get("observe") or {}).get("reason")
                print(f"  ! 没有波形可以画。{why or ''}")
            else:
                opened += do_wave(Path(vcd_rel), out_dir, sim_top or "波形",
                                  args.ascii, True, args.columns)
            if result.get("status") in ("fail", "timeout"):
                failed.append(f"{sim_top} 判定为{_status_line(result)}")

    if want_schem:
        _head("结构图")
        made = do_schematic(inputs, out_dir, sch_top)
        for p in made:
            print(f"  {p}")
        opened += made
        if not made:
            failed.append("结构图未生成")

    if not opened and not failed:
        failed.append("没有任何输出（试试 --wave 或 --schematic）")
    return EXIT_OK


def _run_compare(args, want_wave: bool, want_schem: bool,
                 opened: list[Path], failed: list[str]) -> int:
    """把多个版本各自跑一遍，产出的文件并用版本名区分。

    ★ 这里的 `--top` 是**被对比的那个模块名**（几个版本都得叫这个名），
      用它来分目录 —— 它**不是**仿真顶层。仿真顶层是测试台，
      由 `_guess_sim_top()` 从 `--tb` 里认（`--compare` 的文档示例给的是
      `--top decoder3_8`，拿它当仿真顶层的话测试台根本不会被实例化，
      整组对比一个波形都出不来）。
    """
    top = args.top
    if not top:
        print("--compare 必须显式给 --top（几个版本的顶层模块名必须一致）")
        return EXIT_USAGE

    print(f"对比 {len(args.design)} 个版本，模块 {top}")
    for design in args.design:
        label = design.stem
        _head(f"{label}（{design}）")
        out_dir = args.out / _safe_name(top) / _safe_name(label)
        out_dir.mkdir(parents=True, exist_ok=True)

        if want_wave:
            result = run_sim([design], args.tb,
                             _guess_sim_top([design], args.tb),
                             Path("build"), args.timeout)
            print(f"  判定：{_status_line(result)}")
            for t in result.get("tests") or []:
                if t.get("status") == "fail":
                    print(f"  ✗ {t.get('name')} @ {t.get('time')}"
                          f" 期望 {t.get('expected')} 实际 {t.get('got')}")
            vcd_rel = (result.get("artifacts") or {}).get("vcd")
            if vcd_rel:
                opened += do_wave(Path(vcd_rel), out_dir, f"{top} · {label}",
                                  args.ascii, True, args.columns)
            else:
                why = (result.get("observe") or {}).get("reason")
                print(f"  ! 没有波形可以画。{why or ''}")
                failed.append(f"{label} 没有波形")

        if want_schem:
            made = do_schematic([design] + ([args.tb] if args.tb else []), out_dir, top)
            opened += made
    return EXIT_OK


def _guess_top(inputs: list[Path]) -> str | None:
    """猜**结构图**的顶层：优先非 `tb*` 的模块（被画的应该是设计本身）。"""
    mods = simmod.scan_modules(inputs)
    if not mods:
        return None
    cands = [m for m in mods if not m.lower().startswith("tb")]
    return cands[0] if cands else mods[0]


def _guess_sim_top(designs: list[Path], tb: Path | None) -> str | None:
    """猜**仿真**的顶层 —— 有测试台就**必须**是测试台。

    ★ 这两个顶层不是一回事，混用会静默出错：
      iverilog 收到 `-s <顶层>` 后只展开那一棵子树。拿设计模块当仿真顶层，
      测试台根本不会被实例化 —— 不打印任何标记、不执行 `$dumpfile`。
      症状是「无法判定 / 测试台缺少判定标记 / 没有产生 VCD」，
      **把工具的错说成学习者测试台的错**，而且它明明写对了。

    实测（2026-09-30，`dut_counter4.v` + `tb_counter4.v`，唯一的差别就是顶层名）:

        -s counter4      status=inconclusive   markers= 0
        -s tb_counter4   status=pass           markers=12

    所以：给了 `--tb` 就从**测试台文件**里挑模块；没给才退回设计模块。
    """
    if tb is not None:
        mods = simmod.scan_modules([tb])
        if mods:
            return mods[0]
    mods = simmod.scan_modules(designs)
    if not mods:
        return None
    cands = [m for m in mods if not m.lower().startswith("tb")]
    return cands[0] if cands else mods[0]


def _open(path: Path) -> None:
    try:
        webbrowser.open(path.resolve().as_uri())
    except Exception as e:  # noqa: BLE001 — 打不开浏览器不该让整个流程失败
        print(f"    （自动打开失败：{e}，请手动打开上面的路径）")


if __name__ == "__main__":
    sys.exit(main())
