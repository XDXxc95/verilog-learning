# tests —— 测试台

这个目录有**两类东西**，别混：

| 子目录 | 是什么 | 谁来改 |
|---|---|---|
| `tests/<讲号>/` | **课程测试台**，一讲一个：`tests/L1-05/tb_decoder3_8.v` | 每讲新增 |
| `tests/_smoke/` | **工具链自检**，验证 `tools/` 本身没坏 | 基本不动 |

## 课程测试台

讲号带级别前缀（`L1-` / `L2-` / `L3-`），跟 `exercises/` `solutions/` 对齐。顶层模块名固定 `tb_<设计名>`，设计名必须和 `exercises/` 里的文件名一致，这样命令才是整齐的：

```bash
python tools/sim.py exercises/L1-05/decoder3_8.v --tb tests/L1-05/tb_decoder3_8.v --top tb_decoder3_8
```

写法照抄 `tests/_smoke/tb_counter4.v` 就行，要点：

- 开头 `` `timescale 1ns/1ps `` + `` `include "tb_common.vh" ``（`tb/` 会自动进 include 路径，不用写相对路径）
- 波形固定写 `$dumpfile("build/<名字>.vcd")`，产物统一进 `build/`
- 激励在时钟沿**之后** `#1` 施加，避免和沿抢同一时刻
- 用 `` `VLEARN_CHECK("用例名", 期望, 实际) `` 判定，结尾 `vlearn_summary; $finish;`
- 推荐加 `` `VLEARN_TIMEOUT_GUARD(100000) ``（放模块层级，不是 initial 里）——写错了会挂死时，它能把"卡在哪一步"的波形保下来

改测试台/协议前先看 `tb/tb_common.vh` 文件头，里面有 iverilog 的两个坑。

## `tests/_smoke/` 自检

```bash
python tests/_smoke/run_smoke.py
```

跑 9 个仿真用例 + 5 个工具检查（波形解析、波形 JS、波形绘制、结构图、产物生成），全绿说明工具链完好。里面 `neg/` 下是**故意写错的**用例，用来证明工具真的能抓到错：

| 用例 | 抓什么 |
|---|---|
| `neg/dut_shift_bad.v` | 用 `=` 代替 `<=` 导致流水线两拍塌成一拍 |
| `neg/latch_incomplete_case.v` | `case` 缺 `default` 推断出锁存器 |
| `neg/hang_wait_forever.v` | `wait` 等一个永false 的条件（运行期挂死） |
| `neg/hang_zero_delay.v` | 零延迟无限循环（编译期就能拦下） |
| `neg/syntax_error.v` | 语法错误 + 行列号定位 |
| `neg/tb_quiet.v` | 测试台不打印任何标记（必须判"证据不足"，不能判通过） |

改过 `tools/` 下的任何脚本，都跑一遍这个。
