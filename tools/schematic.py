"""源码 → 结构图 SVG（模块层次图 / 模块内部数据通路图）。

    from schematic import parse_design, render_hierarchy_svg, render_module_svg

    design = parse_design([Path("exercises/01/counter.v")])
    render_hierarchy_svg(design, Path("build/hier.svg"))
    render_module_svg(design.modules["counter"], Path("build/counter.svg"))

**这不是一个 Verilog 语法分析器。** 它是一个"够用就好的结构提取器"：
正则 + 深度计数，抓出模块、端口、声明、assign、always、例化，仅此而已。
完整的语法分析（尤其是 elaboration、参数展开、表达式类型推导）不在射程内。

因此本文件有一条**硬规则**：

    宁可说"这块画不了"，也不要画一张看似合理、实则错误的图。

图是给人建立直觉用的。一张错的依赖图比没有图更糟 —— 学习者会照着它
去理解"信号从哪里流到哪里"，而那个理解是错的。所以凡是不确定的构造，
一律进 `module.unsupported` 并在图上显式标出来，绝不猜。

具体的取舍：

* **`generate`**：能算出圈数就展开，算不出来就**只丢那一块**。判断依据是
  参数的**默认值**（`#(parameter N = 4)` → N=4），所以 `for (i=0; i<N; i=i+1)`
  能算出跑 4 圈，而 `parameter N = `NUM_CH`（编译宏）或 `N` 压根没声明，
  就一律拒绝 —— 猜一个份数会画出一张看着合理、读者却无从察觉的错图。
  拒绝是**逐区域**的：那块 generate 不画（图上写明理由），模块里手写的
  部分照画。展开后各次迭代在信号级（位下标已抹掉）往往完全一样，会合并成
  一条并标 `×N`，否则画 4 个同样的框会让人以为是 4 段不同的逻辑。
  同理支持 `if`/`else` generate（只画成立的那一支，另一支写进说明）和
  参数化位宽 `[DW-1:0]`。`case` generate **明确不支持** —— 分支靠 `值 : 语句`
  切分，而冒号在位宽和三目运算符里都出现，切错了会静默丢分支。
* **参数化例化**（`leaf #(.W(8)) u(...)`）：图按**子模块自己的默认值**画，
  实例框上标出 `#(W=8)` 提示这里有覆盖，但不追进去重算。层次图不受影响。
* **时序边界**：`always @(posedge clk)` 里 `q <= d`，右边读的是**旧值**。
  如果按普通依赖画成 d → q 的实线，画出来的就是一个组合环，
  而这恰恰是学习者最容易搞错的地方。所以时序块的输出用虚线，
  并在框上画出时钟引脚 —— 明确表达"这里隔了一拍"。
* **不认识的标识符**：只保留在本模块**声明过**的名字。函数名、层次引用、
  笔误都会落到"不认识"里，记一条 note 而不是凭空造一个节点。
* **表达式内部结构**：不解析。`a & b | c` 只记成"读了 a、b、c"，
  不画与门或或门 —— 画了就得保证对，保证不了。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

from _lint import strip_comments

# ---------------------------------------------------------------- 词法

_IDENT_RE = re.compile(r"\b([A-Za-z_]\w*)\b")

# 表达式里出现这些词不是信号
_KEYWORDS = {
    "module", "endmodule", "input", "output", "inout", "wire", "reg", "logic",
    "integer", "real", "time", "parameter", "localparam", "defparam", "assign",
    "always", "initial", "begin", "end", "if", "else", "case", "casex", "casez",
    "endcase", "default", "for", "while", "repeat", "forever", "posedge",
    "negedge", "or", "and", "not", "xor", "nand", "nor", "xnor", "buf",
    "generate", "endgenerate", "genvar", "function", "endfunction", "task",
    "endtask", "signed", "unsigned", "supply0", "supply1", "tri", "wand", "wor",
    "specify", "endspecify", "always_comb", "always_ff", "always_latch",
    "automatic", "static", "localparam", "return", "disable", "fork", "join",
    "wait", "casez", "priority", "unique", "full_case", "parallel_case",
}

_DIRECTIONS = ("input", "output", "inout")
_DECL_KINDS = ("wire", "reg", "logic", "integer", "real", "time", "bit")

_RANGE_RE = re.compile(r"\[\s*([^\]:]+)\s*:\s*([^\]]+?)\s*\]")

# 顶层语句切分时认得的关键字
_BLOCK_STARTERS = ("always", "initial", "generate", "function", "task")
_SCOPE_ENDERS = {"endfunction", "endtask", "endgenerate", "endmodule", "endpackage"}


def width_label_of(width: int | None, width_text: str = "") -> str:
    """信号名后面跟的位宽标注。

    三种情况，后两种**必须分清**：
      * 算出来了（`[7:0]` / `[WIDTH-1:0]` 而 WIDTH=8）→ `[8]`
      * 没算出来但原文有范围（参数默认值里有函数调用之类）→ 照抄 `[WIDTH-1:0]`
      * 根本没有范围 → 空
    第二种照抄原文，是因为"位宽是多少我不确定"和"它就是个 1 位信号"
    完全是两回事；只写空字符串会让读者以为是后者。
    """
    if width is not None and width > 1:
        return f"[{width}]"
    if width is None and width_text:
        return width_text
    return ""


@dataclass
class Port:
    name: str
    direction: str
    width_text: str = ""
    width: int | None = None
    signed: bool = False
    reg: bool = False
    line: int = 0

    @property
    def width_label(self) -> str:
        return width_label_of(self.width, self.width_text)


@dataclass
class Decl:
    name: str
    kind: str
    width_text: str = ""
    width: int | None = None
    line: int = 0


@dataclass
class Block:
    """一个 assign 或 always 块。"""
    kind: str                 # "assign" | "always" | "initial"
    line: int
    lhs: list[str] = field(default_factory=list)
    rhs: list[str] = field(default_factory=list)
    clock: str | None = None
    reset: str | None = None
    nonblocking: bool = False
    sensitivity: str = ""
    note: str = ""
    unsupported: str | None = None
    # 来自哪个 generate 块（`generate g`）。空 = 手写的顶层语句。
    origin: str = ""
    # 同源等价语句合并后，这一条代表几份（generate 展开出来的）。
    # 1 = 没合并。**只有 origin 非空时才会 >1**，见 _merge_repeats。
    repeat: int = 1

    @property
    def sequential(self) -> bool:
        return self.kind == "always" and self.clock is not None

    @property
    def label(self) -> str:
        if self.kind == "assign":
            return "assign"
        if self.sequential:
            r = f" / {self.reset}" if self.reset else ""
            return f"always @(posedge {self.clock}{r})"
        if self.kind == "always" and self.sensitivity:
            return f"always @({self.sensitivity})"
        return "always @(*)"


@dataclass
class Instance:
    module: str
    name: str
    conns: list[tuple[str, str]] = field(default_factory=list)
    params: dict[str, str] = field(default_factory=dict)
    line: int = 0
    origin: str = ""
    repeat: int = 1


@dataclass
class Module:
    name: str
    line: int = 0
    file: str = ""
    params: list[str] = field(default_factory=list)
    # 参数的**默认值原文**，和 params 一一对应。默认值就是一段常量表达式，
    # 多数情况下光看源码就能算出来 —— 算出来的那张表见 param_env。
    param_pairs: list[tuple[str, str]] = field(default_factory=list)
    # 参数名 → 按默认值算出的整数。generate 边界和位宽都靠它。
    param_env: dict[str, int] = field(default_factory=dict)
    # 默认值算不出来的参数名。依赖它们的图件一律停用（不猜）。
    param_failed: list[str] = field(default_factory=list)
    ports: list[Port] = field(default_factory=list)
    decls: list[Decl] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)
    unknown_names: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    # generate 展开成功的**说明**（不是错误）：展开了几份、哪一支没画。
    # 和 unsupported 分开放，是因为它们的性质不同 —— 一个是"没画"，
    # 一个是"画了，但你要知道画的只是其中一种参数取值"。
    gen_notes: list[str] = field(default_factory=list)
    # 当前正在展开的 generate 出处，由 _classify_statement 设置、
    # _parse_assign / _parse_always / _parse_instance 读取。
    # 用一个字段而不是给三个函数加参数，是为了不惊动它们的其它调用方。
    _cur_origin: str = field(default="", repr=False)
    # 例化输出接到了本模块的哪些信号上。
    # 只有先知道**子模块**端口的方向才能判断，所以要等全部模块解析完再回填
    # （见 _resolve_instance_outputs），不能在解析单个模块时算出来。
    _instance_outputs: set[str] = field(default_factory=set, repr=False)
    # 每个例化实例各自驱动了哪些信号，画边时用
    _instance_dsts_map: dict[str, set[str]] = field(default_factory=dict, repr=False)

    # ---- 派生信息 ----
    @property
    def inputs(self) -> list[str]:
        return [p.name for p in self.ports if p.direction == "input"]

    @property
    def outputs(self) -> list[str]:
        return [p.name for p in self.ports if p.direction != "input"]

    @property
    def assigned(self) -> set[str]:
        """本模块内部被驱动（assign / always / 例化输出）的信号。"""
        out: set[str] = set()
        for b in self.blocks:
            out.update(b.lhs)
        out |= self._instance_outputs
        return out

    @property
    def declared(self) -> set[str]:
        s = {p.name for p in self.ports}
        s |= {d.name for d in self.decls}
        return s

    def width_of(self, name: str) -> int | None:
        for p in self.ports:
            if p.name == name:
                return p.width
        for d in self.decls:
            if d.name == name:
                return d.width
        return None

    def width_label_of(self, name: str) -> str:
        """给图上打的位宽标注。算不出具体位数时**照抄源码里的范围**。"""
        for coll in (self.ports, self.decls):
            for it in coll:
                if it.name == name:
                    return width_label_of(it.width, it.width_text)
        return ""


@dataclass
class Design:
    modules: dict[str, Module] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def top_candidates(self) -> list[str]:
        """没有被任何模块例化的模块。可能有多个（各自独立的测试台/设计）。"""
        used: set[str] = set()
        for m in self.modules.values():
            for inst in m.instances:
                used.add(inst.module)
        return [n for n in self.order if n not in used]

    def pick_top(self) -> str | None:
        """推断顶层模块。

        多个候选时**要说一声**：一个文件里放了几个互不相关的模块时，
        静默挑第一个会让层次图看着像"这个模块没有子模块"，
        而真正的原因是我们挑错了根。
        """
        cands = [n for n in self.top_candidates() if not n.lower().startswith("tb")]
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            self.notes.append(
                f"有 {len(cands)} 个模块没有被任何模块例化（{', '.join(cands[:4])}"
                f"{'…' if len(cands) > 4 else ''}），默认以 {cands[0]} 为顶层；"
                f"用 --top 显式指定"
            )
            return cands[0]
        # 只有测试台（或者有例化环）
        if self.order:
            self.notes.append("所有模块都被例化过（可能有环），以第一个模块为顶层")
            return self.order[0]
        return None

    def children_of(self, name: str) -> list[Instance]:
        m = self.modules.get(name)
        return m.instances if m else []




# ---------------------------------------------------------------- 语句切分


def iter_statements(text: str) -> list[tuple[str, int]]:
    """把一段代码切成顶层语句，返回 [(原文, 行号)]。

    难点在于 `always @(posedge clk) begin ... end` **结尾没有分号** ——
    只按 `;` 切会把后面的整段代码吸进来。所以同时跟踪 begin/end 深度：
    深度回到 0 且本条语句是块状语句（always/initial/generate/...）时收尾。
    """
    out: list[tuple[str, int]] = []
    i = 0
    n = len(text)
    paren = 0
    blocks = 0
    start = 0
    block_started = False

    while i < n:
        ch = text[i]
        if ch in "([{":
            paren += 1
            i += 1
            continue
        if ch in ")]}":
            paren = max(0, paren - 1)
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            m = _IDENT_RE.match(text, i)
            if m is None:
                i += 1
                continue
            w = m.group(1)
            if w == "begin":
                blocks += 1
                block_started = True
            elif w == "end":
                blocks = max(0, blocks - 1)
                if blocks == 0 and block_started:
                    # 块结束。往后跳过空白，若下一个字符不是 ';'，
                    # 这条语句就到此为止（always/initial 的典型形状）。
                    j = m.end()
                    k = j
                    while k < n and text[k] in " \t\r\n":
                        k += 1
                    if k >= n or text[k] != ";":
                        out.append((text[start:k], _line_of(text, start)))
                        start = k
                        block_started = False
                        i = k
                        continue
            elif w in _SCOPE_ENDERS:
                pass
            elif w in _BLOCK_STARTERS:
                block_started = True
            i = m.end()
            continue
        if ch == ";" and paren == 0 and blocks == 0:
            out.append((text[start:i + 1], _line_of(text, start)))
            start = i + 1
            block_started = False
            i += 1
            continue
        i += 1

    tail = text[start:]
    if tail.strip():
        out.append((tail, _line_of(text, start)))
    return out


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# ---------------------------------------------------------------- 常量求值
#
# 为什么需要它：`generate for (i = 0; i < N; i = i + 1)` 到底展开几份、
# `[WIDTH-1:0]` 到底几位，都得先知道参数的值。而参数的默认值就是一段常量
# 表达式，多数情况下**光看源码就能算出来**（`N = 4`、`W = DW*2`、
# `DEPTH = 1 << ADDR_W`），所以值得算。
#
# 但它**不是通用表达式求值器**，只有一个很窄的意图：给结构图提供
# "这个参数等于几"这一个事实。因此：
#
#   * 算不出来一律返回 None，由调用方记一条"这块画不了"，**绝不猜一个值**。
#     一个猜出来的 N 会展开出错误的份数，画出一张看着合理的错图 ——
#     这比"这张图画不了"坏得多。
#   * 位宽一律按 64 位无符号近似：本工具只用结果去数循环、算位宽，
#     不做位精确的硬件语义模拟。`~` 按 64 位取反，其余运算符直接用 Python 整数。
#   * `x`/`z` 参与的常量一律算不出来（值本身就不确定）。

_EVAL_MASK = (1 << 64) - 1

_TOK_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<num>\d+\s*'\s*[sS]?[bBoOdDhH]\s*[0-9a-fA-FxXzZ?_]+
             |\d+\s*'\s*[sS]?[01xXzZ]
             |'\s*[sS]?[bBoOdDhH]\s*[0-9a-fA-FxXzZ?_]+
             |\d[\d_]*)
    | (?P<sys>\$[A-Za-z_]\w*)
    | (?P<id>[A-Za-z_]\w*)
    | (?P<op>\*\*|<<<|>>>|<=|>=|==|!=|&&|\|\||<<|>>|[-+*/%&|^~!<>()?:])
    """,
    re.X,
)


