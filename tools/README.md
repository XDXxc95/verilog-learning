# tools —— 仿真与可视化工具

只用 **Python 标准库**，不依赖任何 pip 包（这是硬性约束，见根目录 `CLAUDE.md`）。
Verilog 工具链在仓库私有的 `.tools/iverilog/`，脚本自己会去找，不需要配环境变量。

## 十个命令行入口

| 文件 | 作用 |
|---|---|
| `sim.py` | **编译 + 仿真 + 自动判定 + 波形观察**。stdout 是一个 JSON，stderr 是人看的摘要 |
| `viz.py` | 一键出波形 + 结构图（内部调用 `sim.py`）。**学习时用**，产物进 `build/` |
| `lesson.py` | 为一讲生成完整产物包。**出课程材料时用**，产物进 `solutions/<讲号>/artifacts/` |
| `regress.py` | **参数化批量回归**：一个设计 × 一组参数组合，逐个跑再汇总。防「扫了但没扫到区别」 |
| `synth.py` | **综合与资源估算**。零下载两条腿：`-t sizer` 出 FF/门数、`--netlist` 出 blif 网表；`--engine yosys` 是第三条腿——真做优化 + 工艺映射，并支持 `--encoding-compare` 三种状态编码 |
| `cov.py` | **功能覆盖率合并**：多份测试台的覆盖点**取并集**（不是平均），并抓「命中名拼错」 |
| `toggle.py` | **逐位翻转计数**：从波形里数状态寄存器翻了多少位（动态功耗的代理量）。零下载，综合器量不出这个 |
| `check_tools.py` | **按课程级别体检工具能力**，报「缺什么、缺了会怎样、装什么能补」 |
| `setup_tools.py` | 按清单安装工具到 `.tools/`（tgz/zip 解压，不运行下载来的程序） |
| `setup_toolchain.py` | 装 iverilog（Inno Setup 安装包，要静默安装 + 搬迁）+ 自检 |

后四个（`regress` / `synth` / `cov` / `toggle`）**都不需要额外下载任何东西**，
iverilog 自带的后端就够跑。它们对应的能力项因此是 `gap: "none"`。

★ 状态编码的代价是**两半**，得两边都量：`synth.py` 那半是面积
（sizer 给触发器数和门数），`toggle.py` 那半是翻转
（**综合器量不出来**——yosys 也不做活动性分析，它不知道哪些跳变是"热的"）。
所以 `synth.encoding` 即使装了 yosys 也只是补齐了一半，另一半永远来自仿真。

★ **「让综合器自己挑编码」2026-09-29 已接入**（`--engine yosys`），做法和上面这两半
都不是一回事：它不量你已经写好的分配，而是让工具**替你做状态分配**。
限制见下面「yosys 引擎」一节——`fsm_recode` 只有 one-hot 和 binary，没有 gray。

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

⚠️ **四种「缺」要分清楚，混了就会给出误导性的建议**：

| `gap` | 含义 | 报告里怎么呈现 |
|---|---|---|
| `software` | 缺软件，装了就好 | 进「装上这些可补上面的缺口」 |
| `code` | **本仓库还没实现**，装什么都没用 | 明说「缺的是代码不是软件」 |
| `software+code` | **两边都缺**：软件要装，装完还得补代码 | 明说「只补一边都不算齐」 |
| `software` + WSL-only | 缺的软件 **Windows 原生装不了** | **单列**，不进安装建议 |

第三类（`software+code`）是 2026-09-29 加的第 4 个取值，**当前有 7 条**：
`cov.code`、`lint.style`、`sta.estimate`、`sva.assert`、`cdc.formal`、`formal.prop`、`formal.lec`。
其中 6 条原先标的是 `software`（理由是「卡口是 X 不是代码」），
但那个理由把**优先级**当成了**状态**——软件装没装，跟代码缺不缺是两件事。
软件一装，这几条就被报成可用，**L3 可用数因此虚高到 11/19，实际是 6/19**。

★ **`sva.assert` 是第 7 条，而且是「漏网」的那一条**：前 6 条在 2026-09-29 那次
集中修正里被找出来了，它没有——因为当时 `mingw` 还没装，它读起来只是「要装
verilator + mingw」。**等 mingw 真装上的那一刻，它立刻被判成「可用」，而实测跑不出来**
（见下节的实测表）。所以它既是一次修正，也是那条「谎会延迟到装完才发作」的**现场复现**：
空谈状态永远看不出问题，得真去装一次。
它和 `code` **不能挤成一位**：`code_gap` 对「只缺代码」和「两边都缺」都是 True，
而两句给使用者的下一步完全相反——前者「等仓库补，装什么都没用」，
后者「可以先装起来，但装完还不齐」。所以 `CapabilityStatus` 上
`code_gap` / `software_gap` / `both_gaps` 是三个独立的位，
`evaluate()` 里 `ok` 的条件也从 `gap != "code"` 改成 `not c.needs_code`。
⚠️ 这 7 条的软件**现在全都已就位**，所以 `software_gap` 基本是 False、
`check_tools` 对它们一律输出「缺的是代码不是软件」——
**别再说它们「要先装 X」**。
**最要命的是这个谎会延迟发作**：写错成 `software` 时，装之前报告一切正常，
装上 mingw 那一刻才变成「一边报可用、一边打印 caveat 说不可用」。
负例见 `check_manifest.py` 负例 8a——它同时验了装之前和装之后两个状态。

★ **2026-09-29 mingw 装完，这个延迟发作的谎真的发作了一次**：`sva.assert` 当时标的是
`software`（requires `verilator` + `mingw`），两个一装齐，`evaluate()` 立刻判它**可用**，
而实测**根本跑不通**——verilator 的 C++ 链路还有 MAKE / 硬编码的 `PYTHON3` /
`CFG_CXXFLAGS_STD` / `sc_time_stamp` / `verilator_coverage` 五关没接（见下节），
本仓库一行代码都没写。`mingw.caveat` 里明明写着「这一步尚未实现」，
`gap` 却让它判成了可用。**已改成 `software+code`**，L3 可用数从虚高的 8/19 回落到 7/19
（剩下那条 `toolchain.cc` 是真涨的：本仓库现在真有一个能用的现代 g++ 了）。

