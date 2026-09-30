"""iverilog 钉版本 / 哈希 的回归检查（tools/setup_toolchain.py）。

为什么单开这个文件：

    iverilog **不在** `tools/tools.lock.json` 里。那把锁管的是「有 source.asset
    的归档」，而 iverilog 走的是 Inno 安装包 + 「装到默认位置再整体搬进 .tools/」
    这条另路 —— 所以 `--verify-lock` 对它一个字都说不了。

    而它是 L1 起唯一依赖、也是 `sim.py` 判对错用的那个编译器。它一漂，
    学习者看到的是「跑通了但答案不一样」——最难查的一种。所以它的钉法单独一套，
    也就需要单独的负例：**钉了但钉不住**比没钉更坏，因为它会给出「已验证」的错觉。

这里守的每一条都对应一个真实会发生的失效：
    ① 哈希对不上必须**拒装**，而且不许安静地回落到下一个候选
       （回落的是 2022 年的 v12，另一份编译器）
    ② 「没有哈希记录」不许被当成「通过」
       （v12 就是这条 —— 作者没装过它，编一个哈希等于凭空造凭证）
    ③ 「装出来的版本」和「下载的包」是**两件事**，必须分开报
       （包对而版本不对：系统里另有一个 iverilog；版本对而包不知来源）
    ④ 回落必须吵
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from _console import init_console  # noqa: E402

init_console()

import setup_toolchain as ST  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def _capture(fn, *a, **kw) -> tuple[str, object]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*a, **kw)
    return out.getvalue() + err.getvalue(), rc


def _capture_exc(fn, *a, **kw) -> tuple[str, BaseException | None]:
    """跟 _capture 一样，但把异常**收下来**再连同输出一起返回。

    ★ 不能写 `try: _capture(...) except: pass` —— 异常一抛，`with redirect_stdout`
      就退出了，缓冲区里那点输出拿不到，于是「回落时有没有吭声」变成永远测不出。
      第一版就是这么写的，两条断言直接空字符串失败。
    """
    out, err = io.StringIO(), io.StringIO()
    exc: BaseException | None = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            fn(*a, **kw)
        except BaseException as e:  # noqa: BLE001 — 这里就要看抛的是哪一类
            exc = e
    return out.getvalue() + err.getvalue(), exc


# ------------------------------------------------ ① / ② check_installer 三分支
#
# 三取值分开报是本仓库的老账（lint.ran / n_warnings、--verify-lock 的
# 通过/不一致/无法校验）。并进 ok 就是拿「没查」冒充「查过了」。
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    f = tmp / "x.exe"
    f.write_bytes(b"MZ" + b"\0" * 64)

    real_map = ST.INSTALLER_SHA256
    try:
        import hashlib
        good = hashlib.sha256(f.read_bytes()).hexdigest()
        ST.INSTALLER_SHA256 = {"x.exe": good}
        st, msg = ST.check_installer("x.exe", f)
        check(st == "ok", f"哈希一致却没判 ok（收到 {st!r}）")

        ST.INSTALLER_SHA256 = {"x.exe": "0" * 64}
        st, msg = ST.check_installer("x.exe", f)
        check(st == "bad", f"哈希对不上却没判 bad（收到 {st!r}）")
        check("不装" in msg, f"哈希不符时没说清「不装」：{msg!r}")

        ST.INSTALLER_SHA256 = {"y.exe": good}
        st, msg = ST.check_installer("x.exe", f)
        check(st == "unknown", f"没有记录却没判 unknown（收到 {st!r}）")
        check("无法与作者比对" in msg,
              f"没有记录时没说清「这份没法比对」——会被读成通过：{msg!r}")
    finally:
        ST.INSTALLER_SHA256 = real_map

# ③ v12 在真清单里就是「没有记录」那一条。钉一个没下过的哈希 = 凭空造凭证。
check(ST.INSTALLER_CANDIDATES[0] in ST.INSTALLER_SHA256,
      "首选候选没有 sha256 记录 —— 最该钉的那一份反而没钉")
check(ST.INSTALLER_CANDIDATES[1] not in ST.INSTALLER_SHA256,
      "回落候选被钉了 sha256 —— 作者没下过它，这个哈希是哪来的？"
      "（真下过就把值补上并改掉这条断言，别编一个）")

# ------------------------------------------------ ① 哈希不符时不许被重试循环吞掉
#
# download_installer 的重试循环吞 `URLError / OSError / RuntimeError` 去试下一个
# 候选。若 InstallerRejected 也是 RuntimeError 的子类，被换过的 v14 会安静地
# 降级成「v14 不行，改下 v12」，而使用者只知道「最后装上了」。
check(not issubclass(ST.InstallerRejected, RuntimeError),
      "InstallerRejected 继承了 RuntimeError —— 会被重试循环吞掉，"
      "被篡改的包安静地变成「换个候选」")


class _FakeResp:
    """够用的 urlopen 假响应：只喂 download_installer 用到的几个成员。"""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._pos = 0
        self.status = 200
        self.headers = {"Content-Length": str(len(body))}

    def read(self, n: int = -1) -> bytes:
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


real_open, real_cands = ST.urllib.request.urlopen, ST.INSTALLER_CANDIDATES
real_map = ST.INSTALLER_SHA256
try:
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        # 内容合法（MZ 开头、> 1 MB），但哈希与记录不符 —— 正是「服务器上换了文件」
        body = b"MZ" + b"\0" * (1_100_000)
        ST.urllib.request.urlopen = lambda *a, **kw: _FakeResp(body)
        ST.INSTALLER_CANDIDATES = ["fake-a.exe", "fake-b.exe"]
        ST.INSTALLER_SHA256 = {"fake-a.exe": "0" * 64, "fake-b.exe": "0" * 64}

        _, exc = _capture_exc(ST.download_installer, cache, True)
        check(isinstance(exc, ST.InstallerRejected),
              f"哈希不符时抛的是 {type(exc).__name__}（{exc}）—— "
              f"应当是 InstallerRejected，硬停而不是换候选")
finally:
    ST.urllib.request.urlopen = real_open
    ST.INSTALLER_CANDIDATES = real_cands
    ST.INSTALLER_SHA256 = real_map

# 单独的回落横幅检查（上面那次会因为第一个候选就硬停，走不到横幅）
real_cands = ST.INSTALLER_CANDIDATES
real_open = ST.urllib.request.urlopen
try:
    with tempfile.TemporaryDirectory() as td:
        ST.INSTALLER_CANDIDATES = ["gone-a.exe", "gone-b.exe"]
        ST.urllib.request.urlopen = lambda *a, **kw: (_ for _ in ()).throw(
            urllib.error.URLError("offline"))
        out, exc = _capture_exc(ST.download_installer, Path(td), True)
        check("正在回落" in out,
              f"回落到第二个候选时一声不吭 —— 「装了 v12(2022)」和「装了 v14」"
              f"在下游长得一模一样：{out!r}")
        check("不是作者用的那一份" in out,
              f"回落横幅没点明这是另一份编译器：{out!r}")
        check(isinstance(exc, RuntimeError),
              f"两个候选都拿不到时没报失败（抛了 {type(exc).__name__}）")
finally:
    ST.INSTALLER_CANDIDATES = real_cands
    ST.urllib.request.urlopen = real_open

# ------------------------------------------------ ③ --verify 的两头分开判
real_detect, real_probe = ST.detect, ST.probe_version
real_cands, real_map = ST.INSTALLER_CANDIDATES, ST.INSTALLER_SHA256
try:
    class _TC:
        iverilog = Path("fake/iverilog.exe")
        vvp = None
        gtkwave = None

    ST.detect = lambda: _TC()
    ST.INSTALLER_CANDIDATES = ["nope.exe"]          # 缓存里没有 → 无法校验
    out, rc = _capture(ST.do_verify_installer)
    check("无法校验" in out,
          f"缓存里没有安装包时没报「无法校验」—— 会被读成通过：{out!r}")
    check("别把这次结果读成「包是好的」" in out,
          f"一条都没校验成时没提醒：{out!r}")

    ST.probe_version = lambda exe: "Icarus Verilog version 12.0 (stable) ()"
    out, rc = _capture(ST.do_verify_installer)
    check(rc != 0, "装的是 v12（不是钉住的版本）却返回了成功")
    check("不是作者那一份" in out,
          f"版本对不上时没点明 —— 判定内核的编译器漂了却一声不吭：{out!r}")

    ST.probe_version = lambda exe: f"Icarus Verilog version {ST.PINNED_VERSION} (devel) ()"
    out, rc = _capture(ST.do_verify_installer)
    check(rc == 0, f"版本与包都对却判失败（rc={rc}）：{out!r}")
finally:
    ST.detect, ST.probe_version = real_detect, real_probe
    ST.INSTALLER_CANDIDATES, ST.INSTALLER_SHA256 = real_cands, real_map

# ------------------------------------------------ 版本号与文件名对得上
check(ST.PINNED_VERSION.startswith("14"),
      f"PINNED_VERSION 是 {ST.PINNED_VERSION!r}，而首选候选是 "
      f"{ST.INSTALLER_CANDIDATES[0]!r} —— 版本和文件名对不上")

print("setup_toolchain 检查通过" if not failures else "setup_toolchain 检查未通过：")
for f in failures:
    print(f"  ✗ {f}")
sys.exit(1 if failures else 0)
