"""工具能力清单（tools/toolchain.json）与探测逻辑的回归检查。

为什么需要这个文件：

    这类清单驱动的东西有个共同的失效模式——**清单坏了，但程序照常输出
    「全部可用」**。比如清单 JSON 少了个括号、capability 引用了不存在的
    软件，如果不校验，check_tools.py 会开开心心报「无缺口 ✓」，
    而实际上一项都没探测。

    这和 lesson.py 踩过的坑是同一类（见 CLAUDE.md：文件都在、内容空白、不报错）。
    所以这里的断言不看「脚本跑没跑通」，而是看**「坏输入有没有被抓到」**。

每个断言都对应一个具体的失效场景，不是凑数。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from _console import init_console  # noqa: E402

# 下面的结果行里有 ✓ —— GBK 控制台会直接崩，不是显示成乱码。
# 其它 check_*.py 都调了这一句，这里漏过，直到接了 run_smoke 才暴露。
init_console()

import _manifest as M  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def _write(tmp: Path, data) -> Path:
    p = tmp / "m.json"
    p.write_text(
        json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else data,
        encoding="utf-8",
    )
    return p


def _minimal(software=None, capabilities=None) -> dict:
    return {
        "schema": "vlearn.toolchain/1",
        "software": software if software is not None else [],
        "capabilities": capabilities if capabilities is not None else [],
    }


# ---------------------------------------------------------------- 真清单

m = M.load()
problems = M.validate(m)
check(problems == [], f"真清单自洽性校验未通过：{problems}")
check(len(m.capabilities) > 0, "真清单里一条能力都没有")
check(len(m.software) > 0, "真清单里一条软件都没有")

# 每条能力的 requires 都应指向真的软件（validate 已查，这里再独立确认一次数量）
sw_ids = {s.id for s in m.software}
dangling = [(c.id, sid) for c in m.capabilities for sid in c.requires if sid not in sw_ids]
check(dangling == [], f"能力表里有悬空引用：{dangling}")

# L1 必须零缺口。这是有意的课程设计：L1 只依赖 iverilog，不该因为
# 有人往清单里塞了一条 L1 的能力就凭空冒出缺口。
probes = M.probe_all(m)
l1 = M.summarize_for_level(m, "L1", probes)
check(
    l1["capabilities_ok"] == l1["capabilities_total"],
    f"L1 不该有缺口，实测 {l1['capabilities_ok']}/{l1['capabilities_total']}",
    )

# ------------------------------------------------- 真清单：integration 与 gap 不许自相矛盾
#
# 2026-09-29 实测出来的一个**系统性**谎报：有 5 条能力的 `integration` 里写着
# 「仓库有活干，但**卡口是 X 不是代码**：装了才值得写」，而 `gap` 标的是
# `software`。于是软件一装，`evaluate()` 就判它们可用——**而 integration
# 自己承认还没有调用点**。L3 的可用数因此虚高到 11/19（实际 6/19）。
#
# 这条检查是**启发式**：它在 `integration` 的散文里找「还没实现」这类词，
# 不是靠某个机器可读字段。之所以接受这个不精确，是因为 `gap` 已经就是那个
# 字段了——错的是它的**取值**，不是缺一个字段。散文检查放在测试里当护栏，
# 不进 `validate()`（那是给使用者看的硬校验，不该被措辞左右）。
#
# 它查不出什么：换一套说法（「待补」「TODO」）就漏了。**别把它当完备校验**，
# 它只是把已经犯过的那一次钉住。加新能力时若 integration 里提到还没做的接入，
# 就顺手也标 `gap: "software+code"`。
_PENDING_CODE_WORDS = (
    "仓库有活干",
    "本仓库还未实现",
    "本仓库还没实现",
    "尚未实现",
    "还没写",
)
_contradictions = []
for _c in m.capabilities:
    _it = _c.integration or ""
    if any(w in _it for w in _PENDING_CODE_WORDS) and not _c.needs_code:
        _contradictions.append(f"{_c.id}（gap={_c.gap}）")
check(
    _contradictions == [],
    "integration 里承认「本仓库还没写接入代码」，gap 却没有 code —— "
    f"软件一装就会被报成可用：{_contradictions}",
    )

# 反向也确认一次：标了 code 的，integration 得说得出缺什么，不能空着糊弄
_vague = [c.id for c in m.capabilities if c.needs_code and len(c.integration or "") < 20]
check(_vague == [], f"标了缺代码，integration 却没说缺什么：{_vague}")

# ---------------------------------------------------------------- 负例 1：坏 JSON
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    bad = _write(tmp, "{ 这不是合法 JSON")
    try:
        M.load(bad)
        check(False, "坏 JSON 没有报错——会静默降级成空清单并谎报全部可用")
    except M.ManifestError:
        check(True, "")
    except Exception as exc:  # noqa: BLE001
        check(False, f"坏 JSON 抛了预期外的异常类型：{type(exc).__name__}")

    # ------------------------------------------------------------ 负例 2：清单文件不存在
    try:
        M.load(tmp / "does_not_exist.json")
        check(False, "清单文件不存在时没有报错")
    except M.ManifestError:
        check(True, "")
    except Exception as exc:  # noqa: BLE001
        check(False, f"清单缺失时抛了预期外的异常：{type(exc).__name__}")

    # ------------------------------------------------------------ 负例 3：悬空引用
    mm = M.load(
        _write(
            tmp,
            _minimal(
                software=[{"id": "real", "name": "R", "windows": "native"}],
                capabilities=[
                    {
                        "id": "cap.a",
                        "name": "A",
                        "levels": ["L1"],
                        "requires": ["typo_software_id"],
                        "impact": "blocks",
                        "gap": "software",
                    }
                ],
            ),
        )
    )
    probs = M.validate(mm)
    check(
        any("typo_software_id" in p for p in probs),
        "悬空 requires 没有被 validate 抓到——会导致「装什么能补」永远列不出来",
    )

    # ------------------------------------------------------------ 负例 4：未知 impact
    mm = M.load(
        _write(
            tmp,
            _minimal(
                capabilities=[
                    {
                        "id": "cap.b",
                        "name": "B",
                        "levels": ["L1"],
                        "requires": [],
                        "impact": "catastrophic",  # 不在 IMPACT_ORDER 里
                        "gap": "none",
                    }
                ]
            ),
        )
    )
    probs = M.validate(mm)
    check(any("catastrophic" in p for p in probs), "未知的 impact 值没有被 validate 抓到")

    # ------------------------------------------------------------ 负例 5：progavides 悬空
    mm = M.load(
        _write(
            tmp,
            _minimal(
                software=[
                    {
                        "id": "s1",
                        "name": "S",
                        "windows": "native",
                        "provides": ["cap.nonexistent"],
                    }
                ],
                capabilities=[],
            ),
        )
    )
    probs = M.validate(mm)
    check(
        any("cap.nonexistent" in p for p in probs),
        "software.provides 指向不存在的 capability 没有被抓到（本次开发中就真的踩到这个）",
    )

    # -------------------------------------------------- 负例 5b：provides 与 requires 对不上
    #
    # 病根（2026-09-29 在真清单上抓到的）：slang.provides 说它提供 lint.full，
    # 而 lint.full.requires 是 ['verilator']。两个方向**各自**看着都成立，
    # 合起来是一句假话。真清单上这一类一共有 5 条（yosys 认领 sby 的两条、
    # openroad 认领 opensta 的三条，详见各条 note）。
    #
    # 这里三个子例分别锁住：单向多出来、反向漏写、以及合法情形不许误报。
    _prov_case = lambda prov, reqs: M.load(
        _write(
            tmp,
            _minimal(
                software=[
                    {"id": "a", "name": "A", "windows": "native", "provides": prov},
                    {"id": "b", "name": "B", "windows": "native", "provides": []},
                ],
                capabilities=[
                    {"id": "cap.x", "name": "X", "levels": ["L1"], "requires": reqs,
                     "impact": "blocks", "gap": "software"},
                ],
            ),
        )
    )

    # ① a 认领了 cap.x，但 cap.x 依赖的是 b → 必须报
    probs = M.validate(_prov_case(["cap.x"], ["b"]))
    check(any("两边对不上" in p for p in probs),
          f"provides 认领了不属于它的能力，没被抓到——使用者会以为这条能力有两个来源：{probs}")

    # ② cap.x 依赖 b，但 b.provides 是空的 → 同样必须报（漏写方向）
    probs = M.validate(_prov_case([], ["b"]))
    check(any("provides 里没写这条" in p for p in probs),
          f"requires 依赖的软件没写进 provides，没被抓到：{probs}")

    # ③ 正向对照：两边写齐了就不许报（否则上面两条可能是「validate 见谁都报」）
    probs = M.validate(_prov_case(["cap.x"], ["a"]))
    check(probs == [], f"两边对得上的清单被误报了：{probs}")

# ---------------------------------------------------------------- 负例 6：探测失败不能被当成成功
fake = M.Software(
    id="definitely-not-installed",
    name="不可能存在的工具",
    summary="",
    windows="native",
    raw={"check": {"exe": "zznonexistenttool_9f3a", "version_arg": "-V"}},
    check_exe="zznonexistenttool_9f3a",
    version_arg="-V",
)
pr = M.probe(fake)
check(pr.found is False, "探测一个不存在的工具却报告 found=True")
check(pr.exe_path is None, "探测失败时不该有 exe_path")

# ---------------------------------------------------------------- 负例 7（最关键）
# 依赖缺失时，能力**必须**被判为未满足。
# 这是整个 check 脚本存在的意义：如果这里错了，工具链缺一半也会报「无缺口 ✓」。
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    mm = M.load(
        _write(
            tmp,
            _minimal(
                software=[
                    {
                        "id": "ghost",
                        "name": "幽灵工具",
                        "windows": "native",
                        "check": {"exe": "zznonexistenttool_9f3a"},
                        # provides 必须写全：validate 现在双向查，
                        # 漏写会被当成清单错误（这正是负例 5b 锁的那件事）
                        "provides": ["cap.needs_ghost"],
                    }
                ],
                capabilities=[
                    {
                        "id": "cap.needs_ghost",
                        "name": "需要幽灵工具的能力",
                        "levels": ["L2"],
                        "requires": ["ghost"],
                        "impact": "blocks",
                        "gap": "software",
                        "fallback": "没法降级",
                    }
                ],
            ),
        )
    )
    probs = M.validate(mm)
    check(probs == [], f"这份合成清单本该是自洽的，却报：{probs}")

    p = M.probe_all(mm)
    check(p["ghost"].found is False, "幽灵工具被探测成已安装")

    st = M.evaluate(mm, p)
    check(len(st) == 1 and st[0].ok is False, "依赖缺失的能力被判成了 ok=True")
    check(
        st[0].missing_software == ["ghost"],
        f"缺失软件列表不对：{st[0].missing_software}",
    )
    check(st[0].actionable is True, "gap=software 的缺失项应该标为「装软件可解决」")

    summary = M.summarize_for_level(mm, "L2", p)
    check(
        summary["capabilities_ok"] == 0 and summary["capabilities_total"] == 1,
        "summarize_for_level 把缺失的能力算成了可用",
    )
    check(summary["blocking"] == ["cap.needs_ghost"], "blocking 列表没列出这项")

    # ------------------------------------------------------------ 负例 8：代码缺口
    # 缺的是代码（gap=code）时，不该提示「装某个软件」，也不该算 actionable。
    mm2 = M.load(
        _write(
            tmp,
            _minimal(
                capabilities=[
                    {
                        "id": "cap.needs_code",
                        "name": "缺代码的能力",
                        "levels": ["L2"],
                        "requires": [],  # 不依赖任何软件
                        "impact": "shrinks",
                        "gap": "code",
                    }
                ]
            ),
        )
    )
    p2 = M.probe_all(mm2)
    st2 = M.evaluate(mm2, p2)
    check(len(st2) == 1 and st2[0].ok is False, "gap=code 且未实现的能力应判为未满足")
    check(st2[0].code_gap is True, "gap=code 没有被标成代码缺口")
    check(
        st2[0].actionable is False,
        "代码缺口被误标为「装软件可解决」——会让使用者去装一个没用的工具",
    )
    s2 = M.summarize_for_level(mm2, "L2", p2)
    check(s2["missing_software"] == [], "代码缺口不该产生「需要装什么」的建议")

    # ------------------------------------------ 负例 8a：软件和代码**两边都缺**
    #
    # 病根（2026-09-29 在真清单上抓到，就是 cov.code 那条）：`gap` 原先只有
    # software / code 两种取值，而这种「装了软件还得补代码」的情况只能二选一，
    # **两个选项都会骗人**：
    #   写 software → mingw 一装 evaluate() 就判本项可用，而 integration 里
    #                 明写着「仓库有活干」；
    #   写 code     → 又说成「装什么都没用」，可软件确实是卡口之一。
    #
    # 最要命的是这个谎**延迟到装上软件那一刻才发作**——在那之前报告看着一切正常。
    # 所以下面同时验「装之前」和「装之后」两个状态，只看一个会漏掉它。
    mm3 = M.load(
        _write(
            tmp,
            _minimal(
                software=[
                    {
                        "id": "ghost2",
                        "name": "又不存在的工具",
                        "windows": "native",
                        "check": {"exe": "zznonexistenttool_9f3a"},
                        "provides": ["cap.both"],
                    }
                ],
                capabilities=[
                    {
                        "id": "cap.both",
                        "name": "软件和代码都缺的能力",
                        "levels": ["L2"],
                        "requires": ["ghost2"],
                        "impact": "degrades",
                        "gap": "software+code",
                    }
                ],
            ),
        )
    )
    # ① 软件没装：两边都缺，且**不许**标成「装软件就能解决」
    st3 = M.evaluate(mm3, M.probe_all(mm3))
    check(st3[0].ok is False, "software+code 的能力被判成可用")
    check(st3[0].both_gaps is True, "两边都缺的没被标成 both_gaps")
    check(
        st3[0].actionable is False,
        "两边都缺的被标成「装软件可解决」——装完仍不齐，等于让人白跑一趟",
    )
    # ② ★ 把软件「装上」之后，仍然**不许**变成可用。
    #    这一条才是本负例存在的理由：只在装之前断言，就算把 evaluate()
    #    写成 `c.gap != "code"`（漏掉 software+code）也照样能过。
    st3b = M.evaluate(mm3, {"ghost2": M.ProbeResult("ghost2", True, usable=True)})
    check(
        st3b[0].ok is False,
        "软件装上之后，缺代码的 fact 被忘了——这正是「一边报可用、一边说不可用」的现场",
    )
    check(st3b[0].code_gap is True, "装完软件后没剩代码缺口，说明代码那一半被丢了")
    check(
        st3b[0].software_gap is False,
        "软件明明装上了，software_gap 还是 True——两个状态没分开",
    )
    # both_gaps 必须是「两位都还在」，不能是 code_gap 的别名。
    # 装完软件这一刻正是两者分岔的地方：code_gap 仍为 True，both_gaps 该变 False。
    # 少了这一条，把 both_gaps 写成 `return self.code_gap` 也照样能过，
    # 于是「装完软件只剩代码缺口」会被说成「两边都缺」，让人以为还得再装点什么。
    check(
        st3b[0].both_gaps is False,
        "软件已经装上了，却还说「两个都缺」——使用者会去找一个已经装好的软件",
    )

    # ③ 正向对照：纯 software 的能力装上就该可用，否则上面②可能是「怎么都不 ok」
    mm4 = M.load(
        _write(
            tmp,
            _minimal(
                software=[
                    {"id": "ghost3", "name": "第三个幽灵", "windows": "native",
                     "check": {"exe": "zznonexistenttool_9f3a"}, "provides": ["cap.only_sw"]}
                ],
                capabilities=[
                    {"id": "cap.only_sw", "name": "只缺软件", "levels": ["L2"],
                     "requires": ["ghost3"], "impact": "shrinks", "gap": "software"}
                ],
            ),
        )
    )
    check(
        M.evaluate(mm4, {"ghost3": M.ProbeResult("ghost3", True, usable=True)})[0].ok is True,
        "只缺软件的能力，装上之后却判成不可用——负例②的对照失效",
    )

    # ④ gap 写了「缺软件」却列不出软件，是句落不了地的话，validate 必须报
    mm5 = M.load(
        _write(
            tmp,
            _minimal(
                capabilities=[
                    {"id": "cap.no_sw", "name": "说了缺软件却没列", "levels": ["L2"],
                     "requires": [], "impact": "shrinks", "gap": "software"}
                ],
            ),
        )
    )
    check(
        any("落不了地" in p for p in M.validate(mm5)),
        "gap=software 但 requires 为空，validate 没抓到——报告上会是一句没用的建议",
    )

    # ------------------------------------------------------------ 负例 8b：WSL-only 的软件
    # 缺的软件在 Windows 原生装不了时，**不能**和普通缺失混在一句
    # 「装上这些可补」里——使用者照着单子装会发现装不上。
    # 这类要单列，并且不算 actionable。
    mm3 = M.load(
        _write(
            tmp,
            _minimal(
                software=[
                    {
                        "id": "wslonly",
                        "name": "只能 WSL 的工具",
                        "windows": "wsl-only",
                        "check": {"exe": "zznonexistenttool_9f3a"},
                    },
                    {
                        "id": "native",
                        "name": "原生工具",
                        "windows": "native",
                        "check": {"exe": "zznonexistenttool_9f3a"},
                    },
                ],
                capabilities=[
                    {
                        "id": "cap.wsl",
                        "name": "只能靠 WSL 工具的能力",
                        "levels": ["L3"],
                        "requires": ["wslonly"],
                        "impact": "talk_only",
                        "gap": "software",
                    },
                    {
                        "id": "cap.mixed",
                        "name": "两种依赖都缺的能力",
                        "levels": ["L3"],
                        "requires": ["wslonly", "native"],
                        "impact": "shrinks",
                        "gap": "software",
                    },
                ],
            ),
        )
    )
    p3 = M.probe_all(mm3)
    st3 = {s.cap.id: s for s in M.evaluate(mm3, p3)}

    check(
        st3["cap.wsl"].unavailable_software == ["wslonly"],
        f"WSL-only 依赖没被单列：{st3['cap.wsl'].unavailable_software}",
    )
    check(
        st3["cap.wsl"].actionable is False,
        "只剩 WSL-only 依赖的能力被标成「装软件可解决」——会让人去装一个装不上的东西",
    )
    check(
        st3["cap.wsl"].installable_missing == [],
        f"可装清单里混进了 WSL-only：{st3['cap.wsl'].installable_missing}",
    )
    # 混合依赖：还有一个原生装得上的，那就仍然 actionable——不能一刀切
    check(
        st3["cap.mixed"].actionable is True,
        "混合依赖里明明有原生可装的，却被判成不可解决",
    )
    check(
        st3["cap.mixed"].installable_missing == ["native"],
        f"混合依赖的可装清单不对：{st3['cap.mixed'].installable_missing}",
    )

    s3 = M.summarize_for_level(mm3, "L3", p3)
    check(
        [x["id"] for x in s3["missing_software"]] == ["native"],
        f"「装上这些可补」里混进了装不上的：{s3['missing_software']}",
    )
    check(
        [x["id"] for x in s3["unavailable_software"]] == ["wslonly"],
        f"装不上的那堆没被单列：{s3['unavailable_software']}",
    )
    # setup_tools.py --for-level 就是取 missing_software 去装的，
    # 里面混进 WSL-only 的话它会白下一场再失败
    check(
        all(x["windows"] in ("native", "pip") for x in s3["missing_software"]),
        "可装清单里出现了本平台装不了的软件——--for-level 会据此发起下载",
    )

# ---------------------------------------------------------------- 负例 8c：找到了文件 ≠ 能用
#
# 这是**真实踩到**的坑，也是 `check.functional` 存在的全部理由：
# OSS CAD Suite 的 yosys.exe 文件是在的、`--version` 探测也会显示"找到"，
# 但它依赖的整套 mingw 运行时一个都没随组件下发，进程在 main 之前就被系统
# 干掉了（STATUS_DLL_NOT_FOUND，退出码 0xC0000135，**一个字节输出都没有**）。
# 只看 found 的话，报告会堂堂正正打一个 ✓，而它一项能力都补不上。
#
# 这里不依赖机器上装没装 yosys，而是拿**一定存在**的解释器当样本：
# 版本号能打出来（found/version 都正常），但自检命令故意返回非零。
_py = Path(sys.executable)  # 正在跑本脚本的那个解释器，必然存在
_sw = M.Software(
    id="runs-but-fails",
    name="版本号能打但一跑就挂",
    summary="",
    windows="native",
    raw={
        "check": {
            "exe": _py.stem,
            "version_arg": "--version",
            "lib_dirs": ["lib"],
            "functional": {"args": ["-c", "import sys; sys.exit(3)"]},
        }
    },
    check_exe=_py.stem,
    version_arg="--version",
)
pr_bad = M.probe(_sw, extra_path=[_py.parent])
check(pr_bad.found is True, "样本解释器没被找到，这条负例本身失效了")
check(pr_bad.version, "版本号本该能正常解析出来（这正是「只看版本号」会误判的原因）")
check(pr_bad.usable is False, "功能自检失败却被判成 usable=True——会谎报「已安装 ✓」")
check(pr_bad.detail != "", "判了不可用却不给理由，使用者无从下手")
check("退出码" in pr_bad.detail, f"理由里没说清是自检失败：{pr_bad.detail}")

# 关键断言：**不可用的工具不能满足能力依赖**。
# 少了这一条，上面那些 checked 只是摆设——报告照样会把能力算成已补齐。
_p_route = {"runs-but-fails": pr_bad}
# 这段单开一个临时目录：上面那个 `with tempfile.TemporaryDirectory()` 块
# 已经退出、目录已被删掉，顺手复用它里面的 `tmp` 会直接 FileNotFoundError。
with tempfile.TemporaryDirectory() as td2:
    _mm_bad = M.load(
        _write(
            Path(td2),
            _minimal(
                software=[
                    {
                        "id": "runs-but-fails",
                        "name": "版本号能打但一跑就挂",
                        "windows": "native",
                        "check": {"exe": _py.stem},
                    }
                ],
                capabilities=[
                    {
                        "id": "cap.needs_working_tool",
                        "name": "需要一个真能跑的工具",
                        "levels": ["L2"],
                        "requires": ["runs-but-fails"],
                        "impact": "blocks",
                        "gap": "software",
                    }
                ],
            ),
        )
    )
_st_bad = M.evaluate(_mm_bad, _p_route)
check(
    len(_st_bad) == 1 and _st_bad[0].ok is False,
    "工具明明跑不起来，依赖它的能力却被判成 ok=True——这正是本仓库最怕的谎报",
)
check(
    _st_bad[0].missing_software == ["runs-but-fails"],
    f"跑不起来的工具没进缺失列表：{_st_bad[0].missing_software}",
)

# 正向对照：同一份清单、同一个工具，自检命令正常时能力必须判为可用。
# 没有这条，上面的断言可能只是"什么都判不可用"造成的假通过。
_sw_ok = M.Software(
    id="runs-but-fails",
    name="同一工具的自检通过版",
    summary="",
    windows="native",
    raw={"check": {"exe": _py.stem, "functional": {"args": ["-c", "pass"]}}},
    check_exe=_py.stem,
)
pr_ok = M.probe(_sw_ok, extra_path=[_py.parent])
check(pr_ok.found is True and pr_ok.usable is True, "自检通过的工具被误判成不可用")
_st_ok = M.evaluate(_mm_bad, {"runs-but-fails": pr_ok})
check(
    len(_st_ok) == 1 and _st_ok[0].ok is True,
    "自检通过的工具没能满足能力依赖——那上面那条断言就是假通过",
)

# 清单里没写 functional 的工具不能被连累：默认应是可用。
pr_none = M.probe(
    M.Software(
        id="no-func",
        name="没写自检的工具",
        summary="",
        windows="native",
        raw={"check": {"exe": _py.stem}},
        check_exe=_py.stem,
    ),
    extra_path=[_py.parent],
)
check(pr_none.usable is True, "没配自检的工具被默认判成了不可用——会凭空造出缺口")

# ---------------------------------------------------------------- 负例 8d：lib_dirs 必须并入 PATH，不能覆盖
#
# 工具的运行环境变量是 `{**os.environ, **tool_env(sw)}` —— tool_env 的返回值
# 会**盖掉**同名环境变量。所以 PATH 这一项一旦写成"只有 lib 目录"，
# 子进程就再也找不到系统命令了，而这一点不报错、不崩溃，只是行为诡异。
# verilator 之外的工具（yosys 这类要 lib/*.dll 的）都靠这条。
_sw_lib = M.Software(
    id="needs-lib",
    name="要共享库目录的工具",
    summary="",
    windows="native",
    raw={
        "check": {"exe": "x", "lib_dirs": ["lib"]},
        "install": {"dest": ".tools/something"},
    },
    check_exe="x",
)
_env = M.tool_env(_sw_lib)
check("PATH" in _env, "lib_dirs 没有产生 PATH")
_parts = _env["PATH"].split(__import__("os").pathsep)
check(
    _parts[0].replace("\\", "/").endswith(".tools/something/lib"),
    f"lib 目录没有被前置：{_parts[0]}",
)
check(
    len(_parts) > 1,
    "lib_dirs 把整个 PATH 覆盖掉了——子进程会找不到任何系统命令，且不报错",
)

# 清单里没写 lib_dirs 的工具不该被动到 PATH
_sw_nolib = M.Software(
    id="no-lib",
    name="不需要 lib 的工具",
    summary="",
    windows="native",
    raw={"check": {"exe": "x"}},
    check_exe="x",
)
check("PATH" not in M.tool_env(_sw_nolib), "没配 lib_dirs 却凭空改了 PATH")

# ---------------------------------------------------------------- 负例 8e：repo_private 不许回退到系统 PATH
#
# 复用系统上早装过的工具通常是好事，但**结果取决于版本的工具不能复用**：
# 系统 PATH 上那个 mingw 7.3 会让 verilator 编出一串查不出原因的错，
# 而报告只会写「已安装但不可用」——使用者照着去查 PATH 也找不到原因。
# 所以清单里能标 repo_private，探测就只认 .tools/。
#
# 这里拿**一定存在**的解释器当样本（它就在系统 PATH 上），
# 检验"标了私有就找不到、不标就能找到"。少了后半句，前半句可能是
# 因为 exe 名字压根不存在而假通过的。
_py_priv = M.Software(
    id="must-be-private",
    name="只认私有安装的工具",
    summary="",
    windows="native",
    raw={
        "check": {"exe": _py.stem},
        "install": {"dest": ".tools/must-be-private"},
        "repo_private": True,
    },
    check_exe=_py.stem,
    version_arg="--version",
    repo_private=True,
)
pr_priv = M.probe(_py_priv, extra_path=[_py.parent])
check(
    pr_priv.found is False,
    "标了 repo_private 的工具仍然从系统 PATH 上找到了——私有化策略形同虚设",
)
check(
    "私有" in pr_priv.detail or "repo_private" in pr_priv.detail,
    f"没找到但不说清是因为「只认私有安装」，使用者会去翻 PATH：{pr_priv.detail}",
)
check(M.locate(_py_priv) is None, "locate() 绕过了 repo_private，仍会返回系统 PATH 上的那份")

# 正向对照：把 repo_private 去掉，同一个工具、同一个 extra_path 就该找得到。
# 没有这条，上面"没找到"可能只是 exe 名字压根解析错了而假通过。
_py_pub = M.Software(
    id="may-be-public",
    name="可复用系统安装的工具",
    summary="",
    windows="native",
    raw={"check": {"exe": _py.stem}},
    check_exe=_py.stem,
    version_arg="--version",
)
pr_pub = M.probe(_py_pub, extra_path=[_py.parent])
check(
    pr_pub.found is True,
    "去掉 repo_private 后本该找到——那上面那条断言就是假通过",
)

# 再验一次**真的系统 PATH 分支**（不是 extra_path 模拟的那条）：复用系统安装
# 必须在 detail 里标出来，否则读报告的人分不清这工具是仓库装的还是系统上蹭的。
if shutil.which(_py.stem):
    pr_dir = M.probe(_py_pub)
    check(pr_dir.found is True, "解释器就在 PATH 上却没能复用")
    check(
        "系统 PATH" in pr_dir.detail,
        f"从 PATH 复用却没标注出来，报告里会看不出工具是从哪来的：{pr_dir.detail}",
    )
else:
    print(f"  ⊘ 系统 PATH 上没有 {_py.stem}，跳过「PATH 复用要标注来源」这一条")

# repo_private 的意义是"只认 .tools/ 里的那份"，所以必须写 install.dest ——
# 没写就等于在任何机器上都判未安装，且装也装不进来，只会永远缺着。
with tempfile.TemporaryDirectory() as td:
    _mm_priv = M.load(
        _write(
            Path(td),
            _minimal(
                software=[
                    {
                        "id": "priv-no-dest",
                        "name": "标了私有却没写安装位置",
                        "windows": "native",
                        "repo_private": True,
                        "check": {"exe": "x"},
                    }
                ]
            ),
        )
    )
    _probs = M.validate(_mm_priv)
    check(
        any("repo_private" in p for p in _probs),
        "repo_private 没配 install.dest 没有被 validate 抓到——它会永远缺着且装不进来",
    )

# ---------------------------------------------------------------- 负例 9：解压路径穿越
#
# 下载来的归档必须当**不可信输入**处理。成员名里带 ../ 的归档，
# 直接 extractall 会写到目标目录之外（zip slip / tar slip）。
# 这里构造两个真的带越界成员的归档，确认拦住了。
import io  # noqa: E402
import tarfile  # noqa: E402
import zipfile  # noqa: E402

import setup_tools as ST  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    out = tmp / "out"

    # tar：成员名 ../evil.txt
    evil_tgz = tmp / "evil.tgz"
    with tarfile.open(evil_tgz, "w:gz") as tf:
        info = tarfile.TarInfo("../evil.txt")
        payload = b"pwned"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    try:
        ST.extract_tgz(evil_tgz, out, verbose=False)
        check(False, "带 ../ 成员的 tar 被解压了——路径穿越没有拦住")
    except RuntimeError as exc:
        check("越界" in str(exc), f"拦住了但报错信息不明确：{exc}")
    except Exception as exc:  # noqa: BLE001
        check(False, f"tar 路径穿越抛了预期外的异常：{type(exc).__name__}: {exc}")
    check(not (tmp / "evil.txt").exists(), "越界文件真的被写出来了")

    # zip：成员名 ../evil2.txt
    evil_zip = tmp / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../evil2.txt", "pwned")
    try:
        ST.extract_zip(evil_zip, out, verbose=False)
        check(False, "带 ../ 成员的 zip 被解压了——路径穿越没有拦住")
    except RuntimeError:
        check(True, "")
    except Exception as exc:  # noqa: BLE001
        check(False, f"zip 路径穿越抛了预期外的异常：{type(exc).__name__}: {exc}")
    check(not (tmp / "evil2.txt").exists(), "越界文件真的被写出来了")

    # 正向对照：正常归档必须能解压，否则上面两条可能是"什么都拦"造成的假通过
    good_tgz = tmp / "good.tgz"
    with tarfile.open(good_tgz, "w:gz") as tf:
        info = tarfile.TarInfo("bin/hello.txt")
        payload = b"ok"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    ST.extract_tgz(good_tgz, out, verbose=False)
    check((out / "bin" / "hello.txt").is_file(), "正常归档反而没解压出来")

# ---------------------------------------------------------------- 负例 9b：包裹层剥离
#
# 这是**真实踩到**的坑：OSS CAD Suite 的组件 tgz 内部结构是
#     _outputs/windows-x64/yosys/yosyshq/bin/yosys.exe
# 而探测按 `dest/bin/xxx.exe` 去找。第一版只给 zip 剥一层、tgz 完全不剥，
# 结果是「解压完成但探测不到可执行文件」——文件都在，只是路径深了三层。
#
# 这里用合成归档复现这个结构，并且加一条**反向**断言：
# 本来就有多个顶层目录的归档**不能**被剥，否则会把正常包的目录结构搞坏。
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    def _tgz(path: Path, names: list[str]) -> Path:
        """按真实 tar 的样子造归档：以 / 结尾的当目录条目。

        不能让目录名的条目带内容——那会变成一个**同名文件**，
        后面往里写子文件时直接 FileNotFoundError（这里第一版就这么写的）。
        """
        with tarfile.open(path, "w:gz") as tf:
            for n in names:
                if n.endswith("/"):
                    info = tarfile.TarInfo(n.rstrip("/"))
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.size = 0
                    tf.addfile(info)
                else:
                    info = tarfile.TarInfo(n)
                    payload = b"x"
                    info.size = len(payload)
                    tf.addfile(info, io.BytesIO(payload))
        return path

    # 三层包裹（照抄 OSS CAD Suite 的实际结构）
    nested = _tgz(
        tmp / "nested.tgz",
        [
            "_outputs/windows-x64/yosys/",
            "_outputs/windows-x64/yosys/.hash",
            "_outputs/windows-x64/yosys/yosyshq/bin/yosys.exe",
            "_outputs/windows-x64/yosys/yosyshq/share/data.txt",
        ],
    )
    d1 = tmp / "d1"
    ST.extract_tgz(nested, d1, verbose=False)
    stripped = ST._flatten_single_dir(d1, verbose=False)
    check(
        (d1 / "bin" / "yosys.exe").is_file(),
        f"三层包裹层没剥干净（剥了 {stripped} 层），探测会落空："
        f"{sorted(p.name for p in d1.iterdir())}",
    )
    check(
        (d1 / "share" / "data.txt").is_file(),
        "剥包裹层时丢了内容文件",
    )
    # _outputs → windows-x64 → yosys → yosyshq，共 4 层；
    # 然后看到 bin/ share/ 两个内容目录，停手
    check(stripped == 4, f"应该剥 4 层，实际剥了 {stripped} 层")

    # 本来就是多目录的归档：一层都不能剥
    flat = _tgz(
        tmp / "flat.tgz",
        ["bin/tool.exe", "lib/plugin.dll"],
    )
    d2 = tmp / "d2"
    ST.extract_tgz(flat, d2, verbose=False)
    stripped2 = ST._flatten_single_dir(d2, verbose=False)
    check(stripped2 == 0, f"多目录的归档被剥了 {stripped2} 层——会把正常结构搞坏")
    check(
        (d2 / "bin" / "tool.exe").is_file() and (d2 / "lib" / "plugin.dll").is_file(),
        "多目录归档的内容被动过了",
    )

    # 单层包裹（slang / verible 那种），仍然要剥
    one = _tgz(tmp / "one.tgz", ["slang-11.0/bin/slang.exe"])
    d3 = tmp / "d3"
    ST.extract_tgz(one, d3, verbose=False)
    check(ST._flatten_single_dir(d3, verbose=False) == 1, "单层包裹没有剥掉")
    check((d3 / "bin" / "slang.exe").is_file(), "单层包裹剥完路径不对")

    # 剥到只剩一个目录、但那个目录里还有多个条目时必须停手
    # （否则会把 bin/lib 当成包裹层继续吃掉）
    deep = _tgz(tmp / "deep.tgz", ["wrap/bin/a.exe", "wrap/lib/b.dll"])
    d4 = tmp / "d4"
    ST.extract_tgz(deep, d4, verbose=False)
    check(ST._flatten_single_dir(d4, verbose=False) == 1, "wrap 这一层应该剥掉")
    check(
        (d4 / "bin" / "a.exe").is_file(),
        "剥过头了——bin/ 被当成包裹层吃掉了",
    )

# ---------------------------------------------------------------- 负例 9c：下载被截断
#
# 2026-09-29 实测栽的坑，而且是**静默**的那种：572 MB 的 oss-cad-suite 下到
# 540 MB 被掐断，.part 照样改名成正式文件、退出码 0，少 32 MB 谁也不知道。
# 病根在 http.client 里 —— 带长度参数的 read(amt) 遇到连接提前关闭时**返回空串**，
# 不抛 IncompleteRead（源码那段注释自己写着 "Ideally, we would raise
# IncompleteRead ... but it might break compatibility"）。于是"读完了"和
# "读断了"在调用方眼里一模一样。原有的 gzip 魔数检查拦不住：魔数在前两字节。
#
# 这里起一个真会短发的本地 HTTP 服务来复现，而不是单元测试某个私有函数 ——
# 要验的恰恰是"读空串时到底有没有比对长度"这条端到端的判断。
import contextlib  # noqa: E402
import http.server  # noqa: E402
import threading  # noqa: E402

_FULL = b"Y" * 100_000


class _TruncServer(http.server.BaseHTTPRequestHandler):
    """按路径决定怎么发：写全 / 声明了长度却少发 / 无视 Range 返回 200。"""

    def log_message(self, *a):  # 别把访问日志糊到测试输出里
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/trunc":
            self.send_response(200)
            self.send_header("Content-Length", "1000000")  # 声明 1 MB
            self.end_headers()
            self.wfile.write(b"X" * 3000)  # 只给 3 KB 就断
            self.close_connection = True
        elif self.path == "/fine":
            self.send_response(200)
            self.send_header("Content-Length", str(len(_FULL)))
            self.end_headers()
            self.wfile.write(_FULL)
        elif self.path == "/ignore-range":
            # 客户端带 Range 来续传，服务端**无视它**，按 200 发整份。
            # 这时 Content-Length 是整份长度，不能再加 resume_from，
            # 否则读满了也会被判成截断。
            self.send_response(200)
            self.send_header("Content-Length", str(len(_FULL)))
            self.end_headers()
            self.wfile.write(_FULL)
        else:
            self.send_error(404)


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TruncServer)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        base = f"http://127.0.0.1:{port}"
        quiet = contextlib.redirect_stdout(io.StringIO())

        # ① 短发必须抛错，且 .part 要留着（留着才能续传）
        p1 = tmp / "a.tgz.part"
        with quiet:
            try:
                ST._download_once(f"{base}/trunc", p1)
                check(False, "下载被截断却当成成功了——长度根本没比对")
            except ST.TruncatedDownload as exc:
                check("3000" in str(exc) and "1000000" in str(exc),
                      f"抛了截断但没说清差多少：{exc}")
        check(p1.is_file() and p1.stat().st_size == 3000, ".part 没保住，续传就断了")
        check(issubclass(ST.TruncatedDownload, OSError),
              "截断异常不是 OSError，download() 的重试分支接不住，会直接崩")

        # ② 正向对照：发全了就不能报截断，否则上面那条是"什么都判坏"的假通过
        p2 = tmp / "b.tgz.part"
        with quiet:
            ST._download_once(f"{base}/fine", p2)
        check(p2.stat().st_size == len(_FULL), "正常下载的文件大小不对")

        # ③ 回归护栏：服务端无视 Range 返回 200 时，不能拿 resume_from 去加长度
        p3 = tmp / "c.tgz.part"
        p3.write_bytes(b"Z" * 500)  # 假装已经续了 500 字节
        with quiet:
            try:
                ST._download_once(f"{base}/ignore-range", p3)
            except ST.TruncatedDownload as exc:
                check(False, f"服务端无视 Range 反而被判成截断：{exc}")
        check(p3.stat().st_size == len(_FULL),
              f"服务端无视 Range 时应该从头写，实际 {p3.stat().st_size} 字节")
    finally:
        srv.shutdown()
        srv.server_close()

    # ④ 缓存命中也得复核体积：> 1 MB 只说明"不像错误页面"，不说明是完整的
    big = tmp / "cached.tgz"
    big.write_bytes(b"\x1f\x8b" + b"\x00" * (2 * 1024 * 1024))  # 2 MB，像个包
    check(ST._size_shortfall(big, {"size_mb": 572.2}) is not None,
          "缓存里 2 MB 的包被当成完整的 572 MB —— 截断文件会被跳过不重下")
    check(ST._size_shortfall(big, {"size_mb": 2.0}) is None,
          "体积对得上却报缺斤少两")
    check(ST._size_shortfall(big, {}) is None,
          "清单没写 size_mb 时不该瞎猜")

# ---------------------------------------------------------------- 负例 9d：同一归档不许装两遍
#
# 2026-09-29 把 verilator / oss-utils / oss-system-resources 也改指整包之后，
# 清单里有**四项指向同一个 572 MB 归档**（都往 .tools/oss 里放东西）。
# do_install 照着 id 逐个装，就会把同一份 16953 条目的 tgz 解压四遍、
# 往磁盘写四遍 2 GB —— 不报错，只是白干，最难被发现的那种浪费。
#
# 这里把 install_one 换成记账用的桩，直接验"按 (repo,tag,asset) 判重"这条。
# 用桩而不是真装：要验的是**调度**，真装一次要解压 2 GB，那是另一回事。
_shared_src = {
    "type": "github-release-asset",
    "repo": "YosysHQ/oss-cad-suite-build",
    "tag": "2026-09-28",
    "asset": "oss-cad-suite-windows-x64-20260928.tgz",
    "size_mb": 572.2,
}


def _stub_sw(sid: str, asset_src: dict) -> dict:
    return {
        "id": sid, "name": sid, "summary": "", "windows": "native",
        "source": asset_src, "install": {"method": "extract-tgz", "dest": ".tools/oss"},
        "provides": [], "check": {"exe": None},
    }


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    man = M.load(_write(tmp, _minimal(software=[
        _stub_sw("a_suite", _shared_src),
        _stub_sw("b_suite", dict(_shared_src)),                 # 同一份归档
        _stub_sw("c_other", dict(_shared_src, asset="other.tgz", size_mb=7.5)),  # 换归档
    ])))

    calls: list[str] = []
    real_install_one = ST.install_one
    # 签名必须跟 install_one(sw, verbose, lock, force) 对齐——
    # 少一个参数就会 TypeError，而这是 stub 的错、不是被测代码的错
    ST.install_one = lambda sw, verbose, lock, force: (calls.append(sw.id), (True, "ok"))[1]
    try:
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = ST.do_install(man, ["a_suite", "b_suite", "c_other"], verbose=False, lock=False)
        out = buf.getvalue()
    finally:
        ST.install_one = real_install_one

    check(calls == ["a_suite", "c_other"],
          f"同一归档被装了不止一次：实际调用了 {calls}")
    check("是同一个归档" in out and "b_suite" in out,
          "跳过时没说明白是跟谁重复，使用者会以为它自己装好了")
    check(rc == ST.EXIT_OK, f"跳过导致退出码变红：{rc}")
    check(out.count("ok") == 2, f"跳过的那项被算成失败了：{out!r}")

    # 反向对照：换了 asset 就必须真装，否则上面那条可能是"什么都跳"的假通过
    check("c_other" in calls, "不同归档也被跳过了——判重键太宽")

    # --list 的"下载量"列：多条共用一个归档时不能每行都印 572.2 MB ——
    # 那样读起来像要下七次。这里同时验两件事：共用行标 ↳，脚注**只**点共用的那组。
    # （脚注第一版把所有归档的拥有者都列了进去，把独占自己归档的 c_other 也算了进去。）
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        ST.do_list(man)
    listing = buf.getvalue()
    check("↳a_suite" in listing, f"共用同一个归档的那行没标 ↳：\n{listing}")
    check("other.tgz" not in listing.split("注：")[-1],
          "脚注把独占自己归档的条目也算成共用了")
    check("a_suite, b_suite 共用同一个归档" in listing,
          f"脚注没说清是哪几个共用：\n{listing.splitlines()[-3:]}")
    check(listing.count("572.2 MB") == 1,
          f"共用归档的体积应该只在第一行印一次，实际印了 "
          f"{listing.count('572.2 MB')} 次：\n{listing}")
    check("7.5 MB" in listing,
          "独占自己归档的那行体积没印出来")

# ---------------------------------------------------------------- 负例 9e：锁真的有人查吗
#
# 病根：`--lock` 的文档一直写着「记下 sha256，之后靠它校验」，但在 2026-09-29
# 之前**全仓库没有任何代码读 tools.lock.json** —— 锁只写不读，是一张没人查的收据。
# 而那一周刚发生过一次静默截断（572 MB 下到 540 MB 照样报成功），
# 锁存在的全部意义就是防这个。
#
# 这里直接验 _lock_problem 的四种情形，而不是走 CLI：CLI 上「已安装」短路
# 先于锁校验触发，已经装好的机器上那条路根本走不到。要验的是**判据本身**。
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    M_EMPTY = M.load(_write(tmp, _minimal()))
    real_lock_path = ST.LOCK_PATH
    ST.LOCK_PATH = tmp / "tools.lock.json"
    try:
        SRC = {"repo": "R", "tag": "T", "asset": "a.tgz"}
        GOOD = "a" * 64
        BAD = "b" * 64

        # ① 没有记录 → 不拦（第一次装，天经地义）
        check(ST._lock_problem("x", SRC, GOOD) is None, "没有锁记录却拦了")

        ST._write_lock("x", SRC, GOOD)
        # ② 对得上 → 放行
        check(ST._lock_problem("x", SRC, GOOD) is None, "sha256 一致却报不一致")
        # ③ ★ 对不上 → 必须拦，且要把两个哈希都摆出来（只说"不一致"没法排查）
        bad_msg = ST._lock_problem("x", SRC, BAD)
        check(bad_msg is not None, "sha256 不一致却没有拦——锁又变成没人查的收据了")
        check(GOOD in bad_msg and BAD in bad_msg,
              f"报不一致但没把两个哈希都列出来：{bad_msg}")
        # ④ 资产换了（分组件 → 整包）→ 没有可比性，不算不一致，但也不算通过
        check(ST._lock_problem("x", {"repo": "R", "tag": "T", "asset": "b.tgz"}, BAD) is None,
              "资产换了却报 sha256 不一致——这是拿「换了份文件」冒充「文件被改了」")
        # ⑤ 锁里那条没有 sha256 → 是坏记录，不能当成通过
        ST._write_lock("y", SRC, GOOD)
        d = json.loads(ST.LOCK_PATH.read_text(encoding="utf-8"))
        del d["y"]["sha256"]
        ST.LOCK_PATH.write_text(json.dumps(d), encoding="utf-8")
        check(ST._lock_problem("y", SRC, BAD) is not None,
              "锁里没写 sha256 却被当成校验通过")

        # ⑥ 锁文件本身坏了：必须出警告，且**不能**静默降级成"全部通过"
        ST.LOCK_PATH.write_text("{ 这不是 JSON", encoding="utf-8")
        errbuf = io.StringIO()
        with contextlib.redirect_stderr(errbuf):
            got = ST._read_lock()
        check(got == {}, "坏锁文件没被降级成空")
        check("警告" in errbuf.getvalue(),
              "坏锁文件被静默降级了——校验会全绿而实际一个字节都没查")

        # ⑦ --verify-lock 必须把「对不上」「无法校验」分开报，且对不上要红
        ST.LOCK_PATH.write_text(json.dumps({
            "ok_one": {"repo": "R", "tag": "T", "asset": "there.tgz", "sha256": "z" * 64},
            "bad_one": {"repo": "R", "tag": "T", "asset": "there.tgz", "sha256": "0" * 64},
            "gone_one": {"repo": "R", "tag": "T", "asset": "not-cached.tgz", "sha256": "q" * 64},
        }), encoding="utf-8")
        (tmp / "there.tgz").write_bytes(b"hello")
        real_cache = ST.CACHE_DIR
        ST.CACHE_DIR = tmp
        try:
            with contextlib.redirect_stdout(io.StringIO()) as obuf:
                rc = ST.do_verify_lock(M_EMPTY)
            rep = obuf.getvalue()
            real_digest = ST.sha256_of(tmp / "there.tgz")
            # ok_one 的 sha256 是假的，所以 ok=0、bad=2、unknown=1
            check(rc == ST.EXIT_FAIL, f"有条目不一致时 --verify-lock 没红（rc={rc}）")
            check("通过 0" in rep and "不一致 2" in rep and "无法校验 1" in rep,
                  f"三类结果没分开报：\n{rep}")
            check("无法校验" in rep and "不是通过" in rep,
                  "「缓存里没有归档」没有明确排除在通过之外")
            check(real_digest in rep or real_digest[:16] in rep,
                  "没把实算的 hash 打出来，排查时无从对照")
        finally:
            ST.CACHE_DIR = real_cache

        # ⑧ 一条都校验不成时，必须说出来 —— 别让"全绿"实际是"全没查"
        ST.LOCK_PATH.write_text(json.dumps({
            "gone": {"repo": "R", "tag": "T", "asset": "nope.tgz", "sha256": "q" * 64},
        }), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as obuf:
            rc = ST.do_verify_lock(M_EMPTY)
        rep = obuf.getvalue()
        check(rc == ST.EXIT_OK, "只有「无法校验」时不该判失败")
        check("别把这次结果读成" in rep,
              f"一条都没校验成，却没说清这不等于锁干净：\n{rep}")
    finally:
        ST.LOCK_PATH = real_lock_path

# ---------------------------------------------------------------- 负例 9e2：锁从 .tools/ 搬进库
#
# 2026-09-29 之前锁在 `.tools/tools.lock.json`，而 .gitignore 忽略整个 `.tools/`
# —— 于是**锁从来没进过库**。新克隆的人手里没锁 → _lock_problem 查不到 entry
# 就 return None（"第一次装，天经地义"）→ 每次安装都放行。
# **两边都报「安装成功」，一边真被保护、一边没有。**
#
# 搬到 tools/ 之后要有一次迁移，否则**已经在用的人**会从"受保护"悄悄退回
# "没保护"，而且看不出任何变化 —— 升级路径上的静默退化比一开始就没有更坏。
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    real_lock_path = ST.LOCK_PATH
    real_legacy = ST._LEGACY_LOCK_PATH
    ST.LOCK_PATH = tmp / "new" / "tools.lock.json"
    ST._LEGACY_LOCK_PATH = tmp / "old" / "tools.lock.json"
    try:
        # ① 两边都没有 → 不搬，也不许凭空造一个空文件出来
        #    （造了的话 --verify-lock 会说"锁里一条记录都没有"，看着像坏了）
        check(ST._migrate_legacy_lock(verbose=False) is False, "没有旧锁却报了迁移")
        check(not ST.LOCK_PATH.exists(),
              "旧锁不存在却凭空造了个新锁文件——空锁会被读成「校验全过」")

        # ② 只有旧锁 → 内容**逐字**搬过来
        ST._LEGACY_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"yosys": {"repo": "R", "tag": "T", "asset": "a.tgz", "sha256": "a" * 64}}
        ST._LEGACY_LOCK_PATH.write_text(json.dumps(payload), encoding="utf-8")
        check(ST._migrate_legacy_lock(verbose=False) is True, "有旧锁却没有迁移")
        check(json.loads(ST.LOCK_PATH.read_text(encoding="utf-8")) == payload,
              "迁移后的内容和旧锁对不上")

        # ③ ★ 两边都有 → **以新的为准**，绝不让旧锁盖掉入库的那份。
        #    写反了就会：已经在用的人本机那把旧锁，把他从 git 拉下来的新锁覆盖掉，
        #    而两把锁内容不同时，覆盖是静默的。
        ST.LOCK_PATH.write_text(json.dumps({"keeper": payload["yosys"]}), encoding="utf-8")
        check(ST._migrate_legacy_lock(verbose=False) is False, "两边都有锁时还是搬了")
        kept = json.loads(ST.LOCK_PATH.read_text(encoding="utf-8"))
        check("keeper" in kept and "yosys" not in kept,
              f"旧锁把入库的锁盖掉了：{kept}")

        # ④ 旧锁是坏 JSON → 不搬，且**要说出来**（静默跳过 = 用户不知道自己没锁）
        ST.LOCK_PATH.unlink()
        ST._LEGACY_LOCK_PATH.write_text("{ 这不是 JSON", encoding="utf-8")
        errbuf = io.StringIO()
        with contextlib.redirect_stderr(errbuf):
            got = ST._migrate_legacy_lock(verbose=False)
        check(got is False, "旧锁是坏 JSON 却报了迁移成功")
        check(not ST.LOCK_PATH.exists(), "旧锁坏了却造出一个新锁文件")
        check("警告" in errbuf.getvalue(),
              "旧锁坏了被静默跳过——用户会以为自己还有锁")
    finally:
        ST.LOCK_PATH = real_lock_path
        ST._LEGACY_LOCK_PATH = real_legacy

# ---------------------------------------------------------------- 负例 9f：「已安装」短路
#
# 原先每次 install_one 都完整解压一遍：2026-09-29 跑 --install yosys --lock
# 就白解压了 16953 个条目、白写 2 GB。setup_toolchain.py 早有这个短路。
#
# 但短路有个前提：**证据得撑得住**。有 check.exe 的走功能自检；
# 没有 check.exe 的（公共库）只能看目录在不在 —— 那是 found 不是 usable，
# 印出来的话就不能是「自检通过」。
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    (tmp / "d").mkdir()
    (tmp / "d" / "something.dll").write_bytes(b"x")

    real_probe = ST.probe
    try:
        # ① 有 check.exe 且自检通过 → 短路，且说明里点明「自检通过」
        ST.probe = lambda sw: type("P", (), {"found": True, "usable": True, "version": "1.2"})()

        # 用绝对路径绕开 repo_root()，装到临时目录
        sw = M.Software(id="p", name="有自检的工具", summary="", windows="native",
                        raw={"install": {"dest": str(tmp / "d")}, "check": {"exe": "x"}},
                        check_exe="x")
        skip, why = ST._already_installed(sw)
        check(skip is True, "自检通过却没短路")
        check("自检通过" in why, f"短路说明没说是靠自检：{why}")

        # ② ★ 有 check.exe 但自检没过 → 绝不能短路（这正是 found≠usable 的现场）
        ST.probe = lambda sw: type("P", (), {"found": True, "usable": False, "version": None})()
        skip, why = ST._already_installed(sw)
        check(skip is False, "文件在但自检没过，却当成已安装短路了")

        # ③ 没有 check.exe、目录非空 → 短路，但**不许说成「自检通过」**
        sw2 = M.Software(id="q", name="公共库", summary="", windows="native",
                         raw={"install": {"dest": str(tmp / "d")}, "check": {"exe": None}},
                         check_exe=None)
        skip, why = ST._already_installed(sw2)
        check(skip is True, "公共库目录在却没短路——会白解压 2 GB")
        check("自检通过" not in why, f"没自检却说成自检通过：{why}")
        check("证明不了" in why, f"没说清证据有多弱：{why}")

        # ④ 没有 check.exe、目录不存在 → 不能短路
        sw3 = M.Software(id="r", name="公共库2", summary="", windows="native",
                         raw={"install": {"dest": str(tmp / "nope")}, "check": {"exe": None}},
                         check_exe=None)
        skip, _ = ST._already_installed(sw3)
        check(skip is False, "安装目录都不存在却短路了")
    finally:
        ST.probe = real_probe

# ---------------------------------------------------------------- 负例 9h：--restore-lock
#
# 「照锁还原环境」这条命令的价值不在于它装了什么，**在于它明说自己装不了什么**。
# 锁只覆盖「有 source.asset 且加过 --lock」的那些；iverilog 走另一个安装器、
# 没有 sha256、根本不在锁里 —— 所以还原出来的环境**不一定和作者一致**。
# 印一句「环境已还原」而实际差着 iverilog，就是本仓库最防的那种谎。
#
# 这里要守住四件事，每一件都有对应的变异能把它弄红：
#   ① 锁是空的时候不许说「完成」          （变异：把空锁分支删掉）
#   ② 覆盖按**资产**判、不按 id            （变异：改成按 id 判 → b_suite 变假缺口）
#   ③ 没有 sha256 的条目不算覆盖           （变异：_locked_assets 去掉 sha256 过滤）
#   ④ 没 source 的条目必须进「不归锁管」桶 （变异：恢复成按 install.method 过滤，
#                                            → 恰好漏掉 iverilog。第一版就是这么错的）
# 外加一条方向断言：还原时**不许写锁**（变异：把 lock=False 改成 True）。
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    syn = _minimal(software=[
        # a / b 共用同一个归档 —— 锁里只记了 a 时，b 必须也算被覆盖
        {"id": "a_suite", "name": "A套件", "summary": "", "windows": "native",
         "source": {"type": "github-release-asset", "repo": "R", "tag": "T",
                    "asset": "shared.tgz"},
         "install": {"method": "extract-tgz", "dest": ".tools/oss"}},
        {"id": "b_suite", "name": "B套件", "summary": "", "windows": "native",
         "source": {"type": "github-release-asset", "repo": "R", "tag": "T",
                    "asset": "shared.tgz"},
         "install": {"method": "extract-tgz", "dest": ".tools/oss"}},
        {"id": "c_other", "name": "C独立包", "summary": "", "windows": "native",
         "source": {"type": "github-release-asset", "repo": "R", "tag": "T",
                    "asset": "own.zip"},
         "install": {"method": "extract-zip", "dest": ".tools/c"}},
        # ★ iverilog 在真清单里的形状：**既没有 source 也没有 install 段**
        #   （它由 setup_toolchain.py 单独装）。这一格就是 ④ 的现场。
        {"id": "ivi", "name": "iverilog", "summary": "", "windows": "native"},
        {"id": "wslly", "name": "WSL专属", "summary": "", "windows": "wsl-only",
         "install": {"method": "wsl-apt"}},
        {"id": "venvish", "name": "venv工具", "summary": "", "windows": "pip",
         "install": {"method": "venv-pip"}},
    ])
    man = M.load(_write(tmp, syn))
    real_lock_path, real_install = ST.LOCK_PATH, ST.do_install
    real_verify = ST.do_verify_lock
    ST.LOCK_PATH = tmp / "tools.lock.json"
    install_calls: list[tuple[list[str], bool]] = []
    ST.do_install = lambda m, ids, verbose, lock, force=False: (
        install_calls.append((list(ids), lock)), ST.EXIT_OK)[1]
    # 末尾那次核对单独由负例 9i 管；这一节要测的是**覆盖面的判据**，
    # 让它去碰真 CACHE_DIR 只会引入无关噪声（真缓存里没有 shared.tgz）。
    ST.do_verify_lock = lambda m: ST.EXIT_OK
    try:
        def _run() -> tuple[int, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = ST.do_restore_lock(man, verbose=False)
            return rc, out.getvalue(), err.getvalue()

        # ① 空锁：**必须报缺**，而且绝不能印「完成：N 条按锁还原」。
        #    空锁 → 「没什么可还原」和「还原成功」在下游长得一样，含义相反。
        rc, out, err = _run()
        check(rc == ST.EXIT_ENV, f"锁是空的却没报缺（rc={rc}）")
        check("完成" not in out,
              f"空锁却印了「完成」—— 拿「没东西可还原」冒充「还原成功」：{out!r}")
        check("没有可还原的东西" in out + err, "空锁没说清是「没东西」而不是「已还原」")
        check(install_calls == [], "锁是空的却还是去装了东西")

        def _write_lock(data: dict) -> None:
            ST.LOCK_PATH.write_text(json.dumps(data), encoding="utf-8")

        # ② 覆盖按资产判：shared.tgz 锁过 → a_suite 和 b_suite 都算覆盖，
        #    报成缺口就是凭空造一个不存在的洞。
        _write_lock({
            "a_suite": {"repo": "R", "tag": "T", "asset": "shared.tgz", "sha256": "a" * 64},
            "c_other": {"repo": "R", "tag": "T", "asset": "own.zip", "sha256": "b" * 64},
        })
        rc, out, err = _run()
        check(rc == ST.EXIT_OK, f"锁正常却报错（rc={rc}）")
        check("没进锁" not in out,
              f"共用同一归档的条目被报成了假缺口（按 id 而不是按资产判）：{out!r}")
        # ④ 没有 source 的条目**必须在「不归锁管」桶里**，哪怕锁是全的
        check("由 setup_toolchain.py 单独装" in out,
              f"iverilog 形状的条目没被报出来 —— 环境差着判定内核的编译器却一声不吭：{out!r}")
        check("wslly" not in out and "WSL2 专属" in out,
              f"WSL 专属条目混进了缺口桶（它压根不在 Windows 上装）：{out!r}")

        # ⑤ 方向断言：还原时**不许写锁**。写了就把「参照物」变成「本次结果」，
        #    锁也就不再是锁 —— 下次校验拿自己刚下的一份跟自己比，永远通过。
        check(install_calls and all(lock is False for _, lock in install_calls),
              f"还原时把锁写了 —— 参照物被换成本次结果：{install_calls}")

        # ③ 锁里那条没有 sha256 → 不算覆盖。没有这个过滤，「记了个空壳」
        #    也会被当成「锁住了」，而实际什么都没锁住。
        _write_lock({
            "a_suite": {"repo": "R", "tag": "T", "asset": "shared.tgz", "sha256": "a" * 64},
            "c_other": {"repo": "R", "tag": "T", "asset": "own.zip"},
        })
        rc, out, err = _run()
        check("own.zip 没进锁" in out,
              f"锁里那条没 sha256 却被当成覆盖了：{out!r}")
        check("shared.tgz 没进锁" not in out,
              f"有 sha256 的那条被误报成缺口：{out!r}")
    finally:
        ST.LOCK_PATH = real_lock_path
        ST.do_install = real_install
        ST.do_verify_lock = real_verify

# ---------------------------------------------------------------- 负例 9i：还原的凭据是核对，不是安装
#
# 2026-09-29 实测抓到的谎：把锁里 yosys 的 sha256 改成 64 个 0，
#   `--verify-lock` 正确报「不一致 1」，
#   `--restore-lock` 却照样印「完成：6 条按锁还原」——**同一把锁，两个答案**。
# 根因：`install_one` 里 `_already_installed` 短路在 `_lock_problem` **之前**，
# 所以在一台装好的机器上，「已装」的条目**一次都没碰过锁**，`do_install` 的
# 返回值只说明「装的过程没报错」，说明不了「装出来的东西和锁里的一样」。
#
# 这一节守住两件事：
#   ① `do_restore_lock` **必须**调用 `do_verify_lock`，且在 `do_install` 之后
#      （变异：把核对那一段删掉 → 序里没有 verify，且下面 ② 也变红）
#   ② 核对红时**不许印「还原完成」**，必须换成实话并返回红
#      （变异：把 `if verify_rc != EXIT_OK: return verify_rc` 改成恒真短路）
# 外加正向对照：核对绿时才允许说「还原完成」，且要带上「新下 N / 复用已装 M」
# 这个分区 —— 少了它，「核对的是哪一批」就无从判断（同「两个计数必须分开报」）。
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    syn = _minimal(software=[
        {"id": "a_suite", "name": "A套件", "summary": "", "windows": "native",
         "source": {"type": "github-release-asset", "repo": "R", "tag": "T",
                    "asset": "shared.tgz"},
         "install": {"method": "extract-tgz", "dest": ".tools/oss"}},
        {"id": "b_suite", "name": "B套件", "summary": "", "windows": "native",
         "source": {"type": "github-release-asset", "repo": "R", "tag": "T",
                    "asset": "shared.tgz"},
         "install": {"method": "extract-tgz", "dest": ".tools/oss"}},
    ])
    man = M.load(_write(tmp, syn))
    real_lock_path, real_install, real_verify = (
        ST.LOCK_PATH, ST.do_install, ST.do_verify_lock)
    ST.LOCK_PATH = tmp / "tools.lock.json"
    ST.LOCK_PATH.write_text(json.dumps({
        "a_suite": {"repo": "R", "tag": "T", "asset": "shared.tgz", "sha256": "a" * 64},
        "b_suite": {"repo": "R", "tag": "T", "asset": "shared.tgz", "sha256": "a" * 64},
    }), encoding="utf-8")
    order: list[str] = []
    # ★ 两个都已装 —— 正是出问题的那台机器。do_install 在这里是**空转**的，
    #   所以它的返回值只为绿只证明「装的过程没报错」，这正是当初被误当成凭据的东西。
    ST.do_install = lambda m, ids, verbose, lock, force=False: (
        order.append("install"), ST.EXIT_OK)[1]
    try:
        def _run(vrc: int) -> tuple[int, str, str]:
            order.clear()
            ST.do_verify_lock = lambda m: (order.append("verify"), vrc)[1]
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = ST.do_restore_lock(man, verbose=False)
            return rc, out.getvalue(), err.getvalue()

        # ① 顺序：先装后核。反过来（先核后装）核的是**装之前**的缓存，
        #    新下的那批一条都盖不到，等于没核。
        rc, out, err = _run(ST.EXIT_OK)
        check(order == ["install", "verify"],
              f"还原没有先装后核 —— 「照锁还原」没有凭据（实际调用序 {order}）")
        # ② 正向对照：核对绿时才有资格说「还原完成」，且要报出分区。
        check(rc == ST.EXIT_OK, f"核对通过却报错（rc={rc}）")
        check("还原完成" in out,
              f"核对通过了却不敢说「还原完成」：{out!r}")
        check("复用已装" in out,
              f"报告里没把「新下」和「复用已装」分开 —— 核对的是哪一批无从判断：{out!r}")

        # ③ 核对红 → 绝不能印「还原完成」。这正是实测那次：do_install 全绿、
        #    核对报「不一致 1」，而旧版照样印完成。
        rc, out, err = _run(ST.EXIT_FAIL)
        check(rc == ST.EXIT_FAIL,
              f"核对不一致却返回绿（rc={rc}）—— 装的过程没报错被当成了凭据")
        check("还原完成" not in out,
              f"核对没过却印了「还原完成」—— 同一把锁两个答案：{out!r}")
        check("核对没过" in out + err,
              f"核对没过却没说清为什么：{out!r} + {err!r}")
    finally:
        ST.LOCK_PATH = real_lock_path
        ST.do_install = real_install
        ST.do_verify_lock = real_verify

# ---------------------------------------------------------------- 负例 9g：首次安装时目标目录不存在
#
# 2026-09-29 装 verible 实测踩到：`_merge_into(staging, dest)` 里 `dest` 是
# `.tools/verible`，而它**从来不存在**（verible 是本仓库第一个「独占自己归档」
# 的条目）。于是第一句 `shutil.move` 抛 FileNotFoundError，报错信息是
# 「系统找不到指定的路径」——**指不到「目标目录没建」这件事上**。
#
# 为什么一直没暴露：OSS 套件那 8 个条目共用 `.tools/oss`，而它由**先装的**
# 组件建了出来；verilator / yosys 谁先装都行，所以这条路径永远走不到。
# 也就是说，这是个**只在第一次装独立包时**才发作的坑——正是最容易漏测的形状。
#
# 这里直接调 `_merge_into`，把「dst 不存在」这个前提**显式造出来**，
# 并且带一条正向对照（dst 已存在时的正常合并），确认修的是那一处而不是
# 把合并逻辑改坏了。
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    def _tree(root: Path, spec: dict) -> None:
        """按 {名字: 内容或子 dict} 造目录树。"""
        root.mkdir(parents=True, exist_ok=True)
        for name, val in spec.items():
            if isinstance(val, dict):
                _tree(root / name, val)
            else:
                (root / name).write_text(val, encoding="utf-8")

    # ① dst 不存在 —— 装独立包的第一次，就是这一种
    #
    # ★ 这一格**必须只放顶层文件**，不能放子目录，否则这个测试是假的。
    # 原因是 shutil.move 对**目录**会自己兜底：os.rename 因父目录不存在失败后，
    # 它退到 copytree，而 copytree 顺手把 dst 建了出来——于是「dst 没建」这个
    # bug 被掩盖。只有**文件**才会一路走 copy2 → open(dst) 抛 FileNotFoundError。
    # （verible 的暂存目录里 11 个条目大都是 .exe 文件，所以它一撞就响。）
    # 第一版这里放了 bin/ 和 share/，结果拿掉 dst.mkdir 断言照样全绿——
    # 一条**看着在测、其实测不到**的断言，比没有断言更糟。
    src = tmp / "staging"
    _tree(src, {"linter.exe": "exe"})       # 刻意只有一个**文件**
    dst = tmp / "verible"                   # ← 刻意不建
    check(not dst.exists(), "前置条件不成立：测试自己把 dst 建出来了")
    try:
        moved = ST._merge_into(src, dst)
    except FileNotFoundError as exc:
        check(False, f"dst 不存在时 _merge_into 抛了 FileNotFoundError：{exc}")
        moved = -1
    if moved >= 0:
        check(moved == 1, f"首次安装合并的条目数不对：{moved}（应为 1）")
        check((dst / "linter.exe").is_file(), "首次安装没把顶层文件搬过去")
        check(list(src.iterdir()) == [], f"合并后暂存目录还剩东西：{sorted(p.name for p in src.iterdir())}")

    # ①b 同上，但带嵌套目录（装 verible 的完整形状）
    src1b = tmp / "staging1b"
    _tree(src1b, {
        "bin": {"linter.exe": "exe", "fmt.exe": "exe"},
        "share": {"rules.txt": "rules"},
        "README": "doc",
    })
    dst1b = tmp / "verible1b"
    try:
        moved = ST._merge_into(src1b, dst1b)
    except FileNotFoundError as exc:
        check(False, f"带子目录时首次合并也抛了 FileNotFoundError：{exc}")
        moved = -1
    if moved >= 0:
        check(moved == 3, f"首次安装合并的条目数不对：{moved}（应为 bin/share/README 三项）")
        check((dst1b / "bin" / "linter.exe").is_file(), "首次安装没把 bin/ 搬过去")
        check((dst1b / "share" / "rules.txt").is_file(), "首次安装没把 share/ 搬过去")
        check((dst1b / "README").is_file(), "首次安装没把顶层文件搬过去")

    # ② 正向对照：dst 已存在（共用 `.tools/oss` 的那种），要**递归合并**而不是嵌套
    src2 = tmp / "staging2"
    _tree(src2, {
        "bin": {"yosys.exe": "new-yosys", "keep.exe": "new-keep"},
        "lib": {"a.dll": "new-a"},
    })
    dst2 = tmp / "oss"
    _tree(dst2, {
        "bin": {"keep.exe": "OLD-keep", "other.exe": "old-other"},
        "lib": {"zzz.dll": "old-zzz"},
    })
    ST._merge_into(src2, dst2)
    check((dst2 / "bin" / "yosys.exe").is_file(), "已有 dst 时没合并进去（或在错误层级）")
    check(
        not (dst2 / "staging2").exists(),
        "把整个 src 塞进 dst 了（变成 dst/staging2/...）——路径深了一层，探测必然落空",
    )
    check(
        (dst2 / "bin" / "keep.exe").read_text(encoding="utf-8") == "new-keep",
        "同名文件应以 src 为准覆盖（重装/升级时旧文件必须被顶掉）",
    )
    check(
        (dst2 / "bin" / "other.exe").is_file() and (dst2 / "lib" / "zzz.dll").is_file(),
        "合并不是覆盖：dst 原有的、src 里没有的文件不能被删掉",
    )

# ---------------------------------------------------------------- 负例 10：安装护栏
# WSL-only 的软件必须被拒绝，且**不能发起下载**。
# 如果这里漏了，装 opensta 会白下几百 MB 再失败。
_m = M.load()
_sta = _m.software_by_id("opensta")
if _sta is not None:
    ok, msg = ST.install_one(_sta, verbose=False)
    check(ok is False, "WSL-only 的软件没有被拒绝")
    check("WSL" in msg, f"拒绝理由没说清是 WSL 限制：{msg}")

# ---------------------------------------------------------------- 结果

if failures:
    print("check_manifest 检查未通过：")
    for f in failures:
        print(f"  ✗ {f}")
    sys.exit(1)

print("  ✓ 真清单可加载且自洽；L1 零缺口")
print("  ✓ 坏 JSON / 清单缺失 → 抛 ManifestError（不静默降级）")
print("  ✓ 悬空 requires / 未知 impact / 悬空 provides → validate 全部抓到")
print("  ✓ 探测不存在的工具 → found=False，不谎报")
print("  ✓ 依赖缺失 → 能力判为未满足，blocking 列表正确")
print("  ✓ gap=code 与 gap=software 区分正确，不给出无用的安装建议")
print("  ✓ WSL-only 依赖与可安装依赖分开报告，--for-level 不会去下装不上的东西")
print("  ✓ 「文件在」与「真能跑」分开判：自检失败的工具不满足能力（带正向对照）")
print("  ✓ lib_dirs 并入 PATH 而非覆盖（覆盖会让子进程找不到系统命令，且不报错）")
print("  ✓ repo_private 只认仓库私有安装，系统 PATH 上的同名工具不算（带正向对照）")
print("  ✓ 归档路径穿越（tar/zip）被拦住，正常归档仍能解压")
print("  ✓ 首次安装（dst 不存在）能合并；已有 dst 时递归合并、不嵌套、不删旧文件")
print("  ✓ 真清单里 integration 承认未接入的条目，gap 必定带 code")
print("  ✓ 包裹层剥离：多层剥离到位、碰到 bin/ 停手、多目录归档不动")
print("  ✓ WSL-only 的软件被拒绝，不会白下载")
print("manifest 检查通过")