class _EvalError(Exception):
    """算不出来。**只在 _eval_const 内部传播**，出口一律变成 None。"""


def _parse_verilog_int(tok: str) -> int:
    """`8'd10` / `4'hF` / `'b1010` / `1_000` → 整数。含 x/z 的算不出来。"""
    t = tok.replace("_", "").replace(" ", "")
    m = re.fullmatch(r"(\d*)'([sS]?)([bBoOdDhH])([0-9a-fA-FxXzZ?]+)", t)
    if m:
        digits = m.group(4).lower()
        if any(c in digits for c in "xz?"):
            raise _EvalError(f"{tok} 里有 x/z，常量不确定")
        base = {"b": 2, "o": 8, "d": 10, "h": 16}[m.group(3).lower()]
        return int(digits, base)
    m = re.fullmatch(r"(\d*)'([sS]?)([01xXzZ])", t)
    if m:
        if m.group(3).lower() in "xz":
            raise _EvalError(f"{tok} 里有 x/z")
        return int(m.group(3))
    try:
        return int(t, 10)
    except ValueError as e:
        raise _EvalError(f"认不出的数 {tok!r}") from e


class _ExprParser:
    """递归下降。优先级从低到高照抄 Verilog 的运算符表。"""

    def __init__(self, tokens: list[tuple[str, str]], env: dict[str, int]):
        self.toks = tokens
        self.i = 0
        self.env = env

    # ---- 基础 ----
    def _peek_op(self, *ops: str) -> str | None:
        if self.i < len(self.toks):
            kind, val = self.toks[self.i]
            if kind == "op" and val in ops:
                return val
        return None

    def _eat_op(self, op: str) -> bool:
        if self._peek_op(op):
            self.i += 1
            return True
        return False

    # ---- 各层 ----
    def expr(self) -> int:
        return self.ternary()

    def ternary(self) -> int:
        c = self.logic_or()
        if self._eat_op("?"):
            a = self.ternary()
            if not self._eat_op(":"):
                raise _EvalError("三目运算符缺 :")
            b = self.ternary()
            return a if c else b
        return c

    def _bin(self, sub, *ops: str) -> int:
        left = sub()
        while True:
            op = self._peek_op(*ops)
            if op is None:
                return left
            self.i += 1
            right = sub()
            left = _apply_binop(op, left, right)

    def logic_or(self) -> int:
        return self._bin(self.logic_and, "||")

    def logic_and(self) -> int:
        return self._bin(self.bit_or, "&&")

    def bit_or(self) -> int:
        return self._bin(self.bit_xor, "|")

    def bit_xor(self) -> int:
        return self._bin(self.bit_and, "^")

    def bit_and(self) -> int:
        return self._bin(self.equality, "&")

    def equality(self) -> int:
        return self._bin(self.relational, "==", "!=")

    def relational(self) -> int:
        return self._bin(self.shift, "<", "<=", ">", ">=")

    def shift(self) -> int:
        return self._bin(self.additive, "<<", ">>", "<<<", ">>>")

    def additive(self) -> int:
        return self._bin(self.multiplicative, "+", "-")

    def multiplicative(self) -> int:
        return self._bin(self.power, "*", "/", "%")

    def power(self) -> int:
        # 右结合：2**3**2 = 2**(3**2)
        base = self.unary()
        if self._eat_op("**"):
            return _apply_binop("**", base, self.power())
        return base

    def unary(self) -> int:
        op = self._peek_op("-", "+", "~", "!")
        if op:
            self.i += 1
            v = self.unary()
            if op == "-":
                return -v
            if op == "+":
                return v
            if op == "~":
                return (~v) & _EVAL_MASK
            return 0 if v else 1
        return self.primary()

    def primary(self) -> int:
        if self.i >= len(self.toks):
            raise _EvalError("表达式不完整")
        kind, val = self.toks[self.i]
        self.i += 1
        if kind == "num":
            return _parse_verilog_int(val)
        if kind == "sys":
            return self._syscall(val)
        if kind == "id":
            if val in self.env:
                return self.env[val]
            raise _EvalError(f"算不出 {val} 的值（不是本模块里能确定的常量）")
        if kind == "op" and val == "(":
            v = self.expr()
            if not self._eat_op(")"):
                raise _EvalError("括号不配对")
            return v
        raise _EvalError(f"表达式里有认不出的东西 {val!r}")

    def _syscall(self, name: str) -> int:
        if name != "$clog2":
            raise _EvalError(f"不支持的系统函数 {name}")
        if not self._eat_op("("):
            raise _EvalError("$clog2 后面缺括号")
        v = self.expr()
        if not self._eat_op(")"):
            raise _EvalError("$clog2 的括号不配对")
        # 忠实于 Verilog：$clog2(1) = 0、$clog2(0) = 0，**不**兜到 1。
        # 常有 `AW = $clog2(DEPTH)` 在 DEPTH=1 时得 0，那就是真实行为。
        if v <= 1:
            return 0
        return (v - 1).bit_length()


def _apply_binop(op: str, a: int, b: int) -> int:
    if op in ("<<", "<<<"):
        return a << b if 0 <= b < 4096 else _raise(f"移位位数 {b} 离谱")
    if op in (">>", ">>>"):
        return (a >> b) if 0 <= b < 4096 else _raise(f"移位位数 {b} 离谱")
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        if b == 0:
            raise _EvalError("除数是 0")
        # Verilog 的整除向零截断，Python 的 // 是向下取整 —— 负数上不一样
        q = abs(a) // abs(b)
        return q if (a < 0) == (b < 0) else -q
    if op == "%":
        if b == 0:
            raise _EvalError("取模的除数是 0")
        r = abs(a) % abs(b)
        return r if a >= 0 else -r
    if op == "**":
        if b < 0 or b > 64:
            raise _EvalError(f"指数 {b} 不支持")
        return a**b
    if op == "&":
        return a & b
    if op == "|":
        return a | b
    if op == "^":
        return a ^ b
    if op == "&&":
        return 1 if (a and b) else 0
    if op == "||":
        return 1 if (a or b) else 0
    if op == "==":
        return 1 if a == b else 0
    if op == "!=":
        return 1 if a != b else 0
    if op == "<":
        return 1 if a < b else 0
    if op == "<=":
        return 1 if a <= b else 0
    if op == ">":
        return 1 if a > b else 0
    if op == ">=":
        return 1 if a >= b else 0
    return _raise(f"不支持的运算符 {op}")


def _raise(msg: str):
    raise _EvalError(msg)


