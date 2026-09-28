"""源码级检查：补上 iverilog **不做**的那一类分析。

为什么需要这个文件：

    iverilog 是事件驱动仿真器，没有综合概念。它对
    「组合 always 里赋值不完整 → 推断出锁存器」**完全沉默**——
    实测 `-Wall` 对这种情况一句 warning 都不给（latch_incomplete_case.v）。

    而 latch 恰恰是最难靠仿真发现的一类错误：它能编译、能跑，
    功能测试甚至可能通过，错在综合出来的硬件结构上。
    所以只能自己扫源码。

判定规则（关键：宁可漏报，不可冤枉）

    只在**能证明**是锁存器时才报：某个条件分支里赋值的信号，
    在该 always 块内**没有其他任何赋值**——那么条件不成立时它保持原值，
    这就是锁存器的定义。

    这条规则自动放过了最常见的正确写法：

        always @(*) begin
            y = 1'b0;          // 先给默认值
            if (en) y = a;     // 这个 if 没有 else，但 y 在块内另有赋值
        end

    y 在块内有 if 之外的赋值，所以不报。这是有意为之：
    把正确代码报成错误，比漏报有害得多——学习者会开始不信任工具。

已知局限（明确写出来，不装作能覆盖）

    * case 是否穷举只能靠字面量推断。分支里写的是参数名（如 `S_IDLE`）时
      无法求值，此时降为 medium 置信度并在文案里说明"请自行确认"。
    * 只认 begin/end 或单条语句形态的分支体，不做完整语法分析。
    * 不处理 function/task 内部的赋值、generate 展开。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 数据结构


@dataclass
class LintFinding:
    kind: str            # 目前只有 "latch_inference"
    file: str
    line: int
    signals: list[str]   # 会被综合成锁存器的信号名
    construct: str       # if_without_else / case_without_default
    confidence: str      # high / medium / low
    detail_zh: str


# ---------------------------------------------------------------- 词法处理

_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_TOKEN_RE = _IDENT_RE

# 赋值左值的识别。
#   (?<![<>=!])  排除 <=、>=、==、!=
#   (?!=)        再挡一次 ==
_ASSIGN_RE = re.compile(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?<![<>=!])=(?!=)")

# 这些词在赋值正则里会被误当成左值，剔掉
_NOT_SIGNALS = {
    "if", "else", "case", "casez", "casex", "default", "begin", "end",
    "endcase", "always", "assign", "for", "while", "repeat", "integer",
    "reg", "wire", "logic", "return", "posedge", "negedge", "or", "and",
}


def strip_comments(text: str) -> str:
    """把注释与字符串字面量抹成空格。

    **逐字符长度必须保持不变**：行号是靠数换行符算出来的，
    一旦这里改变了文本长度，所有报错行号都会漂。
    这也是一次遍历同时处理 //、/* */ 和 "..." 的原因——
    分成几个 re.sub 顺序执行时，字符串里的 // 会被当成注释、注释里未闭合的引号
    会被当成字符串，两种顺序都会错。
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(_blank_keep_newlines(text[i:j]))
            i = j
        elif c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                if text[j] == "\n":
                    break
                j += 1
            out.append(_blank_keep_newlines(text[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _blank_keep_newlines(seg: str) -> str:
    return "".join(ch if ch == "\n" else " " for ch in seg)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# ---------------------------------------------------------------- 括号/块匹配


def _match_paren(text: str, open_pos: int) -> int:
    """返回与 open_pos 处 '(' 配对的 ')' 的位置，找不到返回 -1。"""
    depth = 0
    for i in range(open_pos, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


# 这些 token 会终止当前的 begin/end 作用域，遇到说明源码不平衡。
#
# 注意：**不要把 endcase 放进来**。case 内部的 begin/end 本来就是配平的，
# endcase 不影响 begin/end 计数。早期版本把它当终止符，结果
# `always @(*) begin ... case ... endcase ... end` 整块匹配失败，
# 块体一路吞到文件末尾，于是"该信号在块内别处有没有赋值"的判断
# 把后续模块的赋值也算了进来，latch 直接漏报（实测踩过）。
_SCOPE_TERMINATORS = {"endmodule", "endfunction", "endtask", "endgenerate"}


def _match_begin(text: str, begin_pos: int) -> int:
    """返回与 begin_pos 处 'begin' 配对的 'end' 之后的位置，找不到返回 -1。

    用 token 级比较：`endcase` 不会匹配到 `end`，
    因为整个 token 不等于 "end"。
    """
    depth = 0
    for m in _TOKEN_RE.finditer(text, begin_pos):
        w = m.group(0)
        if w == "begin":
            depth += 1
        elif w == "end":
            depth -= 1
            if depth == 0:
                return m.end()
        elif w in _SCOPE_TERMINATORS and depth > 0:
            return -1
    return -1


def _body_span(text: str, pos: int) -> tuple[int, int]:
    """从 pos 开始取一条语句或一个 begin/end 块的 (start, end)。"""
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    m = _TOKEN_RE.match(text, pos)
    if m and m.group(0) == "begin":
        end = _match_begin(text, pos)
        return (pos, end if end > 0 else len(text))
    # 单条语句：找同一层级的下一个分号，跳过括号内容
    depth = 0
    i = pos
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0:
            return (pos, i + 1)
        elif ch == "b" and _TOKEN_RE.match(text, i) and _TOKEN_RE.match(text, i).group(0) == "begin":
            # 单条语句位置上出现了 begin（例如 if 后面跟块）
            end = _match_begin(text, i)
            return (pos, end if end > 0 else len(text))
        i += 1
    return (pos, len(text))


def _match_case_end(text: str, pos: int) -> int:
    """从 pos（**已经在 case 内部**）起找配对的 endcase 之后的位置。

    深度从 1 起算：调用方给的位置在开启的 case 关键字之后，
    若从 0 起算，第一个 endcase 会把深度打到 -1，永远等不到 depth==0，
    整个 case 分支会被静默跳过（实测踩过：latch 一个都报不出来）。
    """
    depth = 1
    for m in _TOKEN_RE.finditer(text, pos):
        w = m.group(0)
        if w in ("case", "casez", "casex"):
            depth += 1
        elif w == "endcase":
            depth -= 1
            if depth == 0:
                return m.end()
    return -1


# ---------------------------------------------------------------- 赋值收集


def _lhs_set(text: str) -> set[str]:
    """收集文本里所有赋值语句的左值信号名。"""
    out: set[str] = set()
    for m in _ASSIGN_RE.finditer(text):
        name = m.group(1)
        if name not in _NOT_SIGNALS:
            out.add(name)
    return out


def _latch_signals(block: str, span: tuple[int, int]) -> list[str]:
    """找出只在该条件分支里被赋值、块内别处没有赋值的信号。

    这些信号在条件不成立时保持原值 → 锁存器。
    """
    inner = _lhs_set(block[span[0]:span[1]])
    outer = _lhs_set(block[: span[0]] + block[span[1]:])
    return sorted(inner - outer)


# ---------------------------------------------------------------- case 穷举判断

# 形如 2'b00 / 4'hF / 3'd5 的字面量
_LITERAL_RE = re.compile(r"\b(\d+)'([bBoOdDhH])([0-9a-fA-F_xXzZ?]+)\b")

# 行首的 case 分支标签，如 "  2'b00: ..." / "  S_IDLE: begin"
_ITEM_LABEL_RE = re.compile(r"(?m)^[ \t]*([^;:\n]+?)[ \t]*:")


def _case_is_exhaustive(case_body: str) -> bool | None:
    """判断 case 是否覆盖了选择信号的全部取值。

    返回 True（确定穷举）/ False（确定不穷举）/ None（判断不了，例如标签是参数名）。
    """
    labels = _ITEM_LABEL_RE.findall(case_body)
    if not labels:
        return None   # 分支没写在独立行上，放弃判断

    values: set[int] = set()
    width = 0
    for label in labels:
        parts = [p.strip() for p in label.split(",")]
        for p in parts:
            m = _LITERAL_RE.fullmatch(p)
            if not m:
                return None   # 有标识符标签，无法求值
            base = {"b": 2, "o": 8, "d": 10, "h": 16}[m.group(2).lower()]
            digits = m.group(3).replace("_", "")
            if any(ch in digits for ch in "xXzZ?"):
                return None   # 带无关位，覆盖面无法判定
            values.add(int(digits, base))
            width = max(width, int(m.group(1)))

    if width <= 0 or width > 16:
        return None
    return values == set(range(1 << width))


# ---------------------------------------------------------------- 主扫描

_ALWAYS_RE = re.compile(r"\balways\b\s*@")
_IF_RE = re.compile(r"\bif\b\s*\(")
_CASE_RE = re.compile(r"\b(case|casez|casex)\b\s*\(")


def scan_file(path: Path, display_name: str | None = None) -> list[LintFinding]:
    """扫描单个文件，返回发现的问题。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    text = strip_comments(raw)
    name = display_name or str(path)
    findings: list[LintFinding] = []

    for am in _ALWAYS_RE.finditer(text):
        pos = am.end()
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1

        # 敏感列表：@(*) / @* 是组合；带 posedge/negedge 是时序，跳过
        if pos < len(text) and text[pos] == "*":
            body_start = pos + 1
        elif pos < len(text) and text[pos] == "(":
            close = _match_paren(text, pos)
            if close < 0:
                continue
            sens = text[pos + 1: close]
            if re.search(r"\b(posedge|negedge)\b", sens):
                continue          # 时序逻辑，不适用
            body_start = close + 1
        else:
            continue

        bstart, bend = _body_span(text, body_start)
        block = text[bstart:bend]

        findings += _scan_comb_block(block, bstart, text, name)

    return findings


def _scan_comb_block(block: str, block_off: int, text: str, name: str) -> list[LintFinding]:
    findings: list[LintFinding] = []

    # ---- if 没有 else ----
    for im in _IF_RE.finditer(block):
        close = _match_paren(block, im.end() - 1)
        if close < 0:
            continue
        span = _body_span(block, close + 1)
        # 分支体之后是否紧跟 else？
        after = span[1]
        while after < len(block) and block[after] in " \t\r\n":
            after += 1
        tok = _TOKEN_RE.match(block, after)
        if tok and tok.group(0) == "else":
            continue

        sigs = _latch_signals(block, span)
        if not sigs:
            continue
        findings.append(LintFinding(
            kind="latch_inference",
            file=name,
            line=_line_of(text, block_off + im.start()),
            signals=sigs,
            construct="if_without_else",
            confidence="high",
            detail_zh=(
                f"组合 always 里的 if 没有 else，且 {', '.join(sigs)} 在这个块里"
                f"只在 if 成立时被赋值——条件不成立时它会保持上一次的值，"
                f"综合出来就是一个锁存器。"
            ),
        ))

    # ---- case 没有 default ----
    for cm in _CASE_RE.finditer(block):
        close = _match_paren(block, cm.end() - 1)
        if close < 0:
            continue
        end = _match_case_end(block, close + 1)
        if end < 0:
            continue
        cbody = block[close + 1:end]
        if re.search(r"\bdefault\b", cbody):
            continue

        sigs = _latch_signals(block, (close + 1, end))
        if not sigs:
            continue

        exhaustive = _case_is_exhaustive(cbody)
        if exhaustive is True:
            continue   # 分支已覆盖全部取值，不会产生锁存器，不报

        if exhaustive is False:
            conf, why = "high", "分支没有覆盖选择信号的全部取值"
        else:
            conf = "medium"
            why = "无法确认分支是否覆盖了全部取值（标签可能是参数名，求不出值）"

        findings.append(LintFinding(
            kind="latch_inference",
            file=name,
            line=_line_of(text, block_off + cm.start()),
            signals=sigs,
            construct="case_without_default",
            confidence=conf,
            detail_zh=(
                f"组合 always 里的 case 没有 default，且 {', '.join(sigs)} 在这个块里"
                f"只在 case 分支里被赋值——若某个取值没有对应分支，它会保持上一次的值，"
                f"综合出来就是锁存器。{why}。"
            ),
        ))

    return findings


def scan_files(files: list[Path]) -> list[LintFinding]:
    """扫描多个文件。同一个 (文件, 行, 构造) 只保留一条。"""
    seen: set[tuple[str, int, str]] = set()
    out: list[LintFinding] = []
    for f in files:
        for finding in scan_file(f):
            key = (finding.file, finding.line, finding.construct)
            if key in seen:
                continue
            seen.add(key)
            out.append(finding)
    return out
