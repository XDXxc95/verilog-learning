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
print("  ✓ 归档路径穿越（tar/zip）被拦住，正常归档仍能解压")
print("  ✓ 包裹层剥离：多层剥离到位、碰到 bin/ 停手、多目录归档不动")
print("  ✓ WSL-only 的软件被拒绝，不会白下载")
print("manifest 检查通过")