def _tokenize_expr(expr: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    n = len(expr)
    while pos < n:
        m = _TOK_RE.match(expr, pos)
        if not m:
            raise _EvalError(f"表达式里有认不出的字符 {expr[pos]!r}")
        pos = m.end()
        if m.lastgroup == "ws":
            continue
        out.append((m.lastgroup, m.group()))
    return out


def eval_const(expr: str, env: dict[str, int]) -> int | None:
    """把一段常量表达式算成整数；算不出来返回 None（**不抛异常**）。"""
    try:
        p = _ExprParser(_tokenize_expr(expr), env)
        v = p.expr()
        if p.i != len(p.toks):
            raise _EvalError("表达式后面还有多余的东西")
        return v
    except (_EvalError, RecursionError):
        return None


def const_failure_reason(expr: str, env: dict[str, int]) -> str:
    """算不出来时，给一句能指名道姓的理由（图上的提示要用它）。"""
    try:
        p = _ExprParser(_tokenize_expr(expr), env)
        p.expr()
        return "表达式无法求值"
    except _EvalError as e:
        return str(e)
    except RecursionError:
        return "表达式嵌套太深"


def build_param_env(pairs: list[tuple[str, str]]) -> tuple[dict[str, int], list[str]]:
    """按默认值算出一张"参数 → 整数"的表。

    多轮迭代是为了处理参数之间的依赖：`parameter DW = 8; parameter W = DW*2;`
    一轮扫下来 W 算不出（DW 还没进表），第二轮才行。达到不动点就停 ——
    剩下的就是**真的算不出来**的（默认值里有函数调用、有外部宏之类），
    它们的名字要如实报出去，因为凡是依赖它们的图都得停用。
    """
    env: dict[str, int] = {}
    pending = list(pairs)
    for _ in range(len(pending) + 2):
        if not pending:
            break
        rest: list[tuple[str, str]] = []
        for name, raw in pending:
            v = eval_const(raw, env)
            if v is None:
                rest.append((name, raw))
            else:
                env[name] = v
        if len(rest) == len(pending):
            break
        pending = rest
    return env, [name for name, _ in pending]


# ---------------------------------------------------------------- 解析


def parse_design(paths: list[Path]) -> Design:
    design = Design()
    for path in paths:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            design.notes.append(f"无法读取 {path}：{e}")
            continue
        text = strip_comments(raw)
        for mod_text, mod_line in _split_modules(text):
            try:
                m = _parse_module(mod_text, mod_line, path.name)
            except Exception as e:  # noqa: BLE001 — 解析器绝不能把整张图带崩
                design.notes.append(f"{path.name}:{mod_line} 模块解析失败（{e}），已跳过")
                continue
            if m.name in design.modules:
                design.notes.append(f"模块 {m.name} 重复定义，后面的那份被忽略")
                continue
            design.modules[m.name] = m
            design.order.append(m.name)

    _resolve_instance_outputs(design)
    return design


def _split_modules(text: str) -> list[tuple[str, int]]:
    """按 module/endmodule 配对切分。

    模块不能嵌套，所以顺序配对即可。`endmodule` 出现在字符串里会误判，
    但设计代码里几乎不会有；真有的话下面 _parse_module 会报解析失败而不是画错图。
    """
    out: list[tuple[str, int]] = []
    pos = 0
    while True:
        m = re.search(r"\bmodule\b", text[pos:])
        if not m:
            break
        s = pos + m.start()
        e = re.search(r"\bendmodule\b", text[s:])
        if not e:
            out.append((text[s:], _line_of(text, s)))
            break
        end = s + e.end()
        out.append((text[s:end], _line_of(text, s)))
        pos = end
    return out


def _match_paren(text: str, open_pos: int) -> int:
    """返回与 text[open_pos]（应为 '('）配对的 ')' 的下标，找不到返回 -1。"""
    depth = 0
    i = open_pos
    n = len(text)
    while i < n:
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_top_commas(s: str, sep: str = ",") -> list[str]:
    """按顶层分隔符切开。括号里的分隔符不算 —— 名字沿用历史，
    `sep` 参数是后来加的：generate 的 `for` 头要按 `;` 切三段。"""
    out: list[str] = []
    depth = 0
    cur = 0
    for i, c in enumerate(s):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == sep and depth == 0:
            out.append(s[cur:i])
            cur = i + 1
    out.append(s[cur:])
    return [x.strip() for x in out if x.strip()]


def _width_from(range_text: str, env: dict[str, int] | None = None) -> tuple[int | None, str]:
    """把 `[7:0]` 解析成位宽 8。`[WIDTH-1:0]` 用参数默认值算，算不出填 None。

    注意返回值第二项（原文）**不管算不算得出来都保留**：算不出来时它是
    唯一能给读者看的信息，图上就照原样显示 `[WIDTH-1:0]`，比什么都不写强，
    也比瞎写一个位数强。
    """
    m = _RANGE_RE.search(range_text)
    if not m:
        return None, range_text.strip()
    hi, lo = m.group(1).strip(), m.group(2).strip()
    h = eval_const(hi, env or {})
    l = eval_const(lo, env or {})
    if h is not None and l is not None:
        return abs(h - l) + 1, range_text.strip()
    return None, range_text.strip()


# ------------------------------------------------------------ generate 展开
#
# 目标是：`for (i = 0; i < 4; i = i + 1)` 这种**边界能从源码算出来**的
# generate，把它当成 4 份普通语句展开，图上照常画。
#
# ★ 底线和别处一样：**算不出来就明说不画，绝不猜份数。**
#   猜一个 N 会展开出错误的份数，画出一张看着合理的错图 ——
#   那比"这张图画不了"坏得多，因为读者无从察觉。
#
# 只展开三种形态，其余一律拒绝（拒绝时整段从正常解析里抹掉，
# 免得 `for` / `case` 被当成例化名解析出一堆垃圾节点）：
#
#     generate ... endgenerate            （显式）
#     for (...) ...                        （隐式 generate，Verilog-2001 起合法）
#     if (...) ... [else ...]              （同上）
#
# `case` generate 不展开 —— 它的 item 分割要认冒号，而冒号在位宽和 `?:`
# 里都出现，切错了会静默丢分支。宁可明说不支持。

_GENERATE_BOUND_LIMIT = 4096   # 循环上界的安全阀：算错了也不至于挂死

# 展开时把循环变量替换成 `(值)`，所以各圈之间的差别**只**体现在这些
# 括号里的数字上。合并等价语句前把 `(数字)` 抹掉，就能让各圈"看起来
# 一样"（`d[(0)]` / `d[(1)]` → `d[]`），从而认出它们是同一个 generate
# 的拷贝。不能改成"取第一个标识符"那种更粗的归一化：`.x(a+b)` 和
# `.x(a+c)` 会被归一成同一个键，把两处**不同**的连接合并成一条。
_GENVAL_ARTIFACT_RE = re.compile(r"\((\d+)\)")


def _norm_conn(expr: str) -> str:
    return _GENVAL_ARTIFACT_RE.sub("", expr)


def _strip_comments(text: str) -> str:
    """把注释和字符串**内容**换成空格，长度和换行都保持不变。

    扫描 generate 边界必须先做这一步：`// generate 一下` 这样的注释
    会让 begin/end 计数错位。换行保留，所以偏移量、行号都还能直接用。

    只在本模块的扫描里用，**不动 iter_statements 的行为** —— 那是既有
    解析路径，改它会牵连所有已经验过的用例。
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    out[i] = " "
                    i += 1
                if i < n:
                    if text[i] != "\n":
                        out[i] = " "
                    i += 1
            if i < n:
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def _stmt_end(text: str, i: int) -> int:
    """从 i 往后找这一条语句的结束偏移（**不含**结尾的 `;`）。

    规则和 iter_statements 一致：同时看括号深度和 begin/end 深度，
    块状语句结尾没有分号。用来切 generate 的循环体 / 分支体。
    """
    n = len(text)
    paren = 0
    blocks = 0
    k = i
    started = False
    while k < n:
        ch = text[k]
        if ch in "([{":
            paren += 1
            k += 1
            continue
        if ch in ")]}":
            paren = max(0, paren - 1)
            k += 1
            continue
        if ch == ";":
            if paren == 0 and blocks == 0:
                return k
            k += 1
            continue
        if ch.isalpha() or ch == "_":
            m = _IDENT_RE.match(text, k)
            if m is None:
                k += 1
                continue
            w = m.group(1)
            if w == "begin":
                blocks += 1
                started = True
            elif w == "end":
                blocks = max(0, blocks - 1)
                if blocks == 0 and started:
                    j = m.end()
                    t = j
                    while t < n and text[t] in " \t\r\n":
                        t += 1
                    if t >= n or text[t] != ";":
                        return t
            elif w in _BLOCK_STARTERS or w in _SCOPE_ENDERS:
                started = True
            k = m.end()
            continue
        k += 1
    return n


def _gen_stmt_end(text: str, i: int, head: str) -> int:
    """generate 里一条语句的结束偏移。`if` 要把 `else` 那一支一起圈进来。

    不圈的话会**切出半条语句**：`if (c) begin ... end else begin ... end`
    里 `_stmt_end` 在第一个 `end` 后就返回（因为 `end` 后面不是 `;`），
    于是 else 分支留在原地被当成一条顶层语句 —— 它的头是 `else`，
    走到 _parse_instance 就是一个凭空冒出来的假节点。
    """
    end = _stmt_end(text, i)
    if head != "if":
        return end
    n = len(text)
    while True:
        j = end
        while j < n and text[j] in " \t\r\n":
            j += 1
        if not text.startswith("else", j):
            return end
        if j + 4 < n and (text[j + 4].isalnum() or text[j + 4] == "_"):
            return end          # `elsewhere` 之类的标识符，不是 else
        k = j + 4
        while k < n and text[k] in " \t\r\n":
            k += 1
        if k >= n:
            return end
        end = _stmt_end(text, k)


def _find_kw(text: str, i: int, kw: str) -> int:
    """从 i 开始找**同层**的下一个关键字 kw 的偏移；找不到返回 -1。

    同层 = 括号深度 0、begin/end 深度 0。用来配平
    generate/endgenerate、case/endcase、function/endfunction。
    """
    n = len(text)
    paren = 0
    blocks = 0
    k = i
    while k < n:
        ch = text[k]
        if ch in "([{":
            paren += 1
            k += 1
            continue
        if ch in ")]}":
            paren = max(0, paren - 1)
            k += 1
            continue
        if ch.isalpha() or ch == "_":
            m = _IDENT_RE.match(text, k)
            if m is None:
                k += 1
                continue
            w = m.group(1)
            if w == "begin":
                blocks += 1
            elif w == "end":
                blocks = max(0, blocks - 1)
            elif paren == 0 and blocks == 0 and w == kw:
                return k
            k = m.end()
            continue
        k += 1
    return -1


def _paren_group(text: str, i: int) -> tuple[str, int]:
    """从 i（应为 `(`）取出括号里的一段，返回 (内容, 右括号后一位)。"""
    if i >= len(text) or text[i] != "(":
        return "", i
    close = _match_paren(text, i)
    if close < 0:
        return "", i
    return text[i + 1:close], close + 1


def _split_gen_header(header: str) -> list[str] | None:
    """把 `i = 0; i < 4; i = i + 1` 按顶层分号切成三段。"""
    parts = _split_top_commas(header, sep=";")
    parts = [p.strip() for p in parts if p.strip()]
    return parts if len(parts) == 3 else None


@dataclass
class _GenItem:
    """展开出来的一条语句。"""
    text: str
    line: int
    origin: str = ""       # 「generate g」这样的出处；空表示不是 generate 来的


@dataclass
class _Region:
    kind: str              # "for" | "if" | "case"
    start: int             # 在模块体里的起始偏移（含关键字）
    end: int               # 结束偏移（不含）
    line: int              # start 对应的文件行号


def _scan_regions(text: str) -> list[_Region]:
    """找出模块体里所有顶层的 generate 区域。

    显式 `generate ... endgenerate` 整段算一个区域；隐式的
    `for` / `if` / `case` 按语句边界算一个区域。
    `function` / `task` / `always` / `initial` 里的东西**不算** ——
    它们里面的 if 是普通语句，不是 generate。
    """
    out: list[_Region] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch.isalpha() or ch == "_":
            m = _IDENT_RE.match(text, i)
            if m is None:
                i += 1
                continue
            w = m.group(1)
            if w == "generate":
                close = _find_kw(text, m.end(), "endgenerate")
                end = len(text)
                if close >= 0:
                    em = _IDENT_RE.match(text, close)
                    end = em.end() if em else close + len("endgenerate")
                out.append(_Region("generate", i, end, _line_of(text, i)))
                i = end
                continue
            if w in ("for", "if", "case"):
                end = _gen_stmt_end(text, i, w)
                out.append(_Region(w, i, end, _line_of(text, i)))
                i = end
                continue
            if w in ("function", "task"):
                # function / task 整段作为一个"只抹掉、不展开"的区域。
                # 抹掉是必须的：iter_statements 对 `function ... begin ... end
                # ... endfunction` 的 begin/end 计数会切出奇怪的碎片，碎片
                # 的头可能是 `begin` 或 `endfunction`，落到 _parse_instance
                # 就凭空多出一个假节点（实测报出过一个叫 `begin if` 的实例）。
                kw = "endfunction" if w == "function" else "endtask"
                close = _find_kw_nested_ok(text, m.end(), kw)
                end = close if close > 0 else _stmt_end(text, i)
                out.append(_Region(w, i, end, _line_of(text, i)))
                i = end
                continue
            if w in ("always", "initial"):
                # always 里的 if 不是 generate；整条语句跳过
                i = _stmt_end(text, i)
                continue
            i = m.end()
            continue
        i += 1
    return out


def _find_kw_nested_ok(text: str, i: int, kw: str) -> int:
    """找 kw（endfunction / endtask），允许中间有 begin/end。找不到返回 -1。"""
    n = len(text)
    k = i
    while k < n:
        if text[k].isalpha() or text[k] == "_":
            m = _IDENT_RE.match(text, k)
            if m is None:
                k += 1
                continue
            if m.group(1) == kw:
                return m.end()
            k = m.end()
            continue
        k += 1
    return -1


def _blank(text: str, start: int, end: int) -> str:
    """把 [start, end) 抹成空格（**换行保留**），偏移量和行号因此不变。"""
    seg = text[start:end]
    return text[:start] + re.sub(r"[^\n]", " ", seg) + text[end:]


def _subst_genvar(expr: str, var: str, val: int) -> str:
    return re.sub(r"\b" + re.escape(var) + r"\b", f"({val})", expr)


def _expand_for(region_text: str, content_line: int, env: dict[str, int],
                origin: str) -> tuple[list[_GenItem], list[str], list[str]]:
    """展开一个 for generate。返回 (语句, 拒绝理由, 说明)。"""
    items: list[_GenItem] = []
    # `for (…)`
    k = region_text.find("(")
    if k < 0:
        return [], ["generate 的 for 缺括号，这块不展开"], []
    header, after = _paren_group(region_text, k)
    parts = _split_gen_header(header)
    if parts is None:
        return [], ["generate 的 for 头不是 `初值; 条件; 步进` 三段，这块不展开"], []
    init_s, cond_s, step_s = parts

    mi = re.match(r"^\s*([A-Za-z_]\w*)\s*=\s*(.+)$", init_s, re.S)
    ms = re.match(r"^\s*([A-Za-z_]\w*)\s*=\s*(.+)$", step_s, re.S)
    if not mi or not ms:
        return [], ["generate 的 for 初值/步进不是 `变量 = 表达式`，这块不展开"], []
    var = mi.group(1)
    if ms.group(1) != var:
        return [], [f"generate 的 for 步进改的是 {ms.group(1)} 而不是循环变量 {var}，这块不展开"], []

    body = region_text[after:]
    # 循环体的行号要从**原文**算：后面替换循环变量会改文本长度，
    # 但每次都从 region_text 重新数，就不会累积漂移。
    body_line = content_line + _line_of(region_text, after) - 1
    val = eval_const(mi.group(2), env)
    if val is None:
        return [], [f"generate 的 for 初值 {mi.group(2).strip()} 算不出来，这块不展开"], []

    label = _gen_label(body)
    tag = f"generate {label}" if label else "generate for"
    # 去 begin-end 外壳：名字要先取出来（_gen_label 看的就是外壳），再去壳。
    # 行号一律从 region_text 的起点**绝对**算，不做累加 —— 累加写法
    # （先算 after 的行、再加上去壳的偏移）会和 _line_of 的 1 基差
    # 悄悄差一行，而且差多少取决于 begin 是不是和 for 在同一行。
    body, inner_off = _unwrap_begin(body)
    body_line = content_line + _line_of(region_text, after + inner_off) - 1
    values: list[int] = []
    while len(values) < _GENERATE_BOUND_LIMIT:
        c = eval_const(cond_s, {**env, var: val})
        if c is None:
            return [], [
                f"{tag} 的循环条件 {cond_s.strip()} 算不出来"
                f"（循环变量 {var} 已经知道是 {val}，所以缺的是别的参数），这块不展开"
            ], []
        if not c:
            break
        values.append(val)
        nv = eval_const(ms.group(2), {**env, var: val})
        if nv is None:
            return [], [f"{tag} 的步进 {ms.group(2).strip()} 算不出来，这块不展开"], []
        val = nv
    if len(values) >= _GENERATE_BOUND_LIMIT:
        return [], [f"{tag} 的循环次数超过 {_GENERATE_BOUND_LIMIT}，块不展开（八成是条件写错了）"], []
    if not values:
        # 0 次循环是合法的（`for (i = 0; i < N; i = i + 1)` 且 N = 0），
        # 什么都不画，也**不算错误**。但也不能悄悄咽掉 —— 读者会以为
        # 这个 generate 被漏解析了，说明一下。
        return [], [], [f"{tag} 展开出来是 0 份（循环条件一开始就不成立），没有画出任何东西"]

    infos: list[str] = []
    if len(values) > 1:
        infos.append(
            f"{tag} 展开了 {len(values)} 份（{var} = "
            + ", ".join(str(v) for v in values[:6])
            + ("…" if len(values) > 6 else "") + "）"
        )
    for v in values:
        sub_env = {**env, var: v}
        sub_body = _subst_genvar(body, var, v)
        # origin 用同一个 tag（**不带**循环变量的值）—— 带上的话每一轮
        # 的出处都不同，后面的等价合并就认不出它们是同一个 generate 的
        # 各份拷贝，图上会出现 4 个一模一样的框。
        sub_items, refusals, sub_infos = _expand_text(sub_body, body_line, sub_env, tag)
        items.extend(sub_items)
        infos.extend(sub_infos)
        if refusals:
            return [], refusals, []
    return items, [], infos


def _expand_if(region_text: str, content_line: int, env: dict[str, int],
               origin: str) -> tuple[list[_GenItem], list[str], list[str]]:
    """展开一个 if generate：算出条件，只保留**成立的那一支**。

    这就是参数化结构图的意义所在 —— 一份源码对应多个不同的电路，
    图只画当前参数取值下的那一个。哪一支被丢掉必须写在说明里，
    否则读者会以为源码里根本没有另一支。
    """
    k = region_text.find("(")
    if k < 0 or not region_text.lstrip().startswith("if"):
        return [], ["generate 的 if 形式认不出来，这块不展开"], []
    cond_s, after = _paren_group(region_text, k)
    kws = _split_if_else(region_text[after:])
    if kws is None:
        return [], ["generate 的 if / else 切不出来（嵌套太怪），这块不展开"], []
    then_s, then_off, else_s, else_off = kws

    c = eval_const(cond_s, env)
    # 标签一律在**去 begin-end 外壳之前**取：去了壳 `begin : buf` 就没了，
    # 后取永远是空串（这条踩过：说明里写成了"只画了 else 那一支"）。
    then_lbl = _gen_label(then_s)
    else_lbl = _gen_label(else_s or "")
    # 标签要按**实际取到的那一支**取：`if (c) begin : a ... end else begin : b ... end`
    # 在 c 为假时画的是 b，那时把块标成 `generate a` 就是指着源码里
    # 根本没画出来的那块说"就是它"。取错了比不取更坏。
    taken_label = then_lbl if c else else_lbl
    tag = f"generate {taken_label}" if taken_label else "generate if"
    if c is None:
        return [], [
            f"generate if 的条件 {cond_s.strip()} 算不出来，两块内容都画不了"
            f"（**只有一支是真的**，画哪一支都会误导）"
        ], []
    if c:
        then_s, inner_off = _unwrap_begin(then_s)
        then_line = content_line + _line_of(region_text, after + then_off + inner_off) - 1
        items, refusals, infos = _expand_text(then_s, then_line, env, tag)
        drawn = then_lbl or "then 那一支"
        dropped = f"else 那一支（{else_lbl}）" if else_lbl else ("else 那一支" if else_s else "")
    else:
        if not else_s:
            # 条件不成立又没有 else：这个 generate 什么都不产生。
            # 和 for 的 0 次循环一样，合法，但要说一声。
            return [], [], [
                f"generate if 按 {cond_s.strip()} = 0 取真，条件不成立又没有 else，"
                f"没有画出任何东西"
            ]
        else_s, inner_off = _unwrap_begin(else_s)
        else_line = content_line + _line_of(region_text, after + else_off + inner_off) - 1
        items, refusals, infos = _expand_text(else_s, else_line, env, tag)
        drawn = else_lbl or "else 那一支"
        dropped = f"then 那一支（{then_lbl}）" if then_lbl else "then 那一支"
    if refusals:
        return [], refusals, []
    if dropped:
        infos = list(infos) + [
            f"generate if 按 {cond_s.strip()} 取真（= {int(bool(c))}），"
            f"只画了 {drawn}，{dropped}没画 —— 源码里还在，换组参数就会是另一张图"
        ]
    return items, [], infos


def _split_if_else(text: str) -> tuple[str, int, str | None, int] | None:
    """把 `if` 后面剩下的部分切成 (then 体, then 偏移, else 体, else 偏移)。

    偏移是相对传入 text 的起点，调用方拿它去算行号（行号比切出来的
    文本本身更耐用：文本长度会随循环变量替换而变）。else 不存在时给 None。
    """
    t = text
    n = len(t)
    # then 体：跳过空白
    i = 0
    while i < n and t[i] in " \t\r\n":
        i += 1
    if i >= n:
        return None
    end = _stmt_end(t, i)
    then_s = t[i:end]
    j = end
    while j < n and t[j] in " \t\r\n":
        j += 1
    if j < n and t.startswith("else", j) and not (j + 4 < n and (t[j + 4].isalnum() or t[j + 4] == "_")):
        k = j + 4
        while k < n and t[k] in " \t\r\n":
            k += 1
        if k >= n:
            return then_s, i, None, 0
        e2 = _stmt_end(t, k)
        return then_s, i, t[k:e2], k
    return then_s, i, None, 0


def _gen_label(body: str) -> str:
    """取 `begin : 名字` 里的名字。"" on failure."""
    m = re.match(r"\s*begin\s*:\s*([A-Za-z_]\w*)", body)
    return m.group(1) if m else ""


def _expand_text(text: str, base_line: int, env: dict[str, int],
                 origin: str) -> tuple[list[_GenItem], list[str], list[str]]:
    """把一段模块体文本展开成普通语句列表。

    返回 (语句, 拒绝理由, 说明)。**拒绝时语句列表是空的** —— 那块区域
    整段从正常解析里抹掉了，绝不让它的内容漏进普通路径变成垃圾节点。

    `base_line` 是 text[0] 在文件里的行号 —— 行号一律**在替换循环变量之前**
    从原文本算出来，所以替换串多长都不影响它。
    """
    regions = _scan_regions(text)
    if not regions:
        out: list[_GenItem] = []
        for stmt, ln in iter_statements(text):
            if stmt.strip():
                out.append(_GenItem(stmt, base_line + ln - 1, origin))
        return out, [], []

    refusals: list[str] = []
    infos: list[str] = []
    items: list[_GenItem] = []
    masked = text
    for r in regions:
        seg = text[r.start:r.end]
        seg_line = base_line + r.line - 1
        if r.kind == "case":
            refusals.append(
                f"第 {seg_line} 行有 case generate：本工具不展开 case，"
                f"内部结构没画（case 分支的 `值 : 语句` 切不准，切错会静默丢分支，"
                f"宁可不画）"
            )
            masked = _blank(masked, r.start, r.end)
            continue
        if r.kind in ("function", "task"):
            # 整个 function / task 抹掉，只留一句说明。不是"画不了"，
            # 是**不该画** —— 它是个被调用的黑盒，不是数据通路。
            refusals.append(
                f"第 {seg_line} 行有 {r.kind}：调用它的地方按黑盒处理，内部不展开"
            )
            masked = _blank(masked, r.start, r.end)
            continue
        if r.kind == "generate":
            inner = _region_inner(seg)
            inner_line = seg_line + _line_of(seg, seg.find("generate") + 8) - 1
            sub, sub_ref, sub_info = _expand_text(inner, inner_line, env, origin)
        elif r.kind == "for":
            sub, sub_ref, sub_info = _expand_for(seg, seg_line, env, origin)
        else:
            sub, sub_ref, sub_info = _expand_if(seg, seg_line, env, origin)
        if sub_ref:
            # 这一块算不出来：**只丢它自己**，模块里手写的那部分照画。
            # 丢整张图是过度反应 —— 其余部分的连接关系是对的，而警告
            # 会写在图上，说明"这块区域没画进去"。反过来，如果把
            # 算不出来的 generate 里那些 assign 当成顶层 assign 画出来，
            # 就成了一张看着合理但错的图，那才是必须避免的。
            refusals.extend(sub_ref)
        else:
            infos.extend(sub_info)
            items.extend(sub)
        masked = _blank(masked, r.start, r.end)

    for stmt, ln in iter_statements(masked):
        if stmt.strip():
            items.append(_GenItem(stmt, base_line + ln - 1, origin))
    items.sort(key=lambda it: it.line)
    return items, _dedup(refusals), _dedup(infos)


def _dedup(seq: list[str]) -> list[str]:
    """去重且保持顺序。嵌套循环会让同一条说明按外层圈数重复 N 遍
    （`row` 跑 2 圈，里面的 `col` 说明就攒了 2 条一模一样的），
    报两遍一模一样的展开说明只会让人以为有两处问题。"""
    out: list[str] = []
    for s in seq:
        if s not in out:
            out.append(s)
    return out


def _region_inner(seg: str) -> str:
    """`generate ... endgenerate` 之间的内容。"""
    k = seg.find("generate")
    body = seg[k + len("generate"):] if k >= 0 else seg
    e = body.rfind("endgenerate")
    return body[:e] if e >= 0 else body


_BEGIN_END_RE = re.compile(r"^\s*begin(?:\s*:\s*[A-Za-z_]\w*)?\s*")


def _unwrap_begin(text: str) -> tuple[str, int]:
    """去掉 `begin : 名字 ... end` 的外壳，返回 (内部文本, 内部相对原点的偏移)。

    **必须**做这一步：循环体/分支体的典型写法就是包一层 begin-end，
    而 iter_statements 会把整个 begin-end 当成**一条**语句交出来
    （那个行为是给 always 块用的，这里不适用）。不去壳的话，
    `_expand_text` 在里面找不到任何可解析的语句，展开出来就是空的 ——
    图上是空的，却谁也不报错，这正是最坏的一种失败。

    带上偏移是为了让行号还能算准；去壳后文本整体前移了，不留偏移的话
    行号会少算几行。
    """
    m = _BEGIN_END_RE.match(text)
    if not m:
        return text, 0
    e = text.rfind("end")
    if e < m.end():
        return text, 0
    if text[e + 3:].strip():
        # `end` 后面还有东西，说明这个 end 不是配对的终止符（八成是
        # `end else` 这种已经被切开的尾巴跑进来了），不敢去壳
        return text, 0
    return text[m.end():e], m.end()


def _parse_module(text: str, line: int, filename: str) -> Module:
    m_head = re.search(r"\bmodule\s+([A-Za-z_]\w*)", text)
    if not m_head:
        raise ValueError("找不到 module 名")
    mod = Module(name=m_head.group(1), line=line, file=filename)

    body_start = m_head.end()

    # ---- 参数表 #( ... ) ----
    rest = text[body_start:]
    mp = re.match(r"\s*#\s*\(", rest)
    if mp:
        open_pos = body_start + mp.end() - 1
        close = _match_paren(text, open_pos)
        if close < 0:
            raise ValueError("参数表的括号不配对")
        for chunk in _split_top_commas(text[open_pos + 1:close]):
            # 名字后面的默认值要**一起收下来** —— 参数化结构图的全部
            # 依据就是这个默认值（除非例化处覆盖，那属于调用方的事）。
            nm = re.search(r"\b(?:parameter|localparam)?\s*([A-Za-z_]\w*)\s*=\s*(.*)",
                           chunk, re.S)
            if nm:
                mod.params.append(nm.group(1))
                mod.param_pairs.append((nm.group(1), nm.group(2).strip()))
        body_start = close + 1

    # ---- 端口表 ( ... ) ----
    rest = text[body_start:]
    mopen = re.search(r"\(", rest)
    semi = text.find(";", body_start)
    header_ports: list[str] = []
    if mopen is not None and semi >= 0 and body_start + mopen.start() < semi:
        open_pos = body_start + mopen.start()
        close = _match_paren(text, open_pos)
        if close < 0:
            raise ValueError("端口表的括号不配对")
        header_ports = _split_top_commas(text[open_pos + 1:close])
        body_start = close + 1

    body_end = text.rfind("endmodule")
    body = text[body_start:body_end if body_end > 0 else len(text)]

    # ---- 参数环境 ----
    # 模块体里的 parameter / localparam 也算参数（而且**模块内可见**），
    # 所以要在解析端口和语句之前先把它们连同默认值一起收齐。
    # 不先收集的话，`localparam W = 8;` 声明在端口下面时，
    # 端口上的 `[W-1:0]` 就永远算不出位宽。
    _collect_body_param_pairs(mod, body)
    mod.param_env, mod.param_failed = build_param_env(mod.param_pairs)

    _parse_ansi_ports(mod, header_ports)

    # ---- 模块体 ----
    # iter_statements(body) 给出的行号是**相对 body** 的。
    # 读者要的是文件里的行号（好去编辑器里跳转），所以要补两段偏移：
    # body 在模块文本里的行偏移 + 模块文本在文件里的行偏移。
    # 不补的话每个模块都从"第 1 行"开始报，等于没有行号。
    line_base = (line - 1) + (_line_of(text, body_start) - 1)
    items, refusals, infos = _expand_text(body, line_base + 1, mod.param_env, "")
    mod.unsupported.extend(refusals)
    mod.gen_notes.extend(infos)

    for it in items:
        _classify_statement(mod, it.text, it.line, it.origin)

    _merge_repeats(mod)
    _collect_unknowns(mod)
    return mod


_PARAM_STMT_RE = re.compile(
    r"^\s*(?:parameter|localparam)\b(.*)$", re.S)


def _collect_body_param_pairs(mod: Module, body: str) -> None:
    """把模块体里的 `parameter X = 表达式;` / `localparam Y = ...;` 收进 param_pairs。

    只按顶层语句扫，不看 generate 里面的（那里面的 localparam 作用域在
    块内，属于另一回事，本工具不处理）。
    """
    for stmt, _ in iter_statements(body):
        m = _PARAM_STMT_RE.match(stmt)
        if not m:
            continue
        decl = m.group(1)
        # `parameter [3:0] X = 4;` —— 位宽是可选的，先抠掉
        drm = _RANGE_RE.search(decl)
        if drm and "=" in decl and drm.start() < decl.index("="):
            decl = decl[:drm.start()] + " " + decl[drm.end():]
        for chunk in _split_top_commas(decl):
            m2 = re.match(r"\s*([A-Za-z_]\w*)\s*(?:=\s*(.*))?$", chunk.strip(), re.S)
            if not m2:
                continue
            name = m2.group(1)
            if name in mod.params:
                continue
            default = (m2.group(2) or "").strip()
            mod.params.append(name)
            mod.param_pairs.append((name, default))


def _merge_repeats(mod: Module) -> None:
    """把 generate 展开出来的**等价语句**合并成一条，记下它代表几份。

    `for (i=0;i<4;i=i+1) assign y[i] = ~a[i];` 展开后是 4 条 assign，
    信号级的图上看（位下标已经被抹掉）它们**一模一样**。画 4 个同样的框
    既占地方又让人以为是 4 段不同的逻辑，所以合并成一条并标 `×4`。

    只合并 origin 非空的块 —— 手写的两条相同 assign 也合并的话，
    `×2` 这个标注就成了谎话（它们不是同一个 generate 展开出来的）。
    """
    for coll, maker in ((mod.blocks, "block"), (mod.instances, "inst")):
        seen: dict[tuple, object] = {}
        kept: list = []
        for it in coll:
            if not it.origin:
                kept.append(it)
                continue
            if maker == "block":
                key = (it.origin, it.kind, it.label, tuple(it.lhs), tuple(it.rhs),
                       it.note, it.clock, it.reset)
            else:
                key = (it.origin, it.module,
                       tuple((p, _norm_conn(c)) for p, c in it.conns),
                       tuple(sorted(it.params.items())))
            if key in seen:
                seen[key].repeat += 1
                continue
            seen[key] = it
            kept.append(it)
        coll[:] = kept


# 端口声明里除了信号名之外的词
_PORT_NOISE = {"input", "output", "inout", "wire", "reg", "logic", "signed",
               "unsigned", "bit", "var", "int", "longint", "shortint"}


def _parse_ansi_ports(mod: Module, chunks: list[str]) -> None:
    """解析 ANSI 风格端口表：`input clk, input wire [7:0] d, output reg q`。

    方向/位宽**会继承**：`input a, b` 里的 b 沿用 a 的方向和位宽。
    每个 chunk 里只会有一个信号名 —— 把噪声词剔掉之后剩下的那个就是。
    """
    cur_dir: str | None = None
    cur_width_text = ""
    cur_signed = False
    cur_reg = False

    for chunk in chunks:
        c = chunk.strip()
        if not c:
            continue

        low = c.lower()
        has_dir = next((d for d in _DIRECTIONS if re.search(r"\b" + d + r"\b", low)), None)
        if has_dir:
            cur_dir = has_dir
            rm = _RANGE_RE.search(c)
            cur_width_text = rm.group(0) if rm else ""
            cur_signed = bool(re.search(r"\bsigned\b", low))
            cur_reg = bool(re.search(r"\b(reg|logic)\b", low))
        elif _RANGE_RE.search(c):
            # 没有方向但有位宽：继承方向，更新位宽
            cur_width_text = _RANGE_RE.search(c).group(0)

        # 信号名 = 剔掉方向/类型/位宽之后剩下的标识符
        # （没有方向关键字时同样有效，因为噪声词已经被剔干净）
        cleaned = _RANGE_RE.sub(" ", c)
        name = ""
        for m in _IDENT_RE.finditer(cleaned):
            if m.group(1).lower() not in _PORT_NOISE:
                name = m.group(1)
                break

        if cur_dir is None or not name:
            continue
        w, wtext = _width_from(cur_width_text, mod.param_env) if cur_width_text else (None, "")
        mod.ports.append(Port(name=name, direction=cur_dir, width_text=wtext,
                              width=w, signed=cur_signed, reg=cur_reg,
                              line=mod.line))


def _first_ident(s: str) -> str:
    s = re.sub(r"\[[^\]]*\]", " ", s)
    m = _IDENT_RE.search(s)
    return m.group(1) if m else ""


def _classify_statement(mod: Module, stmt: str, line: int, origin: str = "") -> None:
    s = stmt.strip()
    if not s:
        return
    head_m = re.match(r"([A-Za-z_]\w*)", s)
    if not head_m:
        return
    head = head_m.group(1)
    # 出处供 _parse_assign / _parse_always / _parse_instance 读取（见 Module._cur_origin）
    mod._cur_origin = origin

    if head in _DIRECTIONS:
        _parse_body_port(mod, s, head, line)
        return
    if head in _DECL_KINDS or head in ("parameter", "localparam"):
        _parse_body_decl(mod, s, line)
        return
    if head == "assign":
        _parse_assign(mod, s, line)
        return
    if head == "always":
        _parse_always(mod, s, line)
        return
    if head == "initial":
        mod.unsupported.append(f"第 {line} 行有 initial 块：只存在于测试台，不画进结构图")
        return
    if head == "genvar":
        # genvar 是展开期的量，不是电路里的信号，画进图里只会多一个悬空节点
        return
    if head == "endgenerate":
        # generate 区域在 _expand_text 里已经被整段抹掉了，正常走不到这里。
        # 真走到了说明边界没配平（比如嵌套 generate 的脏写法），
        # 留一条记录好过静默忽略。
        mod.unsupported.append(
            f"第 {line} 行有个落单的 endgenerate：generate 边界没配平，这块没解析"
        )
        return
    if head == "generate":
        # 正常路径上 generate 区域已经被展开并抹掉了，能走到这里只有两种情况：
        # 展开失败（拒绝理由已经记在 unsupported 里了），或者编译器不允许的
        # 脏写法。两条都只需要留下说明。
        mod.unsupported.append(
            f"第 {line} 行有 generate，但没能展开（见上面的具体理由），"
            f"这块区域里的结构没有画进图里"
        )
        return
    if head in ("for", "if", "case"):
        # 同上：这也应该在展开阶段就被处理掉。落到这儿说明边界没认出来，
        # 硬按例化解析会得到一堆以 `for` / `if` 命名的假节点，宁可只报一声。
        mod.unsupported.append(
            f"第 {line} 行有个没能识别的 {head} generate：边界没认出来，这块没解析"
        )
        return
    if head == "else":
        # `if (c) begin ... end else begin ... end` 的边界要是切在第一个
        # `end` 上，else 那一支就会掉到这里。从前它会被 _parse_instance
        # 默默吃掉 —— 图上看不出少了东西，源码里的那一支就这么消失了。
        # 这种"静默丢结构"是最坏的失败，所以至少要说出来。
        mod.unsupported.append(
            f"第 {line} 行有个落单的 else：if generate 的分支边界没切对，"
            f"else 那一支里的结构没有画进图里"
        )
        return
    if head in ("function", "task"):
        mod.unsupported.append(
            f"第 {line} 行有 {head}：调用它的地方按黑盒处理，内部不展开"
        )
        return
    if head in ("specify", "defparam"):
        return

    # 剩下的按例化看：`子模块名 #(...) 实例名 (...);`
    _parse_instance(mod, s, line)


def _parse_body_port(mod: Module, s: str, direction: str, line: int) -> None:
    if any(p.name and p.direction == direction and p.name in s for p in mod.ports):
        pass
    rm = _RANGE_RE.search(s)
    wtext = rm.group(0) if rm else ""
    w, _ = _width_from(wtext, mod.param_env) if wtext else (None, "")
    names_part = s[rm.end():] if rm else re.sub(r"^\s*" + direction, "", s)
    for nm in _split_top_commas(names_part.rstrip(";").strip()):
        name = _first_ident(nm)
        if name and not any(p.name == name for p in mod.ports):
            mod.ports.append(Port(name=name, direction=direction,
                                  width_text=wtext, width=w,
                                  reg=("reg" in s or "logic" in s), line=line))


def _parse_body_decl(mod: Module, s: str, line: int) -> None:
    kind_m = re.search(r"\b(wire|reg|logic|integer|real|time|bit)\b", s)
    rm = _RANGE_RE.search(s)
    wtext = rm.group(0) if rm else ""
    w, _ = _width_from(wtext, mod.param_env) if wtext else (None, "")
    kind = kind_m.group(1) if kind_m else "parameter"

    if not kind_m:
        for nm in _split_top_commas(s.split("=", 1)[0].replace("parameter", "").replace("localparam", "")):
            name = _first_ident(nm)
            if name and name not in mod.params:
                mod.params.append(name)
        return

    names_part = s[rm.end():] if rm else s[kind_m.end():]
    for nm in _split_top_commas(names_part.rstrip(";").strip()):
        # 声明里可能带初值 `reg [3:0] q = 0;` —— 取等号左边
        name = _first_ident(nm.split("=")[0])
        if name and not any(d.name == name for d in mod.decls):
            mod.decls.append(Decl(name=name, kind=kind, width_text=wtext,
                                  width=w, line=line))


_ASSIGN_STMT_RE = re.compile(
    r"^\s*assign\s+(.+?)\s*=\s*(.+?);?\s*$", re.S | re.M)
_LHS_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*$")


def _parse_assign(mod: Module, s: str, line: int) -> None:
    m = _ASSIGN_STMT_RE.match(s)
    if not m:
        mod.unsupported.append(f"第 {line} 行的 assign 写法认不出来，已跳过")
        return
    lhs_names = _assign_targets(m.group(1))
    rhs_names = _expr_signals(m.group(2), set(mod.params))
    mod.blocks.append(Block(kind="assign", line=line, lhs=lhs_names, rhs=rhs_names,
                            origin=mod._cur_origin))


def _assign_targets(text: str) -> list[str]:
    """assign 左边可能是 `{a, b}` 或 `y[3:0]`，取其中的信号名。"""
    t = text.strip()
    if t.startswith("{") and t.endswith("}"):
        out: list[str] = []
        for part in _split_top_commas(t[1:-1]):
            n = _first_ident(part)
            if n:
                out.append(n)
        return out
    m = re.match(r"([A-Za-z_]\w*)", t)
    return [m.group(1)] if m else []


_SEQ_RE = re.compile(r"^\s*always\s*@\s*\((.*?)\)", re.S)


def _parse_always(mod: Module, s: str, line: int) -> None:
    m = _SEQ_RE.match(s)
    if not m:
        if re.match(r"^\s*always\s+@\s*\*", s):
            sens = "*"
        else:
            mod.unsupported.append(
                f"第 {line} 行的 always 没有可识别的敏感列表（可能是 `always #5 ...` "
                f"这类测试台写法），不画进结构图"
            )
            return
    else:
        sens = m.group(1).strip()

    clock = None
    reset = None
    if sens not in ("*",):
        edges = re.findall(r"\b(posedge|negedge)\s+([A-Za-z_]\w*)", sens)
        others = [t for t in _split_top_commas(sens) if not re.match(r"\s*(pos|neg)edge", t)]
        if edges and not others:
            clock = edges[0][1]
            reset = edges[1][1] if len(edges) > 1 else None
        elif edges and others:
            # `@(posedge clk or rst)` —— 混了边沿和电平，这是**真的**容易出错，
            # 但不是"画不了"，而是"值得提醒"
            clock = edges[0][1]
            reset = edges[1][1] if len(edges) > 1 else None
            mod.unsupported.append(
                f"第 {line} 行的敏感列表既有时钟边沿又有电平（{sens}），"
                f"综合出来的行为可能和你想的不一样"
            )
        else:
            clock = None
            mod.unsupported.append(
                f"第 {line} 行的 always 用的是显式信号列表 `@({sens})` 而不是 `@(*)`。"
                f"这种写法漏信号会导致仿真和综合结果不一致，建议改成 always @(*)"
            )

    body_start = m.end() if m else s.find("*") + 1
    body = s[body_start:]

    lhs, rhs = _scan_always_body(body, set(mod.params))
    nonblocking = _has_depth0_assignment(body, "<=")

    blk = Block(kind="always", line=line, lhs=lhs, rhs=rhs,
                clock=clock, reset=reset, nonblocking=nonblocking,
                sensitivity=sens if sens != "*" else "*",
                origin=mod._cur_origin)
    if clock and not nonblocking:
        blk.note = "时序块里用了阻塞赋值 =，请确认这是你想要的"
    mod.blocks.append(blk)


def _depth0_assignments(text: str) -> list[tuple[int, int, str]]:
    """找出**括号深度为 0** 的赋值，返回 [(运算符起点, 运算符长度, 左侧名字)]。

    深度判断是必须的：`if (cnt <= 5) cnt <= cnt + 1;` 里第一个 `<=` 是
    比较运算、第二个才是赋值。只看符号的话会把比较当成赋值，
    结果 `cnt` 被从"被读到的信号"里抠掉 —— 图上就少一条边，
    而且没有任何提示。用源文本正则在有这个 bug 时照样"看起来正常"。
    """
    out: list[tuple[int, int, str]] = []
    i = 0
    n = len(text)
    depth = 0
    while i < n:
        c = text[i]
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]}":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0:
            op_len = 0
            if text.startswith("<=", i):
                op_len = 2
            elif c == "=" and text[i:i + 2] != "==" and text[i - 1:i] not in ("<", ">", "!", "="):
                op_len = 1
            if op_len:
                # 从运算符往左找目标名（跳过空白和位选 `[...]`）
                j = i - 1
                while j >= 0 and text[j] in " \t\r\n":
                    j -= 1
                if j >= 0 and text[j] == "]":
                    d2 = 1
                    j -= 1
                    while j >= 0 and d2:
                        if text[j] == "]":
                            d2 += 1
                        elif text[j] == "[":
                            d2 -= 1
                        j -= 1
                    while j >= 0 and text[j] in " \t\r\n":
                        j -= 1
                k = j
                while k >= 0 and (text[k].isalnum() or text[k] == "_"):
                    k -= 1
                name = text[k + 1:j + 1]
                if name and name[0].isalpha() or (name and name[0] == "_"):
                    out.append((i, op_len, name))
                i += op_len
                continue
        i += 1
    return out


def _has_depth0_assignment(text: str, op: str) -> bool:
    return any(text[a:a + ln] == op for a, ln, _ in _depth0_assignments(text))


def _scan_always_body(body: str, drop: set[str] | None = None) -> tuple[list[str], list[str]]:
    """从 always 块体里提取 (被赋值的信号, 被读到的信号)。

    做法：逐条语句找出所有深度 0 的赋值，把**左侧名字**从文本里抠掉，
    剩下的标识符就是被读到的 —— 这样 `if (en) q <= q + 1;` 会正确得到
    读 {en, q}、写 {q}，而不是把 en 漏掉。

    条件表达式（`if (...)`、`case (...)`）因为没被抠掉，自然是"被读到"的，
    这正是我们想要的：条件也是数据依赖。
    """
    lhs: list[str] = []
    read_text_parts: list[str] = []

    for stmt, _ in iter_statements(body) or [(body, 0)]:
        cuts = _depth0_assignments(stmt)
        if not cuts:
            read_text_parts.append(stmt)
            continue
        pieces: list[str] = []
        prev = 0
        for (op_at, op_len, name) in cuts:
            # 只抠掉名字本身（连同它左边的位选），运算符留在原处不影响识别
            name_end = op_at
            j = name_end - 1
            while j >= 0 and stmt[j] in " \t\r\n":
                j -= 1
            if j >= 0 and stmt[j] == "]":
                d2 = 1
                j -= 1
                while j >= 0 and d2:
                    if stmt[j] == "]":
                        d2 += 1
                    elif stmt[j] == "[":
                        d2 -= 1
                    j -= 1
            name_start = j - len(name) + 1
            pieces.append(stmt[prev:max(prev, name_start)])
            prev = name_end
            if name not in lhs:
                lhs.append(name)
        pieces.append(stmt[prev:])
        read_text_parts.append(" ".join(pieces))

    return lhs, _expr_signals(" ".join(read_text_parts), drop)


# 数字字面量。必须先抠掉再找标识符 ——
# `4'h0` 里的 `h0` 在词法上完全长得像一个信号名，不抠的话图上会
# 凭空多出一个叫 h0 的节点，而且它还连不到任何地方（因为没声明过）。
_LITERAL_RE = re.compile(
    r"\d+\s*'\s*[sS]?\s*[bBoOdDhH]\s*[0-9a-fA-FxXzZ?_]+"   # 4'h0 / 8'd255
    r"|\d+\s*'\s*[sS]?\s*[01xXzZ]"                          # 4'1（少用）
    r"|'\s*[sS]?\s*[bBoOdDhH]\s*[0-9a-fA-FxXzZ?_]+"          # 'h0
    r"|\b\d[\d_]*\.[\d_]+([eE][+-]?\d+)?"                    # 实数 1.5
    r"|\b\d[\d_]*\b"                                         # 十进制 255 / 1_000
)


_SYSFUNC_RE = re.compile(r"\$[A-Za-z_]\w*")


def _expr_signals(expr: str, drop: set[str] | None = None) -> list[str]:
    """表达式里出现的、**不是关键字也不是数字**的标识符（保持出现顺序、去重）。

    `drop` 是"这些名字不是信号"，传模块的参数名集合。必须传 ——
    `assign y[i] = a[r*C + c];` 展开后是 `a[(0)*(3)+(0)]`，里面的 C 是
    **参数**不是信号。不过滤的话：位下标里出现的参数会被算进"读"集合，
    框里会写成"读 a, C"；而且它不在端口/声明表里，会被报成
    「未在本模块声明的名字」—— 一条完全错误的警告。
    """
    drop = drop or set()
    # `$clog2` / `$signed` 这些系统函数名必须整段抠掉：`\b` 在 `$` 和 `c`
    # 之间是断的，所以 `$clog2` 会被当成一个叫 `clog2` 的标识符。
    # 曾实测：`addr == i[$clog2(DEPTH)-1:0]` 报出「未在本模块声明的名字：clog2」，
    # 一条纯属虚构的警告。
    cleaned = _SYSFUNC_RE.sub(" ", expr)
    cleaned = _LITERAL_RE.sub(" ", cleaned)
    out: list[str] = []
    for m in _IDENT_RE.finditer(cleaned):
        w = m.group(1)
        if w in _KEYWORDS or w in drop:
            continue
        if w not in out:
            out.append(w)
    return out


def _parse_instance(mod: Module, s: str, line: int) -> None:
    m = re.match(r"\s*([A-Za-z_]\w*)\s*(#\s*\()?", s)
    if not m:
        return
    sub = m.group(1)
    pos = m.end()
    params: dict[str, str] = {}
    if m.group(2):
        close = _match_paren(s, pos - 1)
        if close < 0:
            return
        for chunk in _split_top_commas(s[pos:close]):
            if "(" in chunk and ")" in chunk:
                # .W(8) 形式
                nm = re.match(r"\s*\.\s*([A-Za-z_]\w*)\s*\((.*)\)\s*$", chunk, re.S)
                if nm:
                    params[nm.group(1)] = nm.group(2).strip()
            elif "=" in chunk:
                nm = re.match(r"\s*([A-Za-z_]\w*)\s*=\s*(.*)$", chunk, re.S)
                if nm:
                    params[nm.group(1)] = nm.group(2).strip()
        pos = close + 1

    m2 = re.match(r"\s*([A-Za-z_]\w*)?\s*\(", s[pos:])
    if not m2:
        return
    inst_name = m2.group(1) or sub
    open_pos = pos + m2.end() - 1
    close = _match_paren(s, open_pos)
    if close < 0:
        return

    conns: list[tuple[str, str]] = []
    for chunk in _split_top_commas(s[open_pos + 1:close]):
        pm = re.match(r"\s*\.\s*([A-Za-z_]\w*)\s*\((.*)\)\s*$", chunk, re.S)
        if pm:
            conns.append((pm.group(1), pm.group(2).strip()))
        else:
            conns.append(("", chunk))

    # 实例名：generate 展开出来的重名是**正常**的（同一个 `u_f` 被复制 N 份，
    # 真正的唯一名字由层次名给出）。所以这里不查重，交给 _merge_repeats 合并。
    mod.instances.append(Instance(module=sub, name=inst_name,
                                  conns=conns, params=params, line=line,
                                  origin=mod._cur_origin))


def _resolve_instance_outputs(design: Design) -> None:
    """把"例化的输出端口接到了本模块的哪个信号上"标出来。

    只有先知道子模块端口的**方向**，才能判断 `.q(q)` 里的 q 是被驱动的。
    所以要等所有模块都解析完再来一遍。
    """
    for mod in design.modules.values():
        outs: set[str] = set()
        per_inst: dict[str, set[str]] = {}
        for inst in mod.instances:
            child = design.modules.get(inst.module)
            if child is None:
                continue
            out_ports = {p.name for p in child.ports if p.direction != "input"}
            mine: set[str] = set()
            for port, expr in inst.conns:
                if port in out_ports:
                    n = _first_ident(expr)
                    if n:
                        mine.add(n)
            per_inst[inst.name] = mine
            outs |= mine
        mod._instance_outputs = outs
        mod._instance_dsts_map = per_inst


def _collect_unknowns(mod: Module) -> None:
    known = mod.declared
    seen: list[str] = []
    for b in mod.blocks:
        for n in b.rhs + b.lhs:
            if n not in known and n not in seen:
                seen.append(n)
    if seen:
        mod.unknown_names = seen


# ---------------------------------------------------------------- SVG 基础


class Svg:
    def __init__(self, width: int, height: int, *, title: str = ""):
        self.w = width
        self.h = height
        self.parts: list[str] = []
        self.title = title

    def raw(self, s: str) -> None:
        self.parts.append(s)

    def rect(self, x, y, w, h, *, fill="var(--box)", stroke="var(--line)",
             rx=6, dash=None, width=1.4) -> None:
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{width}"{d}/>'
        )

    def text(self, x, y, s, *, cls="lbl", anchor="start", size=None) -> None:
        st = f' style="font-size:{size}px"' if size else ""
        self.parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" '
            f'class="{cls}"{st}>{html.escape(s)}</text>'
        )

    def path(self, d: str, *, cls="edge", dash=None, marker=True) -> None:
        da = f' stroke-dasharray="{dash}"' if dash else ""
        mk = ' marker-end="url(#arrow)"' if marker else ""
        self.parts.append(f'<path d="{d}" class="{cls}" fill="none"{da}{mk}/>')

    def render(self, extra_defs: str = "") -> str:
        body = "\n  ".join(self.parts)
        t = f"<title>{html.escape(self.title)}</title>\n" if self.title else ""
        return _SVG_DOC.format(w=self.w, h=self.h, title=t, defs=extra_defs, body=body)


# SVG 的整体样式。用 CSS 变量 + prefers-color-scheme，明暗都能看。
# 字体给的是等宽字体栈 —— 图里的文字多是信号名，等宽对齐更整齐。
_SVG_DOC = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">
{title}<defs>
  <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
          markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--edge)"/>
  </marker>
  <marker id="clock" viewBox="0 0 10 10" refX="5" refY="5"
          markerWidth="6" markerHeight="6" orient="auto">
    <path d="M 0 2 L 5 5 L 0 8" fill="none" stroke="var(--edge)" stroke-width="1.4"/>
  </marker>
  {defs}
</defs>
<style>
  svg {{ background: var(--bg); font-family: ui-monospace, Consolas, "Cascadia Mono", monospace; }}
  :root {{ --bg:#fff; --box:#f8fafc; --line:#94a3b8; --fg:#0f172a; --dim:#64748b;
           --edge:#475569; --seq:#7c3aed; --comb:#0369a1; --inst:#b45309; --warn:#dc2626; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0f1115; --box:#171b23; --line:#3b4252; --fg:#e5e7eb; --dim:#94a3b8;
             --edge:#94a3b8; --seq:#c084fc; --comb:#7dd3fc; --inst:#fbbf24; --warn:#f87171; }}
  }}
  text {{ fill: var(--fg); font-size: 12px; dominant-baseline: middle; }}
  .lbl {{ font-size: 12.5px; }}
  .sub {{ fill: var(--dim); font-size: 11px; }}
  .pin {{ fill: var(--dim); font-size: 11px; }}
  .edge {{ stroke: var(--edge); stroke-width: 1.4; }}
  .comb {{ stroke: var(--comb); }}
  .seq  {{ stroke: var(--seq); }}
  .warn {{ fill: var(--warn); font-size: 11.5px; }}
  .ttl  {{ font-size: 15px; font-weight: 600; }}
</style>
  {body}
</svg>
"""


def _wrap_text(s: str, limit: float) -> list[str]:
    """按**估算宽度**折行（不是按字符数）。

    中文一个字顶两个 ASCII 宽，按字符数折行的话中文行会明显更长。
    优先在标点/空格处断，断不了就硬断。
    """
    lines: list[str] = []
    cur = ""
    cur_w = 0.0
    for ch in s:
        w = _tw(ch, 12.0)
        if cur_w + w > limit and cur:
            # 回退到最近的分隔符，让断点看起来自然一点
            cut = max(cur.rfind(p) for p in "，。；、：,; ") + 1
            if cut > len(cur) * 0.5:
                lines.append(cur[:cut])
                cur = cur[cut:]
                cur_w = _tw(cur, 12.0)
            else:
                lines.append(cur)
                cur, cur_w = "", 0.0
        cur += ch
        cur_w += w
    if cur:
        lines.append(cur)
    return lines


def _tw(s: str, size: float = 12.5) -> float:
    """粗略估算文本宽度。

    没有字体度量，只能估：ASCII 约 0.62em，CJK 约 1.0em。
    估宽了只是留白多一点，估窄了文字会溢出框 —— 所以刻意往宽里估。
    """
    n = 0.0
    for ch in s:
        n += 1.0 if ord(ch) > 0x2E80 else 0.62
    return n * size


# ---------------------------------------------------------------- 层次图


@dataclass
class _HNode:
    module: str
    instance: str
    line: int
    children: list["_HNode"] = field(default_factory=list)
    x: float = 0.0
    depth: int = 0
    width: float = 0.0


def render_hierarchy_svg(design: Design, out: Path, top: str | None = None) -> Path:
    top = top or design.pick_top()
    if top is None or top not in design.modules:
        raise SchematicError("找不到顶层模块，无法生成层次图")

    root = _build_tree(design, top, top, 0, set())
    _layout_tree(design, root)

    BOX_W, BOX_H = 190, 52
    GAP_X, GAP_Y = 26, 86

    # 提示文字必须换行并且**把画布撑到装得下** ——
    # 一行写到底的话，长提示会直接跑出右边（SVG 会老老实实裁掉），
    # 留下的半句话比没有提示更容易误导。
    #
    # 顺序很重要：提示要占几行得**先算出来**，树的起始 y 才能让开它。
    # 先画树再叠提示的话，第一行提示会正好压在顶层框上（字和框重叠、
    # 两边都看不清），而 SVG 照样是合法文件，检查脚本也发现不了。
    # 注意 _wrap_text 的 limit 是**像素**（内部乘了字号），不是字符数：
    # 给 44 会变成每行三个汉字，图能看但没法读。620px ≈ 一屏大半宽。
    note_lines: list[str] = []
    for n in design.notes[:2]:
        note_lines += _wrap_text(n, 620)
    note_h = len(note_lines) * 16
    TOP = 60 + note_h

    svg = Svg(0, 0)

    def emit(n: _HNode) -> None:
        x = n.x
        y = TOP + n.depth * (BOX_H + GAP_Y)
        m = design.modules.get(n.module)
        svg.rect(x, y, BOX_W, BOX_H)
        svg.text(x + BOX_W / 2, y + 19, n.module, cls="lbl", anchor="middle")
        sub = (f"实例 {n.instance}" if n.instance != n.module else "顶层")
        if m and m.params:
            sub += f" · #({len(m.params)})"
        svg.text(x + BOX_W / 2, y + 37, sub, cls="sub", anchor="middle")

        for c in n.children:
            cx = c.x + BOX_W / 2
            cy = TOP + c.depth * (BOX_H + GAP_Y)
            px, py = x + BOX_W / 2, y + BOX_H
            mid = py + (cy - py) / 2
            svg.path(f"M {px:.1f} {py:.1f} L {px:.1f} {mid:.1f} "
                     f"L {cx:.1f} {mid:.1f} L {cx:.1f} {cy:.1f}")
            # 连线上标实例名 + 端口数
            inst = next((i for i in m.instances if i.name == c.instance), None)
            lbl = c.instance
            if inst and inst.conns:
                lbl += f"  ({len(inst.conns)} 个端口)"
            svg.text((px + cx) / 2, mid - 8, lbl, cls="sub", anchor="middle")
            emit(c)

    emit(root)

    max_x = _max_x(root)
    svg.w = int(max_x + BOX_W + 30)
    svg.h = int(TOP + (_max_depth(root) + 1) * (BOX_H + GAP_Y) + 20)
    svg.title = f"{top} 模块层次图"

    header = [f'<text x="18" y="28" class="ttl">模块层次图 · {html.escape(top)}</text>']
    for i, ln in enumerate(note_lines):
        header.append(f'<text x="18" y="{46 + i * 16}" class="warn">'
                      f'{html.escape(ln)}</text>')

    # 撑宽画布时用**更悲观**的估算（13px/em，而不是提示文字实际的 11.5px）：
    # 图里指定的是等宽字体栈，浏览器到底挑中哪一款、advance 是多少都不确定。
    # 按乐观值撑画布，换台机器就可能少几个字被裁掉 —— 多留点白不花钱。
    note_w = max((_tw(ln, 13.0) + 30 for ln in note_lines), default=0.0)
    svg.w = int(max(svg.w, note_w))
    svg.parts = header + svg.parts

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg.render(), encoding="utf-8", newline="\n")
    return out


