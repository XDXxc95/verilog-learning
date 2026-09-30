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

★ 锁写在 **tools/tools.lock.json**（入库，跟着 git 走），不是 `.tools/` 里。
   这是 2026-09-29 改的：旧位置 `.tools/tools.lock.json` 被 .gitignore 忽略，
   于是锁**从来没进过库**——作者这台机器受锁保护，别人克隆下来手里没锁，
   `_lock_problem` 查不到记录就一律放行，**两边都报"安装成功"**。
   判断"判题环境一致"靠的就是这把锁，它必须在库里。
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

# 锁定文件：记录装过的版本与 sha256。
#
# ★ 它**必须放在入库的路径下**，不能跟其他东西一起待在 `.tools/` 里。
# 2026-09-29 之前它在 `.tools/tools.lock.json`，而 `.gitignore` 忽略整个
# `.tools/` —— 于是**锁本身从来没有进过库**。后果不是"少了一份文件"，是
# 锁对作者生效、对别人不生效：新克隆的人手里没有锁，`_lock_problem` 查不到
# entry 就 `return None`（"第一次装，天经地义"），每次安装都放行。
# **两边都报「安装成功」，一边真被保护、一边没有** —— 正是本仓库反复在防的
# 那类谎，而且它长得跟正常一模一样。
#
# 放在 tools/ 下的理由：它描述的是 `tools/toolchain.json` 里那批下载源，
# 跟清单是同一件事的两面，理应挨着；且 `tools/` 本来就在版本控制里。
LOCK_PATH = repo_root() / "tools" / "tools.lock.json"

# 旧位置。只给 `_migrate_legacy_lock()` 用一次，之后可以删掉这段。
_LEGACY_LOCK_PATH = repo_root() / ".tools" / "tools.lock.json"


def _gh_url(src: dict) -> str:
    return f"https://github.com/{src['repo']}/releases/download/{src['tag']}/{src['asset']}"


# ---------------------------------------------------------------- 下载


class TruncatedDownload(OSError):
    """连接没报错，但读到的字节数少于服务端声明的长度。

    刻意继承 OSError，好让 download() 的既有重试分支顺手接住它 —— 截断跟
    "连接被掐断"是同一件事，处理方式也该一样：保留 .part，下次从这里续传。
    """


def _download_once(url: str, part: Path) -> int | None:
    """一次下载尝试，从 part 的现有长度续传。失败会抛异常，part 保留。

    返回服务端声明的**总长度**（拿不到就是 None），供调用方复核。
    """
    resume_from = part.stat().st_size if part.is_file() else 0
    headers = {"User-Agent": "verilog-learning-setup/1.0"}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        # 只有服务端真按 Range 返回 206 才能续写，否则得从头来。
        # ★ 先定 mode 再算 total，顺序不能反：服务端可能**无视 Range 返回 200**
        #   （Content-Length 是整份长度），这时若照旧把它加上 resume_from，
        #   读满也会判成截断 —— 修一个 bug 造一个 bug。
        mode = "ab" if resume_from and resp.status == 206 else "wb"
        decl = resp.headers.get("Content-Length")
        total = 0
        if decl:
            total = int(decl) + (resume_from if mode == "ab" else 0)
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

    # ★ 这一段是必需的，不是保险。resp.read() 在「连接被掐断」和「文件正常读完」
    #   两种情况下**都返回空串**，循环都 break，函数都正常返回 —— 不比对就等于
    #   把截断当成功。2026-09-29 实测栽过：572 MB 的 oss-cad-suite 下到 540 MB
    #   被掐断，.part 照样改名成正式文件、退出码 0，少 32 MB 谁也不知道；
    #   上一版唯一的防线是下面那个 gzip 魔数检查，而魔数在前两个字节就写对了。
    #   这是拿 found 当 usable，跟 lint.ran / n_warnings=0 是同一个坑。
    if total and done != total:
        raise TruncatedDownload(
            f"连接提前结束：读到 {done} 字节，服务端声明 {total} 字节"
            f"（差 {total - done} 字节）。.part 保留，下次续传。"
        )
    return total or None


# 清单里的 size_mb 是四舍五入到 0.1 MB 的，留 1 MB 余量足够吸收舍入误差，
# 又远小于任何一种真实截断（实测那次差 32 MB）。
SIZE_SLACK = 1 * 1024 * 1024


