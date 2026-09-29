"""工具链冒烟测试：把 sim.py 的每种结论都跑一遍并校验。

    python tests/_smoke/run_smoke.py

为什么要单独有这么一个东西：

    只测"正确代码能通过"是自欺欺人 —— 一个永远报 PASS 的判定器看起来也全绿。
    必须**故意写坏**，确认每种失败都被正确地识别成对应的结论，
    否则判定链路是否真的在工作，没有任何证据。

    这些用例同时也是回归网：sim.py / tb_common.vh / _lint.py 改动之后
    跑一遍，能立刻发现判定行为有没有被改坏。

每个用例断言四件事：退出码、status、error_tags、以及若干附加条件
（例如超时用例必须保留住已打印的标记 —— 那是协作式超时最核心的价值）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tools" / "sim.py"
SMOKE = ROOT / "tests" / "_smoke"
NEG = SMOKE / "neg"

# Windows 控制台默认是 GBK，直接 print 的 ✓/✗ 会抛 UnicodeEncodeError
# （不是显示成乱码，是**直接崩掉**）。复用 tools/_console.py 的同一套处理。
sys.path.insert(0, str(ROOT / "tools"))
from _console import init_console  # noqa: E402
from _manifest import load as load_manifest, probe as probe_software  # noqa: E402

init_console()


def _have_verilator() -> tuple[bool, str]:
    """verilator 装了且**真能用**吗？

    判据用 probe（`found and usable`）而不是 `locate`：文件在但跑不起来
    （VERILATOR_ROOT 烘死那种）时，lint 用例必然红，而那不是本仓库代码的问题。
    跳过并说清原因，比报一个与本次改动无关的失败有用。

    注意 `probe` 会真跑一次 verilator 的自检，所以只在这里调一次、缓存住。
    """
    try:
        sw = load_manifest().software_by_id("verilator")
        if sw is None:
            return False, "清单里没有 verilator 条目"
        p = probe_software(sw)
        if not p.found:
            return False, "没找到 verilator（python tools/setup_tools.py --install verilator --lock）"
        if not p.usable:
            return False, f"verilator 找到了但不可用：{p.detail}"
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"探测 verilator 出错：{type(exc).__name__}: {exc}"


VERILATOR_OK, VERILATOR_WHY = _have_verilator()


@dataclass
class Case:
    name: str
    args: list[str]
    status: str
    exit_code: int
    tags: list[str] = field(default_factory=list)
    # 附加断言：JSON 里的点号路径 -> 期望值
    expect: dict = field(default_factory=dict)
    # tail 里必须出现的子串（验证被 kill 时输出有没有留住）
    tail_has: list[str] = field(default_factory=list)
    # tail 里必须**不**出现的子串
    tail_lacks: list[str] = field(default_factory=list)
    # 依赖 verilator 真能用的用例。缺工具时**跳过而不是失败**——
    # 「缺工具不阻塞」是这个仓库的既定立场（见 tools/toolchain.json），
    # 让工具链的缺失把自检搞红，等于把环境问题和代码问题混成一锅。
    # 但降级路径本身必须有**不依赖工具**的用例覆盖，见下面那条 --verilator-path。
    needs_verilator: bool = False
    why: str = ""


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT)).replace("\\", "/")


CASES: list[Case] = [
    Case(
        name="正确设计 + 自检测试台",
        args=[_rel(SMOKE / "dut_counter4.v"), "--tb", _rel(SMOKE / "tb_counter4.v"),
              "--top", "tb_counter4"],
        status="pass", exit_code=0,
        expect={"verdict.total": 5, "verdict.passed": 5, "compile.ok": True,
                # 观察层：没有失败用例时不能凭空造一个"首次分歧"出来
                "observe.ok": True,
                "observe.first_divergence": None,
                "observe.clock.period_text": "10ns",
                "observe.clock.n_rising_edges": 6},
        why="happy path：编译、仿真、判定、VCD 全链路走通",
    ),
    Case(
        name="非阻塞赋值的移位寄存器（正确）",
        args=[_rel(NEG / "dut_shift_good.v"), "--tb", _rel(NEG / "tb_shift.v"),
              "--top", "tb_shift"],
        status="pass", exit_code=0,
        tags=[],
        why="同一测试台对正确实现的判定 —— 与下一条配对，证明测试台真能区分",
    ),
    Case(
        name="阻塞赋值的移位寄存器（错误）",
        args=[_rel(NEG / "dut_shift_bad.v"), "--tb", _rel(NEG / "tb_shift.v"),
              "--top", "tb_shift"],
        status="fail", exit_code=1,
        tags=["off_by_one"],
        expect={"verdict.failed": 2},
        why="时序块里用 = 代替 <=，两级寄存器塌缩成一级；测试台应报 q2 早一拍",
    ),
    # ---- 观察层（observe）。三条一组：正确 / 错误 / 无波形 ----
    Case(
        name="观察层：正确设计",
        args=[_rel(NEG / "dut_shift_good.v"), "--tb", _rel(NEG / "tb_shift.v"),
              "--top", "tb_shift"],
        status="pass", exit_code=0,
        expect={"observe.ok": True,
                # 全过就没有"首次分歧"，绝不能为了字段好看硬填一个
                "observe.first_divergence": None,
                "observe.clock.signal": "tb_shift.clk",
                "observe.clock.period_text": "10ns",
                # 1 位的端口在 VCD 里会被合并成一个 ID，所以是 4 个流而不是 8 个
                "observe.n_signals": 4},
        why="时钟靠跳变规律性认出来（不是靠名字），且没有失败用例时不造分歧",
    ),
    Case(
        name="观察层：错误设计（阻塞赋值）",
        args=[_rel(NEG / "dut_shift_bad.v"), "--tb", _rel(NEG / "tb_shift.v"),
              "--top", "tb_shift"],
        status="fail", exit_code=1,
        expect={"observe.ok": True,
                # 最早的那个失败，不是 tests 里的第一个（顺序可能被打乱）
                "observe.first_divergence.case": "q2_still_old_this_cycle",
                "observe.first_divergence.time_text": "26.000ns",
                "observe.first_divergence.expected": "0",
                "observe.first_divergence.got": "1",
                # 26ns 之前已经过去了 5/15/25 三个上升沿
                "observe.first_divergence.edge_index": 3,
                "observe.first_divergence.edge_time_text": "25ns",
                "observe.first_divergence.n_failed": 2},
        why="把失败时刻换算成「第几个时钟沿」——这是讲解时序错误时最有用的一句话；"
            "时间字符串的单位换算按**字符串里写的单位**来，不是 VCD 的显示单位",
    ),
    Case(
        name="观察层：测试台不产波形",
        args=[_rel(NEG / "dut_shift_good.v"), "--tb", _rel(NEG / "tb_quiet.v"),
              "--top", "tb_quiet"],
        status="inconclusive", exit_code=1,
        # 这一条的**要害**是：观察层拿不到波形时，判定结论一个字都不能变。
        # 上一条同样的设计配 tb_shift 是 pass，这里只是换了个不产波形的测试台，
        # 结果就必须还是"inconclusive"（tb_quiet 不打标记），而不是崩掉或变成 pass。
        expect={"observe.ok": False, "verdict.inconclusive": True},
        why="观察层不许把判定带崩：没波形就 ok=false + 说清原因，status 不受影响",
    ),
    Case(
        name="观察层：--no-observe 关得掉",
        args=[_rel(NEG / "dut_shift_good.v"), "--tb", _rel(NEG / "tb_shift.v"),
              "--top", "tb_shift", "--no-observe"],
        status="pass", exit_code=0,
        expect={"observe.ok": False,
                # 关掉观察后判定必须完全不变（和上面"正确设计"那条同一组数字）
                "verdict.passed": 6,
                "verdict.total": 6},
        why="关掉观察只是少一层辅助信息，判定结果与开着时逐字段一致",
    ),
    Case(
        name="观察层：纯组合逻辑没有时钟",
        args=[_rel(NEG / "dut_mux2.v"), "--tb", _rel(NEG / "tb_mux2.v"),
              "--top", "tb_mux2"],
        status="pass", exit_code=0,
        # 要害是 observe.ok 必须为 True：只有"波形读到了、但里面确实没有时钟"
        # 才算验过"不会硬造时钟"。若这里 ok 是 False，那就是拿"没波形"
        # 冒充"没时钟"，断言看着绿而已（这正是本文件开头警告的那类假通过）。
        expect={"observe.ok": True,
                "observe.clock": None,
                "observe.n_signals": 4,
                "verdict.passed": 3},
        why="找不到时钟就给 None，**不硬报一个周期**；"
            "纯组合逻辑本来就没有时钟这个概念",
    ),
    # ---- verilator lint。三条一组：抓到了 / 关得掉 / 工具不在 ----
    Case(
        name="lint：组合环（iverilog 沉默，verilator 报出）",
        args=[_rel(NEG / "dut_comb_loop.v"), "--tb", _rel(NEG / "tb_comb_loop.v"),
              "--top", "tb_comb_loop"],
        status="pass", exit_code=0,
        tags=["combinational_loop"],
        expect={"verdict.passed": 3, "verdict.total": 3,
                "lint.ran": True, "lint.ok": True,
                "lint.n_warnings": 1, "lint.n_warnings_raw": 1,
                # -Wno-fatal 的功劳：只有警告时不算 %Error。
                # 少了它 verilator 会追加一行 "Exiting due to N warning(s)"，
                # 于是"有警告"和"真出错"就再也分不开了。
                "lint.n_errors": 0,
                # 诊断要标明来路：verilator 和 iverilog 的权威性不一样
                "compile.diagnostics.0.source": "verilator",
                "compile.diagnostics.0.kind": "combinational_loop",
                # 固定为 warning：它**不参与 compile.ok**，也就不可能把
                # 通过的判定翻成失败。这是 lint 层最重要的一条自我约束。
                "compile.diagnostics.0.severity": "warning",
                # tests/ 属于对学习者隐藏的目录，verilator 的诊断也不许回显源码
                "compile.diagnostics.0.source_excerpt": None,
                # 自研扫描对组合环一无所知，这里必须是 0 条 ——
                # 否则说明这条用例其实是被 _lint.py 抓到的，跟 verilator 无关
                "compile.diagnostic_summary.combinational_loop": 1},
        needs_verilator=True,
        why="**status 一个字都没变**（3/3 通过）却报出结构错误 —— 这正是 lint 补的盲区；"
            "iverilog -Wall 对这个 assign 完全沉默（实测），功能仿真也是对的",
    ),
    Case(
        name="lint：自研扫描报过的锁存器不重复报",
        args=[_rel(NEG / "latch_incomplete_case.v")],
        status="inconclusive", exit_code=1,
        tags=["latch_inference", "case_incomplete"],
        expect={"lint.ran": True, "lint.n_warnings": 1, "lint.n_warnings_raw": 2,
                # 留下来的那一条必须是 verilator 独有的 case 不穷举，
                # 而不是和 _lint.py 重复的锁存器
                "compile.diagnostics.1.source": "verilator",
                "compile.diagnostics.1.kind": "case_incomplete",
                "compile.diagnostics.0.source": "lint",
                "compile.diagnostics.0.kind": "latch_inference"},
        needs_verilator=True,
        why="verilator 也报了 LATCH，但 _lint.py 已经在同一个文件里报过同类问题，"
            "再挂一条只是噪音（而且它的解释是英文的）。两个计数刻意分开："
            "n_warnings 是「转成了几条诊断」，n_warnings_raw 是「verilator 报了几条」，"
            "只报一个就会出现「说 1 条实际 2 条」这种对不上账的情况",
    ),
    Case(
        name="lint：--no-lint 关得掉，判定逐字段不变",
        args=[_rel(NEG / "dut_comb_loop.v"), "--tb", _rel(NEG / "tb_comb_loop.v"),
              "--top", "tb_comb_loop", "--no-lint"],
        status="pass", exit_code=0,
        tags=[],
        expect={"verdict.passed": 3, "verdict.total": 3,
                "lint.ran": False, "lint.ok": False, "lint.n_warnings": 0,
                # 关掉之后 diagnostic 一条都不剩 —— 组合环这个 tag 也就无从谈起。
                # 断言它**不在** error_tags 里（tags=[] 那行已经覆盖了）
                "compile.diagnostics.0.source": None},
        why="关掉 lint 只是少一层提示，判定结果与开着时逐字段一致",
    ),
    Case(
        name="lint：verilator 不在时如实报「没跑」",
        args=[_rel(NEG / "dut_comb_loop.v"), "--tb", _rel(NEG / "tb_comb_loop.v"),
              "--top", "tb_comb_loop",
              "--verilator-path", "build/_smoke_no_such_verilator"],
        status="pass", exit_code=0,
        tags=[],
        expect={"verdict.passed": 3, "verdict.total": 3,
                "lint.ran": False, "lint.ok": False, "lint.n_warnings": 0,
                "compile.diagnostics.0.source": None},
        why="**这条最关键**：没有工具时绝不能报成「检查过，干净」——"
            "两者都是零条诊断，含义却相反。判定同样一个字不变。"
            "刻意用 --verilator-path 指向不存在的目录，好让它在**没装 verilator 的机器上也照样跑**，"
            "降级路径的覆盖不依赖环境",
    ),
    Case(
        name="lint：程序起来了但压根不是 verilator",
        args=[_rel(NEG / "dut_comb_loop.v"), "--tb", _rel(NEG / "tb_comb_loop.v"),
              "--top", "tb_comb_loop",
              "--verilator-path", ".tools/iverilog/bin/iverilog.exe"],
        status="pass", exit_code=0,
        tags=[],
        expect={"lint.ran": False, "lint.ok": False, "lint.n_warnings": 0},
        why="这条专打「**有输出所以判成跑过了**」。进程正常启动、也吐了字，"
            "但输出里没有 Verilation Report —— 说明没跑到 elaboration。"
            "拿「有没有输出」当判据的话，这里会报成「检查了，没问题」，"
            "而实际上**什么都没检查**。拿 iverilog 冒充 verilator 是刻意的："
            "它是个真会跑起来的程序，能稳定造出这个形状，且不依赖 verilator 装没装",
    ),
    Case(
        name="语法错误",
        args=[_rel(NEG / "syntax_error.v")],
        status="compile_error", exit_code=2,
        expect={"compile.first_error.line": 12, "compile.first_error.kind": "syntax",
                # 编译都没过，lint 就没跑 —— 但**块必须在**，且说清没跑。
                # 少了这个默认值，这条结果里压根没有 lint 这个键，
                # 读的人会把「没这个字段」当成「lint 没问题」。
                "lint.ran": False, "lint.ok": False},
        why="带行号的结构化错误 + 中文提示；顺带锁住「每个 status 下 lint 块形状统一」",
    ),
    Case(
        name="顶层模块名写错",
        args=[_rel(SMOKE / "dut_counter4.v"), "--top", "counter_4"],
        status="compile_error", exit_code=2,
        expect={"compile.first_error.kind": "bad_top"},
        why="最高频的手误，应被拦截并给出候选名，而不是抛 iverilog 原文",
    ),
    Case(
        name="组合 always 缺 case default（锁存器）",
        args=[_rel(NEG / "latch_incomplete_case.v")],
        status="inconclusive", exit_code=1,
        tags=["latch_inference"],
        expect={"compile.ok": True, "compile.diagnostics.0.severity": "warning"},
        why="iverilog 一句 warning 都不给，靠 tools/_lint.py 的源码扫描发现；"
            "编译本身必须是成功的",
    ),
    Case(
        name="运行期挂死（等待永不成立的条件）",
        args=[_rel(NEG / "hang_wait_forever.v"), "--timeout", "5"],
        status="timeout", exit_code=3,
        tags=["sim_timeout"],
        expect={"run.timed_out": True, "run.tree_killed": True},
        tail_has=["TEST_BEGIN"],
        why="超时必须杀掉整棵进程树，且**已打印的标记要留住**"
            "（靠 tb_common.vh 里每次标记后的 $fflush）",
    ),
    Case(
        name="零延迟 always（iverilog 编译期就拒绝）",
        args=[_rel(NEG / "hang_zero_delay.v")],
        status="compile_error", exit_code=2,
        expect={"compile.first_error.kind": "infinite_loop"},
        why="这类形状根本到不了运行时，iverilog 静态拒绝；"
            "能编译但要靠运行时超时兜底的是上一条",
    ),
    Case(
        name="测试台没有任何判定标记",
        args=[_rel(NEG / "dut_shift_good.v"), "--tb", _rel(NEG / "tb_quiet.v"),
              "--top", "tb_quiet"],
        status="inconclusive", exit_code=1,
        expect={"verdict.inconclusive": True, "run.returncode": 0},
        why="退出码 0 且无报错，但**绝不能判通过** —— 证据不足就是证据不足",
    ),
    # ---- 参数覆盖（--param）。四条一组：生效 / 默认值 / 拒绝瞎猜 / 硬报错 ----
    Case(
        name="参数覆盖：--param 真的改到了 elaboration",
        args=[_rel(SMOKE / "dut_counterN.v"), "--tb", _rel(SMOKE / "tb_counterN.v"),
              "--param", "WIDTH=3"],
        status="pass", exit_code=0,
        # 这一行是"参数真生效了"的直接证据：判定结果本身分不出
        # "参数生效" 和 "参数被静默忽略"（后者仿真照跑，只是用默认值）
        tail_has=["EFFECTIVE_WIDTH=3"],
        expect={"verdict.passed": 3, "verdict.total": 3,
                # WIDTH=3 时数 8 拍回绕到 0，回绕这个覆盖点也打到了
                "cover.ok": True, "cover.affects_verdict": False,
                "cover.n_declared": 3, "cover.n_covered": 3, "cover.n_missed": 0,
                "cover.missed": [], "cover.complete": True, "cover.ratio": 1.0},
        why="参数透传链路（sim.py 定根模块 → -P → 测试台 → DUT）走通："
            "判定期望值随 WIDTH 现算，参数没生效时 count_8_clocks 必红",
    ),
    Case(
        name="参数覆盖：不传参数就是默认值",
        args=[_rel(SMOKE / "dut_counterN.v"), "--tb", _rel(SMOKE / "tb_counterN.v")],
        status="pass", exit_code=0,
        tail_has=["EFFECTIVE_WIDTH=4"],
        expect={"verdict.passed": 3, "verdict.total": 3,
                # WIDTH=4 数 8 拍不回绕，"回绕"这个覆盖点天然打不到
                "cover.n_declared": 3, "cover.n_covered": 2, "cover.n_missed": 1,
                "cover.missed": ["count_wraps"], "cover.complete": False,
                "cover.ratio": 0.667},
        why="与上一条配对：同一个测试台、不同的参数给出不同结果，"
            "才说明参数是生效的，而不是测试台把期望值写死了",
    ),
    Case(
        name="参数覆盖：定不下根模块时宁可拒绝，不猜",
        args=[_rel(SMOKE / "dut_counterN.v"), _rel(NEG / "dut_mux2.v"),
              "--param", "WIDTH=3"],
        status="usage_error", exit_code=64,
        expect={"error.kind": "no_root_for_param"},
        why="iverilog 的 -P 只认根模块，且**放错模块时静默忽略**"
            "（实测退出码 0、仿真照跑、参数保持默认值）。"
            "宁可不跑也不能猜一个根模块，否则使用者拿到的是悄悄不对的结果",
    ),
    Case(
        name="参数覆盖：参数名在根上不存在 → 硬报错",
        args=[_rel(SMOKE / "dut_counterN.v"), "--tb", _rel(SMOKE / "tb_counterN.v"),
              "--param", "NOSUCH=3"],
        status="compile_error", exit_code=2,
        tags=["param_not_found"],
        # first_error 必须是这条诊断：iverilog 把它打成 ":0:"（没有文件定位），
        # 以前整条会被丢掉 —— 编译明明失败了，diagnostics 却是空的、
        # first_error 是 null。这条断言就是钉住那个修复。
        expect={"compile.ok": False,
                "compile.first_error.kind": "param_not_found"},
        why="「模块名对、参数名错」是 iverilog 唯一会硬报错的情形，"
            "必须解析出来并给出可照做的中文提示",
    ),
    # ---- specify 路径延迟。一对：默认忽略 / --specify 生效 ----
    Case(
        name="specify：默认忽略（按零延迟跑）",
        args=[_rel(SMOKE / "dut_dly.v"), "--tb", _rel(SMOKE / "tb_dly.v")],
        status="fail", exit_code=1,
        expect={"verdict.failed": 1, "verdict.total": 2},
        why="不写 --specify 时 iverilog 不认 specify 块。这条是下一条的对照——"
            "少了它，「开了 --specify 就通过」可能只是因为本来就能过",
    ),
    Case(
        name="specify：--specify 让路径延迟生效",
        args=[_rel(SMOKE / "dut_dly.v"), "--tb", _rel(SMOKE / "tb_dly.v"),
              "--specify"],
        status="pass", exit_code=0,
        expect={"verdict.passed": 2, "verdict.total": 2},
        why="同一份设计、同一个测试台，只多一个开关，判定从 fail 翻成 pass",
    ),
    # ---- 覆盖点不是判定标记 ----
    Case(
        name="覆盖点：只有覆盖标记、没有判定标记 → 仍判「没判定」",
        args=[_rel(NEG / "dut_mux2.v"), "--tb", _rel(NEG / "tb_cover_only.v")],
        status="inconclusive", exit_code=1,
        expect={"verdict.protocol_used": False,
                "verdict.inconclusive": True,
                # 覆盖数据照样给，和"没判定"互不干扰
                "cover.ok": True, "cover.n_declared": 1, "cover.n_covered": 1},
        why="覆盖点一旦把 protocol_used 置起来，只打覆盖点的测试台就会被误判成"
            "「按协议汇报过判定」；覆盖是观察项，不许碰判定",
    ),
]


def _dig(obj, path: str):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            i = int(part)
            if i >= len(cur):
                return None
            cur = cur[i]
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


def run_case(c: Case) -> list[str]:
    """跑一个用例，返回失败原因列表（空表示通过）。"""
    proc = subprocess.run(
        [sys.executable, str(SIM), *c.args, "--quiet"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    fails: list[str] = []

    # stdout 本来就是 JSON（人类摘要走 stderr），直接解析
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return [f"stdout 不是合法 JSON：{proc.stdout[:300]!r}"]

    if result.get("status") != c.status:
        fails.append(f"status={result.get('status')!r}，期望 {c.status!r}")
    if proc.returncode != c.exit_code:
        fails.append(f"退出码={proc.returncode}，期望 {c.exit_code}")

    tags = result.get("error_tags") or []
    for t in c.tags:
        if t not in tags:
            fails.append(f"error_tags 缺少 {t!r}（实际 {tags}）")

    for path, want in c.expect.items():
        got = _dig(result, path)
        if got != want:
            fails.append(f"{path}={got!r}，期望 {want!r}")

    tail = ((result.get("run") or {}).get("tail") or "")
    for s in c.tail_has:
        if s not in tail:
            fails.append(f"run.tail 里缺少 {s!r}（实际长度 {len(tail)}）")
    for s in c.tail_lacks:
        if s in tail:
            fails.append(f"run.tail 里不应出现 {s!r}")

    return fails


def main() -> int:
    width = max(len(c.name) for c in CASES)
    failures = 0
    skipped = 0
    for c in CASES:
        if c.needs_verilator and not VERILATOR_OK:
            print(f"  ⊘ {c.name:<{width}}  {c.status:<14} exit={c.exit_code}")
            print(f"      跳过：{VERILATOR_WHY}")
            print()
            skipped += 1
            continue

        fails = run_case(c)
        mark = "✓" if not fails else "✗"
        print(f"  {mark} {c.name:<{width}}  {c.status:<14} exit={c.exit_code}")
        if c.why:
            print(f"      {c.why}")
        for f in fails:
            print(f"      ↳ {f}")
        if fails:
            failures += 1
        print()

    total = len(CASES) - skipped
    if failures:
        print(f"冒烟测试未通过：{total - failures}/{total} 个用例通过（另跳过 {skipped} 个）")
        return 1
    tail = f"（另跳过 {skipped} 个需要 verilator 的用例）" if skipped else ""
    print(f"冒烟测试全部通过：{total}/{total}{tail}")

    return _run_tool_checks()


# 判定链路之外的工具检查。分成几个脚本是因为它们依赖的东西不同
# （波形解析要 VCD，绘制检查要 node），任何一个缺失都不该拖垮其余部分。
TOOL_CHECKS = ["check_wave.py", "check_wave_js.py", "check_wave_draw.py",
               "check_observe.py", "check_schematic.py", "check_lesson.py",
               "check_regress.py", "check_synth.py", "check_cov.py",
               "check_toggle.py", "check_manifest.py", "check_vlint.py"]


def _run_tool_checks() -> int:
    print()
    print("=" * 60)
    print("工具检查")
    print("=" * 60)
    bad = 0
    for name in TOOL_CHECKS:
        path = SMOKE / name
        if not path.exists():
            continue
        print(f"\n--- {name} " + "-" * max(0, 40 - len(name)))
        p = subprocess.run([sys.executable, str(path)], cwd=str(ROOT),
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        # 这些脚本自己会打印细节，原样透传（不吞输出 —— 报错信息就是价值所在）
        sys.stdout.write(p.stdout)
        if p.stderr.strip():
            sys.stdout.write(p.stderr)
        if p.returncode != 0:
            bad += 1

    print()
    if bad:
        print(f"工具检查未通过：{bad} 个脚本失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