def _build_tree(design: Design, module: str, instance: str, depth: int,
                path: set[str]) -> _HNode:
    node = _HNode(module=module, instance=instance, line=0, depth=depth)
    if module in path:
        # 递归例化（或环）—— 画下去会无限展开
        node.children = []
        design.notes.append(f"检测到递归例化：{module}，该分支不再展开")
        return node
    path = path | {module}
    for inst in design.children_of(module):
        node.children.append(_build_tree(design, inst.module, inst.name, depth + 1, path))
    return node


def _layout_tree(design: Design, root: _HNode) -> float:
    """后序布局：叶子按顺序排开，父节点居中于子节点之上。返回子树右边界。"""
    BOX_W, GAP_X = 190, 26

    def width_of(n: _HNode) -> float:
        if not n.children:
            n.width = BOX_W
        else:
            total = sum(width_of(c) for c in n.children) + GAP_X * (len(n.children) - 1)
            n.width = max(BOX_W, total)
        return n.width

    width_of(root)

    def place(n: _HNode, left: float) -> None:
        if not n.children:
            n.x = left + max(0.0, (n.width - BOX_W) / 2)
            return
        cur = left
        for c in n.children:
            place(c, cur)
            cur += c.width + GAP_X
        first, last = n.children[0], n.children[-1]
        n.x = (first.x + last.x) / 2
        # 子节点整体可能比父节点窄，重新居中一次
        if n.x < left:
            n.x = left + max(0.0, (n.width - BOX_W) / 2)

    place(root, 20)
    return root.width


