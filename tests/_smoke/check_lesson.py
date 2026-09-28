"""lesson.py 产物生成的检查：确认产物包真的齐全、而且**内容非空**。

    python tests/_smoke/check_lesson.py

这个脚本重点盯的是一类特定的 bug：**文件生成了、但里面是空的**。

第一版就踩过：`run.txt` 有话可写，但人类可读摘要接错了流
（sim.py 刻意让摘要走 stderr、stdout 只放 JSON），结果 run.txt 里
那一段是一片空白——**不报错、不崩溃、文件也在**，只是内容是空的。
肉眼看文件名根本发现不了。

所以这里的断言不看"文件存不存在"，而是看"里面的关键内容在不在"。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from _console import init_console  # noqa: E402

init_console()

import lesson  # noqa: E402

SMOKE = ROOT / "tests" / "_smoke"
TB = SMOKE / "tb_counter4.v"
# 复用已有的 smoke 夹具当"设计文件"。它本来就叫 counter4，
# 和 tb_counter4.v 天然配对，不用为此另造一份。
DUT = SMOKE / "dut_counter4.v"
DESIGN = "counter4"
OUT_ROOT = ROOT / "build" / "_smoke_lesson"

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        fails.append(msg)


def main() -> int:
    if OUT_ROOT.exists():
        # 只删自己刚建的这个目录：路径写死、且在 build/ 下，不做通配
        shutil.rmtree(OUT_ROOT)

    ok = lesson.gen_one(
        root=ROOT, lesson="L1-05", design=DESIGN,
        tb=TB, src=DUT,
        out_root=OUT_ROOT, timeout=None, columns=None,
        open_after=False, out_lines=[],
    )
    check(ok, "gen_one 返回 False（判定或编译没过）")

    out = OUT_ROOT / DESIGN

    # ---- 产物清单齐全 ----
    for name in ("run.txt", "wave_ascii.txt", "wave.html", "hierarchy.svg"):
        check((out / name).is_file(), f"缺少产物 {name}")

    modules = sorted(p.name for p in out.glob("module_*.svg"))
    check("module_counter4.svg" in modules, "缺少 module_counter4.svg")
    check("module_tb_counter4.svg" in modules, "缺少 module_tb_counter4.svg")

    # ---- run.txt：命令 + 摘要 + JSON 三段都要在 ----
    # 这是本次重点：摘要那一段曾经是空的
    run = (out / "run.txt").read_text(encoding="utf-8")
    check("$ python tools/sim.py" in run, "run.txt 里没有记录执行的命令行")
    check("判定通过" in run, "run.txt 里没有人类可读的判定摘要（接错输出流了？）")
    check("用例：5/5 通过" in run, "run.txt 的摘要里没有用例统计")
    check('"schema": "vlearn.sim/1"' in run, "run.txt 里没有完整的 JSON")
    check('"status": "pass"' in run, "run.txt 的 JSON 里状态不是 pass")

    # 摘要不能是"只留个标题、底下一片空"——分别断言两段都有实质内容
    head, _, tail = run.partition("完整 JSON")
    check(len(head.strip().splitlines()) >= 5, "run.txt 摘要段行数过少，疑似空白")
    check(len(tail.strip().splitlines()) >= 20, "run.txt JSON 段行数过少，疑似截断")

    # ---- wave_ascii.txt：真的画出了波形 ----
    asc = (out / "wave_ascii.txt").read_text(encoding="utf-8")
    check(len(asc.splitlines()) >= 5, "wave_ascii.txt 行数过少")
    check("tb_counter4.q" in asc, "ASCII 波形里没有总线信号名")
    # 画波形用的制表符，缺了说明渲染没真正发生
    check(any(ch in asc for ch in "─‾│"), "ASCII 波形里没有绘制字符，渲染没发生")

    # ---- wave.html：自包含 ----
    html = (out / "wave.html").read_text(encoding="utf-8")
    check(len(html) > 5000, "wave.html 体积过小，疑似没渲染出内容")
    check("<canvas" in html, "wave.html 里没有 canvas 元素")
    for pat in ("http://", "https://", "src=\"./"):
        check(pat not in html, f"wave.html 不是自包含的（出现外部引用 {pat}）")

    # ---- SVG：合法 XML 且有实际图元 ----
    for svg in sorted(out.glob("*.svg")):
        try:
            tree = ET.parse(svg)
        except ET.ParseError as e:
            fails.append(f"{svg.name} 不是合法 XML：{e}")
            continue
        n = len(list(tree.iter()))
        check(n >= 3, f"{svg.name} 元素过少（{n} 个），疑似空图")

    # ---- 定位逻辑：配对靠文件名，配不上要能返回空而不是编一个 ----
    triples = lesson.designs_of(ROOT, "L1-05", "solutions")
    check(triples == [], "不存在的讲不该返回任何配对")
    check(lesson.find_lessons(ROOT, level=1) == [], "当前 tests/ 下不该有 L1-* 的讲")

    print()
    if fails:
        for f in fails:
            print(f"  ✗ {f}")
        print(f"\nlesson.py 检查未通过：{len(fails)} 项")
        return 1
    print("  ✓ 产物清单齐全（run.txt / wave_ascii / wave.html / 层次图 / 模块图）")
    print("  ✓ run.txt 三段齐全：命令行 + 判定摘要 + 完整 JSON（非空）")
    print("  ✓ ASCII 波形含信号名与绘制字符；HTML 自包含且有 canvas")
    print("  ✓ SVG 均为合法 XML 且含实际图元")
    print("  ✓ 讲号/文件名配对逻辑：配不上时返回空，不臆造")
    print("\nlesson.py 检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