def _size_shortfall(path: Path, src: dict) -> str | None:
    """文件比清单声明的体积小了不止一点 → 返回人话说明；否则 None。

    只查"小了"不查"大了"：rolling tag 的同名资产体积会变，往上比会误报。
    清单没写 size_mb 就返回 None（不猜）。
    """
    mb = src.get("size_mb")
    if not mb:
        return None
    expect = int(float(mb) * 1024 * 1024)
    actual = path.stat().st_size
    if actual >= expect - SIZE_SLACK:
        return None
    return (
        f"只有 {actual / 1048576:.1f} MB，清单声明 {mb} MB "
        f"（少了 {(expect - actual) / 1048576:.1f} MB）"
    )


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
        # ★ 缓存命中也要复核大小。上面那个 1 MB 只是"不像个错误页面"，
        #   不足以说明文件是完整的：截断到一半的 540 MB 同样 > 1 MB。
        #   这里拿清单里的 size_mb 当**下界**（只查"小了"，不查"大了"）——
        #   size_mb 是四舍五入到 0.1 MB 的，且 bucket-* 是滚动 tag 体积会变，
        #   往上比会误报，往下比才是真的坏了。
        short = _size_shortfall(target, src)
        if short is None:
            if verbose:
                print(f"  [跳过] 已下载 {src['asset']}")
            return target
        if verbose:
            print(f"  [重下] 缓存里的 {src['asset']} {short}")
        target.replace(target.with_suffix(target.suffix + ".part"))

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

    # 兜底：Content-Length 缺失（分块传输）时上面那道精确比对不成立，
    # 这时清单里的 size_mb 是唯一的尺子。宁可判"下坏了"让它续传，
    # 也不要把半截包交给解压 —— 解压到一半失败，报的会是 tar 的错，
    # 看起来像归档损坏，实际是下载没完。
    short = _size_shortfall(part, src)
    if short is not None:
        raise RuntimeError(
            f"{src['asset']} 下载不完整：{short}。.part 保留，重跑会续传。"
        )

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
    # ★ dst 可能还不存在 —— **第一次**装某个独立包时就是这样
    # （OSS 套件那几条走不到这里：`.tools/oss` 由先装的组件建出来了，
    #  所以这个坑一直躲着没暴露）。不建的话下面第一句 shutil.move 就抛
    # FileNotFoundError，而报错信息是「系统找不到指定的路径」，
    # 完全指不到「目标目录没建」这件事上。
    # 2026-09-29 装 verible 时实测踩到：文件全解压好了、锁也写了，
    # 合并阶段一个都没搬出去，`.tools/verible` 根本没生成。
    dst.mkdir(parents=True, exist_ok=True)

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


def _already_installed(sw) -> tuple[bool, str]:
    """已经装好且**能证明**吗？返回 (是否短路, 说明给人看)。

    ★ 为什么非要有这个：setup_tools 原先每次调用都完整解压一遍。
    2026-09-29 跑 `--install yosys --lock` 就白解压了 16953 个条目、白写 2 GB ——
    不报错，只是慢，最难发现的那种浪费。setup_toolchain.py 早就有这个短路
    （CLAUDE.md 写着「工具链就位时直接短路、绝不重装」），setup_tools 一直没有。

    ★ 判据是「能证明」，不是「目录在」。没有 check.exe 的条目（oss-utils 之类）
    探测不出任何东西，probe 的 found 只反映「安装目录存在」——拿它短路，
    就等于把「证明不了」当成「已经装好」，正是本仓库反复在防的那件事。

    但也不能反过来「证明不了就每次都重装」——那要白解压 2 GB，而这两个条目
    根本不单独安装，它们随整包一起来。取舍是：**照样短路，但话必须说弱**，
    印「目录在，证明不了」而不是「自检通过」。判据弱一档，措辞就得弱一档。
    """
    if sw.check_exe:
        pr = probe(sw)
        if pr.found and pr.usable:
            ver = f"  {pr.version}" if pr.version else ""
            return True, f"{sw.name} 已安装且自检通过，跳过{ver}（要重装加 --force）"
        return False, ""

    # 没有可执行文件可探测的条目（公共库 / 公共资源）。只能看安装目录在不在。
    #
    # ★ 证据强度不一样，**话就不能一样**：这里印的是「目录非空」，
    #   不是「自检通过」。混成同一句话，就是把 found 当 usable，
    #   而这两条恰好共用 .tools/oss —— 目录存在只是因为 yosys 在那儿。
    #   但也不该反过来「因为证明不了就每次都重装」：那要白白解压 2 GB，
    #   而这两个条目根本不会单独安装（它们随整包一起来）。
    dest = sw.install_dest
    if dest and dest.is_dir():
        try:
            if any(dest.iterdir()):
                return True, (
                    f"{sw.name} 的安装目录 {dest} 非空，跳过 —— "
                    f"⚠ 这条没有 check.exe，**只是目录在，证明不了组件真的装了**"
                    f"（要重装加 --force）"
                )
        except OSError:
            return False, ""
    return False, ""


