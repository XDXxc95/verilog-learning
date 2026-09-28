"""控制台输出初始化（共享模块）。

Windows 上 Python 默认按系统区域编码（简体中文环境是 GBK）写 stdout，
这会导致两类问题：
  1. 中文注释/提示在 UTF-8 终端（Git Bash、Windows Terminal）里显示为乱码
  2. `✓`、`→`、`≤` 等符号在 GBK 下无法编码，直接抛 UnicodeEncodeError 崩溃

所有命令行入口都应先调用 init_console()。策略：
  - 把 stdout/stderr 切到 UTF-8，且 errors="replace"（宁可显示占位符也不崩溃）
  - 若 stdout 是真实控制台，把控制台代码页也切到 65001，让 cmd.exe/PowerShell 正确渲染
"""

from __future__ import annotations

import sys


def init_console() -> None:
    """把标准输出切到 UTF-8。可重复调用，失败时静默降级。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            # Python 3.7+ 的 TextIOWrapper 支持 reconfigure
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # 流被重定向/替换成非标准对象时降级，不影响主流程
            pass

    if sys.platform == "win32":
        _set_console_utf8()


def _set_console_utf8() -> None:
    """把 Windows 控制台输出代码页切到 UTF-8。仅在依附真实控制台时有效。"""
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 65001 = UTF-8。对重定向到管道的场景无副作用。
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except (OSError, AttributeError):
        pass