def _max_x(n: _HNode) -> float:
    return max([n.x] + [_max_x(c) for c in n.children])


def _max_depth(n: _HNode) -> int:
    return max([n.depth] + [_max_depth(c) for c in n.children])


# ---------------------------------------------------------------- 内部结构图


def render_module_svg(mod: Module, out: Path, design: Design | None = None) -> Path:
    """画模块内部的数据通路。

    三列布局：**源信号 → 逻辑块 → 被驱动的信号**。
    - 源信号：只有人驱动它、它不驱动别人（模块输入、常量、纯读的信号）
    - 逻辑块：assign / always
    - 被驱动的信号：assign 或 always 的赋值目标、以及例化的输出
    - 反馈：既被写又被读的信号（如计数器自身的 q）会在右侧出现，
      并连一条回边到读它的块上 —— 时序块的回边画成虚线，表示隔了一拍
    """
    srcs, blocks, sinks = _flow_graph(mod, design)

    COL_X = (30.0, 300.0, 720.0)
    COL_W = (210.0, 380.0, 210.0)
    ROW_H = 26.0
    TOP = 76.0

    svg = Svg(0, 0)
    svg.title = f"{mod.name} 内部结构图"

    def col_height(items, block_heights=None) -> float:
        if block_heights is not None:
            return sum(h + 14 for h in block_heights)
        return len(items) * ROW_H

    # ---- 各列高度 ----
    blk_h: list[float] = []
    for b in blocks:
        if isinstance(b, Instance):
            h = max(38.0, 20 + len(b.conns) * 14 + (14 if b.params else 0))
        else:
            # 标题 + 读 + 写 + 出处（generate 展开的才有第四行）
            lines = 1 + (1 if b.lhs else 0) + (1 if b.rhs else 0) + (1 if b.origin else 0)
            h = max(38.0, 16 + lines * 15)
        blk_h.append(h)

    h_src = max(40.0, col_height(srcs))
    h_blk = max(40.0, col_height(blocks, blk_h))
    h_sink = max(40.0, col_height(sinks))
    h_all = max(h_src, h_blk, h_sink)

    positions: dict[str, float] = {}

    # ---- 源信号 ----
    y = TOP + (h_all - h_src) / 2
    for name in srcs:
        _signal_pill(svg, name, mod.width_label_of(name), COL_X[0], y, COL_W[0], "in")
        positions[name] = y + 12
        y += ROW_H

    # ---- 逻辑块 ----
    y = TOP + (h_all - h_blk) / 2
    block_y: list[tuple[float, float]] = []
    for b, h in zip(blocks, blk_h):
        block_y.append((y, h))
        _block_box(svg, b, COL_X[1], y, COL_W[1], h)
        y += h + 14

    # ---- 被驱动的信号 ----
    y = TOP + (h_all - h_sink) / 2
    for name in sinks:
        _signal_pill(svg, name, mod.width_label_of(name), COL_X[2], y, COL_W[2], "out")
        positions[name] = y + 12
        y += ROW_H

    # ---- 连线 ----
    for (b, (by, bh)) in zip(blocks, block_y):
        if isinstance(b, Instance):
            rhs = _instance_srcs(b)
            lhs = sorted(mod._instance_dsts_map.get(b.name, ()))
            # 例化是组合逻辑（子模块内部可能有时序，但连接本身不引入新的拍）
            seq = False
        else:
            rhs = b.rhs
            lhs = b.lhs
            seq = b.sequential
        cls = "seq" if seq else "comb"
        dash = "5 4" if seq else None

        for n in rhs:
            if n not in positions:
                continue
            sy = positions[n]
            if n in srcs:
                x0 = COL_X[0] + COL_W[0]
                x1 = COL_X[1]
            else:
                # 反馈：从右列回读进中列。
                # 起点取寄存器的**左边缘**，不是右边缘 —— 从右边缘起画的话，
                # 这一笔会横穿整个寄存器框（源和目标在同一高度时就是一条
                # 直线压在框上），框里的 `q [4]` 字号和线宽差不多，看起来
                # 像被划掉了。绕到框外面又要额外占一层走线道，
                # 而"寄存器读回逻辑"这件事，短一点反而更清楚。
                x0 = COL_X[2]
                x1 = COL_X[1] + COL_W[1]
            ey = by + bh / 2
            svg.path(_curve(x0, sy, x1, ey), cls=cls, dash=dash)

        for n in lhs:
            if n not in positions:
                continue
            ty = positions[n]
            x0 = COL_X[1] + COL_W[1]
            x1 = COL_X[2]
            ey = by + bh / 2
            svg.path(_curve(x0, ey, x1, ty), cls=cls, dash=dash)

        # 时钟引脚：时序块的左上方画出时钟输入，明确"这里隔了一拍"
        if seq:
            cx = COL_X[1] + 18
            svg.raw(f'<path d="M {cx:.1f} {by - 14:.1f} L {cx:.1f} {by:.1f}" '
                    f'class="seq" stroke-width="1.4" marker-end="url(#clock)"/>')
            svg.text(cx + 6, by - 16, b.clock or "clk", cls="sub")

    # ---- 标题与警告 ----
    # 警告文字画在整张图的**下方**，位置要按实际内容高度算出来 ——
    # 写死 y 坐标的话，模块一大警告就跑到画布外面去了（等于没提示）。
    notes: list[tuple[str, str]] = []      # (文字, class)
    for msg in mod.unsupported:
        notes.append((f"⚠ 不支持：{msg}", "warn"))
    if mod.unknown_names:
        notes.append((
            "⚠ 未在本模块声明的名字（按常量/函数/层次引用处理，未画成节点）："
            + "、".join(mod.unknown_names[:6])
            + ("…" if len(mod.unknown_names) > 6 else ""), "warn"))
    # generate 的展开说明。用 sub 而不是 warn —— 它是**画出来了**，
    # 只是在告诉你"画的是这一组参数取值下的那一份"。
    for msg in mod.gen_notes:
        notes.append((f"· generate：{msg}", "sub"))
    if not notes:
        notes.append(("所有 assign / always 都已解析并画出", "sub"))

    # 同样要折行：unsupported 列表里可能出现很长的表达式，一行写到底
    # 会被 SVG 老实裁掉，只剩半句。
    NOTE_WRAP = 760.0
    wrapped: list[tuple[str, str]] = []
    for msg, cls in notes:
        for ln in _wrap_text(msg, NOTE_WRAP):
            wrapped.append((ln, cls))
    notes = wrapped

    svg.h = int(TOP + h_all + 60 + len(notes) * 17)
    head = [f'<text x="18" y="28" class="ttl">'
            f'{html.escape(mod.name)} · 内部结构图</text>',
            f'<text x="18" y="48" class="sub">'
            f'{html.escape(mod.file)}  ·  第 {mod.line} 行  ·  '
            f'输入 {len(mod.inputs)} / 输出 {len(mod.outputs)}  ·  '
            f'实线=组合，虚线/紫框=时序（隔一拍）</text>']
    for i, (msg, cls) in enumerate(notes):
        head.append(f'<text x="18" y="{svg.h - 20 - (len(notes) - 1 - i) * 17:.0f}" '
                    f'class="{cls}">{html.escape(msg)}</text>')
    svg.parts = head + svg.parts

    note_w = max((_tw(ln, 13.0) + 40 for ln, _ in notes), default=0.0)
    svg.w = int(max(COL_X[2] + COL_W[2] + 30, note_w))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg.render(), encoding="utf-8", newline="\n")
    return out