def install_one(
    sw, verbose: bool = True, lock: bool = False, force: bool = False
) -> tuple[bool, str]:
    """装一个软件。返回 (是否成功, 说明)。"""
    method = sw.raw.get("install", {}).get("method", "")
    dest = sw.install_dest

    if sw.windows == "wsl-only":
        return False, f"{sw.name} 在 Windows 原生不可用（需 WSL2）。本仓库默认不装 WSL。"
    if method == "venv-pip":
        return _install_pip(sw, verbose)
    if not dest:
        return False, f"{sw.id} 没写 install.dest，无法确定安装位置"

    if not force:
        skip, why = _already_installed(sw)
        if skip:
            if verbose:
                print(f"  [跳过] {why}")
            return True, why

    archive = download(sw, CACHE_DIR, verbose)
    digest = sha256_of(archive)

    src = sw.raw.get("source", {})
    if not force:
        problem = _lock_problem(sw.id, src, digest)
        if problem:
            # 拒绝装，而不是装完再警告。锁的作用就是在**装上之前**拦住，
            # 装完再说「其实这份不对」已经晚了——坏的工具已经在 .tools/ 里了。
            return False, problem
    if lock:
        old = _read_lock().get(sw.id) or {}
        if old.get("sha256") and old.get("sha256") != digest and old.get("asset") == src.get("asset"):
            print(f"  [警告] 覆盖锁记录：{old['sha256'][:16]}… -> {digest[:16]}…")
        _write_lock(sw.id, src, digest)

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


def _read_lock() -> dict:
    """读锁文件。坏了就当空的——但**要说出来**，不能静默降级。

    静默降级在这里格外危险：锁坏了 → 读到空 → 校验全部"通过"，
    而实际情况是一个字节都没查。和 check_manifest 负例 1 是同一类坑。
    """
    if not LOCK_PATH.is_file():
        return {}
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"  [警告] {LOCK_PATH.name} 不是合法 JSON（{exc}），锁校验这次全部跳过",
              file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"  [警告] {LOCK_PATH.name} 顶层不是对象，锁校验这次全部跳过",
              file=sys.stderr)
        return {}
    return data


def _migrate_legacy_lock(verbose: bool = True) -> bool:
    """把旧位置（`.tools/tools.lock.json`）的锁搬到入库的 `tools/` 下。搬了返回 True。

    ★ 为什么要有这一步：锁从 `.tools/` 搬到 `tools/` 那天，**已经在用的人**
    （包括作者这台机器）手里的锁还在旧位置。不搬的话，新路径读不到东西 →
    校验全部静默放行 → 他们从"受保护"悄悄退回"没保护"，而且**看不出任何变化**。
    升级路径上的静默退化，比一开始就没有更坏：人会以为保护还在。

    只在**新位置还没有文件**时才搬。两边都有的话以新的为准——那份是入库的、
    大家共用的，旧的只是本机残留。
    """
    if LOCK_PATH.exists() or not _LEGACY_LOCK_PATH.is_file():
        return False
    try:
        data = json.loads(_LEGACY_LOCK_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"  [警告] 旧锁 {_LEGACY_LOCK_PATH} 不是合法 JSON（{exc}），没有搬。"
              f"它会继续被忽略——要锁定请重新用 --lock 装一次。", file=sys.stderr)
        return False
    if not isinstance(data, dict):
        print(f"  [警告] 旧锁 {_LEGACY_LOCK_PATH} 顶层不是对象，没有搬。", file=sys.stderr)
        return False
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if verbose:
        print(f"  [迁移] 锁从 {_LEGACY_LOCK_PATH.name} 搬到 {LOCK_PATH}（{len(data)} 条）"
              f"—— 旧文件留在原地不动，可以自己删。\n"
              f"         搬的理由：旧位置在 .tools/ 里，被 .gitignore 忽略，"
              f"所以那把锁**从来没进过库**，别人克隆下来是没锁的。")
    return True