第四类最容易搞错：`opensta` / `openroad` 是 WSL-only，`setup_tools.py` 会拒绝安装。
如果它们被混进「装上这些可补」的清单里，使用者照着装会发现装不上——所以
`CapabilityStatus.unavailable_software` 把它们单拎出来，并且**不算 `actionable`**。
`setup_tools.py --for-level` 取的正是过滤后的清单，不会白下载。

`software[]` 里还可以写 **`caveat`**（装之前得知道的坑）。典型例子是 verilator：
`--lint-only` 装完即用，但覆盖率、并发断言走的是 `--binary`，需要 C++ 编译器 + `make`。

### ★ 一个归档喂多个条目（2026-09-29 起）

清单里有 **8 条指向同一个归档**：`yosys` / `verilator` / `formal` / `eqy` / `z3` /
`slang` / `oss-utils` / `oss-system-resources` 都来自
`oss-cad-suite-windows-x64-20260928.tgz`（572.2 MB），都往 `.tools/oss` 里放东西。
这是**有意**的，不是复制粘贴错了。

（`slang` 是 2026-09-29 补进来的第 8 条，原先指向 MikePopoloski 的独立 zip 5.1 MB。
改成整包的理由不是「想统一」，是那句依据**本身是错的**：清单里写着「套件里只有
yosys-slang-plugin，没有独立的 slang.exe」，实测 `.tools/oss/bin/slang.exe` 就在那儿，
加上 `lib_dirs` 后直接能 lint。这跟「iverilog 跑不了综合/STA」是同一种错：
一句没人验过的断言被当成事实写进了文档，而它决定了使用者该不该再下一次 5 MB。）

起因是分组件那条路走不通：`windows-x64-yosys.tgz`（42 MB）不是独立发行版，
只是给整包做增量覆盖的，单独装上 yosys.exe 直接 `STATUS_DLL_NOT_FOUND`，
根因是 `libstdc++-6.dll` 里 `std::fpos<int>` 与 `std::fpos<_Mbstatet>` 两套 C 库 ABI
（详见 `toolchain.json` 的 `yosys.source.note`）。既然 yosys 绕不过整包，
单独留几份 21 MB / 13 MB / 5 MB 的小覆盖层就是**名义上的便宜**——
任何新机器装 L2 都得下整包，那几个资产不会被单独用到，留着只会让
`tools.lock.json` 和磁盘上的实际来源对不上。

两条配套机制，缺一个就会出问题：

1. **`do_install` 按 `(repo, tag, asset)` 判重**，同一个归档只下载、只解压一次。
   不去重就会把同一份 16953 条目的 tgz 解压八遍、往磁盘写八遍 2 GB ——
   不报错、只是白干。
2. **`--list` 的「下载量」列只在第一条打印体积**，其余标 `↳yosys`，并在末尾用脚注
   说明哪几条共用。否则八行都印 572.2 MB，读起来像要下八次。
   列名从「体积」改成「**下载量**」也是这个原因：它的含义是「装它要下多少」，
   不是「它自己多大」。

⚠️ **不共这个整包的条目有两条：`verible` 和 `mingw`**（后者 2026-09-29 才加）。
`verible`（6.9 MB 独立 zip → `.tools/verible`）是**第一个**独占自己归档的条目——
正因为前面 8 条
全都共用 `.tools/oss`、而那个目录由先装的组件顺手建了出来，
`_merge_into` 里「目标目录还不存在」这条路径**一直没被走到**，一装 verible 就抛
`FileNotFoundError`（详见 `setup_tools.py` 里那段注释和 `check_manifest.py` 负例 9g）。

负例见 `check_manifest.py` 负例 9d（含两条反向对照：换了归档必须真装、
脚注不许把独占自己归档的条目也算进去）。

### ⚠️ 磁盘占用（2026-09-29 实测）

装完之后 `.tools/` 的实际占用 —— **总量约 4.0 GB**，其中前三行是大头：

| 目录 | 占用 | 是什么 | 能删吗 |
|---|---|---|---|
| `.tools/oss` | **2.1 GB** | 整包解压出来的东西（含 slang / gtkwave / eqy / sby 等 8 个条目的内容） | ❌ 别无选择，它就是这个量和这个位置 |
| `.tools/mingw` | **940 MB** | MinGW-w64 解压后的样子（**zip 只有 261 MB，解开来是 3.6 倍**——gcc 的 include/ 和 lib/ 占大头） | ❌ 删了 `cov.code` / `sva.assert` 连软件那一半都没了 |
| `.tools/_cache` | 866 MB | 下载原件（整包 572 MB + mingw 261 MB + iverilog 27 MB + verible 6.9 MB） | ❌ **别删**，见下 |
| `.tools/iverilog` | 85 MB | iverilog + gtkwave | ❌ 课程 L1 起就要用 |
| `.tools/verible` | 17 MB | Verible 的 11 个可执行文件（不共 `.tools/oss` 的独立包之一） | ❌ 装了才有的那点能力就靠它 |

★ **`mingw` 是清单里性价比最差的一项，装之前想清楚**：下 261 MB、占 940 MB 磁盘，
换来的是「有一个现代 g++」，而 `cov.code` / `sva.assert` **装完仍然不能动手**
（还有五关没接，见下节）。`toolchain.cc` 那一条是真拿到了。

**只有缓存可删，而缓存恰恰不该删**：`--verify-lock` 是拿缓存里的文件算 sha256
去对锁记录的。缓存不在，它只能报「无法校验 6」——**全绿变成全没查**，
而锁存在的意义正是防那份 572 MB 被截断或换掉（静默截断这事真发生过，见下）。
省 866 MB 把这道校验废掉，不划算。

**目前没有卸载 / 回收命令**，`.tools/` 是 gitignore 的、删了就得重下 572 MB + 261 MB。
装之前先确认磁盘够 —— 这张表是使用者第一次跑 `--install yosys --lock`
**之前**就有权知道的数字。清单只管「装」不管「卸」，这一节补的就是另一半。

