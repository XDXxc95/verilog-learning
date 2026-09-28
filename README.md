# verilog-learning

个人 Verilog 学习仓库。目标方向：**扎实基础 + 可综合 RTL**（面向 FPGA / 数字 IC 打底）。

仓库自带一套**私有 Verilog 工具链**和**仿真自动判定脚本**：不依赖、也不修改系统环境（不写 PATH、不装 pip 包）。

---

## 五分钟上手

### 0. 确认工具链可用

```bash
python tools/setup_toolchain.py --check
```

输出结尾是 `状态：可用 ✓` 就没问题。显示「未找到」的话见下面 [工具链](#工具链)。

### 1. 跑一次仿真（编译 + 仿真 + 自动判定）

```bash
python tools/sim.py tests/_smoke/dut_counter4.v --tb tests/_smoke/tb_counter4.v --top tb_counter4
```

这一步做三件事：编译 → 跑仿真 → **按测试台打印的标记自动判对错**。

- **stdout 是一个 JSON**（给程序读的，可用 `--json 结果.json` 存盘）
- **stderr 是人看的摘要**
- **退出码 0 = 通过**，非 0 见下表

### 2. 看波形和结构图

```bash
python tools/viz.py tests/_smoke/dut_counter4.v --tb tests/_smoke/tb_counter4.v --top tb_counter4 --open
```

产物在 `build/viz/<顶层名>/`：

- `wave.html` —— 可交互波形（滚轮缩放、拖动平移、时间光标、明暗主题），双击也能直接开
- `module_*.svg` —— 每个模块的结构图
- `hierarchy.svg` —— 层次关系

加 `--ascii` 可以同时在终端打印一版 ASCII 波形，适合快速扫一眼。

---

## 课程分三级

教程按覆盖范围分三个级别，**开始学习前先选定级别**：

| 级别 | 名称 | 一句话定位 |
|---|---|---|
| **L1** | 数电知识学习验证 | 用 Verilog 把数电课本上的电路写出来验证，语法刻意做减法 |
| **L2** | 基础 Verilog 学习 | 全面学 Verilog，语言的各种使用场景都覆盖 |
| **L3** | 能力提升 | 面向实际数字 IC 设计工作，含流程、时序约束、验证方法学、**对接工作** |

**是阶梯，不是平行线**，但可以从中途进入（有基础就直接从 L2 或 L3 开始）。选定级别后仍要在该级内部做一次摸底，决定从第几讲切入。

详见 **[`curriculum/README.md`](curriculum/README.md)**，各级大纲：

- [`curriculum/L1-digital-logic.md`](curriculum/L1-digital-logic.md)
- [`curriculum/L2-verilog-language.md`](curriculum/L2-verilog-language.md)
- [`curriculum/L3-professional.md`](curriculum/L3-professional.md)

> **L3 有个前提要说清楚**：仓库自带的只有 iverilog（仿真器）。据此——
> **能补的**：装 yosys 就能真综合、看门数、比状态编码；装 verilator 能补真 lint；装 sby/eqy/z3 能做形式验证。这些都在 Windows 原生有方案。
> **补不了的**：STA、CDC 静态检查、DFT、功耗分析在 Windows 原生**无方案**（要 WSL2，本仓库默认不装），只能讲原理、读报告样例、手算 slack。
> **另外**：L3-18 的 SVA 无论工具怎么配都要改大纲——iverilog 不支持 `assert property`。
> 详见 [`curriculum/L3-professional.md`](curriculum/L3-professional.md)，体检用 `python tools/check_tools.py --level L3`。

## 目录结构

| 目录 | 放什么 | 入库 |
|---|---|---|
| `curriculum/` | 课程大纲：总览 + L1/L2/L3 三个级别文件 | ✓ |
| `exercises/` | 练习题：一讲一个文件夹，`README.md` 是**题目**，`*.v` 是作答 | ✓ |
| `solutions/` | 解答：`README.md` 是**解答说明**，`*.v` 是参考实现，`artifacts/` 是**实际运行产物** | ✓ |
| `tests/` | 测试台：`tests/L1-05/tb_decoder3_8.v`。`tests/_smoke/` 是工具链自检，不是课程内容 | ✓ |
| `tb/` | 公共测试台头文件 `tb_common.vh`（判定协议） | ✓ |
| `tools/` | 仿真、波形、结构图脚本 + `toolchain.json`（工具能力清单） | ✓ |
| `AGENT.md` | **agent 在这个仓库里的行为契约**（怎么用这些工具去教学） | ✓ |
| `progress/` | 学习进度与笔记 | ✓ |
| `build/` | 所有运行产物（vvp / vcd / html / svg） | ✗ 已 gitignore |
| `.tools/` | 私有工具链，约 25MB 二进制 | ✗ 已 gitignore |

**约定**：同一讲的东西用同一个编号，四处对齐。编号带级别前缀（`L1-` / `L2-` / `L3-`）——

```
exercises/L1-05/README.md          题目（详尽版，见下）
exercises/L1-05/decoder3_8.v       作答
tests/L1-05/tb_decoder3_8.v        测试台（顶层模块名固定为 tb_decoder3_8）
solutions/L1-05/README.md          解答说明（含逻辑分析、常见错误对照）
solutions/L1-05/decoder3_8.v       参考答案
```

**题目和解答都有固定规范，不会"只给一段代码"**：

- 题目详见 [`exercises/README.md`](exercises/README.md) —— 含功能描述、真值表/时序图、**完整接口定义**、边界示例、分层提示、常见错误预警、自检清单
- 解答详见 [`solutions/README.md`](solutions/README.md) —— 含设计思路与方案取舍、逐段分析、**综合出的电路**、**常见错误对照（现象→原因→后果）**、真实验证输出、扩展思考

---

## 常用命令

| 想干什么 | 命令 |
|---|---|
| 检查工具链 | `python tools/setup_toolchain.py --check` |
| 编译+仿真+判定 | `python tools/sim.py <design.v> --tb <tb.v> --top <name>` |
| 只做语法检查，不仿真 | `python tools/sim.py <design.v> --syntax-only` |
| 关掉 verilator 结构检查 | `python tools/sim.py ... --no-lint`（自研的锁存器扫描不受影响） |
| 波形 + 结构图一键出 | `python tools/viz.py <design.v> --tb <tb.v> --top <name> --open` |
| 只要波形 / 只要结构图 | `python tools/viz.py <design.v> --wave` / `--schematic` |
| 终端里看 ASCII 波形 | `python tools/viz.py <design.v> --tb <tb.v> --ascii` |
| 我的实现 vs 参考答案对照 | `python tools/viz.py --compare solutions/L1-05/decoder3_8.v exercises/L1-05/decoder3_8.v --tb tests/L1-05/tb_decoder3_8.v --top decoder3_8` |
| 生成一讲的产物包（出课程材料用） | `python tools/lesson.py L1-05` |
| **按级别体检工具能力** | `python tools/check_tools.py --level L2` |
| 看清单里有什么工具、装没装 | `python tools/setup_tools.py --list` |
| 装某个工具（进 `.tools/`） | `python tools/setup_tools.py --install yosys verilator --lock` |
| 工具链自检（改过工具就跑） | `python tests/_smoke/run_smoke.py` |

> **学到哪一级，工具就配到哪一级。** 仓库不预先装一大堆——L1 只需要 iverilog（已装），
> L2 想要 verilator + yosys 补 lint 和状态编码对比，L3 才涉及形式验证。
> `check_tools.py` 会告诉你「这一级缺什么、缺了会怎样、装什么能补」，
> **装不装由你定，缺工具不阻塞开课**。详见 [`tools/README.md`](tools/README.md)。

`--compare` 会把两份实现各跑一遍，产物并排放在 `build/viz/<顶层名>/<文件名>/`。

---

## 分工：判定 / 观察 / 叙述

这个仓库按 **AI agent 驱动学习** 的目标设计：学习者只写 Verilog，工具的事全透明。为此分三层，
**权威性单向**：

```
对话外壳（agent，契约见 AGENT.md）   ← 翻译、给台阶、说清什么没验证过。**不许下判定**
        ↑ 只读下面两层的输出
感知层 `_observe.py`                  ← observe 块：从 VCD 里量出来的**事实**（不是判断）
        ↑
判定内核 `sim.py`                     ← status / verdict。**唯一权威**
```

**为什么要多这一层**：学习者要的是"别让我碰工具"，所以 agent 得替他看结果。但 agent 手里只有
`PASS`/`FAIL` 时讲不出教学价值；而给它一张波形图让它自己看，它**一定会编时序**——
"你的 q 在第 20ns 变成 1"从图里读是猜的，且猜得非常自信。

所以中间加一层：`_observe.py` 只报告**量得到的量**，agent 只转述这些量。

`sim.py` 的 JSON 里因此多一个 `observe` 块：识别出的时钟（靠**跳变规律性**认，不靠信号名）、
最早的失败发生在**第几个时钟上升沿**、每个信号动过几次、有没有未知值。
拿不准就给 `null` 并说明原因，**不硬凑一个值**。用 `--no-observe` 可以关掉。

> `observe` 的任何失败都**不许影响判定**。它整块包在 try 里，坏了就少讲两句，
> `status` 一个字都不能变。详见 [`tools/README.md`](tools/README.md)。

---

## 判定结果怎么读

`sim.py` 的退出码：

| 码 | 含义 |
|---|---|
| `0` | 通过 |
| `1` | 不通过，或**证据不足**（见下） |
| `2` | 编译失败（语法错误、顶层模块名写错等） |
| `3` | 运行出错 / 超时 |
| `4` | 环境问题（工具链没找到） |
| `64` | 命令参数用错了 |

JSON 里的 `status` 字段比退出码更细：

| `status` | 含义 |
|---|---|
| `pass` | 所有用例通过 |
| `fail` | 有用例失败 |
| `compile_error` | 编译没过 |
| `inconclusive` | **证据不足**：测试台没按协议输出标记、没有 `TEST_DONE` 收尾、或一个用例都没跑 |
| `timeout` | 超时被强制终止 |
| `runtime_error` | 仿真非正常退出 |
| `env_error` / `usage_error` | 工具链缺失 / 参数错误 |

`error_tags` 会给出可能的原因标签，例如 `x_or_z`（出现 x/z，多半是复位没做）、`off_by_one`（差 1，边界问题）、`latch_inference`（组合逻辑推断出锁存器）、`combinational_loop`（输出绕回了自己的输入）。

> **重要**：仿真**正常退出不等于逻辑正确**。测试台不吭声时 `sim.py` 一律判 `inconclusive`，绝不判通过——所以测试台必须老老实实打印判定标记（见 `tb/tb_common.vh`）。同理，**用例全过也不等于设计对**：`latch_inference` / `combinational_loop` / `case_incomplete` 这三类，仿真可以全过而硬件是错的（iverilog 对此完全沉默），所以在 `error_tags` 里点出来——**它们永远不改变 `status`**。

`compile_error` 时 JSON 里带 `first_error`，包含行列号、出错行的源码片段，以及中文的 `hint_zh` / `why_zh` / `fix_suggestions_zh`，不用自己对着 iverilog 的输出猜。

### 三条诊断来源，权威性不一样

`compile.diagnostics[]` 里每条都带 `source`，说明**是谁说的**：

| `source` | 谁 | 有多硬 |
|---|---|---|
| `iverilog` | 编译器报错 | **硬**。编译过不去就是过不去 |
| `lint` | 自研 `_lint.py` 扫源码 | 半硬。零依赖永远能跑，只覆盖锁存器 |
| `verilator` | 外部 verilator lint | **软**。覆盖广（位宽/组合环/多驱动/未驱动），但和 iverilog 接受范围不同，报出来的不一定是错——**永远只是 warning，不改判定** |

加上 `lint` 块（`ran` / `ok` / `n_warnings` / `skipped_reason_zh`），用来回答"到底检查了没有"：
**`ran: false`（没跑）和 `n_warnings: 0`（跑了，干净）含义相反**，别看错。

> verilator 没装也不影响判定——`lint.ran` 会是 `false` 并附一句说明。装法：
> `python tools/setup_tools.py --install verilator --lock`。详见 [`tools/README.md`](tools/README.md)。

---

## 写测试台

测试台靠**打印约定格式的标记行**让 `sim.py` 判对错，协议定义在 `tb/tb_common.vh`，最常用的就三个宏：

```verilog
`timescale 1ns/1ps              // 必须写，时间单位要明确
`include "tb_common.vh"         // sim.py 会自动把 tb/ 加进 include 路径

module tb_counter4;
    reg clk = 0, rst_n, en;
    wire [3:0] q;
    counter4 dut (.clk(clk), .rst_n(rst_n), .en(en), .q(q));

    `VLEARN_TIMEOUT_GUARD(100000)   // 协作式超时，放模块层级，不放 initial 里

    always #5 clk = ~clk;

    initial begin
        $dumpfile("build/counter4.vcd");   // 波形产物统一写进 build/
        $dumpvars(0, tb_counter4);
        ...激励...
        `VLEARN_CHECK("reset", 4'h0, q)
        `VLEARN_CHECK("count1", 4'h1, q)
        vlearn_summary;              // 打印 TEST_SUMMARY + TEST_DONE
        $finish;
    end
endmodule
```

顶层模块名统一叫 `tb_<设计名>`。

改 `tb_common.vh` 前**务必**先读文件头的说明——里面有 iverilog 的两个坑（宏参数会在字符串字面量里被替换、任务里 `$time` 会用错时间单位），踩了不会报错，只会悄悄出垃圾数据。

---

## 工具链

iverilog（含 GTKWave）装在仓库私有的 `.tools/iverilog/` 下，**不写系统 PATH**，所以：

- 你在任何其它终端里敲 `iverilog` 都不会找到它——这是故意的，仓库不影响你的环境；
- 所有脚本自己会去 `.tools/iverilog/bin` 找，不需要你配环境变量；
- 万一想手动跑：`.tools/iverilog/bin/iverilog.exe`、`vvp.exe`、`gtkwave.exe`。

重装：

```bash
python tools/setup_toolchain.py --install    # 下载并安装到 .tools/
python tools/setup_toolchain.py --selftest   # 装完编译+仿真跑一遍验证
```

> 安装包会**弹窗要求点「下一步」**，这是安装程序本身的行为，压不住；装到一半卡住就是在等这个弹窗，去点一下就行。

当前已装：Icarus Verilog 14.0 + GTKWave 3.3.128。波形默认用生成的 HTML 看，GTKWave 备用。

---

## 两个阶段与分支

| 阶段 | 分支 | 做什么 |
|---|---|---|
| **搭建阶段（当前）** | `main` | 工具链、课程大纲、出题规范、文档。**不含课程内容** |
| **学习阶段** | 新建学习分支 | 选级别 → 摸底 → 出题/作答/解答 → 记进度 |

`main` 是**模板分支**，用于上传 GitHub 共享；个人的作答和 `progress/` 记录留在学习分支上，不进 `main`。

## 现在到哪一步了

搭建阶段，基建已完成：

- ✅ 工具链 + 仿真判定 + 波形/结构图 —— `python tests/_smoke/run_smoke.py` 全部通过
- ✅ 三级课程大纲 —— L1 / L2 / L3 已定稿（含 13 道章末综合题），见 `curriculum/`
- ✅ 出题与解答规范 —— 见 `exercises/README.md`、`solutions/README.md`
- ✅ **每讲产物包生成** —— `python tools/lesson.py <讲号>` 一键出「运行记录 + 波形 + 结构图」，
  产物入库到 `solutions/<讲号>/artifacts/`，见 [`solutions/README.md`](solutions/README.md)
- ✅ **工具能力清单** —— `tools/toolchain.json` 记「能力 → 需要什么软件 → 缺了会怎样」，
  `check_tools.py` 按级别体检，`setup_tools.py` 按需安装。见 [`tools/README.md`](tools/README.md)
- ✅ **感知层 + agent 契约** —— `_observe.py` 从波形量出可照讲的事实，`sim.py` 输出 `observe` 块；
  `AGENT.md` 定下 agent 该怎么用这些工具去教学。见上面[分工](#分工--判定--观察--叙述)
- ✅ **lint 层** —— 编译通过后再跑一遍结构检查：自研 `_lint.py`（锁存器，零依赖）+
  外部 verilator（位宽/组合环/多驱动/未驱动）。**只加 warning，永不改判定**。
  负例 `tests/_smoke/neg/dut_comb_loop.v` 展示的正是「三条用例全过 + 报出组合环」
- ⬜ **开始学习** —— 等明确指令，届时**先新建学习分支**，再选级别