def _lock_problem(sid: str, src: dict, digest: str) -> str | None:
    """比对锁记录与这次实算的 sha256。返回问题描述；没问题或没记录返回 None。

    ★ 这个方法存在的理由：`--lock` 的文档一直写着「记下 sha256，之后靠它校验」，
    但在 2026-09-29 之前**全仓库没有任何代码读这个文件**——锁只写不读，
    是一张没人查的收据。而那一周刚刚发生过一次静默截断（572 MB 下到 540 MB
    照样报成功），锁本来就是防这个的。
    """
    entry = _read_lock().get(sid)
    if not entry:
        return None
    recorded = entry.get("sha256")
    if not recorded:
        return f"锁里 {sid} 那条没有 sha256 字段"
    # 资产换了（例如分组件 → 整包）就没有可比性：记录的是另一份文件。
    # 这不是错误，但也不能算"校验通过"，得说清楚。
    if entry.get("asset") != src.get("asset"):
        return None
    if recorded == digest:
        return None
    return (
        f"{src.get('asset')} 的 sha256 与锁里记的不一致：\n"
        f"        锁里：{recorded}\n"
        f"        实算：{digest}\n"
        f"        同名资产内容变了（滚动 tag 每天覆盖），或下载被篡改/截断。\n"
        f"        确认要接受新的这份就加 --force。"
    )


def _write_lock(sid: str, src: dict, digest: str) -> None:
    data = _read_lock()
    data[sid] = {
        "repo": src.get("repo"),
        "tag": src.get("tag"),
        "asset": src.get("asset"),
        "sha256": digest,
    }
    LOCK_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        # ★ 必须显式指定。默认 newline=None 会把 \n 翻成 \r\n，而这个文件是
        #   入库的 —— 同一把锁在 Windows 上写出来带 CRLF、在 Linux 上不带，
        #   每次跨平台 --lock 都会产生一份「整个文件都改了」的假 diff。
        #   （toolchain.json 那次整文件 diff 就是这么来的。）
        newline="\n",
    )
    print(f"  [锁定] {sid} sha256={digest[:16]}… 记入 {LOCK_PATH.name}")


# ---------------------------------------------------------------- 报告


def do_list(manifest) -> int:
    probes = {s.id: probe(s) for s in manifest.software}
    print(f"清单版本 {manifest.schema}（更新于 {manifest.updated}）\n")
    # ★ 多条指向同一个归档时，体积只在**第一条**打印，其余标成"↳ 同上"。
    #   否则整包里的 7 个工具每个都显示 572.2 MB，读起来像要下 7 次 ——
    #   实际 do_install 按 (repo,tag,asset) 判重，只解压一次。
    #   体积列的含义是「装它要下多少」，不是「它自己多大」。
    from _manifest import WINDOWS_LABEL

    asset_owner: dict[tuple, str] = {}
    groups: dict[tuple, list[str]] = {}
    for sw in manifest.software:
        src = sw.raw.get("source", {})
        if src.get("asset"):
            groups.setdefault(
                (src.get("repo"), src.get("tag"), src.get("asset")), []
            ).append(sw.id)

    print(f"{'id':<22} {'名称':<24} {'状态':<10} {'下载量':>9}  Windows 可得性")
    print("─" * 100)
    unprovable: list[str] = []
    for sw in manifest.software:
        p = probes[sw.id]
        if not sw.check_exe:
            # ★ 没有 check.exe 时 probe 的 found 只反映「安装目录存在」，
            #   它的 detail 自己都写着「不能证明组件真的装了」。
            #   这种条目印成「已安装 ✓」就是把免责声明丢掉、只留结论。
            state = "未知 ?" if p.found else "未安装"
            if p.found:
                unprovable.append(sw.id)
        else:
            state = "已安装 ✓" if p.found else "未安装"
        src = sw.raw.get("source", {})
        key = (src.get("repo"), src.get("tag"), src.get("asset"))
        if src.get("asset") and key in asset_owner:
            size = f"↳{asset_owner[key]}"
        else:
            size = f"{sw.size_mb} MB" if sw.size_mb else "—"
            if src.get("asset"):
                asset_owner[key] = sw.id

        print(f"{sw.id:<22} {sw.name:<24} {state:<10} {size:>9}  {WINDOWS_LABEL.get(sw.windows, sw.windows)}")
    # 只报**真的**有多方共用的那几组。按 key 分组再筛 len>1，
    # 别拿 asset_owner.values() 当"共用者"——那是所有归档的拥有者，
    # 会把 mingw / slang / verible 这些独占自己归档的条目也列进去。
    for key, ids_in_group in groups.items():
        if len(ids_in_group) > 1:
            print(f"\n注：{', '.join(ids_in_group)} 共用同一个归档（{key[2]}），"
                  f"安装时只下载、只解压一次。")
    if unprovable:
        print(f"\n注：{', '.join(unprovable)} 显示「未知 ?」是因为它们没有可执行文件可探测，"
              f"只能看到安装目录在不在——**目录在 ≠ 组件真的装了**。")
    print("\n安装：python tools/setup_tools.py --install <id> [<id>...]")
    print("体检：python tools/check_tools.py --level L2")
    return EXIT_OK