def _curve(x0: float, y0: float, x1: float, y1: float) -> str:
    """三次贝塞尔。横平竖直的折线在密集时糊成一团，曲线更容易跟。"""
    mx = (x0 + x1) / 2
    return (f"M {x0:.1f} {y0:.1f} "
            f"C {mx:.1f} {y0:.1f} {mx:.1f} {y1:.1f} {x1:.1f} {y1:.1f}")


def _signal_pill(svg: Svg, name: str, width_label: str,
                 x: float, y: float, w: float, side: str) -> None:
    """`width_label` 是**已经格式化好的**标注（可能空、可能是 `[WIDTH-1:0]`）。"""
    h = 20.0
    label = name + (" " + width_label if width_label else "")
    if side == "in":
        svg.rect(x, y, w, h, fill="var(--box)", stroke="var(--line)", rx=10)
        svg.text(x + 10, y + h / 2, label, cls="lbl")
    else:
        svg.rect(x, y, w, h, fill="var(--box)", stroke="var(--line)", rx=10)
        svg.text(x + 10, y + h / 2, label, cls="lbl")


def _repeat_suffix(n: int) -> str:
    """generate 展开合并后的份数标注。1 份不标 —— 标注只在有信息时出现。"""
    return f"  ×{n}" if n > 1 else ""


