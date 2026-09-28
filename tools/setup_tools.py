"""按 tools/toolchain.json 清单下载并安装工具到仓库私有目录。

用法：
    python tools/setup_tools.py --list                  # 看清单里有什么、装没装
    python tools/setup_tools.py --install yosys         # 装一个
    python tools/setup_tools.py --install yosys verilator
    python tools/setup_tools.py --for-level L2          # 装 L2 缺的能力所需的全部软件
    python tools/setup_tools.py --install yosys --lock  # 同时记录 sha256

与 setup_toolchain.py 的分工：
    setup_toolchain.py  装 iverilog（Inno Setup 安装包，要静默安装 + 搬迁）
    setup_tools.py      装清单里的其它工具（都是 tgz / zip 解压，简单得多）

环境铁律（硬约束，别"优化"掉）：
  - 全部解压到 <repo>/.tools/ 下的子目录，**不写系统 PATH、不装系统目录**
  - 解压直接用 tarfile / zipfile，**不运行任何下载来的可执行文件**，
    所以没有弹窗、没有注册表写入、没有静默安装参数那一堆坑
  - pip 类工具建 <repo>/.tools/venv/ 私有虚拟环境，不碰系统 site-packages

⚠️ 关于可复现性：OSS CAD Suite 的 bucket-* 是**滚动 tag**，同名资产每天被覆盖。
   今天装的 yosys 和明天装的可能不是同一个版本。要固定版本必须用 --lock 记下
   sha256，之后靠它校验。不带 --lock 的安装等于"装到哪个算哪个"。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _console import init_console  # noqa: E402
from _manifest import ManifestError, load, probe, validate  # noqa: E402
from _toolchain import repo_root  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 64
EXIT_ENV = 4

# 单次 socket 读的超时。别调小：OSS CAD Suite 的组件动辄几十 MB，
# 实测这条线路上会掉到几 KB/s 甚至长时间 0.0 MB/s，60 秒会中途掐断
# （表现是留下一个 .part 文件、然后"下载失败"，看起来像 URL 错了）。
DOWNLOAD_TIMEOUT = 300
# 下载失败重试次数。配合断点续传，断了就从 .part 的位置接着下。
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_WAIT = 5
CHUNK = 256 * 1024

# 下载缓存目录（与 setup_toolchain.py 共用 .tools/_cache）
CACHE_DIR = repo_root() / ".tools" / "_cache"
# 锁定文件：记录装过的版本与 sha256
LOCK_PATH = repo_root() / ".tools" / "tools.lock.json"


def _gh_url(src: dict) -> str:
    return f"https://github.com/{src['repo']}/releases/download/{src['tag']}/{src['asset']}"


# ---------------------------------------------------------------- 下载


def _download_once(url: str, part: Path) -> None:
    """一次下载尝试，从 part 的现有长度续传。失败会抛异常，part 保留。"""
    resume_from = part.stat().st_size if part.is_file() else 0
    headers = {"User-Agent": "verilog-learning-setup/1.0"}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        total = resp.headers.get("Content-Length")
        total = int(total) + resume_from if total else 0
        # 只有服务端真按 Range 返回 206 才能续写，否则得从头来
        mode = "ab" if resume_from and resp.status == 206 else "wb"
        done = resume_from if mode == "ab" else 0

        t0 = time.time()
        with open(part, mode) as fh:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    speed = (done - resume_from) / max(time.time() - t0, 1e-6)
                    print(
                        f"\r         {pct:3d}%  {done // 1024:>8} / {total // 1024} KB"
                        f"  {speed / 1024 / 1024:.1f} MB/s",
                        end="",
                        flush=True,
                    )
    print()


def download(sw, dest_dir: Path, verbose: bool = True) -> Path:
    """下载到 dest_dir。支持断点续传 + 失败重试。返回落盘路径。

    重试是必需的，不是保险：这条线路上实测会中途断开，60 秒超时的第一版
    直接把 verilator（21 MB）下崩了，表现是留下一个 256 KB 的 .part 然后
    「下载失败」——看起来像 URL 写错，实际是连不稳。
    """
    src = sw.raw.get("source", {})
    if src.get("type") != "github-release-asset":
        raise RuntimeError(f"{sw.id} 的来源类型 {src.get('type')!r} 不支持直接下载")

    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / src["asset"]
    part = target.with_suffix(target.suffix + ".part")

    if target.is_file() and target.stat().st_size > 1_000_000:
        if verbose:
            print(f"  [跳过] 已下载 {src['asset']}")
        return target

    url = _gh_url(src)
    if verbose:
        kb = part.stat().st_size // 1024 if part.is_file() else 0
        suffix = f"（从 {kb} KB 续传）" if kb else ""
        print(f"  [下载] {url}{suffix}")

    last: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            if verbose and attempt > 1:
                print(f"  [重试] 第 {attempt}/{DOWNLOAD_RETRIES} 次…")
            _download_once(url, part)
            last = None
            break
        except (urllib.error.URLError, OSError, TimeoutError) as exc:  # noqa: PERF203
            last = exc
            if verbose:
                print(f"  [中断] {type(exc).__name__}: {exc}")
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(DOWNLOAD_RETRY_WAIT)
    if last is not None:
        kb = part.stat().st_size // 1024 if part.is_file() else 0
        raise RuntimeError(
            f"下载在 {DOWNLOAD_RETRIES} 次尝试后仍失败（已下 {kb} KB，"
            f"下次运行会从这里续传）：{type(last).__name__}: {last}"
        ) from last

    # 校验下载内容真的是压缩包，防止把 404 页面当成工具装进去。
    # setup_toolchain.py 那边查的是 MZ（exe），这里查 gzip / zip 魔数。
    with open(part, "rb") as fh:
        magic = fh.read(4)
    if not (magic[:2] == b"\x1f\x8b" or magic[:2] == b"PK" or magic[:4] == b"\xfd7zX"):
        part.unlink(missing_ok=True)
        raise RuntimeError(f"下载内容不是压缩包（文件头 {magic!r}），可能是错误页面")

    part.replace(target)
    return target


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- 解压
#
# 安全要点：解压下载来的归档**必须防路径穿越**（zip slip / tar slip）。
# 恶意或损坏的归档里可以有 ../../ 开头的成员名，直接 extractall 会写到
# 目标目录之外。Python 3.12+ 的 filter="data" 会拦，但为了兼容性这里
# 自己再校验一遍成员路径，两道防线。


def _safe_members_tar(tf: tarfile.TarFile, dest: Path):
    dest_r = dest.resolve()
    for m in tf.getmembers():
        target = (dest / m.name).resolve()
        if not str(target).startswith(str(dest_r)):
            raise RuntimeError(f"归档成员越界，拒绝解压：{m.name}")
        if m.islnk() or m.issym():
            link = (dest / m.linkname).resolve()
            if not str(link).startswith(str(dest_r)):
                raise RuntimeError(f"归档内链接越界，拒绝解压：{m.name} -> {m.linkname}")
        yield m


def extract_tgz(archive: Path, dest: Path, verbose: bool = True) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        members = list(_safe_members_tar(tf, dest))
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, members=members, filter="data")
        else:
            tf.extractall(dest, members=members)
    if verbose:
        print(f"  [解压] {len(members)} 个条目 -> {dest}")


def _safe_members_zip(zf: zipfile.ZipFile, dest: Path):
    dest_r = dest.resolve()
    for n in zf.namelist():
        target = (dest / n).resolve()
        if not str(target).startswith(str(dest_r)):
            raise RuntimeError(f"归档成员越界，拒绝解压：{n}")
        yield n


def extract_zip(archive: Path, dest: Path, verbose: bool = True) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        names = list(_safe_members_zip(zf, dest))
        zf.extractall(dest, members=names)
    if verbose:
        print(f"  [解压] {len(names)} 个条目 -> {dest}")


FLATTEN_MAX_DEPTH = 8

# 这些是**内容目录**，不是包裹层，剥到这里必须停手。
#
# 为什么需要这个停手名单：如果某个包的 `bin/` 里**只有一个文件**，
# 光看「只有一个子目录」的结构会继续把 `bin/` 也剥掉，结果变成
# `dest/tool.exe` 而不是 `dest/bin/tool.exe`——探测按 `dest/bin/` 找，照样落空。
# 合成归档测出来的（tests/_smoke/check_manifest.py 负例 9b）。
FLATTEN_CONTENT_DIRS = frozenset(
    {
        "bin", "sbin", "lib", "lib64", "libexec", "share", "include",
        "etc", "man", "doc", "docs", "license", "licenses", "src",
        "python", "scripts",
    }
)


def _flatten_single_dir(dest: Path, verbose: bool = True) -> int:
    """把「只有一个子目录」的包裹层反复剥掉，直到剩下真正的内容。

    为什么是**反复**而不是剥一层（第一版就错在这）：不同来源的包裹层数不一样，
    而且同一来源的不同组件也不一样。实测两个真实例子——

        slang-11.0/bin/slang.exe                     剥 1 层
        _outputs/windows-x64/yosys/yosyshq/bin/...   剥 4 层（OSS CAD Suite 的 tgz）

    第一版只给 zip 剥一层、tgz 完全不剥，结果 OSS CAD Suite 的组件解压到
    `.tools/oss/_outputs/windows-x64/yosys/yosyshq/bin/`，探测按 `dest/bin/xxx.exe`
    去找必然落空——报的是「解压完成但探测不到可执行文件」，文件其实都在。

    剥的规则（不硬编码 `_outputs` / `yosyshq` 这类名字，只看结构）：
    当前层只有一个（非隐藏的）子目录、且它的名字**不在内容目录名单里**，
    就说明它还是个包裹层。两个停手条件：出现 ≥2 个条目，或者碰上 `bin/` 这种内容目录。

    ⚠️ 这个函数**只能在空的暂存目录上调用**，不能在共享的安装目录上调用：
    共享目录里已经有别的组件（bin/ lib/ ...），「只有一个子目录」永远不成立，
    等于一层都不剥。verilator 第一次装就是这么失败的——yosys 已经占住了
    `.tools/oss`，verilator 解出来的 `_outputs/...` 原封不动留在那里。
    所以 install_one 走的是「解压到暂存目录 → 剥 → 合并进安装目录」。

    返回剥掉的层数，便于报告和测试。
    """
    stripped = 0
    for _ in range(FLATTEN_MAX_DEPTH):
        entries = [e for e in dest.iterdir() if not e.name.startswith(".")]
        if len(entries) != 1 or not entries[0].is_dir():
            break
        inner = entries[0]
        if inner.name.lower() in FLATTEN_CONTENT_DIRS:
            break
        if verbose:
            print(f"  [整理] 去掉包裹层 {inner.name}/")
        for item in list(inner.iterdir()):
            shutil.move(str(item), str(dest / item.name))
        inner.rmdir()
        stripped += 1
    return stripped


def _merge_into(src: Path, dst: Path) -> int:
    """把 src 的内容合并进 dst，目录同名就递归合并、文件同名就覆盖。

    不能用 shutil.move(src, dst)：dst 已存在且是目录时，move 会把整个 src
    **塞进** dst 里面，变成 `dst/src/bin/...`，路径又深了一层。
    OSS CAD Suite 的组件要共用一个 `.tools/oss`（verilator 和 yosys 都往
    `bin/` `lib/` 里放东西），所以必须按目录递归合并。

    返回移动过去的条目数。
    """
    moved = 0
    for item in list(src.iterdir()):
        target = dst / item.name
        if item.is_dir() and target.is_dir():
            moved += _merge_into(item, target)
            item.rmdir()
        else:
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(item), str(target))
            moved += 1
    return moved


# ---------------------------------------------------------------- 安装


def install_one(sw, verbose: bool = True, lock: bool = False) -> tuple[bool, str]:
    """装一个软件。返回 (是否成功, 说明)。"""
    method = sw.raw.get("install", {}).get("method", "")
    dest = sw.install_dest

    if sw.windows == "wsl-only":
        return False, f"{sw.name} 在 Windows 原生不可用（需 WSL2）。本仓库默认不装 WSL。"
    if method == "venv-pip":
        return _install_pip(sw, verbose)
    if not dest:
        return False, f"{sw.id} 没写 install.dest，无法确定安装位置"

    archive = download(sw, CACHE_DIR, verbose)
    digest = sha256_of(archive)
    if lock:
        _write_lock(sw.id, sw.raw.get("source", {}), digest)

    # 解压到**暂存目录**再剥包裹层，最后合并进安装目录。
    #
    # 为什么不直接解压到 dest：多个组件共用同一个 dest（OSS CAD Suite 的
    # yosys / verilator / formal 都往 .tools/oss 里放），
    # 而 _flatten_single_dir 靠「只有一个子目录」来判断包裹层。dest 里已经有
    # 前一个组件留下的 bin/ lib/ 之后，这个条件永远不成立，包裹层就一层都剥不掉。
    # verilator 第一次装正是这么失败的：解出来的 `_outputs/...` 原封不动留下。
    if method not in ("extract-tgz", "extract-zip"):
        return False, f"未知的安装方式：{method!r}"

    staging = Path(tempfile.mkdtemp(prefix=f".staging-{sw.id}-", dir=dest.parent))
    try:
        if method == "extract-tgz":
            extract_tgz(archive, staging, verbose)
        else:
            extract_zip(archive, staging, verbose)
        # 包裹层数取决于打包方，与归档格式无关，两种都要整理
        _flatten_single_dir(staging, verbose)
        moved = _merge_into(staging, dest)
        if verbose:
            print(f"  [合并] {moved} 个条目 -> {dest}")
    finally:
        # 合并完暂存目录应该是空的；没清干净说明有东西没搬过去，
        # 这时候删掉就等于静默丢文件，所以留着让人能看见
        if staging.exists():
            leftovers = list(staging.iterdir())
            if leftovers:
                print(
                    f"  [警告] 暂存目录还有 {len(leftovers)} 个条目没合并："
                    f"{[p.name for p in leftovers][:5]} —— 保留在 {staging}",
                    file=sys.stderr,
                )
            else:
                shutil.rmtree(staging, ignore_errors=True)

    # 装完立刻探测，确认真的可用——不能只看「文件都解出来了」就算成功
    pr = probe(sw)
    if pr.found:
        ver = f"  {pr.version}" if pr.version else ""
        return True, f"{sw.name} 就绪：{pr.exe_path}{ver}"
    return False, (
        f"{sw.name} 解压完成但探测不到可执行文件"
        f"（找的是 {sw.check_exe}，找过：{', '.join(pr.searched[-3:])}）"
    )


def _install_pip(sw, verbose: bool = True) -> tuple[bool, str]:
    """pip 类工具：建仓库私有 venv 再装，绝不碰系统 site-packages。"""
    import subprocess

    venv_dir = sw.install_dest
    assert venv_dir is not None
    py = venv_dir / ("Scripts" if sys.platform == "win32" else "bin") / (
        "python.exe" if sys.platform == "win32" else "python"
    )
    if not py.is_file():
        if verbose:
            print(f"  [venv] 创建仓库私有虚拟环境 {venv_dir}")
        rc = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)]).returncode
        if rc != 0:
            return False, f"创建 venv 失败（退出码 {rc}）"

    pkg = sw.raw.get("source", {}).get("package", sw.id)
    if verbose:
        print(f"  [pip ] {py} -m pip install {pkg}")
    rc = subprocess.run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check", pkg]
    ).returncode
    if rc != 0:
        return False, f"pip install {pkg} 失败（退出码 {rc}）"
    return True, f"{sw.name} 已装入 {venv_dir}（仓库私有，未影响系统 Python）"


def _write_lock(sid: str, src: dict, digest: str) -> None:
    data = {}
    if LOCK_PATH.is_file():
        try:
            data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data[sid] = {
        "repo": src.get("repo"),
        "tag": src.get("tag"),
        "asset": src.get("asset"),
        "sha256": digest,
    }
    LOCK_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"  [锁定] {sid} sha256={digest[:16]}… 记入 {LOCK_PATH.name}")


# ---------------------------------------------------------------- 报告


def do_list(manifest) -> int:
    probes = {s.id: probe(s) for s in manifest.software}
    print(f"清单版本 {manifest.schema}（更新于 {manifest.updated}）\n")
    print(f"{'id':<22} {'名称':<24} {'状态':<10} {'体积':>9}  Windows 可得性")
    print("─" * 100)
    for sw in manifest.software:
        p = probes[sw.id]
        state = "已安装 ✓" if p.found else "未安装"
        size = f"{sw.size_mb} MB" if sw.size_mb else "—"
        from _manifest import WINDOWS_LABEL

        print(f"{sw.id:<22} {sw.name:<24} {state:<10} {size:>9}  {WINDOWS_LABEL.get(sw.windows, sw.windows)}")
    print("\n安装：python tools/setup_tools.py --install <id> [<id>...]")
    print("体检：python tools/check_tools.py --level L2")
    return EXIT_OK


def do_install(manifest, ids: list[str], verbose: bool, lock: bool) -> int:
    by_id = {s.id: s for s in manifest.software}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        print(f"清单里没有这些 id：{', '.join(unknown)}", file=sys.stderr)
        print(f"可用：{', '.join(by_id)}", file=sys.stderr)
        return EXIT_USAGE

    ok_count = fail_count = 0
    for sid in ids:
        sw = by_id[sid]
        print(f"\n── {sw.name} ({sid}) " + "─" * 40)
        try:
            ok, msg = install_one(sw, verbose, lock)
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            ok, msg = False, f"失败：{type(exc).__name__}: {exc}"
        print(("  ✓ " if ok else "  ✗ ") + msg)
        ok_count += ok
        fail_count += not ok

    print(f"\n完成：{ok_count} 成功，{fail_count} 失败")
    print("装完请跑：python tools/check_tools.py --level L2")
    return EXIT_OK if fail_count == 0 else EXIT_FAIL


def do_for_level(manifest, level: str, verbose: bool, lock: bool) -> int:
    from _manifest import summarize_for_level

    probes = {s.id: probe(s) for s in manifest.software}
    summary = summarize_for_level(manifest, level, probes)
    ids = [s["id"] for s in summary["missing_software"]]
    if not ids:
        print(f"{level} 没有缺失项，无需安装。")
        return EXIT_OK
    print(f"{level} 缺 {len(summary['unmet'])} 项能力，需要这些软件：{', '.join(ids)}\n")
    return do_install(manifest, ids, verbose, lock)


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    init_console()
    ap = argparse.ArgumentParser(
        description="按 tools/toolchain.json 安装工具到仓库私有目录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python tools/setup_tools.py --list\n"
            "  python tools/setup_tools.py --install yosys verilator --lock\n"
            "  python tools/setup_tools.py --for-level L2\n"
        ),
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="列出清单里的软件与安装状态")
    g.add_argument("--install", nargs="+", metavar="ID", help="安装指定的软件")
    g.add_argument("--for-level", choices=["L1", "L2", "L3"], help="装某一级别缺失能力所需的软件")
    ap.add_argument("--lock", action="store_true", help="记录 sha256 到 .tools/tools.lock.json")
    ap.add_argument("--quiet", action="store_true", help="少打印")
    args = ap.parse_args(argv)

    try:
        manifest = load()
    except ManifestError as exc:
        print(f"清单加载失败：{exc}", file=sys.stderr)
        return EXIT_ENV
    problems = validate(manifest)
    if problems:
        print("清单自洽性校验未通过：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return EXIT_ENV

    if args.list:
        return do_list(manifest)
    if args.for_level:
        return do_for_level(manifest, args.for_level, not args.quiet, args.lock)
    return do_install(manifest, args.install, not args.quiet, args.lock)


if __name__ == "__main__":
    sys.exit(main())