> 2026-09-29 清理记录：删掉了 4 份**改整包之后再没人读**的旧分组件资产
> （`windows-x64-{yosys,verilator,utils,system-resources}.tgz`，74.2 MB）和
> `build/formaltest/`。删前核对过清单与锁里都没有活引用，删后 `--verify-lock`
> 仍是 `通过 4，不一致 0，无法校验 0`（**这是当时那个时间点的数**，锁后来长到 6 条）。
> 以后再混进旧资产，照这个办法删。

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

### ★ 「自检通过」离「这条能力能动手」还有多远（mingw 的实测）

**这是 `found` / `usable` 之外还要再分一层的最直白的例子。** `mingw` 装完：

```
found  = True
usable = True      ← functional 那条 g++ 自检真的编过了，不是假的
```

**但它换来的能力仍然不能动手。** 2026-09-29 拿一份最小的 SVA 文件实测，
2026-09-30 又把 `--cc --exe --coverage` 全链路完整重跑了一遍，卡点依次是这五关：

| # | 卡在哪 | 为什么 | 本仓库做了吗 |
|---|---|---|---|
| 1 | `make` 找不到 | verilator 认 `MAKE` 环境变量，指到 `.tools/mingw/bin/mingw32-make.exe` 即可。★ 另外 **makefile 必须在 `obj/` 目录里跑**（`make -f obj/V<top>.mk` 会找不到 `V<top>_classes.mk`） | ❌ 没接 |
| 2 | `python3` 是商店占位桩 | `verilated.mk` 里 `PYTHON3 = python3` 是**硬编码**、会盖掉环境变量；PATH 上那个 `python3` 是 `WindowsApps\python3`（退出码 49）。得用 **make 命令行变量**才顶得过去 | ❌ 没接 |
| 3 | **链接失败** | 症状里那句 `undefined reference to WinMain` 是个**误读**，真因跟 WinMain、跟 libstdc++ ABI 分裂都无关。真因是 `verilated.mk` 里 `CFG_CXXFLAGS_STD` **是空的**（套件在 Fedora / GCC 15.2.1 上配的，那边默认就是 gnu++17），GCC 16.2 于是用了**自己的默认 gnu++20**；C++20 下编译器生成的是 `basic_string` 的 **C4** 移动构造引用，而这份 libstdc++ 的 `.a` / `.dll.a` 都只提供 **C1 / C2**。加 `CFG_CXXFLAGS_STD=-std=gnu++17` 即消失 | ❌ 没接 |
| 4 | `sc_time_stamp` 未定义 | verilator 把它声明成 `VL_ATTR_WEAK`，而这个属性在 mingw 上不生效，得自己写一个 `double sc_time_stamp() { return 0.0; }` | ❌ 没接 |
| 5 | `verilator_coverage` 跑不起来 | 它是个 **Perl** 脚本，本机 msys perl 缺 `Pod::Usage`。但 `cov.dat` 是纯文本（`C '<两字母字段前缀拼成的键>' <计数>`），**用 Python 读完全可行**，不必依赖 perl | ❌ 没接 |

★ **配方是齐的、能跑通的**：2026-09-30 按上面五条手工走通了 `--cc --exe --coverage`
全链路，可执行文件跑出结果、`cov.dat` 1500 字节、toggle / line / branch / expr
四类记录齐全。**但「能手工跑通」不等于「仓库接了」**——listing 上那几条要一条条手敲，
`tools/` 里没有任何一处做过。所以这两条能力**仍然不能动手**，`gap` 保持 `software+code`。
还有一条**没验证的前提**：本仓库的测试台（大量 `#延迟` + `$display` 标记）能否过
verilator 还没试过。

**这张表的用法**：`usable=True` 只证明「g++ 能编 verilator 风格的代码」，
**不证明「verilator 能借它造出模型」**。后者才是 `cov.code` / `sva.assert` 要的。
所以那两条的 `gap` 是 `software+code` 而不是 `software`——
**装完软件那一刻它们会被 `evaluate()` 判成可用，而实际跑不出来**，正是最该防的那类谎。

顺带纠正两条旧结论：(1)「瓶颈是编译器不是 make，换现代 mingw 就好」——换完才发现
**只对了一半**，编译器那一层解决了，上面这五关一层都没少；(2)「最后卡在链接，
报 `undefined reference to WinMain` + 一串 `std::__cxx11::basic_string`，是 libstdc++
ABI 分裂」——**这个诊断是错的**，`WinMain` 只是链接失败症状里的噪声，真因是 `-std`
版本不对导致要的符号变体不存在。**错的诊断比没有诊断更坏**：照着它去「补 DLL」
会一路走到 `STATUS_ENTRYPOINT_NOT_FOUND`，全是白工。

⚠️ **可复现性**：OSS CAD Suite 的 `bucket-*` 是**滚动 tag**，同名资产每天被覆盖。
要固定版本必须 `--lock` 记 sha256——不带 `--lock` 的安装等于「装到哪个算哪个」。

### ★ 判题环境怎么保证一致（锁在 `tools/tools.lock.json`）

**二进制不进库**（`.gitignore` 忽略整个 `.tools/`，约 4.0 GB），**锁进库**。
这两件事缺一不可，分开说清楚：

| | 放哪 | 为什么不这么放不行 |
|---|---|---|
| 工具二进制（约 4.0 GB） | `.tools/`，**gitignore** | 传上去仓库就没法用了；而且它们本来就该按平台重装 |
| **锁**（`tools/tools.lock.json`） | `tools/`，**入库** | 它记录每个归档的 sha256 + `(repo, tag, asset)`，是「大家装的是不是同一份」的唯一依据 |

```bash
python tools/setup_tools.py --install yosys verilator --lock   # 装 + 记锁
python tools/setup_tools.py --verify-lock                      # 拿缓存核对 sha256
python tools/setup_tools.py --restore-lock                     # 照锁把软件装回来
python tools/setup_tools.py --purge-cache                      # 看 _cache 占了多少（只预演）
```