def _block_box(svg: Svg, b, x: float, y: float, w: float, h: float) -> None:
    if isinstance(b, Instance):
        sub = ""
        if b.params:
            sub = "#(" + ", ".join(f"{k}={v}" for k, v in list(b.params.items())[:2]) + ")"
        sub += _repeat_suffix(b.repeat)
        svg.rect(x, y, w, h, fill="var(--box)", stroke="var(--inst)", rx=4)
        svg.text(x + 12, y + 15, f"{b.module}", cls="lbl")
        svg.text(x + 12 + _tw(b.module) + 10, y + 15 + 1, sub, cls="sub")
        yy = y + 32
        for port, expr in b.conns[:8]:
            if port:
                svg.text(x + 16, yy, f".{port}", cls="pin")
                svg.text(x + 16 + _tw("." + port, 11) + 4, yy, f"({expr})", cls="sub")
            yy += 14
        return

    seq = b.sequential
    stroke = "var(--seq)" if seq else "var(--comb)"
    svg.rect(x, y, w, h, fill="var(--box)", stroke=stroke, rx=4)
    # 时序块右边加一道竖线，和寄存器的画法呼应
    if seq:
        svg.raw(f'<line x1="{x + w - 7:.1f}" y1="{y + 3:.1f}" '
                f'x2="{x + w - 7:.1f}" y2="{y + h - 3:.1f}" '
                f'stroke="var(--seq)" stroke-width="2"/>')
    svg.text(x + 12, y + 15, b.label, cls="lbl" if seq else "lbl")
    line2 = ""
    if b.rhs:
        line2 = "读 " + ", ".join(b.rhs[:4]) + ("…" if len(b.rhs) > 4 else "")
    svg.text(x + 12, y + 32, line2, cls="sub")
    if b.lhs:
        svg.text(x + 12, y + 47, "写 " + ", ".join(b.lhs[:4])
                 + ("…" if len(b.lhs) > 4 else ""), cls="sub")
    if b.origin:
        # 出处写在左下角：这条语句不是手写的顶层语句，是 generate 展开出来的。
        # 没有这个标注，读者会去源码里找一行根本不存在的 assign。
        svg.text(x + 12, y + 62, b.origin + _repeat_suffix(b.repeat), cls="sub")
    if b.note:
        svg.text(x + 12, y + h - 12, "⚠ " + b.note, cls="warn")


