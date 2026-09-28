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

* **`generate` / 参数化例化**：展开需要知道参数的实际取值，靠文本推不出来。
  整个模块的内部图标记为不支持（层次图仍然给，因为例化关系是确定的）。
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
        return f"[{self.width}]" if self.width and self.width > 1 else ""


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


@dataclass
class Module:
    name: str
    line: int = 0
    file: str = ""
    params: list[str] = field(default_factory=list)
    ports: list[Port] = field(default_factory=list)
    decls: list[Decl] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)
    unknown_names: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
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


def _split_top_commas(s: str) -> list[str]:
    out: list[str] = []
    depth = 0
    cur = 0
    for i, c in enumerate(s):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(s[cur:i])
            cur = i + 1
    out.append(s[cur:])
    return [x.strip() for x in out if x.strip()]


def _width_from(range_text: str) -> tuple[int | None, str]:
    """把 `[7:0]` 解析成位宽 8。`[WIDTH-1:0]` 这种含参数的返回 None。"""
    m = _RANGE_RE.search(range_text)
    if not m:
        return None, range_text.strip()
    hi, lo = m.group(1).strip(), m.group(2).strip()
    if hi.isdigit() and lo.isdigit():
        w = abs(int(hi) - int(lo)) + 1
        return w, range_text.strip()
    return None, range_text.strip()


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
            nm = re.search(r"\b(?:parameter|localparam)?\s*([A-Za-z_]\w*)\s*=", chunk)
            if nm:
                mod.params.append(nm.group(1))
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

    _parse_ansi_ports(mod, header_ports)

    body_end = text.rfind("endmodule")
    body = text[body_start:body_end if body_end > 0 else len(text)]

    # ---- 模块体 ----
    # iter_statements(body) 给出的行号是**相对 body** 的。
    # 读者要的是文件里的行号（好去编辑器里跳转），所以要补两段偏移：
    # body 在模块文本里的行偏移 + 模块文本在文件里的行偏移。
    # 不补的话每个模块都从"第 1 行"开始报，等于没有行号。
    line_base = (line - 1) + (_line_of(text, body_start) - 1)
    for stmt, stmt_line in iter_statements(body):
        _classify_statement(mod, stmt, stmt_line + line_base)

    _collect_unknowns(mod)
    return mod


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
        w, wtext = _width_from(cur_width_text) if cur_width_text else (None, "")
        mod.ports.append(Port(name=name, direction=cur_dir, width_text=wtext,
                              width=w, signed=cur_signed, reg=cur_reg,
                              line=mod.line))


def _first_ident(s: str) -> str:
    s = re.sub(r"\[[^\]]*\]", " ", s)
    m = _IDENT_RE.search(s)
    return m.group(1) if m else ""


def _classify_statement(mod: Module, stmt: str, line: int) -> None:
    s = stmt.strip()
    if not s:
        return
    head_m = re.match(r"([A-Za-z_]\w*)", s)
    if not head_m:
        return
    head = head_m.group(1)

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
    if head == "generate":
        mod.unsupported.append(
            f"第 {line} 行有 generate：展开需要知道参数的实际取值，靠源码推不出来，"
            f"内部结构图已停用"
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
    w, _ = _width_from(wtext) if wtext else (None, "")
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
    w, _ = _width_from(wtext) if wtext else (None, "")
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
    rhs_names = _expr_signals(m.group(2))
    mod.blocks.append(Block(kind="assign", line=line, lhs=lhs_names, rhs=rhs_names))


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

    lhs, rhs = _scan_always_body(body)
    nonblocking = _has_depth0_assignment(body, "<=")

    blk = Block(kind="always", line=line, lhs=lhs, rhs=rhs,
                clock=clock, reset=reset, nonblocking=nonblocking,
                sensitivity=sens if sens != "*" else "*")
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


def _scan_always_body(body: str) -> tuple[list[str], list[str]]:
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

    return lhs, _expr_signals(" ".join(read_text_parts))


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


def _expr_signals(expr: str) -> list[str]:
    """表达式里出现的、**不是关键字也不是数字**的标识符（保持出现顺序、去重）。"""
    cleaned = _LITERAL_RE.sub(" ", expr)
    out: list[str] = []
    for m in _IDENT_RE.finditer(cleaned):
        w = m.group(1)
        if w in _KEYWORDS:
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

    mod.instances.append(Instance(module=sub, name=inst_name,
                                  conns=conns, params=params, line=line))


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
            lines = 1 + (1 if b.lhs else 0) + (1 if b.rhs else 0)
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
        _signal_pill(svg, name, mod.width_of(name), COL_X[0], y, COL_W[0], "in")
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
        _signal_pill(svg, name, mod.width_of(name), COL_X[2], y, COL_W[2], "out")
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


def _signal_pill(svg: Svg, name: str, width: int | None,
                 x: float, y: float, w: float, side: str) -> None:
    h = 20.0
    label = name
    if width and width > 1:
        label += f" [{width}]"
    if side == "in":
        svg.rect(x, y, w, h, fill="var(--box)", stroke="var(--line)", rx=10)
        svg.text(x + 10, y + h / 2, label, cls="lbl")
    else:
        svg.rect(x, y, w, h, fill="var(--box)", stroke="var(--line)", rx=10)
        svg.text(x + 10, y + h / 2, label, cls="lbl")


def _block_box(svg: Svg, b, x: float, y: float, w: float, h: float) -> None:
    if isinstance(b, Instance):
        sub = ""
        if b.params:
            sub = "#(" + ", ".join(f"{k}={v}" for k, v in list(b.params.items())[:2]) + ")"
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
