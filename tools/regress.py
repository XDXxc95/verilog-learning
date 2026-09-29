"""多参数 × 多宏组合批量回归。

    python tools/regress.py dut.v --tb tb_dut.v --param WIDTH=3,4,5
    python tools/regress.py dut.v --tb tb_dut.v --param WIDTH=2:6 --define MODE=0,1

把「同一份源码、不同参数取值」的仿真一次跑完，出一张对照表。
这是**参数化设计**的核心证据——参数化的意义就在于同一份源码在不同取值下
是不同规模的电路，只看一个取值等于没验证过。

## 为什么不能只是"循环调用 sim.py"

因为最要命的失败是**静默的**：参数没生效，每一轮都跑同一个电路，
二十个组合全绿，看起来漂亮极了。这条链路上有两个地方会悄悄断掉：

  1. `iverilog -P` 只认**根模块**。本仓库的根是测试台，所以参数必须声明在
     **测试台**上，再由测试台 `#(.WIDTH(WIDTH))` 透传给待测模块。
     声明在待测模块上是没用的——不过这种写法 iverilog 会硬报
     `parameter not found in`，属于"错得响亮"，归 sim.py 管。
  2. **测试台声明了参数却没往下传**。这时 `-P` 生效、编译通过、仿真跑完，
     但待测模块用的还是默认值。**iverilog 一声不响**，每一轮结果完全相同。

第 2 条是这个脚本存在的理由。两道防线：

  · **生效值核对**（硬）：测试台若打印 `EFFECTIVE_<参数名>=<值>`，
    就逐轮比对"我要的值"和"它说生效的值"。对不上直接判失败。
    这个约定见 `tests/_smoke/tb_counterN.v` —— 那里已经这么写了。
  · **结果指纹**（兜底）：把每一轮的判定结果压成一个指纹
    （status + 用例名/结果 + 覆盖点命中集）。某一维扫下来指纹只有一种，
    就说明**这一维什么都没改变**。没有生效值证据时，这是失败判据：
    "扫了但没扫到任何区别"和"扫全了"在表面上是一模一样的。

第二道防线是启发式（真要有一个参数恰好不影响本测试台观察到的任何东西，
它也会报警），所以给了 `--allow-inert` 放行——但**要求你显式放行**，
而不是默默放过。

## 和 sim.py 的关系

`regress.py` **不做判定**。每一轮都调 `sim.py` 的判定内核，它自己只负责
编排和汇总，结论原样搬过来。所以"对错"仍然只有一个权威来源。
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import sim as simmod               # noqa: E402
from _console import init_console  # noqa: E402
from _toolchain import detect, repo_root  # noqa: E402

SCHEMA = "vlearn.regress/1"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 64
EXIT_ENV = 4

# 扫描上限。存在的意义不是省钱，是**挡住手滑**：把 `WIDTH=1:4096` 当成笔误
# 打出来，真跑起来要几分钟才有人发现不对。超了直接拒绝并说清楚。
MAX_COMBOS = 256

_AXIS_RE = re.compile(r"^([A-Za-z_]\w*)\s*=\s*(.+)$", re.DOTALL)
_RANGE_RE = re.compile(r"^(-?\d+)\s*:\s*(-?\d+)$")
# 测试台自报的生效值。名字按参数名拼，所以一条 `$display("EFFECTIVE_W=%0d", W)`
# 就能同时充当"我声明了这个参数"和"它真的是这个值"两份证据。
_EFFECTIVE_RE_TPL = r"EFFECTIVE_{name}\s*=\s*(\S+)"

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.=-]+")


# ---------------------------------------------------------------- 参数解析


def _expand_values(raw: str) -> list[str] | None:
    """把一个轴的值展开成列表。`3,4,5` 是枚举，`2:6` 是闭区间。

    区间只认纯十进制整数——`2:6` 写成 `2 : 6` 也认，但 `a:b` 不认。
    不猜：认不出来就原样当成一个值返回，让 iverilog 去报错。
    """
    parts = [p.strip() for p in raw.split(",")]
    if any(p == "" for p in parts):
        return None
    out: list[str] = []
    for p in parts:
        m = _RANGE_RE.match(p)
        if not m:
            out.append(p)
            continue
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi < lo:
            return None
        # 区间展开也要受上限约束，否则 `1:99999` 会在展开这一步就把内存吃掉
        if hi - lo + 1 > MAX_COMBOS:
            return None
        out.extend(str(v) for v in range(lo, hi + 1))
    return out


def _parse_axis(items: list[str], kind: str) -> tuple[dict[str, list[str]], str | None]:
    """解析 `--param NAME=v1,v2` / `--define NAME=v1,v2`（可重复）。

    同名的轴再出现一次就**并集的顺序追加**，而不是覆盖：`--param W=3,4 --param W=5`
    读作 3,4,5 比丢掉前一半更符合直觉，也更不容易悄悄少扫一档。
    """
    axes: dict[str, list[str]] = {}
    for item in items:
        m = _AXIS_RE.match(item.strip())
        if not m:
            return {}, (
                f"--{kind} 的写法应该是 NAME=VALUE[,VALUE...]，收到的是 {item!r}。"
                f"（例如 --{kind} WIDTH=4,8）"
            )
        name, raw = m.group(1), m.group(2)
        vals = _expand_values(raw)
        if vals is None:
            return {}, (
                f"--{kind} {name} 的取值列表看不懂：{raw!r}。"
                "写成逗号分隔的枚举（4,8,16）或闭区间（2:6）。"
            )
        for v in vals:
            if v not in axes.setdefault(name, []):
                axes[name].append(v)
    return axes, None


def _combos(params: dict[str, list[str]],
            defines: dict[str, list[str]]) -> list[dict]:
    """笛卡尔积。空轴一律禁止——'零个组合'跑出来是一张空表，最容易被读成'全过'。"""
    names_p = sorted(params)
    names_d = sorted(defines)
    out: list[dict] = []
    for pv in itertools.product(*(params[n] for n in names_p)):
        for dv in itertools.product(*(defines[n] for n in names_d)):
            out.append({
                "params": dict(zip(names_p, pv)),
                "defines": dict(zip(names_d, dv)),
            })
    return out


# ---------------------------------------------------------------- 运行


def _slug(combo: dict) -> str:
    parts = [f"{k}={v}" for k, v in sorted(combo["params"].items())]
    parts += [f"def_{k}={v}" for k, v in sorted(combo["defines"].items())]
    s = _SLUG_RE.sub("_", "__".join(parts))
    return s or "run"


def _run_combo(root: Path, combo: dict, index: int, designs: list[Path],
               tb: Path | None, top: str | None, workdir: Path, timeout: float,
               no_lint: bool, observe: bool) -> dict:
    """跑一个组合。判定完全交给 sim.py 的判定内核。

    目录名带上序号前缀：slug 是拼出来的，理论上两个不同的组合可能拼出同名
    （值里带 `__` 之类）。撞名会让两轮共用一个 workdir，第二轮覆盖第一轮的产物，
    而表面上一切正常 —— 加个序号前缀，这一类就彻底不可能发生。
    """
    name = f"{index:02d}__{_slug(combo)}"
    cfg = simmod.SimConfig(
        designs=designs,
        tb=tb,
        top=top,
        workdir=workdir / name,
        name=name,
        timeout=timeout,
        gflags=[],
        includes=[],
        defines=[f"{k}={v}" for k, v in sorted(combo["defines"].items())],
        syntax_only=False,
        no_vcd=not observe,
        no_observe=not observe,
        no_lint=no_lint,
        iverilog_path=None,
        verilator_path=None,
        params=[f"{k}={v}" for k, v in sorted(combo["params"].items())],
    )
    result, code = simmod._run_sim(cfg)
    return {"result": result, "exit_code": code, "workdir": str(cfg.workdir)}


def _reported_effective(result: dict, axis: str) -> str | None:
    """从仿真的原始输出里抠出测试台自报的生效值。

    取 `run.tail`（输出被截断时尾部也在，而 `$display` 这条一定在最前面，
    所以 head/tail 两头都找一遍）。找不到就给 None——**不许拿请求值顶替**，
    那正是这个函数要防的事。
    """
    run = result.get("run") or {}
    for key in ("tail", "head"):
        text = run.get(key) or ""
        m = re.search(_EFFECTIVE_RE_TPL.format(name=re.escape(axis)), text)
        if m:
            return m.group(1).strip()
    return None


def _fingerprint(result: dict) -> str:
    """一轮结果的指纹：判定 + 用例结果 + 覆盖命中集。

    刻意**不包含**时间戳、耗时、`$finish at` 那些每次都变的东西——
    指纹要回答的是"这一轮观察到了什么不一样"，掺进噪声就永远只有一种指纹。

    `TESTS` 里带上用例名和结果而不是只带总通过数：`WIDTH=3` 和 `WIDTH=4`
    都可能 3/3 通过，但覆盖命中的集合不同（回绕那条只有窄位宽才打得到），
    只看通过数会把这两个真不一样的运行判成"没区别"。
    """
    tests = [(t.get("name"), t.get("status")) for t in (result.get("tests") or [])]
    cov = result.get("cover") or {}
    parts = [
        str(result.get("status")),
        json.dumps(tests, ensure_ascii=False),
        json.dumps(sorted(cov.get("hit") or []), ensure_ascii=False),
        json.dumps(sorted(cov.get("missed") or []), ensure_ascii=False),
        json.dumps(sorted(result.get("error_tags") or []), ensure_ascii=False),
    ]
    return " | ".join(parts)


def _digest(combo: dict, run: dict, axes_all: list[str]) -> dict:
    """一轮的单行摘要。够诊断，又不至于把完整 sim JSON 塞进来。"""
    r = run["result"]
    v = r.get("verdict") or {}
    cov = r.get("cover") or {}
    first = (r.get("compile") or {}).get("first_error") or r.get("error") or {}
    return {
        "params": combo["params"],
        "defines": combo["defines"],
        "status": r.get("status"),
        "passed": v.get("passed", 0),
        "failed": v.get("failed", 0),
        "total": v.get("total", 0),
        "protocol_used": v.get("protocol_used"),
        "cover": {
            "ok": cov.get("ok"),
            "n_declared": cov.get("n_declared", 0),
            "n_covered": cov.get("n_covered", 0),
            "missed": cov.get("missed") or [],
            "ratio": cov.get("ratio"),
        },
        "tests": [{"name": t.get("name"), "status": t.get("status")}
                  for t in (r.get("tests") or [])],
        "effective": {a: _reported_effective(r, a) for a in axes_all},
        "error_tags": r.get("error_tags") or [],
        "first_error": (first.get("message") or first.get("raw")) if isinstance(first, dict) else None,
        "note_zh": v.get("note_zh") or (r.get("error") or {}).get("message"),
        "fingerprint": _fingerprint(r),
        "workdir": run["workdir"],
    }


# ---------------------------------------------------------------- 轴核对


def _axis_report(kind: str, name: str, values: list[str],
                 digests: list[dict]) -> dict:
    """一个维度扫完之后的核对结果。

    多维同时扫的时候，指纹的差异说不清是哪一维带来的。所以核对要
    **按"其余维固定"分组**：固定住其它所有轴，只看这一维从最小到最大时
    结果有没有变化。每一组是一次干净的对照，组内全是同一个其余维组合。

    笛卡尔积保证了每一组都包含这一维的全部取值，所以"组内有没有差异"
    就是"这一维起没起作用"的直接判据。
    """
    key = "params" if kind == "param" else "defines"
    rep: dict = {
        "kind": kind,
        "name": name,
        "values": values,
        "effective_ok": None,     # None = 测试台没报，无从核对
        "mismatches": [],
        "n_groups": 0,            # 其余维固定后，能形成的对照组数
        "n_varying_groups": 0,    # 其中这一维真的造成了差异的组数
        "inert": False,
        "evidence": "none",
        "note_zh": None,
    }

    # 生效值核对：只有**每一轮都报了**才谈得上核对。
    # 报一半比不报更可疑（比如 `ifdef` 包着的打印语句），所以只要缺一条
    # 就整体降级成"没有证据"，不拿有的那几条硬撑。
    reported: dict[str, str] = {}
    complete = True
    for d in digests:
        val = d["effective"].get(name)
        if val is None:
            complete = False
            break
        reported[d[key][name]] = val
    if complete and reported:
        rep["effective_ok"] = True
        rep["evidence"] = "effective"
        rep["effective"] = dict(sorted(reported.items()))
        for want, got in sorted(reported.items()):
            if not _same_int(want, got):
                rep["effective_ok"] = False
                rep["mismatches"].append({"asked": want, "reported": got})

    # 结果指纹：按"其余维固定"分组，组内比较这一维各取值的结果。
    #
    # 为什么必须固定其余维：指纹里记的是"这一轮观察到了什么"。多维一起变时
    # 差异可能来自别的轴，直接拿全局指纹去重会把"这一维有用"误判成"没用"
    # （或反过来）。分组之后组内只有这一维在变，差异的归因才是确定的。
    groups: dict[tuple, dict[str, str]] = {}
    for d in digests:
        merged = {**d["params"], **d["defines"]}
        others = tuple(sorted((k, v) for k, v in merged.items() if k != name))
        # 同一个取值在组内只该出现一次（笛卡尔积保证），真重复了说明有别的
        # 东西在变（比如仿真本身不确定），那就没法当对照用，标记成脏组。
        slot = groups.setdefault(others, {})
        prev = slot.get(d[key][name])
        slot[d[key][name]] = d["fingerprint"] if prev in (None, d["fingerprint"]) else "!DIRTY!"

    # 只有"组内包含不止一个取值"的组才算一次对照。单取值的组（比如只扫了
    # 一个值）比不出任何东西，算进去只会把 inert 判成 True。
    informative = {g: s for g, s in groups.items()
                   if len(s) > 1 and "!DIRTY!" not in s.values()}
    rep["n_groups"] = len(informative)
    if informative:
        rep["n_varying_groups"] = sum(
            1 for s in informative.values() if len(set(s.values())) > 1
        )
        rep["inert"] = rep["n_varying_groups"] == 0
        if rep["inert"]:
            rep["note_zh"] = (
                f"{kind} {name} 扫了 {len(values)} 个取值"
                f"（{', '.join(values)}），在 {len(informative)} 组对照里，"
                f"每一组的判定结果、用例结果、覆盖命中**完全一样**。"
            )
    else:
        # 走到这里可能是取值只有一个，也可能是回归中途停了（中断时只跑了一轮）。
        # 两种原因都写出来，免得读者把"没跑完"读成"参数本来就没用"。
        rep["note_zh"] = (
            f"{kind} {name} 扫了 {len(values)} 个取值，实际只跑了 {len(digests)} 个组合，"
            f"不足一次对照（需要固定其余维、至少两个取值）。"
        )
    return rep


def _same_int(a: str, b: str) -> bool:
    """比较生效值。`4` 和 `4'd4` / `0x4` 是同一个值，`4` 和 `04` 也是。

    比不出来就退回字符串比较——宁可比得严一点报一次假警，也不要因为
    格式差异把真问题放过。
    """
    def norm(s: str) -> int | None:
        s = s.strip().replace("_", "")
        m = re.match(r"^(?:\d+)?'([bodhBODH])([0-9a-fA-FxXzZ]+)$", s)
        try:
            if m:
                base = {"b": 2, "o": 8, "d": 10, "h": 16}[m.group(1).lower()]
                return int(m.group(2), base)
            return int(s, 0)
        except ValueError:
            return None
    na, nb = norm(a), norm(b)
    if na is None or nb is None:
        return a.strip() == b.strip()
    return na == nb


# ---------------------------------------------------------------- 汇总


def _collect_problems(digests: list[dict], axes: list[dict],
                      allow_inert: bool, aborted: str | None) -> list[dict]:
    problems: list[dict] = []

    if aborted:
        problems.append({"kind": "aborted", "message_zh": aborted, "stopped": True})

    # 每一轮自己的问题
    for d in digests:
        label = _label(d)
        if d["status"] != "pass":
            problems.append({
                "kind": "combo_not_pass",
                "combo": label,
                "message_zh": f"{label}：判定为 {d['status']}",
                "detail_zh": d["note_zh"],
                "first_error": d["first_error"],
                "workdir": d["workdir"],
            })
        elif d["cover"]["ok"] and d["cover"]["missed"]:
            # 覆盖有洞**不是**判定失败（内核里写死了 affects_verdict=false），
            # 但批量回归的看点之一就是"这一档参数少验了什么场景"，
            # 所以单列一档，措辞上也明确它不是错。
            problems.append({
                "kind": "coverage_hole",
                "combo": label,
                "message_zh": (
                    f"{label}：判定通过，但有 "
                    f"{len(d['cover']['missed'])} 个覆盖点没打到"
                    f"（{', '.join(d['cover']['missed'])}）——这说明测得浅，"
                    f"不是写错了。"
                ),
                "severity": "info",
            })

    # 每一维的核对
    for a in axes:
        label = f"{a['kind']} {a['name']}"
        if a["mismatches"]:
            got = "、".join(f"{m['asked']}→{m['reported']}"
                            for m in a["mismatches"])
            problems.append({
                "kind": "param_not_effective",
                "axis": label,
                "message_zh": (
                    f"{label} 没有真正生效：要求 {got}（左边是命令行给的、"
                    f"右边是测试台自报的生效值）。检查测试台有没有把这个值"
                    f"透传给待测模块。"
                ),
            })
        elif a["inert"] and a["evidence"] == "effective" and a["effective_ok"]:
            # 生效值核对过、结果又完全一样 —— 那"没区别"是**事实**而不是可疑：
            # 参数确实传下去了，只是这一档取值不影响本测试台观察到的任何东西。
            # 仍然报出来（info），因为"扫了一维却什么都没扫出来"本身是值得知道的。
            problems.append({
                "kind": "inert_but_verified",
                "axis": label,
                "severity": "info",
                "message_zh": a["note_zh"] + "生效值核对无误，所以这不是没传下去，"
                              "而是这一维在这套激励下确实观察不到差别。",
            })
        elif a["inert"] and not allow_inert:
            problems.append({
                "kind": "sweep_looks_inert",
                "axis": label,
                "message_zh": (
                    a["note_zh"] +
                    f"而测试台没有打印生效值（`EFFECTIVE_{a['name']}=`），"
                    "无法证明参数真的传下去了。先确认测试台把参数透传给了待测模块"
                    "（`dut #(.W(W)) u (...)`）；确认无误、只是这一维确实不影响"
                    "本测试台的观察项，再加 --allow-inert 放行。"
                ),
            })
    return problems


def _label(d: dict) -> str:
    parts = [f"{k}={v}" for k, v in sorted(d["params"].items())]
    parts += [f"{k}={v}" for k, v in sorted(d["defines"].items())]
    return " ".join(parts) if parts else "(无参数)"


# ---------------------------------------------------------------- 输出


def _print_summary(rep: dict) -> None:
    """人类可读摘要。**走 stderr**，和 sim.py 保持同一个约定：
    stdout 只放 JSON，程序读那边。"""
    w = sys.stderr.write
    w("=" * 60 + "\n")
    w(f"多参数回归：{rep['n_combos']} 个组合")
    w(f"（{' × '.join(rep['axis_labels'])}）\n" if rep["axis_labels"] else "\n")
    w("-" * 60 + "\n")

    head = f"  {'#':>3}  {'组合':<28}{'判定':<14}{'用例':<8}{'覆盖':<10}\n"
    w(head)
    for i, d in enumerate(rep["combos"], 1):
        cov = d["cover"]
        cov_s = (f"{cov['n_covered']}/{cov['n_declared']}" if cov["n_declared"]
                 else "—")
        if cov["missed"]:
            cov_s += " !"
        tests = f"{d['passed']}/{d['total']}" if d["protocol_used"] else "无标记"
        w(f"  {i:>3}  {_label(d):<28}{d['status']:<14}{tests:<8}{cov_s:<10}\n")

    w("-" * 60 + "\n")
    for a in rep["axes"]:
        if a["n_groups"]:
            tail = (f"{a['n_groups']} 组对照里 {a['n_varying_groups']} 组有差异"
                    if a["n_varying_groups"]
                    else f"{a['n_groups']} 组对照里**没有一组**有差异")
        else:
            tail = "取值不足两个，比不出差异"
        w(f"  · {a['kind']} {a['name']:<12} 取值 {'/'.join(a['values'])}   {tail}\n")
        if a["evidence"] == "effective":
            eff = "/".join(a.get("effective", {}).get(v, "?") for v in a["values"])
            w(f"    测试台自报生效值：{eff}"
              f"{'（与要求一致）' if a['effective_ok'] else '（★ 对不上）'}\n")
        else:
            w(f"    测试台没有打印 EFFECTIVE_{a['name']}=，"
              f"参数是否生效没有直接的证据\n")

    if rep["problems"]:
        w("-" * 60 + "\n")
        for p in rep["problems"]:
            icon = {"info": "·", "aborted": "✗"}.get(p.get("severity"), "✗")
            w(f"  {icon} {p['message_zh']}\n")
    else:
        w("-" * 60 + "\n")
        w(f"  · {rep['n_combos']} 个组合全部通过，各维参数都核对过生效。\n")

    if rep["aborted"]:
        w("\n结果：回归中断 ✗\n")
    elif rep["ok"]:
        w(f"\n结果：{rep['n_combos']} 个组合全部通过 ✓\n")
    else:
        w("\n结果：有组合未通过 ✗\n")
    w("=" * 60 + "\n")


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    init_console()
    root = repo_root()

    ap = argparse.ArgumentParser(
        prog="regress.py",
        description="多参数 × 多宏组合批量回归（判定仍由 sim.py 独立完成）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("## 为什么不能只是", 1)[0].split("\n", 1)[1].strip(),
    )
    ap.add_argument("designs", nargs="*", type=Path, help="设计源文件（可多个）")
    ap.add_argument("--tb", type=Path, help="测试台文件")
    ap.add_argument("--top", help="顶层模块名（测试台名）")
    ap.add_argument("--param", action="append", default=[], metavar="NAME=v1,v2",
                    help="扫描的参数（可重复）。值写 `a,b,c` 或区间 `2:6`")
    ap.add_argument("--define", action="append", default=[], metavar="NAME=v1,v2",
                    help="扫描的宏定义（可重复），语法同 --param")
    ap.add_argument("--workdir", type=Path, help="产物根目录（默认 build/regress/<顶层名>）")
    ap.add_argument("--timeout", type=float, default=30.0, help="每个组合的超时秒数")
    ap.add_argument("--allow-inert", action="store_true",
                    help="放行「某一维扫下来结果完全一样、又没有生效值证据」的告警")
    ap.add_argument("--observe", action="store_true",
                    help="为每个组合生成并解析波形（默认关，批量跑时通常不需要）")
    ap.add_argument("--no-lint", action="store_true",
                    help="不跑 verilator lint（默认跑：换参数可能换出一个新的锁存器）")
    ap.add_argument("--json", type=Path, help="把 JSON 结果同时写入该文件")
    ap.add_argument("--full", action="store_true",
                    help="JSON 里带上每个组合的完整 sim 结果（很大，默认只放摘要）")
    ap.add_argument("--pretty", action="store_true", help="JSON 缩进输出")
    ap.add_argument("--quiet", action="store_true", help="不输出人类可读摘要")
    args = ap.parse_args(argv)

    params, err = _parse_axis(args.param, "param")
    if err:
        print(err, file=sys.stderr)
        return EXIT_USAGE
    defines, err = _parse_axis(args.define, "define")
    if err:
        print(err, file=sys.stderr)
        return EXIT_USAGE

    if not params and not defines:
        print("没有要扫描的维度：至少给一个 --param 或 --define。", file=sys.stderr)
        print("（只跑一遍的话直接用 sim.py，不必绕道这里。）", file=sys.stderr)
        return EXIT_USAGE

    combos = _combos(params, defines)
    if len(combos) > MAX_COMBOS:
        print(f"组合数 {len(combos)} 超过上限 {MAX_COMBOS}，拒绝执行。"
              f"（先确认没把区间写错，再拆成几次跑。）", file=sys.stderr)
        return EXIT_USAGE

    if not args.designs:
        print("没有给设计源文件。", file=sys.stderr)
        return EXIT_USAGE

    tc = detect(None)
    if not tc.ok:
        rep = {
            "schema": SCHEMA, "ok": False, "status": "env_error",
            "error": {"kind": "no_toolchain",
                      "message": "找不到 iverilog，无法回归。"},
            "toolchain": tc.as_dict(),
            "next_actions_zh": ["先跑 python tools/setup_toolchain.py 装工具链"],
        }
        _emit(rep, args)
        return EXIT_ENV

    name = args.top or (args.tb.stem if args.tb else args.designs[0].stem)
    workdir = args.workdir or (root / "build" / "regress" / name)

    axis_labels = ([f"--param {n}({len(v)})" for n, v in sorted(params.items())]
                   + [f"--define {n}({len(v)})" for n, v in sorted(defines.items())])
    axes_all = sorted(set(params) | set(defines))

    digests: list[dict] = []
    raw: list[dict] = []
    aborted: str | None = None
    for i, combo in enumerate(combos, 1):
        if not args.quiet:
            sys.stderr.write(f"  [{i}/{len(combos)}] {_slug(combo)} …\n")
            sys.stderr.flush()
        run = _run_combo(root, combo, i, args.designs, args.tb, args.top,
                         workdir, args.timeout, args.no_lint, args.observe)
        r = run["result"]
        digests.append(_digest(combo, run, axes_all))
        raw.append(run)

        # 参数名在根模块上不存在 → 后面每一轮都会以同样的方式挂掉，
        # 再跑下去只是把同一条错误重复 N 遍。停在这里，报一条就够。
        # iverilog 对"放错模块的 -P"是静默忽略的，但"名字压根没有"会硬报，
        # 所以这一条抓得准。
        tags = r.get("error_tags") or []
        if "param_not_found" in tags or (r.get("error") or {}).get("kind") == "bad_param":
            aborted = (
                f"参数在测试台上找不到（{_label(digests[-1])}）——iverilog 的 -P "
                f"只认根模块，本仓库的根是测试台。把参数声明到测试台上"
                f"（`module tb_xxx #(parameter W = 4);`），再由测试台透传给待测模块。"
                f"剩下的 {len(combos) - i} 个组合没有跑。"
            )
            break

    axes = []
    for kind, table in (("param", params), ("define", defines)):
        for n, vals in sorted(table.items()):
            mine = [d for d in digests if n in (d["params"] if kind == "param" else d["defines"])]
            axes.append(_axis_report(kind, n, vals, mine))

    problems = _collect_problems(digests, axes, args.allow_inert, aborted)
    # severity == "info" 的不算失败：覆盖有洞、参数确实不影响 —— 都是
    # "值得知道"而不是"出错了"。判定失败仍然是唯一翻盘的东西。
    fatal = any(p.get("severity") != "info" for p in problems)

    rep = {
        "schema": SCHEMA,
        "ok": not fatal,
        "status": "aborted" if aborted else ("pass" if not fatal else "fail"),
        "aborted": aborted,
        "n_combos": len(digests),
        "n_combos_planned": len(combos),
        "handler": "regress.py（判定由 sim.py 完成）",
        "axes": axes,
        "axis_labels": axis_labels,
        "combos": digests,
        "problems": problems,
        "workdir": str(workdir),
        "notes_zh": [
            "参数扫描的每一轮都是独立的 sim.py 判定，本脚本不参与判定。",
            "覆盖有洞不是判定失败：它说明测得不深，不进 status。",
        ],
    }
    if args.full:
        rep["full_results"] = [r["result"] for r in raw]

    rep["next_actions_zh"] = _next_actions(rep)
    _emit(rep, args)
    return EXIT_OK if rep["ok"] else EXIT_FAIL


def _next_actions(rep: dict) -> list[str]:
    out: list[str] = []
    kinds = {p["kind"] for p in rep["problems"]}
    if "aborted" in kinds:
        out.append(rep["aborted"])
    if "param_not_effective" in kinds:
        out.append("参数没生效：在测试台上加一行自报，`$display(\"EFFECTIVE_W=%0d\", W);`，"
                   "再确认测试台例化时把 W 透传了下去。")
    if "sweep_looks_inert" in kinds:
        out.append("某一维扫下来结果完全一样：先查透传，确认无误后加 --allow-inert。")
    if "combo_not_pass" in kinds:
        out.append("有组合判定未通过，逐条看 problems 里带 combo 的那几条的 workdir。")
    if not out:
        out.append("全部通过。若要留档，用 --json build/regress.json 把结果存下来。")
    return out


def _emit(rep: dict, args) -> None:
    if not args.quiet:
        _print_summary(rep)
    text = json.dumps(rep, ensure_ascii=False, indent=2 if args.pretty else None)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
