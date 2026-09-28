# tools —— 仿真与可视化工具

只用 **Python 标准库**，不依赖任何 pip 包（这是硬性约束，见根目录 `CLAUDE.md`）。
Verilog 工具链在仓库私有的 `.tools/iverilog/`，脚本自己会去找，不需要配环境变量。

## 六个命令行入口

| 文件 | 作用 |
|---|---|
| `sim.py` | **编译 + 仿真 + 自动判定 + 波形观察**。stdout 是一个 JSON，stderr 是人看的摘要 |
| `viz.py` | 一键出波形 + 结构图（内部调用 `sim.py`）。**学习时用**，产物进 `build/` |
| `lesson.py` | 为一讲生成完整产物包。**出课程材料时用**，产物进 `solutions/<讲号>/artifacts/` |
| `check_tools.py` | **按课程级别体检工具能力**，报「缺什么、缺了会怎样、装什么能补」 |
| `setup_tools.py` | 按清单安装工具到 `.tools/`（tgz/zip 解压，不运行下载来的程序） |
| `setup_toolchain.py` | 装 iverilog（Inno Setup 安装包，要静默安装 + 搬迁）+ 自检 |

## 工具能力清单（`toolchain.json`）

这个仓库会按课程级别用到不同的工具：L1 只要 iverilog，L2 开始想要 verilator（真 lint）
和 yosys（状态编码对比），L3 才涉及形式验证。**清单驱动，不写死在代码里**——加工具只改 JSON。

两张表互链：

| 表 | 给谁看 | 内容 |
|---|---|---|
| `capabilities[]` | 学习者 | 能力 → 属于哪级 → 依赖哪个软件 → **缺了会怎样** |
| `software[]` | 安装用 | 软件 → Windows 可得性 → 下载源 → 体积 → 装到哪 → 怎么探测 |

```bash
python tools/check_tools.py --level L2    # 体检：缺什么、装什么能补
python tools/setup_tools.py --list        # 清单里有什么、装没装
python tools/setup_tools.py --for-level L2 --lock
```

**「缺了会怎样」分四级**（这是给使用者做判断的信息，不是硬门槛）：

| 级别 | 含义 |
|---|---|
| 🔴 `blocks` | 整讲做不了，必须改大纲 |
| 🟠 `degrades` | 动手环节塌掉，主课能上 |
| 🟡 `shrinks` | 缩水但主课能上 |
| ⚪ `talk_only` | **大纲从未承诺能动手**，缺工具不构成缺陷 |

⚠️ **三种「缺」要分清楚，混了就会给出误导性的建议**：

| `gap` | 含义 | 报告里怎么呈现 |
|---|---|---|
| `software` | 缺软件，装了就好 | 进「装上这些可补上面的缺口」 |
| `code` | **本仓库还没实现**，装什么都没用 | 明说「缺的是代码不是软件」 |
| `software` + WSL-only | 缺的软件 **Windows 原生装不了** | **单列**，不进安装建议 |

第三类最容易搞错：`opensta` / `openroad` 是 WSL-only，`setup_tools.py` 会拒绝安装。
如果它们被混进「装上这些可补」的清单里，使用者照着装会发现装不上——所以
`CapabilityStatus.unavailable_software` 把它们单拎出来，并且**不算 `actionable`**。
`setup_tools.py --for-level` 取的正是过滤后的清单，不会白下载。

`software[]` 里还可以写 **`caveat`**（装之前得知道的坑）。典型例子是 verilator：
`--lint-only` 装完即用，但覆盖率、并发断言走的是 `--binary`，需要 C++ 编译器 + `make`。
没有 `caveat` 的话，报告读起来就是「装上它就好了」，而实际不是。

### ⚠️ 「文件在」≠「能用」：`check` 里的三个字段

这一条是踩出来的，不是设计出来的。**只看可执行文件在不在，会堂堂正正地谎报。**
探测结果因此分三档：`✓ 可用` / `⚠ 已安装但不可用` / `✗ 未安装`。中间那档全靠 `check` 里这几个字段：

| 字段 | 作用 | 不写会怎样 |
|---|---|---|
| `env` | 运行**必需**的环境变量，值里的 `${dest}` 会替换成安装目录 | verilator 会报 `Cannot find verilated_std_waiver.vlt`——它的 `VERILATOR_ROOT` 在构建时烘死成了 `/yosyshq` |
| `lib_dirs` | 相对安装目录的共享库目录，**并入** `PATH` 最前面 | yosys.exe 起不来（`STATUS_DLL_NOT_FOUND`）。注意是"并入"不是"覆盖"：覆盖会把系统 PATH 冲掉，且不报错 |
| `functional.args` | 真跑一次的**功能自检**命令，退出码非 0 即判为不可用 | 上面两个配错了只会显示成「已安装 ✓」，能力却一项都没补上 |