★ **锁是闸门不是收据**：`install_one` 里 sha256 对不上就 `return False` **拒绝安装**
（要 `--force` 才放行）。理由见 `_lock_problem` 的注释——「装完再告诉你这份不对」
已经晚了，坏的工具已经在 `.tools/` 里了。

★ **`--restore-lock` 是「拉下代码之后怎么把环境弄成作者那样」的那一个动作。**
它跟 `--for-level` / `--install` 不是一回事：`--for-level L2` 装的是「L2 需要什么」
（随清单变动而变），`--install X` 装的是「我要 X」（靠人眼抄 id，抄漏一条两边都看不出来）。
只有 `--restore-lock` 是**照锁里记的那一份**装。

**它最要紧的不是装了什么，是它明说装不了什么**：锁覆盖不到的地方会**分两桶印出来**——
「能锁但没锁」（版本可能和作者不同）和「不归这把锁管」（`iverilog` 走另一个安装器、
没有 sha256 就归这桶）。两桶混在一起，使用者会以为 `iverilog` 也是个「补一下就好」的洞。
还原时**不写锁**（`do_install(..., lock=False)`）：这条命令的方向是「照锁装」，
顺手把本次下载的哈希记进去，参照物就变成本次结果了，锁也就不再是锁。

★ **它最后一定会补一次 `--verify-lock`，那句「还原完成」的凭据是这次核对，不是安装成功。**
这不是锦上添花，是 2026-09-29 实测抓出来的漏洞：`install_one` 里 `_already_installed`
的短路发生在 `_lock_problem` **之前**，所以在一台装好的机器上，「已装」的条目
**一次都不会碰锁**，`do_install` 返回绿只说明「装的过程没报错」。实测现场是同一把锁
两个答案——把 `yosys` 的 sha256 改成 64 个 0，`--verify-lock` 正确报「不一致 1」，
而 `--restore-lock` 照样印「完成：6 条按锁还原」。现在核对红了就**不许说「还原完成」**，
改成一句实话并以非零码退出；报告里也把「新下 N 条」和「复用已装 M 条」分开印，
因为**只有后者是靠这次核对兜住的**（同「两个计数必须分开报」）。

★ **2026-09-29 之前锁在 `.tools/tools.lock.json`，跟二进制一起被忽略了**，
所以它**从来没进过库**。后果不是「少一份文件」，而是**锁对作者生效、对别人不生效**：
新克隆的人手里没锁，`_lock_problem` 查不到 entry 就 `return None`（"第一次装，
天经地义"），每次安装都放行——**两边都报「安装成功」，一边真被保护、一边没有**。
现在锁入库，并且 `main()` 里有一次 `_migrate_legacy_lock()`：老用户升级后
自动把旧位置的锁搬过来，**不然他们会从「受保护」悄悄退回「没保护」而看不出变化**。

**这套机制目前的边界（别高估它）**：

- 清单里 12 条带 `source`，锁里目前 **6 条**（装的时候加了 `--lock` 的才算）。
  没进锁的那几条**装完不校验**。
- ★ **按资产判覆盖，不按 id**：`slang` / `formal` / `eqy` / `z3` 没各自的锁记录，
  但它们跟 `yosys` 是**同一个 tgz**，已经被锁住了 —— 报成缺口就是凭空造一个洞。
  `_locked_assets()` 就是干这个的（`--restore-lock` 也用它）。
- **`iverilog` 不在这把锁的覆盖范围内**：它由 `setup_toolchain.py` 装，走的是
  另一条路（Inno 安装包 + 「装到默认位置再整体搬进 `.tools/`」），
  硬塞进这把锁会让 `--restore-lock` 报一条假的「没写 install.dest」失败。
  `--restore-lock` 会**专门把它点出来**，不会装作盖到了。
  ★ **但它自己有另一套钉法**（2026-09-29 补）：sha256 记在 `setup_toolchain.py`
  的 `INSTALLER_SHA256`（随代码入库），下载完**一律核验**（缓存命中也核），
  对不上就**拒装**且**不许回落到下一个候选**——回落的是 2022 年的 v12，
  另一份编译器。回落现在会印一段大字，不再静默。核对用
  `python tools/setup_toolchain.py --verify`：它**分两头报**，因为「下载的包」
  和「装上来的编译器」是两件事，两头都对照样会说谎（包对而版本不对 =
  系统里另有一个 iverilog）。v12 那一份**故意不钉哈希**——作者没下过它，
  编一个等于凭空造凭证，所以它走的是「没有记录 → 无法与作者比对」那条路。
- 所有 `tag` 都是**日期/版本钉死的**（没有 `bucket-*`），这一半本来就做了。

### `--purge-cache`：866 MB 的下载归档收不收

`.tools/_cache` 里躺着下载过的归档（整包 572 MB、mingw 261 MB、iverilog 安装器
25.8 MB、verible 6.6 MB）。**装完之后它们只剩一个用途**：让 `--verify-lock`
拿原始归档重算 sha256。所以删掉**不会动已装好的工具**，但会让
`--verify-lock` 从「通过 N」变成「**无法校验 N**」——那是「没查」不是「不一致」。

```bash
python tools/setup_tools.py --purge-cache           # 只预演，列出来 + 报能回收多少
python tools/setup_tools.py --purge-cache --force   # 真删
```

默认**只预演**：删的是磁盘上唯一一份原始归档，重装时得重下（整包那个 572 MB
尤其疼），所以不该让一次手滑决定它。目录不删（`_cache` 里出现目录时不替人做主）。

★ **判据是「这 866 MB 换到的是什么」**：留着它，`--verify-lock` 是个能真跑的
一致性检验；删了它，那条命令就只剩「无法校验」，而**锁记录本身还在库里**——
`--install X --lock` 重下一份就能恢复。也就是说这是个**纯空间换便利**的取舍，
不是「删了就坏」。当前选的是留着。

