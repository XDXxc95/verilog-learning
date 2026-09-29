"""工具能力清单的加载、校验与探测（共享模块）。

被 check_tools.py（报告能力缺口）和 setup_tools.py（安装）引用。
清单本体是 tools/toolchain.json，本模块**不硬编码任何工具名**——
加新工具只改 JSON，不改代码。

为什么要做成清单驱动而不是写死在代码里：

    这个仓库会按课程级别使用不同的工具。L1 只需要 iverilog，
    L2 开始想要 verilator（真 lint）和 yosys（状态编码对比），
    L3 才涉及形式验证。写死就变成「不管学到哪一级，都先装一大堆」。

    做成清单之后，check_tools.py --level L2 才能回答
    「你学到 L2 了，有两项能力缺失，缺了会怎样，装什么能补」，
    而不是笼统地报「工具不全」。

环境铁律（与 _toolchain.py 一致）：所有软件默认装进 <repo>/.tools/ 下的子目录，
不写系统 PATH、不装系统目录。pip 类工具走 <repo>/.tools/venv/。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from _toolchain import repo_root

# 清单文件与工具同目录
MANIFEST_PATH = Path(__file__).resolve().parent / "toolchain.json"

# 探测可执行文件时的超时。工具探测必须快，卡住就是故障。
PROBE_TIMEOUT = 20

# 影响级别的展示顺序与图标。顺序 = 严重程度递减。
IMPACT_ORDER = ["blocks", "degrades", "shrinks", "talk_only"]
IMPACT_ICON = {
    "blocks": "🔴",
    "degrades": "🟠",
    "shrinks": "🟡",
    "talk_only": "⚪",
}
IMPACT_LABEL = {
    "blocks": "整讲做不了",
    "degrades": "动手环节塌掉",
    "shrinks": "缩水但主课能上",
    "talk_only": "本来只讲",
}

# 软件的 Windows 可得性
WINDOWS_LABEL = {
    "native": "Windows 原生可用",
    "pip": "需 pip（装入仓库私有 venv）",
    "wsl-only": "Windows 原生无方案，需 WSL2",
    "unavailable": "无可用方案",
}


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


# ---------------------------------------------------------------- 数据结构


@dataclass
class Software:
    id: str
    name: str
    summary: str
    windows: str
    raw: dict
    check_exe: str | None = None
    version_arg: str | None = None
    # 只认仓库私有的那一份，**不回退到系统 PATH**。
    #
    # 默认 False 是有道理的：使用者在系统上早就装过某个工具时，复用它比
    # 逼他再装一份好。但有一条例外——**结果取决于版本的工具不能复用**。
    # g++ 就是典型：verilator 是按 GCC 15.2.1 配的，撞上系统 PATH 上那个
    # 2017 年的 mingw 7.3 会连报四个错，而报告里只会写"已安装但不可用"，
    # 使用者照着去查 PATH 也找不到原因。
    #
    # 这类工具在清单里写 `"repo_private": true`，探测就只认 `.tools/`：
    # 没装就是**没装**（可以装），而不是"装了但用不了"（无从下手）。
    repo_private: bool = False

    @property
    def install_dest(self) -> Path | None:
        dest = self.raw.get("install", {}).get("dest")
        return repo_root() / dest if dest else None

    @property
    def size_mb(self) -> float | None:
        src = self.raw.get("source", {})
        return src.get("size_mb")

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "windows": self.windows}


@dataclass
class Capability:
    id: str
    name: str
    levels: list[str]
    requires: list[str]
    gap: str  # none | software | code
    impact: str
    fallback: str | None
    integration: str | None
    note: str
    raw: dict

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "levels": list(self.levels),
            "requires": list(self.requires),
            "gap": self.gap,
            "impact": self.impact,
            "integration": self.integration,
        }


@dataclass
class Manifest:
    software: list[Software] = field(default_factory=list)
    capabilities: list[Capability] = field(default_factory=list)
    impact_levels: dict = field(default_factory=dict)
    schema: str = ""
    updated: str = ""

    def software_by_id(self, sid: str) -> Software | None:
        for s in self.software:
            if s.id == sid:
                return s
        return None

    def capabilities_for_level(self, level: str) -> list[Capability]:
        """某一级别的能力，按影响严重程度排序。"""
        sel = [c for c in self.capabilities if level in c.levels]
        return sorted(sel, key=lambda c: IMPACT_ORDER.index(c.impact))

    def distinct_software_for_level(self, level: str) -> list[Software]:
        """某个级别变得**必需**（相对低级）的软件，即该级能力新引入的依赖。

        只列还没装的，供「装什么能补」提示用。
        """
        ids: list[str] = []
        for c in self.capabilities_for_level(level):
            for sid in c.requires:
                if sid not in ids:
                    ids.append(sid)
        return [s for s in (self.software_by_id(i) for i in ids) if s is not None]


# ---------------------------------------------------------------- 加载与校验


class ManifestError(RuntimeError):
    """清单本身有问题（格式错、交叉引用不一致）。"""


def load(path: Path | None = None) -> Manifest:
    """读取清单。清单坏了要**大声报错**——静默降级成空清单会让
    check_tools.py 谎报「全部可用」，比崩溃有害得多。"""
    p = path or MANIFEST_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestError(f"找不到清单文件：{p}") from None
    except json.JSONDecodeError as exc:
        raise ManifestError(f"清单不是合法 JSON：{p}\n  {exc}") from None

    m = Manifest(
        schema=data.get("schema", ""),
        updated=data.get("updated", ""),
        impact_levels=data.get("impact_levels", {}),
    )
    for raw in data.get("software", []):
        chk = raw.get("check", {}) or {}
        m.software.append(
            Software(
                id=raw["id"],
                name=raw.get("name", raw["id"]),
                summary=raw.get("summary", ""),
                windows=raw.get("windows", "unknown"),
                raw=raw,
                check_exe=chk.get("exe"),
                version_arg=chk.get("version_arg"),
                repo_private=bool(raw.get("repo_private", False)),
            )
        )
    for raw in data.get("capabilities", []):
        m.capabilities.append(
            Capability(
                id=raw["id"],
                name=raw.get("name", raw["id"]),
                levels=list(raw.get("levels", [])),
                requires=list(raw.get("requires", [])),
                gap=raw.get("gap", "none"),
                impact=raw.get("impact", "shrinks"),
                fallback=raw.get("fallback"),
                integration=raw.get("integration"),
                note=raw.get("note", ""),
                raw=raw,
            )
        )
    return m


def validate(m: Manifest) -> list[str]:
    """校验清单自洽。返回问题列表，空列表 = 没问题。

    重点抓两类错（两类都是**不报错但结果错**的）：
      1. requires 引用了不存在的 software id —— 会导致「装什么能补」永远列不出来
      2. impact 值不在 IMPACT_ORDER 里 —— 会导致排序时 ValueError
    """
    problems: list[str] = []
    sw_ids = {s.id for s in m.software}
    seen_sw: set[str] = set()
    seen_cap: set[str] = set()

    for s in m.software:
        if s.id in seen_sw:
            problems.append(f"software id 重复：{s.id}")
        seen_sw.add(s.id)
        if s.windows not in WINDOWS_LABEL:
            problems.append(f"software {s.id} 的 windows 值未知：{s.windows!r}")
        # repo_private 的意义就是"只认装进 .tools/ 的那一份"，
        # 没写 install.dest 就没有 .tools/ 可认 —— 于是它会在任何机器上
        # 都判成未安装，且装也装不进来。这种配置错误不会崩，只会永远缺着。
        if s.repo_private and not s.install_dest:
            problems.append(
                f"software {s.id} 标了 repo_private 但没有 install.dest，"
                f"没有可认的私有安装位置"
            )

    for c in m.capabilities:
        if c.id in seen_cap:
            problems.append(f"capability id 重复：{c.id}")
        seen_cap.add(c.id)
        if c.impact not in IMPACT_ORDER:
            problems.append(f"capability {c.id} 的 impact 值未知：{c.impact!r}")
        if c.gap not in ("none", "software", "code"):
            problems.append(f"capability {c.id} 的 gap 值未知：{c.gap!r}")
        for sid in c.requires:
            if sid not in sw_ids:
                problems.append(f"capability {c.id} 引用了不存在的 software：{sid}")
        if not c.levels:
            problems.append(f"capability {c.id} 没写 levels")

    # provides ↔ requires 双向一致性。**只查一个方向不够**——
    # 2026-09-29 实测抓到：slang.provides 说它提供 lint.full，而
    # lint.full.requires 是 ['verilator']。两个方向各自看着都成立，
    # 合起来是一句假话：「装着 slang 却显示 lint.full 可用」还是轻的，
    # 真正的问题是使用者会以为这条能力有两个来源。
    #
    # provides ⊆ requires 是**声明性**的：它说的是「装了谁这条能力就满足」，
    # 所以两边必须逐条对得上，不能有一边多出来。
    cap_requires = {c.id: set(c.requires) for c in m.capabilities}
    for s in m.software:
        for cid in s.raw.get("provides", []):
            if cid not in seen_cap:
                problems.append(f"software {s.id} 的 provides 引用了不存在的 capability：{cid}")
            elif s.id not in cap_requires.get(cid, set()):
                problems.append(
                    f"software {s.id} 的 provides 说它提供 {cid}，"
                    f"但 {cid}.requires = {sorted(cap_requires.get(cid, set()))}，"
                    f"两边对不上"
                )
    for c in m.capabilities:
        for sid in c.requires:
            s = m.software_by_id(sid)
            if s is not None and c.id not in set(s.raw.get("provides", [])):
                problems.append(
                    f"capability {c.id} 依赖 {sid}，但 {sid}.provides 里没写这条"
                )

    return problems


# ---------------------------------------------------------------- 探测


@dataclass
class ProbeResult:
    software_id: str
    found: bool
    exe_path: Path | None = None
    version: str | None = None
    searched: list[str] = field(default_factory=list)
    detail: str = ""
    # 找到了文件 ≠ 能用。清单里给了 check.functional 的工具会被真跑一次，
    # 失败就把 usable 置 False。这样"已安装但一用就报错"不会再被判成可用。
    usable: bool = True


def _candidate_locations(sw: Software) -> list[Path]:
    """按优先级给出该软件可执行文件可能在的目录。"""
    out: list[Path] = []
    dest = sw.install_dest
    if dest:
        # 套件解压后通常有 bin/ 子目录；独立包可能把 exe 铺在根或 bin/ 下
        out.append(dest / "bin")
        out.append(dest)
    # iverilog 是既有私有工具链，单独兜一下
    out.append(repo_root() / ".tools" / "iverilog" / "bin")
    out.append(repo_root() / ".tools" / "iverilog")
    if dest:
        out.append(dest / "venv" / "Scripts" if os.name == "nt" else dest / "venv" / "bin")
    return out


def tool_env(sw: Software) -> dict[str, str]:
    """这个工具运行**必需**的环境变量。清单里写在 `check.env`。

    为什么必须有这个：OSS CAD Suite 的 verilator 是交叉编译出来的，
    `verilator_bin.exe` 里烘死了构建期的 `VERILATOR_ROOT=/yosyshq`。
    不设它的话 `--version` 照样能打印版本号，但 `--lint-only` 会报
    「Cannot find verilated_std_waiver.vlt」直接失败——
    **探测会显示"已安装 ✓"，实际却用不了**，正是这个仓库最想避免的那类谎报。

    `${dest}` 会替换成安装目录，所以清单里写相对路径即可，不写死绝对路径。
    """
    out: dict[str, str] = {}
    dest = sw.install_dest
    chk = sw.raw.get("check", {}) or {}
    for key, val in chk.get("env", {}).items():
        out[key] = str(val).replace("${dest}", str(dest) if dest else "")

    # 共享库目录要**并入** PATH，不能直接覆盖——覆盖会把系统 PATH 冲掉。
    # 为什么非要有：套件里的 exe 在运行时找 DLL（如 yosys.exe 要 libreadline8.dll），
    # Windows 的搜索路径不含同级 lib/，不前置就报
    # 「error while loading shared libraries」退出码 127。
    # 这与 verilator 的 VERILATOR_ROOT 是同一类问题：不设，程序"在"但跑不起来。
    lib_dirs = chk.get("lib_dirs") or []
    if lib_dirs and dest:
        prepend = [str(dest / d) for d in lib_dirs]
        old = out.get("PATH", os.environ.get("PATH", ""))
        out["PATH"] = os.pathsep.join(prepend + ([old] if old else []))
    return out


def locate(sw: Software, extra: Path | None = None) -> Path | None:
    """按清单里的候选位置找到可执行文件，**不做功能自检**。

    与 probe() 的区别是快：probe() 会真跑一次 `check.functional`（该工具
    自己的进程，约几百毫秒），那是「体检」该付的代价，不是「每次仿真」该付的。
    sim.py 每次都要找 verilator，走这条。

    代价是**这里分辨不出「文件在」和「能用」**（verilator 烘死 VERILATOR_ROOT
    就是这个形状：文件找得到，一 lint 就报错）。所以调用方必须把"跑起来但失败"
    当成一个正常结果如实上报，不能默认它能用——`_vlint.run()` 就是那么做的。

    `extra` 是调用方显式指定的路径（文件或目录），优先级最高。
    """
    if extra is not None:
        cand = extra / _exe(sw.check_exe) if extra.is_dir() else extra
        if cand.is_file():
            return cand
        return None

    if not sw.check_exe:
        return None
    exe_name = _exe(sw.check_exe)
    for d in _candidate_locations(sw):
        cand = d / exe_name
        if cand.is_file():
            return cand

    if sw.repo_private:
        # 见 Software.repo_private：这类工具的结果取决于版本，
        # 复用系统上的那一份得到的是"能跑但结论不对"，比找不到更坏。
        return None

    # 最后试系统 PATH：使用者可能早就在系统上装过，允许复用，但不改动它
    which = shutil.which(sw.check_exe)
    return Path(which) if which else None


def probe(sw: Software, extra_path: list[Path] | None = None) -> ProbeResult:
    """探测单个软件。找不到不是异常，是正常结果。

    `extra_path` 是**模拟「这个目录也在系统 PATH 上」**，给测试用的
    （正式调用方一个都没用，见 locate() 的 `extra` 才是显式指定路径）。
    所以标了 repo_private 的软件**连它也不认**——否则"私有化"就成了
    "只要调用方换个说法就能复用系统那一份"，负例也就验不出东西了。
    """
    env = {**os.environ, **tool_env(sw)}

    if not sw.check_exe:
        # 没有可执行文件的（如 oss-utils 这种纯运行库）只能检查目录是否存在。
        # 注意这类探测**分辨不出**"组件真装了"和"别的组件把这个目录建出来了"，
        # 所以 detail 里如实写明判断依据，别让读报告的人以为验证过。
        dest = sw.install_dest
        searched = [str(dest)] if dest else []
        found = bool(dest and dest.is_dir())
        return ProbeResult(
            software_id=sw.id,
            found=found,
            searched=searched,
            detail="无独立可执行文件，按安装目录是否存在判断（不能证明组件真的装了）",
        )

    exe_name = _exe(sw.check_exe)
    searched: list[str] = []
    extra_dirs = [] if sw.repo_private else list(extra_path or [])
    for d in extra_dirs + _candidate_locations(sw):
        searched.append(str(d))
        cand = d / exe_name
        if cand.is_file():
            return _finish_probe(sw, cand, env, searched, from_path=False)

    if sw.repo_private:
        # **不去系统 PATH 找一个替代品**。硬找一个回来会让报告显示
        # 「已安装但不可用」，而真正的问题是"仓库私有那一份没装"——
        # 前者让人去翻 PATH，后者直接给出一条能照做的安装命令。
        return ProbeResult(
            software_id=sw.id,
            found=False,
            searched=searched,
            detail="只认仓库私有安装（清单里标了 repo_private），系统 PATH 上的同名工具不算",
        )

    # 最后试系统 PATH（用户可能早就在系统上装过，允许复用，但不改动它）
    searched.append("PATH")
    which = shutil.which(sw.check_exe)
    if which:
        return _finish_probe(sw, Path(which), env, searched, from_path=True)

    return ProbeResult(software_id=sw.id, found=False, searched=searched)


def _finish_probe(
    sw: Software, exe: Path, env: dict[str, str], searched: list[str], from_path: bool
) -> ProbeResult:
    version = _probe_version(exe, sw.version_arg, env)
    usable, note = _probe_functional(sw, exe, env)
    detail = "来自系统 PATH（非仓库私有）" if from_path else ""
    if note:
        detail = f"{detail}；{note}" if detail else note
    return ProbeResult(
        software_id=sw.id,
        found=True,
        exe_path=exe,
        version=version,
        searched=searched,
        detail=detail,
        usable=usable,
    )


def _probe_version(
    exe: Path, version_arg: str | None, env: dict[str, str] | None = None
) -> str | None:
    if not version_arg:
        return None
    try:
        p = subprocess.run(
            [str(exe), version_arg],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=PROBE_TIMEOUT,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    blob = (p.stdout or "") + (p.stderr or "")
    for line in blob.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _probe_functional(
    sw: Software, exe: Path, env: dict[str, str]
) -> tuple[bool, str]:
    """按清单里的 `check.functional` 真跑一次，回答"它到底能不能用"。

    为什么不能只查版本号：OSS CAD Suite 的 verilator 里烘死了构建期的
    `VERILATOR_ROOT`，`--version` 照样打印版本，但 `--lint-only` 会直接
    「Cannot find verilated_std_waiver.vlt」失败。只看版本号就会报
    「已安装 ✓」，而实际上一个能力都补不上——这正是本仓库反复要避免的谎报。

    返回 (usable, 备注)。
    """
    spec = (sw.raw.get("check", {}) or {}).get("functional")
    if not spec:
        return True, ""
    args = [str(a) for a in spec.get("args", [])]
    try:
        p = subprocess.run(
            [str(exe), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=PROBE_TIMEOUT,
            env=env,
            cwd=str(repo_root()),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"⚠ 功能自检跑不起来：{type(exc).__name__}"

    if p.returncode == 0:
        return True, ""
    blob = (p.stdout or "") + (p.stderr or "")
    first = next((l.strip() for l in blob.splitlines() if l.strip()), "")
    cause = _exit_cause(p.returncode)
    if not first:
        # 一个字节都没有是最容易被误读的情况：既不是"成功了没输出"，
        # 也不是"程序报错"，而是进程在 main 之前就被系统干掉了。
        first = "程序没有任何输出就退出了"
    return False, f"⚠ 找到了文件但功能自检失败（{cause}）：{first[:160]}"


# Windows 上进程在 main 之前挂掉，退出码是 NTSTATUS，一串十进制的
# 3221225781 没人看得懂，得翻译过来才指导得了下一步。
_NTSTATUS = {
    0xC0000135: "找不到依赖的 DLL（STATUS_DLL_NOT_FOUND），通常意味着这个组件不自包含，缺一整套运行时库",
    0xC0000139: "DLL 入口点对不上（STATUS_ENTRYPOINT_NOT_FOUND），多半是混用了别处的同名 DLL",
    0xC0000142: "DLL 初始化失败（STATUS_DLL_INIT_FAILED）",
    0xC0000005: "访问违例（STATUS_ACCESS_VIOLATION）",
}


def _exit_cause(rc: int) -> str:
    if rc in _NTSTATUS:
        return f"退出码 0x{rc:08X}，{_NTSTATUS[rc]}"
    # Windows 下 os.system 风格的高位码转成有符号值后仍可识别
    u = rc & 0xFFFFFFFF
    if u in _NTSTATUS:
        return f"退出码 0x{u:08X}，{_NTSTATUS[u]}"
    return f"退出码 {rc}"


def probe_all(m: Manifest) -> dict[str, ProbeResult]:
    return {s.id: probe(s) for s in m.software}


# ---------------------------------------------------------------- 能力状态


@dataclass
class CapabilityStatus:
    cap: Capability
    ok: bool
    missing_software: list[str] = field(default_factory=list)
    # ok=False 且 gap=="code" 时为 True：不是缺软件，是缺实现
    code_gap: bool = False
    # 缺失的软件里，Windows 原生装不了的那些（wsl-only / unavailable）。
    # 单独拎出来是因为它们**不能**和普通缺失混在一句「装上这些就行」里报告——
    # 使用者照着装会发现装不上。本仓库的默认立场是不装 WSL（见 CLAUDE.md）。
    unavailable_software: list[str] = field(default_factory=list)

    @property
    def installable_missing(self) -> list[str]:
        """缺失的软件里，本仓库在当前平台上真能装上的那部分。"""
        return [sid for sid in self.missing_software if sid not in self.unavailable_software]

    @property
    def actionable(self) -> bool:
        """装软件能不能解决。code gap 装什么都没用；
        缺失软件全是 Windows 原生装不了的，也算「装了也解决不了」。"""
        return (not self.ok) and (not self.code_gap) and bool(self.installable_missing)


def _native_unavailable(m: Manifest, sid: str) -> bool:
    """这个软件在本仓库的默认姿态下装不上吗？（WSL-only 或无方案）

    未知 id 按「装得上」处理——悬空引用是 validate() 的职责，
    这里再去猜一遍只会让两处判断打架。
    """
    sw = m.software_by_id(sid)
    return sw is not None and sw.windows not in ("native", "pip")


def _satisfied(p: ProbeResult) -> bool:
    return p.found and p.usable


def evaluate(m: Manifest, probes: dict[str, ProbeResult]) -> list[CapabilityStatus]:
    out: list[CapabilityStatus] = []
    for c in m.capabilities:
        # 条件必须是 found **且** usable。「文件在」不等于「能用」——
        # verilator 就是版本号能打、lint 却因为烘死的 VERILATOR_ROOT 而失败的例子。
        # 只看 found 会把这种工具判成「已安装 ✓」，然后能力照旧缺着，
        # 报告上却显示补齐了，属于谎报。
        missing = [
            sid
            for sid in c.requires
            if not _satisfied(probes.get(sid, ProbeResult(sid, False)))
        ]

        # gap == "code" 表示「这个能力本仓库还没实现，缺的是代码不是软件」。
        # 这类能力通常 requires 为空，如果只看依赖是否满足就会被判成 ok=True——
        # 于是「多参数批量回归」这种压根没写的功能会显示「可用」。
        # 必须显式排除，否则整个检查会谎报。
        ok = (not missing) and c.gap != "code"
        out.append(
            CapabilityStatus(
                cap=c,
                ok=ok,
                missing_software=missing,
                code_gap=(not ok) and c.gap == "code",
                unavailable_software=[sid for sid in missing if _native_unavailable(m, sid)],
            )
        )
    return out


# ---------------------------------------------------------------- 报告


def summarize_for_level(
    m: Manifest, level: str, probes: dict[str, ProbeResult]
) -> dict:
    """某一级别的能力体检结果。JSON 结构，同时供人读和程序读。"""
    statuses = evaluate(m, probes)
    mine = [s for s in statuses if level in s.cap.levels]

    # 缺的软件分两堆：本平台装得上的（可以去 --install），和装不上的（要 WSL2）。
    # 这两堆**不能混在一句「装上这些可补」里**，否则使用者照着单子装会发现装不上。
    installable_sw = sorted(
        {sid for s in mine for sid in s.installable_missing}
    )
    unavailable_sw = sorted(
        {sid for s in mine for sid in s.unavailable_software}
    )

    def _sw_info(sid: str) -> dict:
        sw = m.software_by_id(sid)
        return {
            "id": sid,
            "name": sw.name if sw else sid,
            "windows": sw.windows if sw else "unknown",
            "size_mb": sw.size_mb if sw else None,
            # 「装之前得知道的坑」——比如 verilator 装了不代表覆盖率和断言就能用。
            # 不带这条，报告读起来就是「装上它就好了」，而实际不是。
            "caveat": (sw.raw.get("caveat") if sw else None),
        }

    return {
        "level": level,
        "capabilities_total": len(mine),
        "capabilities_ok": sum(1 for s in mine if s.ok),
        "blocking": [
            s.cap.id for s in mine if not s.ok and s.cap.impact == "blocks"
        ],
        "unmet": [
            {
                **s.cap.as_dict(),
                "missing_software": s.missing_software,
                "code_gap": s.code_gap,
                "installable_missing": s.installable_missing,
                "unavailable_software": s.unavailable_software,
                "actionable": s.actionable,
                "fallback": s.cap.fallback,
                "note": s.cap.note,
            }
            for s in mine
            if not s.ok
        ],
        # 装上就能补的
        "missing_software": [_sw_info(sid) for sid in installable_sw],
        # 本仓库默认装不上的（WSL2）——列出来是为了「说清楚为什么补不了」，
        # 不是一份待办清单
        "unavailable_software": [_sw_info(sid) for sid in unavailable_sw],
    }
