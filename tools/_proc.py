"""子进程执行（共享模块）：超时杀进程树 + 输出洪泛防护。

两个必须解决的问题：

1. **杀进程树**。`subprocess.run(timeout=...)` 超时只会终止直接子进程，杀不掉 vvp
   派生的后代。仿真里的 `always #0` 零延迟死循环会让仿真时间永不推进，
   进程也就永远不退出。做法是在 Windows 上用**作业对象（Job Object）**：
   把子进程放进作业并设置 KILL_ON_JOB_CLOSE，超时后关闭作业句柄，
   整棵进程树由内核一次性销毁，不依赖 taskkill 的权限与本地化输出。

2. **输出洪泛**。`always @(posedge clk) $display(...)` 配上零延迟时钟，
   能在数秒内产出 GB 级文本，直接打爆内存。做法是用独立线程读取管道，
   只保留**首部与尾部**各若干字节，同时记录真实总量。
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

# 默认保留的输出量：首尾各 256KB。超出部分丢弃但计入总量。
DEFAULT_HEAD = 256 * 1024
DEFAULT_TAIL = 256 * 1024

# 子进程创建标志
CREATE_NO_WINDOW = 0x08000000


@dataclass
class RunResult:
    """一次子进程执行的结果。"""

    returncode: int | None
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str
    stdout_total: int
    stderr_total: int
    truncated: bool = False
    # 超时时是否成功清除了整棵进程树，供上层如实报告
    tree_killed: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def combined(self) -> str:
        return self.stdout + self.stderr


class _BoundedBuffer:
    """只保留首部与尾部的字节缓冲，并记录真实总量。

    典型输出（编译报错、仿真结论）都在首尾，中间海量刷屏内容可以丢。
    """

    def __init__(self, head: int, tail: int) -> None:
        self.head_limit = head
        self.tail_limit = tail
        self._head = bytearray()
        self._tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)

        if len(self._head) < self.head_limit:
            room = self.head_limit - len(self._head)
            self._head.extend(chunk[:room])
            chunk = chunk[room:]

        if chunk:
            self._tail.extend(chunk)
            if len(self._tail) > self.tail_limit:
                # 只保留最后 tail_limit 字节
                del self._tail[: len(self._tail) - self.tail_limit]

    def text(self) -> str:
        head = bytes(self._head).decode("utf-8", errors="replace")
        tail = bytes(self._tail).decode("utf-8", errors="replace")
        if self.total <= len(self._head) + len(self._tail):
            return head + tail
        dropped = self.total - len(self._head) - len(self._tail)
        return f"{head}\n…[中间省略 {dropped} 字节输出]…\n{tail}"

    @property
    def truncated(self) -> bool:
        return self.total > len(self._head) + len(self._tail)


def _reader(stream, buf: _BoundedBuffer) -> None:
    """独立线程读取管道，避免子进程因管道写满而阻塞。"""
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            buf.feed(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


# ------------------------------------------------------------------ 作业对象

def _make_kill_on_close_job():
    """创建一个 KILL_ON_JOB_CLOSE 的作业对象。失败返回 None（调用方降级）。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as w

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                (n, ctypes.c_ulonglong)
                for n in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", w.LARGE_INTEGER),
                ("PerJobUserTimeLimit", w.LARGE_INTEGER),
                ("LimitFlags", w.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", w.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", w.DWORD),
                ("SchedulingClass", w.DWORD),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = w.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            w.HANDLE(job),
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(w.HANDLE(job))
            return None

        return job, kernel32, w
    except (OSError, AttributeError, ValueError):
        return None


def _assign_to_job(job_tuple, pid: int) -> bool:
    try:
        job, kernel32, w = job_tuple
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(w.HANDLE(job), w.HANDLE(handle)))
        finally:
            kernel32.CloseHandle(w.HANDLE(handle))
    except (OSError, AttributeError, ValueError):
        return False


def _close_job(job_tuple) -> bool:
    """关闭作业句柄 —— 因设置了 KILL_ON_JOB_CLOSE，整棵进程树随之销毁。"""
    try:
        job, kernel32, w = job_tuple
        return bool(kernel32.CloseHandle(w.HANDLE(job)))
    except (OSError, AttributeError, ValueError):
        return False


def _taskkill_tree(pid: int) -> bool:
    """降级方案：taskkill 递归杀树。依赖权限，且输出受系统语言影响，故仅作兜底。"""
    try:
        p = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=20,
        )
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ------------------------------------------------------------------ 主入口

def run(
    cmd: list[str],
    cwd: str | None = None,
    timeout: float = 30.0,
    head: int = DEFAULT_HEAD,
    tail: int = DEFAULT_TAIL,
    env: dict[str, str] | None = None,
) -> RunResult:
    """执行命令并捕获输出。超时时杀掉整棵进程树。

    `env` 为 None 表示**继承当前进程环境**；给了就整体替换（subprocess 的语义）。
    调用方若要"继承 + 改几个变量"，得自己写 `{**os.environ, **delta}` ——
    只传 delta 会把 SYSTEMROOT 之类一起丢掉，Windows 上子进程起不来。
    这里是刻意不替调用方合并的：合并成 `{**os.environ, **env}` 看着更贴心，
    但会让"整体替换"这个能力再也表达不出来，而**清空环境**正是探测
    「这个工具到底依赖哪些环境变量」时唯一的手段。
    """
    out_buf = _BoundedBuffer(head, tail)
    err_buf = _BoundedBuffer(head, tail)
    notes: list[str] = []

    creationflags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    t0 = time.monotonic()

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        start_new_session=(sys.platform != "win32"),
        env=env,
    )

    # 立刻放进作业对象，确保超时能连同后代一起销毁
    job_tuple = _make_kill_on_close_job()
    in_job = _assign_to_job(job_tuple, proc.pid) if job_tuple else False
    if job_tuple and not in_job:
        notes.append("job_object_assign_failed")
        _close_job(job_tuple)
        job_tuple = None

    threads = [
        threading.Thread(target=_reader, args=(proc.stdout, out_buf), daemon=True),
        threading.Thread(target=_reader, args=(proc.stderr, err_buf), daemon=True),
    ]
    for t in threads:
        t.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if job_tuple:
            _close_job(job_tuple)  # 内核级销毁整棵树
            job_tuple = None
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if _taskkill_tree(proc.pid):
                notes.append("killed_via_taskkill")
            else:
                notes.append("kill_failed")
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    duration_ms = int((time.monotonic() - t0) * 1000)
    for t in threads:
        t.join(timeout=5)

    if job_tuple:
        _close_job(job_tuple)

    return RunResult(
        returncode=proc.returncode,
        timed_out=timed_out,
        duration_ms=duration_ms,
        stdout=out_buf.text(),
        stderr=err_buf.text(),
        stdout_total=out_buf.total,
        stderr_total=err_buf.total,
        truncated=out_buf.truncated or err_buf.truncated,
        tree_killed=job_tuple is None or in_job,
        notes=notes,
    )