**还有一层保障是无意的但很有效**：smoke 检查器大量使用**手写样本**而不是跑真工具
（`check_vlint.py` 明写了理由是「靠真实运行去凑，换个 verilator 版本就变了」）。
所以环境漂移会让检查**变红**，而不是静默换个答案——**smoke 本身就是一致性检验**。

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

### ⚠️ `viz.py` 里有两个「顶层」，别把它们合成一个变量（2026-09-30 修）

`viz.py` 原来用**同一个** `top` 变量喂结构图和仿真，而 `_guess_top()` 是按
结构图的偏好写的（优先非 `tb*` 的模块）——于是不给 `--top` 时仿真拿到的是
**设计模块**，iverilog 收到 `-s counter4` 只展开那一棵子树，**测试台根本不会
被实例化**：不打印标记、不执行 `$dumpfile`。症状是

```
无法判定
  → 测试台缺少判定标记，无法判定正确性。
  → 测试台应按协议打印 TEST_PASS / TEST_FAIL / TEST_DONE
  ! 仿真没有产生 VCD（测试台里有 $dumpfile/$dumpvars 吗？）
```

**把工具的错说成学习者测试台的错**，而他明明写对了。实测唯一差别就是顶层名：

```
-s counter4      status=inconclusive   markers= 0
-s tb_counter4   status=pass           markers=12
```

`--compare` 更隐蔽：它的 `--top` 是**被对比的模块名**（文档示例给的是
`--top decoder3_8`），喂给仿真同样出不来波形，整组对比全是空。

现在的分工：`_guess_sim_top()` 从 `--tb` 里认仿真顶层，`_guess_top()` 照旧服务
结构图；用户显式给的 `--top` 仍然覆盖仿真顶层（README / AGENT.md 里举的就是
`--top tb_xxx`，那本来就是仿真顶层）。

★ **为什么 smoke 一直没抓到**：`check_wave.py` 测的是波形**渲染器**、
`check_schematic.py` 测的是结构图解析，**`viz.py` 的 CLI 主流程没有任何检查覆盖**。
现在由 `check_viz.py` 补上（端到端跑一遍 `--wave`，断言 5/5 + 波形出得来 +
信号带 `tb_` 作用域）。

### ⚠️ 「找不到波形」不等于「他没写 `$dumpfile`」（2026-09-30 修）

`$dumpfile` 的路径是**相对进程 cwd** 解析的，而 `sim.py` 原来只在 `cfg.workdir`
（默认 `build/`）里递归找 `.vcd`。学习者写 `$dumpfile("dump.vcd")` 再自然不过，
文件落在**仓库根**，于是报出

> 本次仿真没有产生 VCD 波形。测试台里需要 `$dumpfile` + `$dumpvars`

——**文件就躺在旁边**，而这句话断言他的测试台没写。同一类谎：把工具的眼界
说成使用者的错。

现在 `_snapshot_vcd` 扫两个根：`workdir` 递归 + **cwd 只扫一层**（递归扫仓库根
会一路走进 `.tools/`，4 GB，代价和收益不成比例）。对外路径在 workdir 里的照旧，
落在别处的按**仓库根相对**记（和 `toggle.py` 从 JSON 取路径的约定一致）。

⚠️ **代价要认**：`$dumpfile("sub/x.vcd")` 写到别的目录深处仍然找不到。所以
**措辞也跟着改了**——现在说的是「已找过：`build`（递归）和 `<cwd>`（只一层）」，
把判断留给使用者，不再替他下结论。判据：**「我找不到」永远不许说成「你没写」。**

```bash
python tools/lesson.py L1-05          # 生成 L1-05 全部模块的产物包
python tools/lesson.py --list         # 列出已有讲次
python tools/lesson.py --all          # 全部重生成
```

常用命令见根目录 `README.md`。每个脚本 `--help` 都有完整说明和示例。

## `regress` / `synth` / `cov` / `toggle`：四个入口守的一条数
（表里最后一行是 `synth.py` 的一个旗标，不是第五个入口。）

这四个入口看着不相干，其实守的是**同一条纪律**——而这条纪律在本仓库已经栽过五次
（`lint.ran` vs `lint.n_warnings`、`n_ok` vs `n_sizer_ok`、`n_runs` vs `n_runs_with_cover`、
`n_changes` vs `total_bit_toggles`、`comparable` vs `identical`）：

> **「没做」和「做了但结果为零」在下游长得一模一样，含义相反。**
> 凡是这两种情况都可能出现的量，就必须拆成两个字段报。

| 入口 | 拆开的两个数 | 混了会怎样 |
|---|---|---|
| `regress.py` | `n_combos`（跑了几组） / 真的比出差异的对照组数 | 每组跑的都是同一个电路，全绿、退出码 0、毫无异常——和「完全没工作」无法区分 |
| `synth.py` | `n_ok`（整体成功） / `n_sizer_ok`（资源估算成功） / **`n_yosys_ok`（yosys 那条腿成功）** | sizer 的数字照常可信、只是网表出不来时被报成「你的写法综合不了」，是假话。混进同一个数就没法表达「iverilog 那条腿成了、yosys 这条腿没成」 |
| `--encoding-compare` | `comparable`（状态机认出来了） / `identical`（三种编码数字果然相同） | 没认出 FSM 时三次必然相同，混成一个数就会把它报成「编码对面积没影响」——那是一条**看着有结论的假结论** |
| `cov.py` | `n_runs`（跑了几份） / `n_runs_with_cover`（其中几份带覆盖数据） | 没声明覆盖点的那份被当成「覆盖了 0 个点」，把合并比值拉低 |
| `toggle.py` | `n_changes`（状态变了几次） / `total_bit_toggles`（一共翻了几位） | **一次变化可以翻多位。**同一台 4 位计数器、同一套激励：二进制 34 次变化 / 63 位翻转，格雷 34 次变化 / 34 位翻转。只报 `n_changes` 会得出「两种编码一样」的错结论 |

**`regress.py` 的独门职责是防「扫了但没扫到区别」。** 批量回归最典型的失败不是报错，
是每一轮跑的都是同一个电路而你没发现。两道防线：测试台自报 `EFFECTIVE_<名字>=<值>`
逐档核对（硬证据）；结果指纹对比——固定其余维、看这一维各取值之间有没有差异
（启发式，无差异时需显式 `--allow-inert` 才放行，**默认不放行**）。

