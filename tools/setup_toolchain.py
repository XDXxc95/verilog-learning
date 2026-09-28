"""Verilog 工具链安装与自检。

用法：
    python tools/setup_toolchain.py --check       # 只检查，不动任何东西
    python tools/setup_toolchain.py --install     # 下载并静默安装到仓库私有目录
    python tools/setup_toolchain.py --selftest    # 端到端自检：编译→仿真→产出 VCD

安装位置：<repo>/.tools/iverilog/，由 .gitignore 排除。

**不污染用户环境**是本脚本的硬约束：
  - 装在仓库内，不写系统 PATH（并用 /MERGETASKS 显式抑制安装包自带的 PATH 修改任务）
  - 安装前后对注册表 PATH 做只读快照对比，若仍被改动会如实报告
  - 不使用任何 pip 包，只用标准库
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _console import init_console  # noqa: E402
from _toolchain import detect, private_toolchain_dir, probe_version, repo_root  # noqa: E402

# 官方 Windows 构建。v14 为本文件编写时的最新版；v12 是更保守的稳定回落。
# 来源：https://bleyer.org/icarus/  （安装包内已含 GTKWave）
DOWNLOAD_BASE = "https://bleyer.org/icarus/"
INSTALLER_CANDIDATES = [
    "iverilog-v14-20260804-x64_setup.exe",
    "iverilog-v12-20220611-x64_setup.exe",
]
DOWNLOAD_TIMEOUT = 60
CHUNK = 256 * 1024
# 安装步骤的超时。安装包会弹出组件选择窗口需要人手点一下，
# 因此不能设太短；但也不能无限等，否则无人值守时会永久挂起。
INSTALL_TIMEOUT = 420

# Inno Setup 静默安装参数。
# /VERYSILENT 全静默，/SUPPRESSMSGBOXES 抑制弹窗，/NORESTART 不重启，/SP- 跳过前置检查。
# /MERGETASKS 用 "!任务名" 语法**取消**勾选默认任务——这里尝试抑制 PATH 修改与快捷方式，
# 是"不污染用户环境"的关键一步。Inno 对不存在的任务名会忽略，因此多列几个常见命名是安全的。
SILENT_FLAGS = [
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/SP-",
    "/NOCANCEL",
    # 注意：这里刻意不加引号。Inno 的任务名列表不含空格，而经 subprocess 列表传参时
    # 内层引号会被转义成 \" ，Inno 未必按预期解析，反而可能整个参数失效。
    "/MERGETASKS=!modifypath,!addtopath,!desktopicon,!quicklaunchicon,!startmenuicon",
]

# Inno Setup 的退出码含义
INNO_EXIT_MEANING = {
    0: "安装成功",
    1: "安装程序初始化失败",
    2: "用户取消了安装",
    3: "安装过程中发生致命错误",
    4: "安装过程中发生致命错误",
    5: "用户取消了安装",
}


# ---------------------------------------------------------------- PATH 快照

def _read_path_registry() -> dict[str, str | None]:
    """只读地读取用户级与系统级 PATH。读不到（非 Windows / 无权限）时返回 None 值并说明。"""
    if os.name != "nt":
        return {"user": None, "system": None}
    try:
        import winreg
    except ImportError:
        return {"user": None, "system": None}

    targets = {
        "user": (winreg.HKEY_CURRENT_USER, r"Environment"),
        "system": (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    }
    out: dict[str, str | None] = {}
    for key, (root, sub) in targets.items():
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
                val, _ = winreg.QueryValueEx(k, "Path")
                out[key] = str(val)
        except OSError:
            out[key] = None
    return out


def _path_diff(before: dict, after: dict) -> list[str]:
    """比较两次 PATH 快照，返回人类可读的差异说明。"""
    notes = []
    for scope in ("user", "system"):
        b, a = before.get(scope), after.get(scope)
        if b is None or a is None:
            continue
        if b != a:
            added = [p for p in a.split(os.pathsep) if p and p not in b.split(os.pathsep)]
            notes.append(
                f"{scope} PATH 被修改"
                + (f"，新增：{added}" if added else "（内容有变化）")
            )
    return notes


# ---------------------------------------------------------------- 下载

def download_installer(dest_dir: Path, verbose: bool = True) -> Path:
    """下载安装包到 dest_dir。支持断点续传（服务器返回 Accept-Ranges: bytes）。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None

    for name in INSTALLER_CANDIDATES:
        url = DOWNLOAD_BASE + name
        target = dest_dir / name
        part = target.with_suffix(target.suffix + ".part")

        if target.is_file() and target.stat().st_size > 1_000_000:
            if verbose:
                print(f"  [跳过] 已存在安装包 {name}")
            return target

        # 断点续传：从 .part 的现有大小接着下
        resume_from = part.stat().st_size if part.is_file() else 0
        headers = {"User-Agent": "verilog-learning-setup/1.0"}
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"

        try:
            if verbose:
                suffix = f"（从 {resume_from // 1024} KB 续传）" if resume_from else ""
                print(f"  [下载] {url}{suffix}")

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
                total = resp.headers.get("Content-Length")
                total = int(total) + resume_from if total else 0
                mode = "ab" if resume_from and resp.status == 206 else "wb"
                if mode == "wb":
                    resume_from = 0

                done = resume_from
                t0 = time.time()
                with open(part, mode) as fh:
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if verbose and total:
                            pct = done * 100 // total
                            speed = (done - resume_from) / max(time.time() - t0, 1e-6)
                            print(
                                f"\r         {pct:3d}%  {done // 1024:>7} / {total // 1024} KB"
                                f"  {speed / 1024 / 1024:.1f} MB/s",
                                end="",
                                flush=True,
                            )
                if verbose:
                    print()

            # 校验：Windows 可执行文件必须以 MZ 开头，防止把错误页面当成安装包
            with open(part, "rb") as fh:
                magic = fh.read(2)
            if magic != b"MZ":
                part.unlink(missing_ok=True)
                raise RuntimeError(f"下载内容不是有效的 Windows 可执行文件（文件头 {magic!r}）")

            part.replace(target)
            return target

        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            last_err = exc
            if verbose:
                print(f"         [失败] {exc}")
            continue

    raise RuntimeError(f"所有候选安装包均下载失败：{last_err}")


