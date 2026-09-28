"""Verilog 工具链定位（共享模块）。

被 setup_toolchain.py（安装与自检）和 sim.py（编译仿真）共同引用，
确保"去哪儿找 iverilog"这件事只有一处定义，不会随实现漂移。

解析顺序（先命中者胜）：
  1. 显式指定的 bin 目录（命令行 / 环境变量 IVERILOG_BIN）
  2. 仓库私有目录 <repo>/.tools/iverilog/bin   ← 本项目默认安装位置
  3. 系统 PATH

设计约束：本仓库不污染用户已有环境。私有工具链装在仓库内、由 .gitignore 排除，
不写系统 PATH，因此即便机器上早已装有 iverilog，也不会被本仓库改动。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 探测外部命令时的超时（秒）。工具链探测必须快，卡住就是故障。
PROBE_TIMEOUT = 20


def repo_root() -> Path:
    """仓库根目录。本文件位于 <repo>/tools/ 下。"""
    return Path(__file__).resolve().parent.parent


def private_toolchain_dir() -> Path:
    """仓库私有工具链目录 <repo>/.tools/iverilog。"""
    return repo_root() / ".tools" / "iverilog"


def _exe(name: str) -> str:
    """按平台补上可执行后缀。"""
    return f"{name}.exe" if os.name == "nt" else name


@dataclass
class Toolchain:
    """一次工具链探测的完整结果。"""

    iverilog: Path | None = None
    vvp: Path | None = None
    gtkwave: Path | None = None
    source: str = "none"  # explicit / private / path / none
    searched: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """编译 + 仿真必需的两个可执行文件都在，才算可用。"""
        return self.iverilog is not None and self.vvp is not None

    @property
    def bin_dir(self) -> Path | None:
        return self.iverilog.parent if self.iverilog else None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "source": self.source,
            "iverilog": str(self.iverilog) if self.iverilog else None,
            "vvp": str(self.vvp) if self.vvp else None,
            "gtkwave": str(self.gtkwave) if self.gtkwave else None,
            "bin_dir": str(self.bin_dir) if self.bin_dir else None,
            "searched": list(self.searched),
        }


def _candidate_bins(explicit_bin: Path | None) -> list[tuple[str, Path]]:
    """返回 [(来源标签, bin 目录)]，按优先级排序。"""
    out: list[tuple[str, Path]] = []

    # 1) 显式指定
    if explicit_bin:
        out.append(("explicit", Path(explicit_bin)))

    env_bin = os.environ.get("IVERILOG_BIN")
    if env_bin:
        out.append(("explicit", Path(env_bin)))

    # 2) 仓库私有目录。安装器可能把 bin/ 放在 target 下，
    #    也可能直接把可执行文件铺在 target 根下，两种都试。
    priv = private_toolchain_dir()
    out.append(("private", priv / "bin"))
    out.append(("private", priv))

    # 3) 系统上常见的既有安装位置。放在私有目录之后、PATH 之前：
    #    本仓库优先用自己那份，但用户若早已装过 iverilog，也能被直接复用。
    common = [
        Path(r"C:\iverilog\bin"),  # bleyer 安装包的默认位置
        Path(r"C:\Program Files\iverilog\bin"),
        Path(r"C:\Program Files (x86)\iverilog\bin"),
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        common.append(Path(local) / "Programs" / "iverilog" / "bin")
    for d in common:
        out.append(("system", d))

    return out


def _probe_dir(bin_dir: Path, source: str) -> Toolchain | None:
    """在给定目录里找 iverilog / vvp / gtkwave。"""
    iv = bin_dir / _exe("iverilog")
    if not iv.is_file():
        return None

    def optional(name: str) -> Path | None:
        p = bin_dir / _exe(name)
        if p.is_file():
            return p
        found = shutil.which(name)
        return Path(found) if found else None

    return Toolchain(
        iverilog=iv,
        vvp=(bin_dir / _exe("vvp") if (bin_dir / _exe("vvp")).is_file() else None),
        gtkwave=optional("gtkwave"),
        source=source,
    )


def detect(explicit_bin: str | os.PathLike | None = None) -> Toolchain:
    """探测工具链。找不到时返回 ok=False 的 Toolchain，不抛异常。"""
    explicit = Path(explicit_bin) if explicit_bin else None
    searched: list[str] = []

    for source, bin_dir in _candidate_bins(explicit):
        searched.append(str(bin_dir))
        found = _probe_dir(bin_dir, source)
        if found and found.ok:
            found.searched = searched
            return found

    # 3) 系统 PATH
    searched.append("PATH")
    iv = shutil.which("iverilog")
    vvp = shutil.which("vvp")
    if iv and vvp:
        return Toolchain(
            iverilog=Path(iv),
            vvp=Path(vvp),
            gtkwave=Path(shutil.which("gtkwave")) if shutil.which("gtkwave") else None,
            source="path",
            searched=searched,
        )

    return Toolchain(source="none", searched=searched)


def probe_version(exe: Path | str) -> str | None:
    """取可执行文件的版本首行。iverilog 系列把版本打到 stdout 或 stderr 不定，两者都收。"""
    try:
        p = subprocess.run(
            [str(exe), "-V"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    blob = (p.stdout or "") + (p.stderr or "")
    for line in blob.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def install_guide(tc: Toolchain) -> dict:
    """找不到工具链时给 Claude / 学习者看的结构化安装指引。"""
    return {
        "kind": "toolchain_missing",
        "missing": [
            n
            for n, v in (("iverilog", tc.iverilog), ("vvp", tc.vvp))
            if v is None
        ],
        "searched": tc.searched,
        "how_to_fix": "运行 python tools/setup_toolchain.py --install 自动下载安装",
        "manual": {
            "url": "https://bleyer.org/icarus/",
            "file": "iverilog-v14-20260804-x64_setup.exe",
            "note": "官方 Windows 构建，安装包内已含 GTKWave。装完用 --check 验证。",
        },
    }


def main() -> int:
    """直接运行本模块时，打印探测结果，便于快速排查。"""
    import json

    tc = detect()
    print(json.dumps(tc.as_dict(), indent=2, ensure_ascii=False))
    return 0 if tc.ok else 4


if __name__ == "__main__":
    sys.exit(main())