def do_verify_lock(manifest) -> int:
    """把缓存里的归档与锁里记的 sha256 对一遍。

    ★ 三类结果必须**分开报**，尤其是后两类：`n_ok` / `n_bad` / `n_unknown`。
    「缓存里没有这份归档」既不是通过也不是失败——把它并进任何一边都是谎报：
    并进 ok 就是拿"没查"冒充"查过了"，并进 bad 就是拿"没查"冒充"坏了"。
    和 lint.ran / n_warnings、cov 的 no_cover / 0 分是同一个教训。
    """
    data = _read_lock()
    if not data:
        print(f"{LOCK_PATH.name} 里一条记录都没有。**这不是「校验通过」**，"
              f"是压根没有可校验的东西。\n"
              f"（装工具时加 --lock 才会记；锁记录的是「这个归档的 sha256 应该是多少」）")
        return EXIT_OK

    # asset -> 用它的那些 id 的**集合**。不能是单个 id：整包那次之后，
    # 有 7 条指向同一个归档，只留最后一个的话，其余 6 条会被误报成「锁过期」。
    manifest_assets: dict[str, set[str]] = {}
    for s in manifest.software:
        a = s.raw.get("source", {}).get("asset")
        if a:
            manifest_assets.setdefault(a, set()).add(s.id)
    n_ok = n_bad = n_unknown = 0
    print(f"校验 {LOCK_PATH.name}（{len(data)} 条）\n")
    for sid, entry in sorted(data.items()):
        asset = entry.get("asset")
        want = entry.get("sha256") or ""
        if not asset:
            print(f"  ? {sid:24} 锁里没写 asset，无从校验")
            n_unknown += 1
            continue
        users = manifest_assets.get(asset)
        if users is not None and sid not in users:
            print(f"  ! {sid:24} 锁指着 {asset}，而清单里这份只归 "
                  f"{', '.join(sorted(users))} —— 锁过期了")
        archive = CACHE_DIR / asset
        if not archive.is_file():
            print(f"  ? {sid:24} 缓存里没有 {asset}，**无法校验**（不是通过）")
            n_unknown += 1
            continue
        got = sha256_of(archive)
        if got == want:
            print(f"  ✓ {sid:24} {got[:16]}…")
            n_ok += 1
        else:
            print(f"  ✗ {sid:24} 锁里 {want[:16]}…  实际 {got[:16]}…")
            n_bad += 1

    print(f"\n通过 {n_ok}，不一致 {n_bad}，无法校验 {n_unknown}")
    if n_bad:
        print("不一致意味着同名资产的内容变了（滚动 tag 每天覆盖），或下载被篡改/截断。")
        return EXIT_FAIL
    if n_unknown and not n_ok:
        print("一条都没校验成——别把这次结果读成「锁是干净的」。")
    return EXIT_OK