`functional` 的必要性：`--version` 在几乎所有坏情况下都能成功打印。verilator 就是典型——
版本号打得出来，`--lint-only` 却因为 `VERILATOR_ROOT` 而失败。**只看版本号等于没查。**
自检样本放 `tools/probe/`（当前是 `vlearn_probe.v` / `vlearn_probe.cc`），
改这些样本务必跑 `tests/_smoke/check_manifest.py`——样本自己 lint 不干净会让自检全红。

两类典型误报，都已实测复现过：

- **verilator**：装完 `--version` 正常，lint 却失败（`VERILATOR_ROOT` 烘死）。
- **g++**：系统 PATH 上有个 g++ 不代表能用。本机那个 Qt 自带的 mingw 7.3
  连 `-fcf-protection=none`（GCC 8+ 才有）都不认，而 verilator 是按 GCC 15.2.1 配的。
  没有 `functional` 的话，它会以"找到了系统 PATH 上的 g++"为由被判为合格。

⚠️ **可复现性**：OSS CAD Suite 的 `bucket-*` 是**滚动 tag**，同名资产每天被覆盖。
要固定版本必须 `--lock` 记 sha256——不带 `--lock` 的安装等于「装到哪个算哪个」。

### 判定 / 观察 / 叙述：三层，权威性单向

```
对话外壳（agent，契约见 ../AGENT.md）   ← 翻译、给台阶、说清什么没验证过。不许下判定
        ↑ 只读下面两层的输出
感知层 `_observe.py`                    ← observe 块：从 VCD 里量出来的**事实**
        ↑
判定内核 `sim.py`                       ← status / verdict。**唯一权威**
```

**为什么要把"观察"和"判定"分成两层**：学习者的诉求是「我只写 Verilog，
工具别烦我」，那 agent 就得替他看结果。但 agent 手里只有 `PASS`/`FAIL` 时
讲不出任何有教学价值的话；而给它波形图让它自己看，它**一定会编时序**——
"你的 q 在第 20ns 变成 1"这种话从图里读是猜的，且猜得非常自信。

所以中间加一层：`_observe.py` 只报告**量得到的量**，agent 只转述这些量。

```jsonc
"observe": {
  "ok": true,
  "clock": {"signal": "tb_shift.clk", "period_text": "10ns",
            "detected_by": "跳变规律性（非信号名）"},
  "first_divergence": {"case": "q2_still_old_this_cycle", "time_text": "26.000ns",
                       "edge_index": 3, "edge_time_text": "25ns",
                       "expected": "0", "got": "1", "n_failed": 2},
  "signals": [{"path": "tb_shift.q2", "n_changes": 4, "changed": true,
               "initial_value": "x", "n_glitches": 0, "timeline": [...]}],
  "notes": []
}
```

几条刻意的设计，改之前先看：

- **拿不准就给 `null` 并附 `notes`，不硬凑一个值。** 找不到时钟就是 `null`
  （纯组合逻辑本来就没有时钟），信号读不出来就是 `ok:false` + `reason`。
  和 `schematic.py` 一样：宁可说测不出来，也不给一个看着合理但是编的。
- **`observe` 的任何失败都不许影响判定。** `sim.py` 的 `_observe_block()`
  整块包在 try 里，异常降级成 `ok:false`。仿真跑完了、对错判出来了，
  读波形只是额外一层辅助——它坏了就少讲两句，`status` 一个字都不能变。
- **一条完整协议都没有的波形报 `ok:false`，不是"成功但没有信号"。**
  后者读起来像一句事实，其实什么都没验过。
- **时钟靠跳变规律性认，不靠信号名。** 叫 `clk` 的不一定是时钟，
  选中的信号和 `detected_by` 都写进结果，便于核对。
- **不猜哪个作用域是待测模块。** 名字叫 `dut` 只是惯例，VCD 里没有
  实例到模块类型的映射。只给作用域清单，不下结论。
- **同一根线在测试台和待测模块两侧各有一条记录时，只标注不合并。**
  iverilog 不一定合并它们的 ID（实测：1 位端口合并，带 `[3:0]` 的不合并）。
  合并要靠"值和时刻完全一致"来判，两个碰巧同步的独立信号会被错并——
  那比多列一条记录坏得多。用信号上的 `also_in` 指明另一处。

### lint：两层扫描，一条铁律

设计文件编译通过之后，`sim.py` 会再跑一遍结构检查。**两个来源，都并进
`compile.diagnostics`，靠 `source` 字段区分**：