# ---------------------------------------------------------------- 安装

def run_installer(exe: Path, target_dir: Path, verbose: bool = True) -> tuple[bool, str]:
    """静默安装到 target_dir。返回 (是否成功, 说明)。"""
    target_dir.mkdir(parents=True, exist_ok=True)

    # Inno 的 /DIR 参数在含空格时需要引号；这里路径无空格时保持裸值，避免转义歧义。
    dir_arg = f'/DIR="{target_dir}"' if " " in str(target_dir) else f"/DIR={target_dir}"
    cmd = [str(exe), *SILENT_FLAGS, dir_arg]

    if verbose:
        print(f"  [安装] {exe.name}")
        print(f"         目标目录 {target_dir}")
        print("         安装包会弹出组件选择窗口（/VERYSILENT 无法完全压制），")
        print("         用默认选项点下一步即可；其余步骤自动完成。")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, (
            f"安装超时（超过 {INSTALL_TIMEOUT // 60} 分钟）。"
            "多半是组件选择窗口在等待点击——请在桌面上找到它并点完，或直接取消。"
        )
    except OSError as exc:
        return False, f"无法启动安装程序：{exc}"

    meaning = INNO_EXIT_MEANING.get(proc.returncode, f"未知退出码 {proc.returncode}")
    if proc.returncode == 0:
        return True, meaning

    detail = (proc.stderr or proc.stdout or "").strip()
    return False, f"{meaning}" + (f"；输出：{detail[:400]}" if detail else "")