**`synth.py` 最要紧的一条是别把工具的毛病说成学习者的错。** 异步复位
（`always @(posedge clk or negedge rst_n)`）在 yosys / Vivado / DC 里都是标准写法，
但 iverilog 的 blif 后端没实现，报 `Asynchronous clear not implemented yet`——
它有独立归类 `async_reset_unsupported`，提示语里明写「**这不是你的写法问题**」。
混进 `not_synthesizable` 会让学习者去改一份本来完全正确的 RTL。
**2026-09-29 起这件事有了第二份证据**：`--engine yosys` 对同一份异步复位设计照常出
数字，`cross_check` 会把它归成 `yosys_handles_it`。跟踪「这是工具边界不是你的问题」
因此从「一句承诺」变成了「两条腿对得上的结论」——注意是**对得上**，不是「并不矛盾」
就当没看见：两边的结论仍然各摆各的。

另外 `ok` 必须把「用户显式要了网表却没出」算进去，否则就成了
「跑绿了但你要的东西不在」——这是本仓库最要避免的那种报绿，
和 `--netlist` 一起用时 `n_sizer_ok` 可能等于 3 而 `ok` 是 `false`，这不是矛盾。

**`cov.py` 的底线是「合并取并集，绝不取平均」。** 平均**两个方向都会错**：

| 两份测试台 | 平均 | 真值（并集） | 错在哪 |
|---|---|---|---|
| 10/10 与 0/1 | 50% | 10/11 = 91% | **低报**，让人以为还差得远 |
| 1/1 与 0/10 | 50% | 1/11 = 9% | **高报**，而且高报在「看着挺勤快」那一侧 |

覆盖点是**名字**不是**计数**，合并只能在名字集合上做。副产品是 `undeclared_hits`：
**一个拼错的覆盖点不报错、不警告，症状只是覆盖率静默变低**——把所有命中名和所有
声明名并排摆出来，拼错就藏不住。

**`toggle.py` 的底线是「先验可比性，再谈结论」。** 翻转数完全由激励决定——
它是这次仿真的属性，不是设计的属性，换一套激励排名就可能翻过来。
所以多份输入时会做一道**与编码无关**的硬校验：*同一套激励下，状态寄存器在哪些
时刻变，与编码方式无关*（编码是状态到码字的一一映射，状态序列一样 ⇒ 变化时刻
一样）。逐项相等才给结论，否则 `not_comparable` 并**拒绝给比值**——那个数看着
挺合理，却是纯粹的误导。

可比性检查自己抓不到的一种情况单独拦：**两份输入的波形内容完全相同**。
它让"变化时刻逐项相等"必然成立，报出一个看着很像结论的「打平」，实际只说明
`--param` 没接上、或者后一次仿真把前一次的波形覆盖了。这是 `identical_inputs`，
判失败。

反过来，**逐项相等却得到相同的翻位数，那是真结论不是工具坏了**（某些状态图下
两种编码本来就打平），如实报成"无差异"而不是判失败。而**全是 0** 又是另一件事，
它是"没数到翻转"，和"打平"必须分得清——所以 `no_toggles` 单列，判失败。

夹具是 `tests/_smoke/cnt_enc.v`（`GRAY` 参数切换编码）+ `tb_cnt_enc.v`
（同一份测试台，`--param GRAY=1` 切）。用"一个模块 + 参数"而不是两份源文件是有
理由的：两份独立源文件的测试台很容易在细节上悄悄跑偏，而"跑的不是同一件事"
在翻转数上根本看不出来——数字都挺合理。

> 这四个入口的负例都验过，而且是**拆护栏验的**：把每条护栏逐个拆掉重跑，
> 确认对应断言确实变红。没验过的负例等于没有。
> `toggle.py` 拆过六条：两个计数混为一谈、x/z 也算进翻转、可比性恒真、
> 去掉同文件拦截、`bits_per_change` 换分母、窗口首格丢前值——六条都红。

## yosys 引擎（`--engine yosys`，2026-09-29 接入）

`synth.py` 默认那条腿是 iverilog：`-t blif` 出网表、`-t sizer` 估资源。
它**只做翻译不做优化**，给的是工艺无关网表——够讲「组合逻辑怎么变成门」，
讲不了映射、面积优化。`--engine yosys` 补的就是这一段：真跑
`proc → opt → fsm → techmap → abc`，然后报**映射成逐 bit 门之后的**门数。

```bash
python tools/synth.py tests/_smoke/fsm_bin.v --engine yosys --pretty
python tools/synth.py tests/_smoke/fsm_bin.v --engine yosys --encoding-compare --pretty
python tools/synth.py tests/_smoke/fsm_bin.v --engine yosys --fsm-encoding binary
```

★ **它是「多加一条腿」，不是「换掉 iverilog 那条」。** 两条腿对同一份 RTL 的结论
会不一样，报告里两侧都摆出来（`designs[i].cross_check`）。已知的两类分歧：

| 分类 | 什么时候 | 怎么说话 |
|---|---|---|
| `yosys_handles_it` | iverilog 的 blif 后端没实现异步复位，yosys 照常出数字 | **明说「你的 RTL 没问题」**——这是工具的能力边界 |
| `disagreement` | 一边说合不了、另一边给出了数字 | 挑明这是两份证据打架，**绝不只报乐观的那一个** |

### 固定 pass 列表，写死在 `_yosys.py` 里

```
read_verilog [-sv] [-D…] [-I…] <abs posix path>
hierarchy -check -top <top>      (或 -auto-top)
proc
opt
fsm [-encoding one-hot|binary]   (不指定 = 让综合器自己挑)
opt
techmap
opt
abc
opt
tee -o <statfile> stat
```

**不同 pass 顺序给出的数字不可比**（实测过），所以列表写死并在 JSON 里原样输出。
改它等于换了一把尺子，报告里会跟着变。

