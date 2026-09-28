"""在 node 里用 DOM 桩子真跑一遍波形 JS，验证交互路径不炸。

    python tests/_smoke/check_wave_draw.py

check_wave_js.py 只做语法解析 —— 拼错的名字、写漏的字段它一律放行。
这个脚本把生成的 JS 真的执行起来，并模拟一串用户操作（缩放、平移、
折叠、移动光标、切主题），任何一处抛异常都算失败。

顺带断言"确实画了东西"：线条数 / 文本数为 0 说明绘制分支根本没进去，
页面会是一张空白（比报错更难发现）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from _console import init_console  # noqa: E402

init_console()

import vcd as vcdmod  # noqa: E402
import wave as wavemod  # noqa: E402

RUNNER = r"""
const fs = require("fs");
require(process.argv[2]);                 // DOM 桩子
const html = fs.readFileSync(process.argv[3], "utf8");
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const name = "wave";                      // 给 sourceURL 用，报错时行号更可读
(0, eval)(js + "\n//# sourceURL=" + name + ".js");

// 初始化已经跑完（脚本末尾会 buildRows + resize），现在模拟一串用户操作
const cv = __els["cv"];
cv.fire("wheel", { deltaY: -240, offsetX: 600, shiftKey: false });
cv.fire("wheel", { deltaY: 240, offsetX: 300, shiftKey: true });   // 纵向滚动
cv.fire("mousedown", { offsetX: 700 });
cv.fire("mousemove", { offsetX: 500 });                            // 拖动平移
__fireWindow("mouseup", {});                                       // 松手绑在 window 上
cv.fire("mousemove", { offsetX: 800 });                            // 光标 + 取值
cv.fire("click", { offsetX: 100, offsetY: 34 });                   // 折叠层次
cv.fire("mousemove", { offsetX: 900 });
__els["zin"].onclick(); __els["zout"].onclick(); __els["zfit"].onclick();
__els["theme"].onclick();                                          // 明暗切换

// 光标读数要在 mouseleave **之前**取：离开画布会把光标清成 —
const cursorAt = document.getElementById("cursor").textContent;

// 定格到全览视图，把光标放到一个已知位置，看画出来的值对不对。
// 全览下 [NAME_W=250, W=1200) 映射到 [0, end]，x=1000 落在 44.21ns 附近。
__els["zfit"].onclick();
__resetTexts();
cv.fire("mousemove", { offsetX: 1000 });
const cursorVals = __texts.filter(t => t.align === "right").map(t => t.s);
const allTexts = __texts.map(t => t.align + "|" + t.s);

cv.fire("mouseleave", {});

console.log("__RESULT__" + JSON.stringify({
  calls: __calls,
  cursor: cursorAt,
  cursorVals: cursorVals,
  allTexts: allTexts,
  cursorAfterLeave: document.getElementById("cursor").textContent,
  meta: document.getElementById("meta").textContent,
}));
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("  ! 找不到 node，跳过交互路径检查")
        return 0

    dump = vcdmod.parse(ROOT / "build" / "_smoke_counter4.vcd")
    out = ROOT / "build" / "_smoke_wave.html"
    wavemod.render_html(dump, out, title="counter4")

    runner = ROOT / "build" / "_wave_runner.js"
    runner.write_text(RUNNER, encoding="utf-8", newline="\n")

    p = subprocess.run(
        [node, str(runner), str(ROOT / "tests" / "_smoke" / "wave_dom_stub.js"), str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    outtxt = (p.stdout or "")
    m = re.search(r"__RESULT__(\{.*\})", outtxt, re.S)
    if m is None:
        # 把 runner 的输出原样贴出来 —— 报错信息本身就是最有价值的线索
        print("  ✗ 执行波形 JS 时出错：")
        for line in (p.stderr or outtxt).strip().splitlines()[:20]:
            print(f"      {line}")
        return 1

    r = json.loads(m.group(1))
    calls = r["calls"]
    fails: list[str] = []

    for op in ("clearRect", "clip", "stroke", "fillText", "restore"):
        if calls.get(op, 0) < 1:
            fails.append(f"绘制调用 {op} 次数为 0，图可能是空白的")

    # 缩放/平移应该触发多次重画
    if calls.get("clearRect", 0) < 8:
        fails.append(f"重画次数偏少（clearRect={calls.get('clearRect')}），事件可能没生效")
    if calls.get("save", 0) != calls.get("restore", 0):
        fails.append(f"save/restore 不配对：save={calls.get('save')} restore={calls.get('restore')}")

    print(f"  绘制调用：clearRect={calls.get('clearRect')} stroke={calls.get('stroke')} "
          f"fillText={calls.get('fillText')} clip={calls.get('clip')}")
    print(f"  光标读数：{r['cursor']}")
    print(f"  头部信息：{r['meta']}")

    # 光标在画布内必须给出时刻，离开画布必须清掉
    if not re.match(r"^t = [\d.]+ns$", r["cursor"] or ""):
        fails.append(f"时间光标读数不对：{r['cursor']!r}")
    if r["cursorAfterLeave"] != "—":
        fails.append(f"鼠标离开画布后光标没清掉：{r['cursorAfterLeave']!r}")
    # 时间换算：这是一处极易写反的地方（乘除颠倒后画面"看着还行"但刻度全错）
    if "0 ~ 56ns" not in (r["meta"] or ""):
        fails.append(f"头部时长不对（时间换算可能反了）：{r['meta']!r}")

    # 端到端：光标处显示的值必须和解析器算出来的一致。
    # 全览视图下 x=1000 落在 44.21ns，此时 dut.q 已经是 1（35ns 从 0 变 1，
    # 45ns 才变 2），clk 在 40ns~45ns 之间是 0。
    vals = r["cursorVals"]      # 按行序（自上而下），即 q/clk/en/rst_n 各两遍
    print(f"  光标处的值（按行序）：{vals}")
    if len(vals) != len(dump.signals):
        fails.append(f"光标值只画了 {len(vals)} 个，应有 {len(dump.signals)} 个")
    elif vals[-1] != "1":
        fails.append(f"dut.q 在光标处显示 {vals[-1]!r}，44.21ns 应为 1")
    elif vals[0] != "1":
        fails.append(f"q 在光标处显示 {vals[0]!r}，44.21ns 应为 1")

    print()
    if fails:
        for f in fails:
            print(f"  ✗ {f}")
        print(f"\n波形交互检查未通过：{len(fails)} 项")
        return 1
    print("  ✓ 缩放 / 平移 / 纵向滚动 / 折叠 / 光标 / 主题切换全部执行无异常")
    print("  ✓ save-restore 配对，绘制调用确实产生了几何与文字")
    print("\n波形交互检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