def _common_install_dirs() -> list[Path]:
    """安装包可能落地的默认位置（实测 bleyer v14 会无视 /DIR 装到这里）。"""
    dirs = [Path(r"C:\iverilog")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Programs" / "iverilog")
    return dirs


def _relocate_into_private(target: Path, preexisting: set[Path], verbose: bool = True) -> bool:
    """把安装包落在默认位置的安装整体搬进仓库私有目录。

    安全护栏：只搬**本次安装新产生的**目录（preexisting 里没有的）。
    用户机器上原本就有的安装一律不动——这是"不影响已有环境"的底线。
    """
    ok = _probe_private_ok(target)

    for src in _common_install_dirs():
        # 只处理本次安装新产生的目录；用户原有的安装一律不碰
        if src in preexisting or not (src / "bin" / "iverilog.exe").is_file():
            continue

        # 私有目录已就位时，新产生的默认安装是冗余的，直接清掉
        if ok:
            if verbose:
                print(f"  [清理] 移除多余的默认安装 {src}")
            shutil.rmtree(src, ignore_errors=True)
            continue

        if verbose:
            print(f"  [搬迁] 安装包未遵守 /DIR，实际落在 {src}")
            print(f"         正在移入 {target} …")

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        try:
            shutil.move(str(src), str(target))
        except (OSError, shutil.Error) as exc:
            print(f"         [失败] 搬迁出错：{exc}")
            print(f"         改为直接使用该位置：{src}")
            return False

        ok = _probe_private_ok(target)
        if verbose and ok:
            print("         搬迁完成，默认位置已清理干净")

    return ok


def _probe_private_ok(target: Path) -> bool:
    """私有目录里编译+仿真两个可执行文件是否都就位。"""
    return (target / "bin" / "iverilog.exe").is_file() and (
        target / "bin" / "vvp.exe"
    ).is_file()


def do_install(verbose: bool = True, force: bool = False) -> int:
    """完整安装流程。返回进程退出码。"""
    target = private_toolchain_dir()
    cache = repo_root() / ".tools" / "_cache"

    print("=" * 66)
    print("Verilog 工具链安装 —— Icarus Verilog + GTKWave")
    print(f"安装位置：{target}")
    print("本仓库不修改系统 PATH，不安装任何 Python 包。")
    print("=" * 66)

    # 已就位就直接返回。重装既有风险又毫无收益：
    # 该安装包会弹出组件选择对话框，/VERYSILENT 压不住，无人值守时会一直卡住。
    if _probe_private_ok(target) and not force:
        print("\n工具链已就位，无需重新安装。")
        _report_versions(detect())
        print("\n如需强制重装，加 --force。")
        return 0

    path_before = _read_path_registry()
    # 记录哪些默认位置是"本来就有"的，搬迁时坚决不动
    preexisting = {d for d in _common_install_dirs() if d.exists()}

    try:
        installer = download_installer(cache, verbose=verbose)
    except RuntimeError as exc:
        print(f"\n[失败] {exc}")
        print("\n可手动安装：")
        print("  1. 打开 https://bleyer.org/icarus/")
        print(f"  2. 下载 {INSTALLER_CANDIDATES[0]}")
        print(f"  3. 安装时把目标目录设为：{target}")
        print("  4. 重新运行 python tools/setup_toolchain.py --check")
        return 4

    ok, msg = run_installer(installer, target, verbose=verbose)
    if not ok:
        print(f"\n[失败] 安装未成功：{msg}")
        if "初始化失败" in msg or "取消" in msg:
            print("\n多半是需要管理员权限。可以手动双击运行：")
            print(f"  {installer}")
            print("  安装目录用默认值即可，脚本随后会自动搬进仓库私有目录。")
        return 4

    # 安装后核验。实测 bleyer 安装包会无视 /DIR，落到 C:\iverilog，
    # 因此这里不是简单断言失败，而是尝试搬迁并就搬迁结果如实报告。
    if not _relocate_into_private(target, preexisting, verbose=verbose):
        tc = detect()
        if not tc.ok:
            print("\n[失败] 安装已完成，但既没落到默认位置，也没能搬进私有目录。")
            print(f"  期望目录：{target / 'bin'}")
            print(f"  实际探测：{json.dumps(tc.as_dict(), ensure_ascii=False, indent=2)}")
            return 4
        print("\n[提示] 未能搬进私有目录，将直接使用系统上已有的那份安装。")

    tc = detect()
    if not tc.ok:
        print("\n[失败] 搬迁后仍无法定位 iverilog/vvp。")
        print(json.dumps(tc.as_dict(), ensure_ascii=False, indent=2))
        return 4

    print(f"\n[成功] {msg}")
    _report_versions(tc)

    # 诚实报告是否动到了用户环境
    path_after = _read_path_registry()
    diffs = _path_diff(path_before, path_after)
    if diffs:
        print("\n[注意] 安装程序改动了系统环境变量：")
        for d in diffs:
            print(f"  - {d}")
        print("  这些是 iverilog 安装包自带的行为（已尝试用 /MERGETASKS 抑制但未完全生效）。")
        print("  如需还原，请告诉我，我会先展示改动内容再操作。")
    else:
        print("\n[环境] 系统 PATH 未被改动，用户已有环境不受影响。")

    print("\n下一步：python tools/setup_toolchain.py --selftest")
    return 0


# ---------------------------------------------------------------- 检查与自检

def _report_versions(tc) -> None:
    for label, exe in (("iverilog", tc.iverilog), ("vvp", tc.vvp), ("gtkwave", tc.gtkwave)):
        if exe is None:
            print(f"  {label:<9} : 未找到")
            continue
        ver = probe_version(exe)
        print(f"  {label:<9} : {exe}")
        print(f"  {'':<9}   {ver}")


def do_check(as_json: bool = False) -> int:
    tc = detect()
    if as_json:
        payload = tc.as_dict()
        for key, exe in (("iverilog", tc.iverilog), ("vvp", tc.vvp), ("gtkwave", tc.gtkwave)):
            payload[f"{key}_version"] = probe_version(exe) if exe else None
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if tc.ok else 4

    print("=" * 66)
    print("Verilog 工具链自检")
    print("=" * 66)
    print(f"探测来源：{tc.source}")
    print(f"搜索路径：{', '.join(tc.searched)}")
    print("-" * 66)
    _report_versions(tc)
    print("-" * 66)

    if tc.ok:
        print("状态：可用 ✓")
        if tc.gtkwave is None:
            print("提示：gtkwave 未找到，但不影响使用（本仓库自带 HTML 波形查看器）。")
        return 0

    print("状态：不可用 ✗")
    print("\n安装指引：")
    print("  自动：python tools/setup_toolchain.py --install")
    print("  手动：https://bleyer.org/icarus/ 下载安装包，")
    print(f"        安装目录设为 {private_toolchain_dir()}")
    print("  或指定已有安装：设置环境变量 IVERILOG_BIN 指向其 bin 目录")
    return 4


SELFTEST_TB = """\
// 工具链端到端自检：组合逻辑 + 时序逻辑 + 4 值逻辑，并产出 VCD
`timescale 1ns/1ps
module _selftest;
    reg clk = 0, rst_n, a, b;
    wire y_comb;
    reg  y_seq;

    assign y_comb = a & b;

    always @(posedge clk) begin
        if (!rst_n) y_seq <= 1'b0;
        else        y_seq <= y_comb;
    end

    always #5 clk = ~clk;

    // 4 值逻辑的两条典型规则，刻意各留一个不驱动/不初始化：
    //   wire 没有驱动源 -> z（高阻），不是 x
    //   reg  声明后未赋初值 -> x（未知）
    wire undriven_wire;
    reg  undriven_reg;

    integer errors = 0;

    // 激励一律在时钟沿之后 #1 施加，避开沿上的竞争，
    // 这样"组合立即响应 / 时序滞后一拍"的对比才干净。
    initial begin
        $dumpfile("{vcd}");
        $dumpvars(0, _selftest);

        rst_n = 0; a = 0; b = 0;
        @(posedge clk);          // t=5
        @(posedge clk);          // t=15
        #1; rst_n = 1;           // 离开时钟沿再撤销复位

        @(posedge clk);          // t=25：此时 a&b 仍为 0
        #1;

        // --- 组合逻辑应立即跟随输入 ---
        a = 1; b = 1;
        #1;
        if (y_comb !== 1'b1) begin
            $display("SELFTEST_FAIL y_comb expected=1 got=%b", y_comb);
            errors = errors + 1;
        end

        // --- 时序逻辑此刻仍应是上一拍采到的 0（这就是"滞后一拍"）---
        if (y_seq !== 1'b0) begin
            $display("SELFTEST_FAIL y_seq expected=0 (should lag one cycle) got=%b", y_seq);
            errors = errors + 1;
        end

        // --- 下一个时钟沿才把 1 采进去 ---
        @(posedge clk);          // t=35
        #1;
        if (y_seq !== 1'b1) begin
            $display("SELFTEST_FAIL y_seq expected=1 got=%b", y_seq);
            errors = errors + 1;
        end

        // --- 4 值逻辑 ---
        if (undriven_wire !== 1'bz) begin
            $display("SELFTEST_FAIL undriven_wire expected=z got=%b", undriven_wire);
            errors = errors + 1;
        end
        if (undriven_reg !== 1'bx) begin
            $display("SELFTEST_FAIL undriven_reg expected=x got=%b", undriven_reg);
            errors = errors + 1;
        end

        if (errors == 0) $display("SELFTEST_PASS");
        else             $display("SELFTEST_FAIL errors=%0d", errors);
        $finish;
    end
endmodule
"""


def do_selftest(keep: bool = False) -> int:
    """端到端自检：编译 → 仿真 → 校验 VCD 产出。"""
    tc = detect()
    if not tc.ok:
        print("工具链不可用，请先运行 --install")
        return 4

    build = repo_root() / "build"
    build.mkdir(exist_ok=True)
    vcd = build / "_selftest.vcd"
    tb = build / "_selftest.v"
    vvp_out = build / "_selftest.vvp"

    tb.write_text(SELFTEST_TB.format(vcd=vcd.as_posix()), encoding="utf-8")

    print("=" * 66)
    print("工具链端到端自检")
    print("=" * 66)

    # 1) 编译
    cmd = [str(tc.iverilog), "-g2012", "-o", str(vvp_out), str(tb)]
    print(f"[1/4] 编译  {tc.iverilog.name} …")
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)
    if proc.returncode != 0:
        print(f"      失败：\n{proc.stdout}\n{proc.stderr}")
        return 4
    print(f"      成功，产出 {vvp_out.relative_to(repo_root())}")

    # 2) 仿真
    print("[2/4] 仿真  vvp …")
    proc = subprocess.run(
        [str(tc.vvp), str(vvp_out)],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
        cwd=str(repo_root()),
    )
    sim_out = (proc.stdout or "") + (proc.stderr or "")
    for line in sim_out.splitlines():
        if line.strip():
            print(f"      | {line.strip()}")

    if "SELFTEST_PASS" not in sim_out:
        print("      仿真判定失败")
        return 4
    print("      判定通过")

    # 3) VCD 产出
    print("[3/4] 检查波形文件 …")
    if not vcd.is_file():
        print(f"      失败：未生成 {vcd}")
        return 4
    size = vcd.stat().st_size
    head = vcd.read_bytes()[:200].decode("latin-1")
    if "$timescale" not in head and "$date" not in head:
        print("      失败：VCD 头部格式异常")
        return 4
    print(f"      成功，{size} 字节")
    if not keep:
        pass  # 保留产物，供后续人工查看

    # 4) 版本
    print("[4/4] 版本")
    print(f"      {probe_version(tc.iverilog)}")
    print(f"      {probe_version(tc.vvp)}")

    print("-" * 66)
    print("自检全部通过 ✓  工具链可用于本课程。")
    print(f"波形样本：{vcd}")
    return 0


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    init_console()
    ap = argparse.ArgumentParser(
        description="Verilog 工具链安装与自检（安装到仓库私有目录，不污染系统环境）"
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="只检查工具链是否可用")
    g.add_argument("--install", action="store_true", help="下载并静默安装")
    g.add_argument("--selftest", action="store_true", help="端到端自检：编译→仿真→VCD")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出检查结果")
    ap.add_argument(
        "--force",
        action="store_true",
        help="配合 --install：即便工具链已就位也强制重装（需人工点击组件选择窗口）",
    )
    args = ap.parse_args(argv)

    if args.install:
        return do_install(force=args.force)
    if args.selftest:
        return do_selftest()
    return do_check(as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