### 四条实测出来的硬事实（改这块之前先看 `_yosys.py` 的模块头）

1. **「退出码 0」和「有输出」都不是跑过的证据。** 空文件 rc=0、日志打着
   `Printing statistics.`、但一个模块块都没有；缺 DLL 时更是 rc=0 且**零字节**。
   硬判据取 **`=== <module> ===` stat 块存在**——和 `_vlint.py` 拿
   `Verilation Report` 当判据同一条道理。所以 `started` / `reached_stat` / `ran`
   / `ok` 是**四个独立的位**。
2. **仿真专用的 cell 会活到 `abc` 之后，退出码还是 0。** `tests/_smoke/synth_unsynth.v`
   （含 `$display` / `#2` / `initial`）跑出来是 **96 个 cell**，里头躺着 `$print`。
   直接报「96 个门」就是假话。报告里 `gate_count_meaningful` 翻 `false`，
   门数照给但标为不可当结论用，并把 cell 名列出来。
3. **`#延迟` 被静默丢弃**，日志里一句警告都没有（`$monitor` 反倒会警告）。
4. **「没认出来状态机」≠「编码对面积没影响」。** `fsm_recode` 没认出 FSM 时
   `-encoding` 是静默空操作，三种编码量出来一模一样。所以
   `--encoding-compare` 的结果里带 `comparable` / `identical` 两个位：认不出来就
   明说「本次没能比较」，**绝不把三次相同的数字摆成一张看着像结论的表**。

> ★ 一并纠正一条**测错了的**旧结论：探索阶段一度以为 `opt -nodffe -nosdff`
> 不能省（省了 `fsm_detect` 认不出状态机）。**复测三份夹具后不成立**——
> `proc; opt; fsm -encoding X` 与 `proc; opt -nodffe -nosdff; fsm -encoding X`
> 给出的 cells / 触发器位**逐项相同**。所以 `PIPELINE` 里用的是普通的 `opt`。
> 真正会让「引擎其实没生效」的症状（三编码数字全同）出现在**把 `fsm` 整个删掉**时,
> 那也正是变异测试用的那一刀。

### 做不到什么

- **不是真实面积。** `abc` 没带 `-liberty`，用的是内置通用映射
  （`$_AND_` / `$_DFF_*` 这些），比 sizer 可信得多但仍**只能横向比**。
  本仓库没有工艺库文件，接上也不是签核级数字。
- **只支持 one-hot 和 binary。** `fsm_recode` 没有 gray / johnson，
  传别的值**直接报错**（`synth.py` 会以退出码 64 挡下并说明原因）。
  课程讲三种编码，综合器能替你挑的只有两种——格雷必须自己在 RTL 里写。
- **不改变任何判定。** 综合产物回答的是「这么写合成出多少硬件」，
  对错仍然只由 `sim.py` 说了算。

`--encoding-compare` / `--fsm-encoding` **脱离 `--engine yosys` 会被挡下**（退出码 64），
而不是静默换成 yosys——否则命令行上写着 `--fsm-encoding`、跑的却是 iverilog，
数字照样出得来、还看着挺合理。

## 内部模块（不要直接当命令跑）

以下划线开头的是内部共享模块：

| 文件 | 职责 |
|---|---|
| `_toolchain.py` | 工具链定位。**唯一**一处决定去哪找 iverilog（顺序：显式参数 → `IVERILOG_BIN` → `.tools/` → 系统常见路径 → PATH） |
| `_manifest.py` | 能力清单的加载 / 校验 / 探测。**不硬编码任何工具名**，加工具只改 JSON |
| `_proc.py` | 子进程执行 + Windows 上的进程树强杀（job object 为主，`taskkill /F /T` 兜底）+ 超时 |
| `_lint.py` | 源码静态检查，目前只做一件事：**抓组合逻辑推断出的锁存器**。策略是"宁可漏报，不可冤枉" |
| `_vlint.py` | 调外部 verilator 做真 lint（位宽 / 组合环 / 多驱动 / 未驱动 / 未使用）。**只加 warning，永不改 status**，详见下节 |
| `_yosys.py` | 调外部 yosys 做综合与工艺映射。定位 / 固定 pass 列表 / `stat` 解析 / 失败归类 / **非硬件 cell 检测**。硬判据是 stat 块存在，不是退出码 |
| `_observe.py` | 从 VCD 里量出「可以照着讲」的事实（时钟、首次分歧在第几个沿、每个信号动没动过）。**只测量不推断**，拿不准给 `null` |
| `_console.py` | 把控制台切到 UTF-8。**每个 CLI 入口都要调 `init_console()`**，否则 GBK 控制台遇到 `✓` 会崩 |

## 库（没有命令行）

| 文件 | 职责 |
|---|---|
| `vcd.py` | VCD 解析。不支持 `$dumpports` 等扩展命令 |
| `wave.py` | 波形渲染，两套后端：终端 ASCII（会丢窄脉冲，只看大结构）和自包含 HTML（交互式，主用这个） |
| `schematic.py` | 结构图 SVG。**这不是语法分析器**，是正则 + 缩进深度的提取器 |

`schematic.py` 的设计原则值得记住：**宁可说"画不出来"，也不画一个看着合理但是错的。**

`generate` 是这条原则唯一需要解释的地方——它的落地版不是"一律拒画"：

- **参数的默认值算得出圈数就展开。** `#(parameter N = 4)` 配 `for (i=0; i<N; i=i+1)`
  就是 4 圈，没有理由不画。展开后各次迭代在信号级（位下标已抹掉）通常完全一样，
  合并成一条并标 `×N`，并附上 `generate g` 这样的来源标签。
