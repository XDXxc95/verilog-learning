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

    # ---------------- 3. generate：边界能算出来，就该展开 ----------------
    m = design.modules.get("gen_test")
    check(m is not None, "gen_test 没解析出来")
    if m:
        check(not m.unsupported,
              f"循环上界是裸字面量的 generate 不该拒绝：{m.unsupported}")
        check(len(m.blocks) == 1,
              f"4 圈同一条 assign 在信号级完全一样，应合并成 1 条，实际 {len(m.blocks)} 条")
        if len(m.blocks) == 1:
            b = m.blocks[0]
            check(b.lhs == ["y"] and b.rhs == ["a"],
                  f"展开+合并后应是 y <- a，实际 {b.lhs} <- {b.rhs}")
            check(b.repeat == 4, f"应记下这条代表 4 份，实际 {b.repeat}")
            check(b.origin == "generate g",
                  f"应标出它来自 generate g（不然读者会去源码里找一行不存在的 assign），"
                  f"实际 {b.origin!r}")
            check(b.line >= 40,
                  f"展开出来的语句行号要指向源码里 generate 内部那一行，实际 {b.line}")

    # ---------------- 3b. 参数化边界 + 参数化位宽 ----------------
    m = design.modules.get("gen_param_test")
    if m:
        check(m.param_env == {"N": 4, "DW": 8},
              f"参数默认值应算成 N=4、DW=8，实际 {m.param_env}")
        check(not m.param_failed,
              f"这两个参数的默认值都算得出来，不该进 param_failed：{m.param_failed}")
        wl = {p.name: p.width_label for p in m.ports}
        check(wl.get("a") == "[8]" and wl.get("y") == "[8]",
              f"`[DW-1:0]` 应算成 8 位，实际 {wl}")
        check(len(m.blocks) == 1 and m.blocks[0].repeat == 4,
              f"`i < N` 且 N=4，应展开 4 份合并成 1 条，实际 "
              f"{[(len(b.lhs), b.repeat) for b in m.blocks]}")

    # ---------------- 3c. 嵌套 generate：变量要一层层套 ----------------
    m = design.modules.get("gen_nest_test")
    if m:
        check(m.param_env == {"R": 2, "C": 3},
              f"嵌套用例的参数应算成 R=2、C=3，实际 {m.param_env}")
        check(len(m.blocks) == 1 and m.blocks[0].repeat == 6,
              f"2 圈 × 3 圈 = 6 份，应合并成 1 条 ×6，实际 {len(m.blocks)} 条 "
              f"{[b.repeat for b in m.blocks]}")
        if m.blocks:
            check("C" not in m.blocks[0].rhs and "R" not in m.blocks[0].rhs,
                  f"位下标里出现的**参数**不是信号，不该进读集合：{m.blocks[0].rhs}")
        check(not m.unknown_names,
              f"参数 C 是声明过的，不该报成「未在本模块声明的名字」：{m.unknown_names}")
        # 嵌套循环会让内层说明攒好几条一模一样的，必须去重
        check(sum(1 for g in m.gen_notes if "generate col" in g) == 1,
              f"内层 generate 的说明只该出现一次，实际 {m.gen_notes}")

    # ---------------- 3d. if generate：只画成立的那一支 ----------------
    m = design.modules.get("gen_if_test")
    if m:
        check(len(m.blocks) == 1,
              f"if/else generate 只该画出成立的那一支，实际 {len(m.blocks)} 条")
        if m.blocks:
            check(m.blocks[0].lhs == ["y"] and "a" in m.blocks[0].rhs,
                  f"WIDE=1 时应画 `y = ~a`，实际 {m.blocks[0].lhs} <- {m.blocks[0].rhs}")
        check(any("没画" in g for g in m.gen_notes),
              f"被丢掉的那一支必须写进说明，否则读者以为源码里没有它：{m.gen_notes}")
        check(not m.instances,
              f"else 那一支不能被当成顶层语句解析出假节点：{m.instances}")
        # 边界切错时 else 那一段会掉到普通路径上。它既不该变成假节点，
        # 也不该**无声无息地消失** —— 消失得有人吭声。
        check(not any("落单" in u for u in m.unsupported),
              f"if/else 的分支边界没切对，else 那一支掉了出来：{m.unsupported}")

    # ---------------- 3e. 边界算不出来：必须拒绝，绝不猜份数 ----------------
    for name, needle in (("gen_macro_test", "N"), ("gen_undef_test", "WIDTH")):
        m = design.modules.get(name)
        check(m is not None, f"{name} 没解析出来")
        if not m:
            continue
        check(any("算不出来" in u for u in m.unsupported),
              f"{name} 的循环边界算不出来，必须拒绝并说明白：{m.unsupported}")
        check(not m.instances,
              f"{name}: 那条 `for` 绝不能被当成例化解析出假节点：{m.instances}")
        check(any(needle in u for u in m.unsupported),
              f"{name} 的拒绝理由要点出是哪个名字算不出来：{m.unsupported}")
        # generate 里那几条 assign 一条都不许画出来（它们没有确定的份数）
        check(all(b.origin == "" for b in m.blocks),
              f"{name}: 拒绝的 generate 里的 assign 不许漏进普通路径："
              f"{[(b.origin, b.lhs) for b in m.blocks]}")

    m = design.modules.get("gen_undef_test")
    if m:
        check(not m.blocks, f"gen_undef_test 里没有被拒绝之外的东西，不该有块：{m.blocks}")

    m = design.modules.get("gen_macro_test")
    if m:
        # 手写的那条 assign 必须照画 —— 一块 generate 算不出来，
        # 不该把整个模块的内部结构一起停掉
        check(len(m.blocks) == 1 and m.blocks[0].origin == "",
              f"算不出来的只是那块 generate，手写的 assign 应当照画："
              f"{[(b.origin, b.lhs) for b in m.blocks]}")
        check(m.param_failed == ["N"],
              f"默认值是个编译宏，N 应进 param_failed（依赖它的图件都停用），"
              f"实际 {m.param_failed}")
        wl = {p.name: p.width_label for p in m.ports}
        check(wl.get("sel") == "[N-1:0]",
              f"位宽算不出来时要照抄源码原文，**不是**留空"
              f"（留空会被读成「这是个 1 位信号」）：{wl}")

    # ---------------- 3f. case generate：明确拒绝 ----------------
    m = design.modules.get("gen_case_test")
    if m:
        check(any("case" in u for u in m.unsupported),
              f"case generate 应明确拒绝并说明原因：{m.unsupported}")
        check(not m.blocks and not m.instances,
              f"case 里的 assign 不能被画出来：blocks={m.blocks} insts={m.instances}")

    # ---------------- 3g. generate 里例化：合并成一条并记份数 ----------------
    m = design.modules.get("gen_inst_test")
    if m:
        check(len(m.instances) == 1,
              f"4 份同样的例化应合并成 1 条（重名的实例名是正常的，"
              f"唯一名字由层次名给出），实际 {len(m.instances)} 条")
        if m.instances:
            i0 = m.instances[0]
            check(i0.module == "leaf" and i0.repeat == 4,
                  f"应是 leaf ×4，实际 {i0.module} ×{i0.repeat}")
        check("q" in m.assigned,
              f"合并之后 .q(q) 仍然要算作本模块被驱动的信号：{m.assigned}")

    # ---------------- 3h. 对抗性用例（各自抓出过一个真 bug）----------------
    # 隐式 generate：不带 generate/endgenerate 关键字，光写个 for
    m = design.modules.get("sg_implicit_for")
    if m:
        check(len(m.blocks) == 1 and m.blocks[0].repeat == 4,
              f"模块体里裸写的 for 也是 generate，应展开 4 份：{[(b.lhs, b.repeat) for b in m.blocks]}")
        check(not m.instances,
              f"那条 for 不能被当成例化解析出假节点：{[(i.module, i.name) for i in m.instances]}")

    m = design.modules.get("sg_no_begin")
    if m:
        check(len(m.blocks) == 1 and m.blocks[0].repeat == 4,
              f"循环体没有 begin-end 也要能展开：{[(b.lhs, b.repeat) for b in m.blocks]}")

    m = design.modules.get("sg_gen_always")
    if m:
        check(len(m.blocks) == 1 and m.blocks[0].repeat == 2,
              f"generate 里套 always 应展开成 1 条 ×2：{[(b.lhs, b.repeat) for b in m.blocks]}")
        if m.blocks:
            check(m.blocks[0].sequential and m.blocks[0].clock == "clk",
                  f"展开出来仍应是时序块：{m.blocks[0].label}")

    m = design.modules.get("sg_clog2")
    if m:
        wl = {p.name: p.width_label for p in m.ports}
        check(wl.get("addr") == "[4]",
              f"`[$clog2(DEPTH)-1:0]`（DEPTH=16）应算成 4 位，实际 {wl}")
        check(not m.unknown_names,
              f"$clog2 是系统函数不是信号，不该报未声明：{m.unknown_names}")
        if m.blocks:
            check("clog2" not in m.blocks[0].rhs,
                  f"$clog2 不该进读集合：{m.blocks[0].rhs}")

    # ★ 最要紧的一条反面用例：always 里的 for/if/case 不是 generate
    m = design.modules.get("sg_not_generate")
    if m:
        check(not m.instances,
              f"always 里的 for/if/case 绝不能被当成 generate 展开："
              f"{[(i.module, i.name) for i in m.instances]}")
        check(not any("generate" in u for u in m.unsupported),
              f"always 体内没有 generate，不该报 generate 相关的问题：{m.unsupported}")
        check(len(m.blocks) == 1 and m.blocks[0].sequential,
              f"应是一个时序块：{[(b.lhs, b.origin) for b in m.blocks]}")

    m = design.modules.get("sg_comment_trap")
    if m:
        check(len(m.blocks) == 1 and m.blocks[0].repeat == 4,
              f"注释里的 generate/for/endgenerate 必须被无视："
              f"{[(b.lhs, b.repeat) for b in m.blocks]}")

    m = design.modules.get("sg_nested_if")
    if m:
        check(len(m.blocks) == 1 and m.blocks[0].repeat == 3,
              f"3 圈 × 取一支 = 1 条 ×3：{[(b.lhs, b.repeat) for b in m.blocks]}")
        if m.blocks:
            # MODE=2，条件不成立 → 画的是 else 那一支（buf）。
            # 标成 `generate inv` 就是指着源码里没画出来的那块说"就是它"。
            check(m.blocks[0].origin == "generate buf",
                  f"MODE=2 时画的是 else 分支，出处应标 buf，实际 {m.blocks[0].origin!r}")
        check(any("buf" in g and "inv" in g for g in m.gen_notes),
              f"说明要同时点出画了哪一支、丢了哪一支（都要带名字）：{m.gen_notes}")

    m = design.modules.get("sg_zero_iter")
    if m:
        check(not m.blocks and not m.unsupported,
              f"循环跑 0 圈是**合法**的，不是错误也不该画东西："
              f"blocks={m.blocks} unsup={m.unsupported}")
        check(any("0 份" in g for g in m.gen_notes),
              f"跑 0 圈要说一声，不然读者以为这块被漏解析了：{m.gen_notes}")

    m = design.modules.get("sg_func_if")
    if m:
        check(not m.instances,
              f"function 体内的 begin/if 碎片不能被解析成节点："
              f"{[(i.module, i.name) for i in m.instances]}")
        check(any("function" in u for u in m.unsupported),
              f"function 应说明按黑盒处理：{m.unsupported}")
        check(len(m.blocks) == 1 and m.blocks[0].lhs == ["q"],
              f"function 之外的 always 要照常画：{[(b.lhs, b.origin) for b in m.blocks]}")

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
    svg_text: dict[str, str] = {}
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
                svg_text[p.name] = svg_text.get(p.name, "") + s + "\n"

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

    # ---------------- 7. 解析结果真的画到图上了吗 ----------------
    # 上面的断言都停在 Module 数据结构上。解析对了但没画出来（或者
    # 画出来的是旧文案）是同一种失败的另一半，必须端到端再验一次 ——
    # 这正是 lesson.py 那次"文件都在、内容却是空的"踩过的坑。
    t = svg_text.get("module_gen_test.svg", "")
    check("×4" in t,
          f"module_gen_test.svg 上应标出这条 assign 代表 4 份（×4），实际内容里没有")
    check("generate g" in t,
          f"module_gen_test.svg 上应标出 generate 的出处，实际内容里没有")
    check("展开了 4 份" in t,
          f"module_gen_test.svg 上应说明展开了几份，实际内容里没有")

    t = svg_text.get("module_gen_macro_test.svg", "")
    check("[N-1:0]" in t,
          f"module_gen_macro_test.svg 上位宽算不出来时应照抄源码原文 `[N-1:0]`，"
          f"实际内容里没有")
    check("算不出来" in t,
          f"module_gen_macro_test.svg 上必须写明这块没画以及为什么，实际内容里没有")

    t = svg_text.get("module_gen_if_test.svg", "")
    check("没画" in t,
          f"module_gen_if_test.svg 上必须写明另一支没画（否则读者以为源码里没有它），"
          f"实际内容里没有")

    t = svg_text.get("module_gen_inst_test.svg", "")
    check("×4" in t, f"module_gen_inst_test.svg 上应标出例化合并成了 4 份")

    print()
    if fails:
        for f in fails:
            print(f"  ✗ {f}")
        print(f"\n结构图检查未通过：{len(fails)} 项")
        return 1
    print("  ✓ 数字字面量未变成信号；括号内的比较未被当作赋值")
    print("  ✓ generate 能算出来的展开（含嵌套/参数化位宽/例化），算不出来的拒绝")
    print("  ✓ if generate 只画成立的一支，另一支写进说明")
    print("  ✓ 显式敏感列表按预期进 unsupported")
    print("  ✓ 例化输出被认作驱动；三层层次 tb→mid→leaf 正确")
    print("  ✓ SVG 合法 XML、坐标在画布内、文字不溢出")
    print("  ✓ 展开份数 / 出处 / 参数化位宽真的画到了图上")
    print("\n结构图检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