| 来源 | 模块 | 覆盖 | 装了 verilator 才有？ |
|---|---|---|---|
| `lint` | `_lint.py`（自研，纯 Python 扫源码） | 只有锁存器一类，但**零依赖、永远能跑**，报出来的一定推得出来 | 否 |
| `verilator` | `_vlint.py`（外部） | 位宽截断/扩展、组合环、多驱动、未驱动、未使用 | 是 |

两者是**互补不是替代**——verilator 缺席时 `_lint.py` 照常工作，只覆盖锁存器；
verilator 在场时，同一个锁存器问题**不重复报**（按 `(文件, 类别)` 去重，
保留解释更细的自研那条）。

**铁律：lint 一条诊断都不许改 `status`。** 理由不是"警告不重要"，而是
verilator 和 iverilog 的**接受范围不同**——iverilog 编得过而 verilator 报错的
代码是存在的。让 lint 有机会把 `pass` 翻成 `fail`，学习者就会看到
「代码明明跑通了，工具却说过不了」，而真正的原因只是另一个工具的方言差异。
所以 verilator 的 `%Error` **也不进 `diagnostics`**（那通常是方言差异，
混进中文提示里只会污染教学话术），只进 `lint.errors[]` 并附一句"不影响本次判定"。

三条容易踩的：

- **`ran`（真跑到 elaboration 了没有）的硬判据是那行 `Verilation Report`**，
  不是"有没有输出"、更不是"有没有警告"。verilator 把标题打成
  `V e r i l a t i o n   R e p o r t`（逐字符插空格，两个词的拆法还不一样），
  照源码写死字面量匹配不上——见 `_spaced()`。
- **不用 `-Wall`**。它会打开 `DECLFILENAME`（本仓库约定 `dut_counter4.v` 里放
  `counter4`，按 -Wall 几乎每个文件都报）和 `MULTITOP`（哪个是顶层是构建配置
  问题，iverilog 那边用 `-s` 指定）。噪声的代价不是多几行字，而是学习者开始
  整体忽略这个工具。改成显式打开 `MULTIDRIVENPROC` / `UNDRIVEN` / `UNUSEDSIGNAL`
  三项——它们默认关着，而 `MULTIDRIVENPROC` 正好是 iverilog **完全沉默**的一类。
- **`--no-lint` 关得掉**，但关掉之后的结果**不能当"lint 通过"的证据**：关掉和
  干净在 JSON 里长得一模一样（都是零条诊断），所以别拿它下结论。

`tests/_smoke/neg/dut_comb_loop.v` 是这条链路的负例，形状很说明问题：
`assign q = en ? d : q;` —— **测试台三条用例全过**（`verdict.passed: 3`），
而 `error_tags` 里躺着 `combinational_loop`。这正是「仿真通过但硬件是错的」，
和 `latch_inference` 同一类。iverilog 对它**连一句 warning 都不给**
（实测 `-g2012 -Wall -t null` 全静默），只有 verilator 报得出来。

`viz.py` 和 `lesson.py` 的分工：前者跑学习者自己的代码、产物用完即弃；后者跑参考答案、产物是**课程材料的一部分**（入库）。两者都不会自己解析任何东西，只是把 `sim` / `vcd` / `wave` / `schematic` 四块拼起来——**判定逻辑保证只有一份**。

```bash
python tools/lesson.py L1-05          # 生成 L1-05 全部模块的产物包
python tools/lesson.py --list         # 列出已有讲次
python tools/lesson.py --all          # 全部重生成
```

常用命令见根目录 `README.md`。每个脚本 `--help` 都有完整说明和示例。

## 内部模块（不要直接当命令跑）

以下划线开头的是内部共享模块：

| 文件 | 职责 |
|---|---|
| `_toolchain.py` | 工具链定位。**唯一**一处决定去哪找 iverilog（顺序：显式参数 → `IVERILOG_BIN` → `.tools/` → 系统常见路径 → PATH） |
| `_manifest.py` | 能力清单的加载 / 校验 / 探测。**不硬编码任何工具名**，加工具只改 JSON |
| `_proc.py` | 子进程执行 + Windows 上的进程树强杀（job object 为主，`taskkill /F /T` 兜底）+ 超时 |
| `_lint.py` | 源码静态检查，目前只做一件事：**抓组合逻辑推断出的锁存器**。策略是"宁可漏报，不可冤枉" |
| `_vlint.py` | 调外部 verilator 做真 lint（位宽 / 组合环 / 多驱动 / 未驱动 / 未使用）。**只加 warning，永不改 status**，详见下节 |
| `_observe.py` | 从 VCD 里量出「可以照着讲」的事实（时钟、首次分歧在第几个沿、每个信号动没动过）。**只测量不推断**，拿不准给 `null` |
| `_console.py` | 把控制台切到 UTF-8。**每个 CLI 入口都要调 `init_console()`**，否则 GBK 控制台遇到 `✓` 会崩 |