- **算不出来就只丢那一个区域。** 上界写成编译宏（`` `NUM_CH ``）、写成压根没声明的名字、
  或用了 `case` generate —— 这些一律不画，图上写明理由。**绝不猜份数**：
  猜出来的 N 会生成一张看着合理、读者却无从察觉的错图，比没图更糟。
  注意是逐区域丢：算不出来的只是那块 generate，模块里手写的结构照画。
- **`if`/`else` generate 只画成立的那一支**，另一支写进图的说明里，否则读者会以为
  源码里根本没有它。参数化位宽 `[DW-1:0]` 同样按默认值算；实在算不出来就**照抄源码原文**
  显示（留空会被读成"这是个 1 位信号"，是另一回事）。
- **参数化例化**（`leaf #(.W(8)) u(...)`）按子模块自己的默认值画，框上标出覆盖，不追进去。

`case` generate 明确不支持，不是"没实现"：它的分支靠 `值 : 语句` 切分，而冒号在位宽和
三目运算符里都出现，切错了会**静默丢分支**。改这块之前先读 `schematic.py` 文件头的取舍清单。

## 改动规矩

改完**必须**跑：

```bash
python tests/_smoke/run_smoke.py
```

26 个仿真用例 + 14 个工具检查（波形 JS/DOM 两个需要 node，缺了自动跳过；
**2 个**用例和 `check_vlint.py` 需要 verilator，缺了报 `⊘` 跳过而不是判失败）。

★ 这几个数**别照抄**，跑一遍就有：用例数看那行 `冒烟测试全部通过：N/N`，
检查器数看 `grep -c "检查通过$"`，需要 verilator 的看
`grep -c "needs_verilator=True" tests/_smoke/run_smoke.py`。
（数字改过两轮：原先写「5 个用例」2026-09-29 更正为 2；工具检查数
「12 → 13」在 09-29、「13 → 14」在 09-30 ——**加检查器之后没人回头改，
数字一旦过时就成了一句没人验过的断言**。所以这里给的是数法，不是数。）

加新能力时**同时加一个负例**——能证明它真的抓得到错，否则不算完成。

★ **断言必须写成「这个量等于几」，不能只是「打印出来看看」。** 一个结构性的回归
（工具内部改了数据形状）会让检查脚本抛 `TypeError` 而不是报一条可读的红。
`check_synth.py` / `check_cov.py` 里的 `isinstance` 护栏和 `_num()` 就是为这件事
加的——**报错信息得说清是哪里坏了**，光有一行 traceback 等于没定位。

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
- `gap` 带 code 的能力（`code` / `software+code`）如果不在 `evaluate()` 里显式排除，会被判成 `ok=True`——于是「多参数批量回归」这种压根没写的功能显示「可用」；`software+code` 更隐蔽，它 requires 列着真软件，装上就「齐了」，而 `integration` 里明明写着「仓库有活干」
- **「文件在」不能当成「能用」**：能力依赖的满足条件是 `found and usable`。少了 `usable` 这一半，一个启动就崩的工具照样能把能力"补齐"，报告上却看不出任何异常
- `lib_dirs` 必须**并入** PATH 而不是覆盖它——覆盖不报错、不崩溃，只是子进程再也找不到系统命令
- **WSL-only 的依赖必须和可安装的依赖分开报告**，否则「装上这些可补」会列出一份装不上的清单，`--for-level` 还会照着它去发起下载
- 归档路径穿越（tar/zip 的 `../` 成员）必须拦住；正常归档仍要能解压，**否则「什么都拦」也会让上面两条假通过**
- WSL-only 的软件必须**在下载前**拒绝，不能白下几百 MB 再失败
- ★ **下载被截断必须报错，不能当成下完了**（2026-09-29 实测栽的坑，见下）
- **观察层拿不到波形时，判定结论一个字都不能变**：同一个设计换个不产 VCD 的测试台，
  该是 `inconclusive` 就还是 `inconclusive`，不能因为少了一层辅助信息就崩掉或变成 `pass`
- **找不到时钟要给 `null`，不能硬报一个周期**。这条负例的 fixture 必须让
  `observe.ok` 为 `true`（波形真读到了、里面确实没有时钟）——否则就是拿"没波形"
  冒充"没时钟"，断言绿得毫无意义

> 这些负例是**真的验过**的：把护栏逐个拆掉重跑，确认对应断言确实会红。没验过的负例等于没有——
> 它可能只是恰好没触发。拆护栏那一下很便宜，别省。

### ★ 下载静默截断（2026-09-29 实测）

装 yosys 那次，572 MB 的 `oss-cad-suite` 下到 **540 MB** 被掐断，而工具**报的是成功**：
`.part` 改名成正式文件、退出码 0、还打了 100% 的进度条。少 32 MB 无人知晓。

病根在 `http.client` 里，不在网络：带长度参数的 `read(amt)` 遇到连接提前关闭时
**返回空串、不抛 `IncompleteRead`**（源码那段注释自己写着 "Ideally, we would raise
IncompleteRead ... but it might break compatibility"）。于是「读完了」和「读断了」
在调用方眼里**一模一样**——`while chunk := resp.read()` 两种情况都 `break`。

原有的唯一防线是"查 gzip / zip 魔数"，防的是把 404 页面当工具装进去。但它拦不住截断：
**魔数在前两个字节就写对了**。这又是拿 `found` 当 `usable`，和 `lint.ran` /
`n_warnings=0` 是同一个坑，只不过这次骗的是下载器自己。

现在两道：

1. `_download_once` 比对实际读到的字节数和服务端声明的 `Content-Length`（**先定
   `mode` 再算 `total`**——服务端可能无视 `Range` 返回 200，那时 `Content-Length`
   是整份长度，再 `+ resume_from` 就会把读满的判成截断）；不一致抛
   `TruncatedDownload`，刻意继承 `OSError` 好落进 `download()` 既有的重试分支，
   `.part` 保留、下次续传。实测正是靠这条从 540 MB 接着下完的。
2. 缓存命中时用清单里的 `size_mb` 复核体积。`> 1 MB` 只说明"不像个错误页面"，
   不说明是完整的——截断到一半的 540 MB 同样 `> 1 MB`。只往下比不往上比，
   因为 rolling tag 的同名资产体积会变。

负例在 `check_manifest.py` 负例 9c：起一个**真会短发**的本地 HTTP 服务复现，
另加两条反向对照（发全了不许报截断、服务端无视 `Range` 时不许误判）。