def _flow_graph(mod: Module, design: Design | None):
    """分出源信号 / 逻辑块 / 被驱动的信号三组。

    判定只用两条集合关系，规则少 = 不容易出错：

        read    —— 被任何块或例化读到的信号
        written —— 被 assign / always / 例化输出驱动的信号

        源信号     = read 且非 written   （只读，画在左列）
        被驱动的信号 = written             （画在右列）

    "既读又写"的信号（计数器自己的 q、流水线寄存器）**只出现在右列**，
    由一条从右列绕回中列的回边来表达 —— 那正是反馈路径该有的样子。
    如果两边都画一份，图上看不出"这是一个寄存器"，反而更容易误解。
    """
    written = set(mod.assigned)
    read: set[str] = set()
    for b in mod.blocks:
        read.update(b.rhs)
    for inst in mod.instances:
        child = design.modules.get(inst.module) if design else None
        in_ports = ({p.name for p in child.ports if p.direction == "input"}
                    if child else None)
        for port, expr in inst.conns:
            # 只把**输入侧**的连接算成"读"；输出侧的名字是被驱动的
            if in_ports is None or not port or port in in_ports:
                read.update(_expr_signals(expr))

    known = mod.declared
    written &= known
    read &= known

    # 按"端口优先、声明其次"的原始顺序列出，比字典序更贴近源码
    order: list[str] = []
    for p in mod.ports:
        if p.name not in order:
            order.append(p.name)
    for d in mod.decls:
        if d.name not in order:
            order.append(d.name)

    srcs = [n for n in order if n in read and n not in written]
    sinks = [n for n in order if n in written]

    blocks = [b for b in mod.blocks if b.lhs or b.rhs]
    blocks = blocks + list(mod.instances)
    blocks.sort(key=lambda b: b.line)
    return srcs, blocks, sinks


def _instance_srcs(inst: Instance) -> list[str]:
    out: list[str] = []
    for port, expr in inst.conns:
        for n in _expr_signals(expr):
            if n not in out:
                out.append(n)
    return out


class SchematicError(Exception):
    """结构图无法生成。"""


def render_design(design: Design, out_dir: Path, top: str | None = None) -> list[Path]:
    """把层次图 + 每个模块的内部图都画出来。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    made.append(render_hierarchy_svg(design, out_dir / "hierarchy.svg", top))
    for name in design.order:
        m = design.modules[name]
        if not m.ports and not m.blocks and not m.instances:
            continue
        made.append(render_module_svg(m, out_dir / f"module_{name}.svg", design))
    return made