def do_install(manifest, ids: list[str], verbose: bool, lock: bool,
               force: bool = False) -> int:
    by_id = {s.id: s for s in manifest.software}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        print(f"清单里没有这些 id：{', '.join(unknown)}", file=sys.stderr)
        print(f"可用：{', '.join(by_id)}", file=sys.stderr)
        return EXIT_USAGE

    ok_count = fail_count = 0
    # ★ 同一个归档只装一次。清单里 yosys / verilator / oss-utils /
    #   oss-system-resources 四项**指向同一个整包**（都往 .tools/oss 里放东西），
    #   不去重就会把同一份 16953 条目的 tgz 解压四遍、往磁盘写四遍 2 GB。
    #   它不报错，只是白干——正是最不容易被发现的那种浪费。
    #   按 (repo, tag, asset) 判重，不按 id：判的是"是不是同一份文件"。
    done_assets: dict[tuple, str] = {}
    for sid in ids:
        sw = by_id[sid]
        print(f"\n── {sw.name} ({sid}) " + "─" * 40)
        src = sw.raw.get("source", {})
        key = (src.get("repo"), src.get("tag"), src.get("asset"))
        if src.get("asset") and key in done_assets:
            print(f"  [跳过] 与 {done_assets[key]} 是同一个归档（{src['asset']}），不重复解压")
            ok_count += 1
            continue
        try:
            ok, msg = install_one(sw, verbose, lock, force)
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            ok, msg = False, f"失败：{type(exc).__name__}: {exc}"
        print(("  ✓ " if ok else "  ✗ ") + msg)
        ok_count += ok
        fail_count += not ok
        if ok and src.get("asset"):
            done_assets[key] = sid

    print(f"\n完成：{ok_count} 成功，{fail_count} 失败")
    print("装完请跑：python tools/check_tools.py --level L2")
    return EXIT_OK if fail_count == 0 else EXIT_FAIL


def do_for_level(manifest, level: str, verbose: bool, lock: bool,
                 force: bool = False) -> int:
    from _manifest import summarize_for_level

    probes = {s.id: probe(s) for s in manifest.software}
    summary = summarize_for_level(manifest, level, probes)
    ids = [s["id"] for s in summary["missing_software"]]
    if not ids:
        print(f"{level} 没有缺失项，无需安装。")
        return EXIT_OK
    print(f"{level} 缺 {len(summary['unmet'])} 项能力，需要这些软件：{', '.join(ids)}\n")
    return do_install(manifest, ids, verbose, lock, force)


def _locked_assets() -> set[str]:
    """锁里出现过（且记了 sha256）的归档名。**按资产不按 id** 判覆盖。

    ★ 为什么不能按 id 判：整包那次之后，8 个条目共用同一个 tgz，而锁里只会
    留下其中几个（谁加过 --lock 是谁）。按 id 判的话，formal / eqy / z3 / slang
    会被报成「没锁」—— 可它们的文件跟 yosys 是**同一份**，已经被锁住了。
    报成缺口就是凭空造一个不存在的洞，跟漏报一个真实的洞一样坏。
    """
    return {
        e["asset"]
        for e in _read_lock().values()
        if isinstance(e, dict) and e.get("asset") and e.get("sha256")
    }


