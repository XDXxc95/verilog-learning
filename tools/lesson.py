"""为一讲生成完整的「实际运行结果 + 可视化」产物包。

    python tools/lesson.py L1-05              # 生成 L1-05 的全部产物
    python tools/lesson.py L1-05 --open       # 生成后用浏览器打开波形
    python tools/lesson.py --list             # 列出所有已存在的讲
    python tools/lesson.py --all              # 全部讲各生成一遍

## 为什么需要它

课程的每一道练习都必须有**真实跑过的结果**和**能看的波形/结构图**——
不能只给代码让人自己去跑。但一讲是一个题组，动辄七八个模块，
手敲 `sim.py` + `viz.py` 容易漏、容易不一致（漏掉某个变式题、
波形和代码版本对不上）。

这个脚本把整讲的产物一次性生成齐，**题目、测试台、解答、产物四件套保持一致**。

## 产物放哪

    solutions/L1-05/artifacts/decoder3_8/
        run.txt          实际运行的命令 + 判定摘要 + 完整 JSON
        wave_ascii.txt   终端 ASCII 波形（纯文本，可 diff、可贴进文档）
        wave.html        自包含交互波形（双击就能看）
        hierarchy.svg    层次图
        module_*.svg     各模块结构图

**放在 solutions/ 下而不是 build/ 下**：build/ 是 gitignore 的临时目录，
而这里的产物是**课程材料的一部分**，要入库、要随模板一起走。

## 和 viz.py 的分工

    viz.py     学习时用 —— 跑自己的代码，产物进 build/，用完即弃
    lesson.py  出课程材料时用 —— 跑参考答案，产物进 solutions/*/artifacts/，入库
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
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
EXIT_ENV = 4

# 讲号形如 L1-05 / L2-42 / L3-07
LESSON_RE = re.compile(r"^L([123])-(\d{2})$")
TB_PREFIX = "tb_"


def repo_root() -> Path:
    """仓库根目录。按 pyproject/ .git 逐级往上找，找不到就用脚本的上一级。"""
    for p in _HERE.parents:
        if (p / ".git").exists():
            return p
    return _HERE.parent


# ---------------------------------------------------------------- 定位


def find_lessons(root: Path, level: int | None = None) -> list[str]:
    """扫 tests/ 下有哪些讲（有 tb_*.v 才算）。"""
    out = []
    tests = root / "tests"
    if not tests.is_dir():
        return out
    for d in sorted(tests.iterdir()):
        if not d.is_dir() or not LESSON_RE.match(d.name):
            continue
        if level is not None and not d.name.startswith(f"L{level}-"):
            continue
        if any(d.glob(f"{TB_PREFIX}*.v")):
            out.append(d.name)
    return out


def designs_of(root: Path, lesson: str, source: str) -> list[tuple[str, Path, Path]]:
    """找出这一讲里「设计名 → 测试台路径 → 源文件路径」的三元组。

    约定：测试台叫 tb_<设计名>.v，源文件叫 <设计名>.v——
    两边靠名字对齐，所以名字写错就配不上，这里会明确报出来而不是默默跳过。
    """
    tb_dir = root / "tests" / lesson
    src_dir = root / source / lesson
    found = []
    for tb in sorted(tb_dir.glob(f"{TB_PREFIX}*.v")):
        design = tb.stem[len(TB_PREFIX):]
        src = src_dir / f"{design}.v"
        if src.is_file():
            found.append((design, tb, src))
    return found


# ---------------------------------------------------------------- 生成


def gen_one(root: Path, lesson: str, design: str, tb: Path, src: Path,
            out_root: Path, timeout: float | None, columns: int | None,
            open_after: bool, out_lines: list[str]) -> bool:
    """生成单个设计的完整产物包。返回是否成功。"""
    top = f"{TB_PREFIX}{design}"
    out_dir = out_root / design
    out_dir.mkdir(parents=True, exist_ok=True)

    rel_src = src.relative_to(root).as_posix()
    rel_tb = tb.relative_to(root).as_posix()
    cmd = (f"python tools/sim.py {rel_src} --tb {rel_tb} --top {top}")

    def say(msg: str) -> None:
        print(msg)
        out_lines.append(msg)

    say(f"\n--- {lesson} / {design} ---")
    say(f"  $ {cmd}")

    cfg = simmod.SimConfig(
        designs=[src],
        tb=tb,
        top=top,
        workdir=root / "build",
        name=top,
        timeout=timeout if timeout is not None else 30.0,
        gflags=[],
        includes=[],
        defines=[],
        syntax_only=False,
        no_vcd=False,
        # 课程材料的产物里要留观察块：run.txt 的人工摘要会多一行
        # 「时钟周期 / 首次分歧在第几个上升沿」，那正是回看解答时最想看的
        no_observe=False,
        # lint 同理：参考实现是**正确的示范**，摘要里那行「lint：检查过，
        # 没有结构性问题」是它能给出的最有用的一条保证
        no_lint=False,
        iverilog_path=None,
        verilator_path=None,
    )
    result, code = simmod._run_sim(cfg)

    # 人类可读摘要本来是直接打屏的，这里接住它一起写进 run.txt——
    # 「实际运行的过程结果」要的是能看懂的记录，光有 JSON 不算。
    #
    # 注意接的是 stderr 不是 stdout：sim.py 刻意让 stdout 只放 JSON、
    # 摘要走 stderr（见 _print_summary 的文档字符串）。接错流的话
    # run.txt 里的摘要会是一片空白，而且不报错——第一版就踩了这个坑。
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        simmod._print_summary(result, code)
    summary = buf.getvalue()

    say(f"  判定：{result.get('status')}"
        f"（{_verdict_line(result)}）")

    # run.txt：命令 + 摘要 + 完整 JSON，一个文件说清这次运行的全部信息
    run_txt = out_dir / "run.txt"
    run_txt.write_text(
        f"$ {cmd}\n\n"
        f"{summary}\n"
        f"{'=' * 60}\n完整 JSON（程序读这个）\n{'=' * 60}\n"
        f"{json.dumps(result, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )
    say(f"  run.txt         {_kb(run_txt)}")

    if result.get("status") in ("compile_error", "env_error"):
        say("  ! 编译没过，无波形与结构图")
        return False

    # 波形：ASCII 和 HTML 都出。ASCII 能进文档、能 diff；HTML 是人看的。
    vcd_rel = (result.get("artifacts") or {}).get("vcd")
    if vcd_rel and Path(vcd_rel).is_file():
        try:
            dump = vcdmod.parse(Path(vcd_rel))
        except vcdmod.VCDError as e:
            say(f"  ! VCD 解析失败：{e}")
        else:
            asc = out_dir / "wave_ascii.txt"
            asc.write_text(
                wavemod.render_ascii(dump, columns=columns) + "\n",
                encoding="utf-8",
            )
            say(f"  wave_ascii.txt  {_kb(asc)}")

            html = out_dir / "wave.html"
            try:
                wavemod.render_html(dump, html, title=top)
            except wavemod.WaveTooLarge as e:
                say(f"  ! 波形太大，跳过 HTML：{e}")
            else:
                say(f"  wave.html       {_kb(html)}")
                if open_after:
                    _open(html)
    else:
        say("  ! 没有产生 VCD（测试台里要有 $dumpfile/$dumpvars）")

    # 结构图：源文件里带上测试台，层次图才是完整画面（tb → dut）
    design_obj = sch.parse_design([src, tb])
    if design_obj.modules:
        made = sch.render_design(design_obj, out_dir, top)
        for p in made:
            say(f"  {p.name:<15} {_kb(p)}")
        for note in design_obj.notes:
            say(f"  ! {note}")
    else:
        say("  ! 源码里没有 module，跳过结构图")

    return True


def _verdict_line(result: dict) -> str:
    v = result.get("verdict") or {}
    if v.get("total"):
        return f"{v.get('passed', 0)}/{v.get('total')} 个用例"
    return "无判定用例"


def _kb(p: Path) -> str:
    n = p.stat().st_size
    return f"{n / 1024:.1f} KB" if n >= 1024 else f"{n} B"


def _open(path: Path) -> None:
    import webbrowser
    try:
        webbrowser.open(path.resolve().as_uri())
    except Exception as e:  # noqa: BLE001 — 打不开浏览器不该让整个流程失败
        print(f"    （自动打开失败：{e}）")


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    init_console()
    root = repo_root()

    ap = argparse.ArgumentParser(
        prog="lesson.py",
        description="为一讲生成完整的「实际运行结果 + 可视化」产物包",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("## 和 viz.py 的分工", 1)[0].replace(__doc__.split("\n")[0], ""),
    )
    ap.add_argument("lessons", nargs="*", help="讲号，如 L1-05（可多个）")
    ap.add_argument("--all", action="store_true", help="所有已存在的讲各生成一遍")
    ap.add_argument("--list", action="store_true", help="列出所有已存在的讲")
    ap.add_argument("--source", default="solutions", choices=["solutions", "exercises"],
                    help="从哪份实现生成（默认 solutions，即参考答案）")
    ap.add_argument("--out", type=Path,
                    help="产物根目录（默认 solutions/<讲号>/artifacts）")
    ap.add_argument("--timeout", type=float, help="仿真超时秒数（默认 30）")
    ap.add_argument("--columns", type=int, help="ASCII 波形宽度（默认 96）")
    ap.add_argument("--open", action="store_true", help="生成后用浏览器打开每个波形")
    args = ap.parse_args(argv)

    if args.list:
        lessons = find_lessons(root)
        if not lessons:
            print("tests/ 下还没有任何讲（没有 tb_*.v）")
            return EXIT_OK
        print(f"共 {len(lessons)} 讲：")
        for les in lessons:
            n = len(designs_of(root, les, args.source))
            mark = "✓" if n else "·"
            print(f"  {mark} {les}   {n} 个模块")
        return EXIT_OK

    lessons = args.lessons
    if args.all:
        lessons = find_lessons(root)
        if not lessons:
            print("tests/ 下还没有任何讲")
            return EXIT_OK
    if not lessons:
        ap.print_help()
        return EXIT_USAGE

    bad = [les for les in lessons if not LESSON_RE.match(les)]
    if bad:
        print(f"讲号格式不对（应为 L1-05 这样）：{', '.join(bad)}")
        return EXIT_USAGE

    out_lines: list[str] = []
    ok = failed = 0

    for les in lessons:
        triples = designs_of(root, les, args.source)
        if not triples:
            print(f"\n--- {les} ---")
            print(f"  ! 在 {args.source}/{les}/ 下找不到与 tests/{les}/tb_*.v 配对的设计文件")
            print(f"    测试台叫 tb_<设计名>.v，源文件必须叫 <设计名>.v，两边靠名字对齐")
            failed += 1
            continue
        out_root = args.out or (root / "solutions" / les / "artifacts")
        for design, tb, src in triples:
            if gen_one(root, les, design, tb, src, out_root,
                       args.timeout, args.columns, args.open, out_lines):
                ok += 1
            else:
                failed += 1

    print(f"\n{'=' * 60}")
    print(f"完成：{ok} 个产物包生成成功" + (f"，{failed} 个失败" if failed else ""))
    return EXIT_OK if not failed else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