## 库（没有命令行）

| 文件 | 职责 |
|---|---|
| `vcd.py` | VCD 解析。不支持 `$dumpports` 等扩展命令 |
| `wave.py` | 波形渲染，两套后端：终端 ASCII（会丢窄脉冲，只看大结构）和自包含 HTML（交互式，主用这个） |
| `schematic.py` | 结构图 SVG。**这不是语法分析器**，是正则 + 缩进深度的提取器 |

`schematic.py` 的设计原则值得记住：**宁可说"画不出来"，也不画一个看着合理但是错的。** 遇到 `generate`、参数化例化它会直接把整个模块标成 `unsupported`，这是有意为之，别为了"支持更多"去猜。

## 改动规矩

改完**必须**跑：

```bash
python tests/_smoke/run_smoke.py
```

19 个仿真用例 + 8 个工具检查（波形 JS/DOM 两个需要 node，缺了自动跳过；
5 个用例需要 verilator，缺了报 `⊘` 跳过而不是判失败）。加新能力时**同时加一个
负例**——能证明它真的抓得到错，否则不算完成。

lint 那几条负例里有两个刻意的设计，别"优化"掉：

- **三个需要 verilator 的用例之外，另两个用例不需要装 verilator 也能跑**：
  「verilator 不在」是靠 `--verilator-path` 指向一个不存在的文件造的，
  「程序起来了但压根不是 verilator」是靠指向 `iverilog.exe` 造的——
  一个真进程、有输出、但没有 `Verilation Report`。后者覆盖的是否则永远走不到的
  分支，而且**与环境无关**：换台机器结论一样。
- **`check_vlint.py` 不跑 verilator**，用的是手写输出样本。理由和
  `check_observe.py` 一样：要验的是"解析规则"，不是 verilator 的措辞——
  手写才能精确造出「报告行被逐字符拆开」「Windows 盘符路径」「`%Error`
  混在警告里」这些形状，而靠真实运行去凑，换个 verilator 版本就变了。
  它最核心的一条断言是：**`parse_output("")` 分不出「没跑」和「干净」**，
  这个区分只活在 `_REPORT_RE` 里。

`check_observe.py` 的波形是**手写**的，不跑 iverilog：要验的是"解析与提炼的规则"，
不是 iverilog 的转储格式，所以不该依赖 iverilog。手写才能精确造出
「时钟叫 `clk` 但跳得乱七八糟」「总线跳得比时钟还快」「只有 t=0 一条记录」
这类形状——靠仿真去凑要么凑不出来，要么哪天换个 iverilog 版本就变了。

`check_manifest.py` 里那几组负例值得注意，它们防的是**「坏输入但照常报成功」**：

- 清单坏了（JSON 错、悬空引用、未知 impact）→ 必须**大声报错**，绝不能降级成空清单谎报「全部可用」
- `gap: "code"` 的能力没有软件依赖，如果不显式排除，会被判成 `ok=True`——于是「多参数批量回归」这种压根没写的功能显示「可用」
- **「文件在」不能当成「能用」**：能力依赖的满足条件是 `found and usable`。少了 `usable` 这一半，一个启动就崩的工具照样能把能力"补齐"，报告上却看不出任何异常
- `lib_dirs` 必须**并入** PATH 而不是覆盖它——覆盖不报错、不崩溃，只是子进程再也找不到系统命令
- **WSL-only 的依赖必须和可安装的依赖分开报告**，否则「装上这些可补」会列出一份装不上的清单，`--for-level` 还会照着它去发起下载
- 归档路径穿越（tar/zip 的 `../` 成员）必须拦住；正常归档仍要能解压，**否则「什么都拦」也会让上面两条假通过**
- WSL-only 的软件必须**在下载前**拒绝，不能白下几百 MB 再失败
- **观察层拿不到波形时，判定结论一个字都不能变**：同一个设计换个不产 VCD 的测试台，
  该是 `inconclusive` 就还是 `inconclusive`，不能因为少了一层辅助信息就崩掉或变成 `pass`
- **找不到时钟要给 `null`，不能硬报一个周期**。这条负例的 fixture 必须让
  `observe.ok` 为 `true`（波形真读到了、里面确实没有时钟）——否则就是拿"没波形"
  冒充"没时钟"，断言绿得毫无意义

> 这些负例是**真的验过**的：把护栏逐个拆掉重跑，确认对应断言确实会红。没验过的负例等于没有——
> 它可能只是恰好没触发。拆护栏那一下很便宜，别省。