def do_restore_lock(manifest, verbose: bool, force: bool = False) -> int:
    """照 tools/tools.lock.json 把软件装回来 —— 「还原成跟作者一致的那一套」。

    ★ 为什么单开一条命令，而不是让人照着锁一条条 --install：
      `--for-level L2` 装的是「L2 需要什么」，`--install X` 装的是「我要 X」。
      两者都不是「**装成锁里记的那一份**」。前者随清单变动而变，后者靠人眼抄 id
      —— 抄漏一条，环境就悄悄不一致了，而且**两边都看不出来**
      （跟锁躺在 .tools/ 里那次的失效方式一模一样）。

    ★ 这条命令最要紧的不是它装了什么，是它**明说自己装不了什么**。
      锁覆盖的只有「有 source.asset 且加过 --lock」的那些；iverilog 走的是
      另一个安装器（setup_toolchain.py），**没有 sha256、根本不在锁里**。
      所以还原完的环境**不一定和作者一致**。差在哪必须印出来 ——
      印一句「环境已还原」而实际差着 iverilog，正是本仓库最防的那种谎。

    ★ 还原时**不写锁**（`lock=False`）。这条命令的方向是「照锁装」；顺手把这次
      下载的 sha256 记进去，就把「参照物」变成「本次结果」了，锁也就不再是锁。
      `--force` 是唯一的例外，那条路本来就明说了要覆盖。

    ★ **`do_install` 对「已装」的条目会短路，而短路发生在锁校验之前**
      （`install_one` 里 `_already_installed` 在前、`_lock_problem` 在后），
      所以只用 `do_install` 的话，**本来就装着的那几条一次都不会碰锁** ——
      在一台装好的机器上，「N 条按锁还原」这句话**一条都没核对过**。
      2026-09-29 实测抓到：把锁里 yosys 的 sha256 改成 64 个 0，
      `--verify-lock` 正确报「不一致 1」，`--restore-lock` 却照样印「完成：6 条按锁还原」。
      **所以这条命令末尾必须补一次 `do_verify_lock`** —— 那才是「还原」这两个字的凭据。
      这就是本仓库那条「两个计数必须分开报」：新下的条数 ≠ 已装且没核对的条数。
    """
    data = _read_lock()
    if not data:
        print(
            f"{LOCK_PATH} 里一条记录都没有 —— **没有可还原的东西**。\n"
            f"这不是「环境已还原」，是这把锁是空的。\n"
            f"（要么还没人加过 --lock，要么文件没跟着 git 拉下来；"
            f"确认它在库里：git ls-files {LOCK_PATH.name}）",
            file=sys.stderr,
        )
        return EXIT_ENV

    by_id = {s.id: s for s in manifest.software}
    known = [i for i in sorted(data) if i in by_id]
    stale = [i for i in sorted(data) if i not in by_id]

    print(f"照 {LOCK_PATH.name} 还原（锁里 {len(data)} 条，清单认得 {len(known)} 条）")
    if stale:
        print(f"  [警告] 锁里有、清单里没有的 id：{', '.join(stale)} —— 跳过。"
              f"多半是清单改过名字，这把锁比清单旧了。")

    # 先分区，好在报告里把「新下的」和「本来就装着的」分开说 ——
    # 后者的下载流程会短路，**锁校验根本不会执行**（见 docstring 里那条实测）。
    already: list[str] = [i for i in known if _already_installed(by_id[i])[0]]
    n_fresh = len(known) - len(already)

    rc = do_install(manifest, known, verbose, lock=False, force=force)

    # ---- ★ 补一次锁核对 ----------------------------------------------------
    # 这一步**不是可选的**。只跑 do_install 的话，已装的条目短路、锁一次没查，
    # 「照锁还原」就成了一句没有依据的话。do_verify_lock 拿缓存里的归档重算
    # sha256 和锁对，才是这条命令的凭据；它同时覆盖了刚才短路掉的那些。
    print(f"\n{'─' * 60}\n核对（新下 {n_fresh} 条，本来就装着 {len(already)} 条"
          f"——后者的下载流程碰不到锁，全靠这一次）\n")
    verify_rc = do_verify_lock(manifest)

    # ---- 还原完，把「这次没盖到的地方」摊开说 ----------------------------
    locked_assets = _locked_assets()
    uncovered: list[tuple[str, str]] = []   # (id, 为什么没盖到)
    manual: list[tuple[str, str]] = []
    n_wsl = 0
    for s in manifest.software:
        if s.raw.get("windows") == "wsl-only":
            n_wsl += 1
            continue
        src = s.raw.get("source", {})
        if not src.get("asset"):
            # ★ 这里**不能**拿 install.method 做过滤条件。iverilog 在清单里
            #   压根没有 install 段（它由 setup_toolchain.py 单独装），
            #   一带过滤就恰好把最该说的那条漏掉了 —— 第一版就是这么写的，
            #   结果报告里只有 cocotb，iverilog 一个字都没有。
            method = s.raw.get("install", {}).get("method")
            if method == "venv-pip":
                why = "装进仓库私有 venv（pip），没有可锁的归档"
            elif method:
                why = f"安装方式是 {method}，没有可锁的归档"
            else:
                why = "由 setup_toolchain.py 单独装（Inno Setup 包），**没有 sha256**"
            manual.append((s.id, why))
            continue
        if src["asset"] not in locked_assets:
            uncovered.append((s.id, f"{src['asset']} 没进锁"))

    print(f"\n{'─' * 60}")
    if not uncovered and not manual:
        print("锁覆盖到了清单里所有能锁的条目 —— 就版本一致性而言，这次还原是完整的。")
    else:
        # ★ 这两个桶必须分开印。「不归这把锁管」是设计如此，
        #   「没进锁」是缺口，混在一起会让人以为 iverilog 也是个待补的洞。
        if uncovered:
            print(f"⚠ 这些**能锁但没锁**，本次还原出来的版本可能与作者不同（{len(uncovered)} 条）：")
            for sid, why in uncovered:
                print(f"    {sid:24} {why}")
            print("  要补齐：在作者那台机器上 --install <id> --lock，把锁提交进库。")
        if manual:
            print(f"\nℹ 这些不归这把锁管，得单独装（{len(manual)} 条）：")
            for sid, why in manual:
                print(f"    {sid:24} {why}")
            if any(sid == "iverilog" for sid, _ in manual):
                print("  ★ 以上**最要紧的是 iverilog**：它是判定内核 sim.py 用的编译器，"
                      "L1 起唯一依赖，\n"
                      "    而 `INSTALLER_CANDIDATES` 在 v14 下不到时会**静默回退 v12(2022)**。\n"
                      "    也就是说这条还原出来的环境，判题版本可能和作者不同。"
                      "装完自己确认一眼：")
                print("      python tools/setup_toolchain.py   然后  .tools/iverilog/bin/iverilog -V")
        if n_wsl:
            print(f"\n（另有 {n_wsl} 条是 WSL2 专属，Windows 原生装不了，未计入上面两桶）")

    if rc != EXIT_OK:
        return rc
    if verify_rc != EXIT_OK:
        # 核对没过时**绝不说「还原完成」**。环境是不是作者那套，唯一凭据就是这次核对；
        # 它红了说明同名资产内容变了（滚动 tag）或下载被篡改/截断，那这份环境不可信。
        print("\n**核对没过，所以不能说「还原完成」** —— 上面标 ✗ 的那几条"
              "装出来的东西和锁里记的不是同一份。", file=sys.stderr)
        return verify_rc
    print(f"\n还原完成：{len(known)} 条全部与 {LOCK_PATH.name} 核对通过"
          f"（新下 {n_fresh} 条，复用已装 {len(already)} 条）。")
    print("接着体检：python tools/check_tools.py --level L3")
    return EXIT_OK


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
    g = ap.add_mutually_exclusive_group(required=False)
    g.add_argument("--list", action="store_true", help="列出清单里的软件与安装状态")
    g.add_argument("--install", nargs="+", metavar="ID", help="安装指定的软件")
    g.add_argument("--for-level", choices=["L1", "L2", "L3"], help="装某一级别缺失能力所需的软件")
    ap.add_argument("--restore-lock", action="store_true",
                    help="照 tools/tools.lock.json 把软件装回来，并报出锁没盖到的地方")
    ap.add_argument("--lock", action="store_true", help="记录 sha256 到 tools/tools.lock.json")
    ap.add_argument("--verify-lock", action="store_true",
                    help="把缓存里的归档与锁里记的 sha256 对一遍，不一致就退出码非 0")
    ap.add_argument("--force", action="store_true",
                    help="无视「已安装」短路与锁记录，强制重装（会覆盖锁）")
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

    # 只在入口处搬，不放 _read_lock 里：那样连单元测试（猴补丁 LOCK_PATH）
    # 都会顺手搬一份真锁进临时目录，读锁的函数不该有副作用。
    _migrate_legacy_lock(verbose=not args.quiet)

    if args.verify_lock:
        return do_verify_lock(manifest)
    if args.restore_lock:
        return do_restore_lock(manifest, not args.quiet, args.force)
    if args.list:
        return do_list(manifest)
    if args.for_level:
        return do_for_level(manifest, args.for_level, not args.quiet, args.lock, args.force)
    if args.install:
        return do_install(manifest, args.install, not args.quiet, args.lock, args.force)
    ap.print_help()
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
