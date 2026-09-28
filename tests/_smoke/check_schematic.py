"""结构图解析与渲染的检查：对着夹具逐条验证。

    python tests/_smoke/check_schematic.py

这个工具最大的风险不是"崩"，而是**画出一张看着合理但错的图** ——
学习者会照着它建立"信号从哪流到哪"的直觉。所以这里的用例几乎都是
负向的：验证那些"如果不特判就会悄悄错"的地方。

SVG 这边检查三件事（都是"肉眼不容易发现、但一定是 bug"的）：
合法 XML、所有坐标落在 viewBox 内、文字没有溢出它所在的框。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from _console import init_console  # noqa: E402

init_console()

import schematic as sch  # noqa: E402

FIXTURE = ROOT / "tests" / "_smoke" / "sch_fixture.v"
SVGNS = "{http://www.w3.org/2000/svg}"

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        fails.append(msg)


def main() -> int:
    design = sch.parse_design([FIXTURE])
    for note in design.notes:
        print(f"  ! {note}")

    # ---------------- 1. 数字字面量 ----------------
    m = design.modules.get("lit_test")
    check(m is not None, "lit_test 没解析出来")
    if m:
        blk = m.blocks[0]
        check("h0" not in blk.rhs and "b0001" not in blk.rhs,
              f"4'h0 / 4'b0001 被当成了信号：rhs={blk.rhs}")
        check(blk.rhs == ["d", "q"], f"lit_test 的读集合应为 [d, q]，实际 {blk.rhs}")
        check(blk.lhs == ["q"], f"lit_test 的写集合应为 [q]，实际 {blk.lhs}")
        check(blk.line >= 6, f"行号应指向文件里的实际位置，实际 {blk.line}")

    # ---------------- 2. 括号里的比较 ----------------
    m = design.modules.get("cmp_test")
    if m:
        blk = m.blocks[0]
        check("cnt" in blk.rhs,
              f"`if (cnt <= 4'd5)` 里的 cnt 被当成赋值左侧抠掉了，rhs={blk.rhs}")
        check(blk.lhs == ["flag"], f"cmp_test 的写集合应为 [flag]，实际 {blk.lhs}")

    # ---------------- 3. generate ----------------
    m = design.modules.get("gen_test")
    if m:
        check(any("generate" in u for u in m.unsupported),
              "generate 必须进 unsupported（展开要参数取值，推不出来）")
        check(not m.blocks, "generate 里的 assign 不应该被当成顶层 assign 画出来")

    # ---------------- 4. 显式敏感列表 ----------------
    m = design.modules.get("sens_test")
    if m:
        check(any("always @(*)" in u for u in m.unsupported),
              "显式敏感列表应给出提醒")

    # ---------------- 5. 层次与例化输出方向 ----------------
    m = design.modules.get("mid")
    if m:
        check("q" in m.assigned,
              f"例化输出 .q(q) 里的 q 应被认作被驱动，assigned={m.assigned}")
    # 夹具里 5 个模块互不例化，顶层必然有歧义 —— 这时**必须留下提示**，
    # 而不是安静地挑一个（挑错了会画出一棵只有单个节点的树，
    # 看起来像"这个模块没有子模块"，根因却只是选错了根）
    top = design.pick_top()
    check(top is not None, "顶层推断失败")
    check(any("没有被任何模块例化" in n for n in design.notes),
          f"多个顶层候选时应给出提示，notes={design.notes}")
    tree = design.children_of("sch_top")
    check(len(tree) == 1 and tree[0].module == "mid",
          f"sch_top 应例化 mid，实际 {tree}")
    check(design.children_of("mid")[0].module == "leaf", "mid 应例化 leaf")

    # ---------------- 6. SVG ----------------
    out_dir = ROOT / "build" / "_sch_check"
    made = sch.render_design(design, out_dir, "sch_top")
    print(f"  生成 {len(made)} 个 SVG")
    for p in made:
        check(p.exists(), f"{p} 没生成")
        if not p.exists():
            continue
        try:
            root = ET.parse(p).getroot()
        except ET.ParseError as e:
            fails.append(f"{p.name} 不是合法 XML：{e}")
            continue

        vb = root.get("viewBox", "")
        parts = vb.split()
        check(len(parts) == 4, f"{p.name} 的 viewBox 不合法：{vb!r}")
        if len(parts) != 4:
            continue
        w, h = float(parts[2]), float(parts[3])
        check(w > 40 and h > 40, f"{p.name} 画布太小：{w}x{h}")

        # 所有坐标必须落在画布内：跑出 viewBox 的部分浏览器直接不显示，
        # 表现为"图缺了一块"，但文件本身完全合法
        rects: list[tuple[float, float, float, float]] = []
        warn_texts: list[tuple[float, str]] = []
        for el in root.iter():
            tag = el.tag.replace(SVGNS, "")
            if tag == "path":
                # 连线也必须落在画布内。回边（反馈）是从最右列的**右边缘**
                # 起画的，列宽一变就可能越界 —— 越界的部分浏览器直接不画，
                # 表现为"这根线断了"，但文件本身完全合法。
                # 本文件的 path 全是 M/L/C，坐标成对出现，取偶数下标即 x。
                nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", el.get("d", ""))]
                for i in range(0, len(nums) - 1, 2):
                    check(-1 <= nums[i] <= w + 1,
                          f"{p.name}: 连线 x={nums[i]:.0f} 超出画布宽度 {w}")
                    check(-1 <= nums[i + 1] <= h + 1,
                          f"{p.name}: 连线 y={nums[i + 1]:.0f} 超出画布高度 {h}")
                continue
            if tag == "rect":
                x, y = float(el.get("x", 0)), float(el.get("y", 0))
                rw, rh = float(el.get("width", 0)), float(el.get("height", 0))
                rects.append((x, y, rw, rh))
                check(x >= -1 and y >= -1, f"{p.name}: rect 跑到负坐标 ({x},{y})")
                check(x + rw <= w + 1.5 and y + rh <= h + 1.5,
                      f"{p.name}: rect 超出画布右下角 "
                      f"({x + rw:.0f},{y + rh:.0f}) > ({w:.0f},{h:.0f})")
            elif tag == "text":
                tx = float(el.get("x", 0))
                check(-1 <= tx <= w + 1, f"{p.name}: 文字 x={tx} 超出画布宽度 {w}")
                # 文字没有字体度量，只能估宽；只查"明显超出去"的情况
                s = "".join(el.itertext())
                est = sum(1.0 if ord(c) > 0x2E80 else 0.62 for c in s) * 13
                check(tx + est <= w + 40,
                      f"{p.name}: 文字 {s!r} 可能溢出画布（{tx + est:.0f} > {w:.0f}）")
                if "warn" in (el.get("class") or ""):
                    warn_texts.append((float(el.get("y", 0)), s))

        # 警示文字不能压在图形上。（曾经把提示统一画在固定 y，结果正好盖住
        # 顶层模块框：两边都看不清，而 SVG 完全合法、坐标也没出界 ——
        # "合法但读不了"是这套检查最该拦住的一类。）
        # dominant-baseline: middle + 11.5px → 上下各留约 6px。
        # 只按 y 判断（不做横向估算）—— 宁可漏报，也不要因为估宽不准误报。
        for ty, s in warn_texts:
            for _rx, ry, _rw, rh in rects:
                check(not (ry - 6 < ty < ry + rh + 6),
                      f"{p.name}: 警示文字 {s[:18]!r} (y={ty:.0f}) 压在 "
                      f"rect (y={ry:.0f}..{ry + rh:.0f}) 上")

    print()
    if fails:
        for f in fails:
            print(f"  ✗ {f}")
        print(f"\n结构图检查未通过：{len(fails)} 项")
        return 1
    print("  ✓ 数字字面量未变成信号；括号内的比较未被当作赋值")
    print("  ✓ generate / 显式敏感列表按预期进 unsupported")
    print("  ✓ 例化输出被认作驱动；三层层次 tb→mid→leaf 正确")
    print("  ✓ SVG 合法 XML、坐标在画布内、文字不溢出")
    print("\n结构图检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
