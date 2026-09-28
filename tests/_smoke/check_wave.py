"""波形渲染的冒烟检查：对着真实 VCD 跑 ASCII + HTML 两种后端。

    python tests/_smoke/check_wave.py

不检查"画得好不好看"（那要靠眼睛），只检查那些**出错会静默**的地方：
ASCII 行宽是否对齐、信号有没有丢、HTML 是否真的自包含、
NULL 字节／未转义标签有没有混进去、数据下标引用是否对得上。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from _console import init_console  # noqa: E402

init_console()

import vcd as vcdmod  # noqa: E402
import wave as wavemod  # noqa: E402

BUILD = ROOT / "build"
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        fails.append(msg)


def decode(sig: dict, v) -> str:
    """把 HTML payload 里的值解回位串（与 wave.py 里的 JS decode 同一套编码）。

    这里是**故意重写一遍**而不是调用 wave.py 的 Python 版：
    要验的正是"编码端和解码端对得上"，共用一份实现就验不出问题了。
    """
    if not sig["pk"]:
        return v
    out = ""
    for i in range(sig["w"]):
        out = "01xz"[int(v // (4 ** i)) % 4] + out
    return out


def gen_vcd() -> Path:
    """先跑一遍仿真拿到新鲜的 VCD，避免测的是陈年产物。"""
    src = ROOT / "tests" / "_smoke" / "dut_counter4.v"
    tb = ROOT / "tests" / "_smoke" / "tb_counter4.v"
    p = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "sim.py"), str(src),
         "--tb", str(tb), "--top", "tb_counter4", "--quiet"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    res = json.loads(p.stdout.strip().splitlines()[-1])
    check(res.get("status") == "pass", f"仿真未通过：{res.get('status')}")
    v = res.get("artifacts", {}).get("vcd")
    check(bool(v), "sim.py 没产出 VCD 路径")
    return ROOT / v


def main() -> int:
    path = gen_vcd()
    dump = vcdmod.parse(path)
    print(f"VCD: {path.name}  信号 {len(dump.signals)}  跳变 {dump.n_changes()}  notes={dump.notes}")

    # ---------------- ASCII ----------------
    txt = wavemod.render_ascii(dump, columns=88)
    print("\n" + txt + "\n")

    lines = txt.splitlines()
    body = [ln for ln in lines if ln and not ln.startswith("时间单位")]
    widths = {len(ln) for ln in body if ln.strip()}
    # 每行长度必须一致（除中文说明行），否则终端里波形会错位
    check(len(widths) <= 2, f"ASCII 各行长度不一致：{sorted(widths)}")

    for s in dump.signals:
        check(s.path in txt or s.name in txt, f"信号 {s.path} 没出现在 ASCII 里")

    # 值必须真的画出来了，而不是一片空白
    check("‾" in txt, "ASCII 里没有任何高电平")
    check("_" in txt, "ASCII 里没有任何低电平")
    check("│" in txt, "总线行没有画出跳变位置")

    # 窄信号选择
    only_clk = wavemod.render_ascii(dump, ["dut.q"], columns=64)
    check("dut.q" in only_clk and "clk" not in only_clk.replace("tb_counter4.dut.q", ""),
          "按模式筛选信号没生效")

    # ---------------- HTML ----------------
    out = BUILD / "_smoke_wave.html"
    wavemod.render_html(dump, out, title="计数器波形检查")
    raw = out.read_bytes()
    html = raw.decode("utf-8")
    print(f"HTML: {out.name}  {len(raw)/1024:.1f} KB")

    check(len(raw) > 4000, "HTML 太小，八成没写进去")
    check(out.stat().st_size < 200_000, "HTML 异常大，数据可能重复内联了")

    # 自包含：不能有任何外部引用
    for bad in re.findall(r'(?:src|href)\s*=\s*["\'](https?:|//)', html):
        fails.append(f"HTML 引用了外部资源：{bad}")
    check("<script" in html and "cdn" not in html.lower(), "HTML 结构可疑")

    # 数据必须是合法 JSON 且下标能对上
    m = re.search(r"const DATA = (\{.*?\});\r?\n", html, re.S)
    check(m is not None, "HTML 里找不到 DATA 常量")
    if not m:
        return _report()

    data = json.loads(m.group(1).replace("\\u003c", "<"))
    check(len(data["signals"]) == len(dump.signals), "HTML 里的信号数与 VCD 不一致")
    check(len(data["streams"]) < len(dump.signals),
          "别名信号没去重，跳变数据被存了多份")
    for s in data["signals"]:
        check(0 <= s["i"] < len(data["streams"]), f"信号 {s['p']} 的流下标越界")
    # 流必须按时间严格递增（二分查找 / 视口裁剪都依赖这个不变量）
    for si, st in enumerate(data["streams"]):
        check(all(a < b for a, b in zip(st["t"], st["t"][1:])),
              f"流 #{si} 的时间戳不是严格递增的")

    # 真实值抽样：dut.q 在 45ns 应该是 2（和测试台的时序对上才算数据没串）
    hits = [s for s in data["signals"] if s["p"].endswith("dut.q")]
    check(len(hits) == 1, f"dut.q 应该只有一个，实际 {len(hits)}")
    if hits:
        sig = hits[0]
        st = data["streams"][sig["i"]]
        vals = [decode(sig, v) for v in st["v"]]
        k = max(i for i, t in enumerate(st["t"]) if t <= 45000)
        check(vals[k] == "0010", f"dut.q@45ns = {vals[k]!r}，期望 0010")
        # x 必须原样保留：渲染层的颜色分支按单字符 'x'/'z' 判断
        check(any(set(v) & set("xz") for v in vals),
              "dut.q 里没有 x，初值可能被丢了")
        check(st["v"][0] != "xxxx", "打包开关没生效（值仍是位串）")

    # 打包必须是无损的：解回来要和 vcd.py 里的原值逐字节相同
    for sig in data["signals"]:
        if not sig["pk"]:
            continue
        st = data["streams"][sig["i"]]
        orig = [s for s in dump.signals if s.path == sig["p"]][0]
        check(orig.stream.values[:50] == [decode(sig, v) for v in st["v"]][:50],
              f"{sig['p']} 打包-解码后与原值不一致")

    return _report()

    return _report()


def _report() -> int:
    print()
    if fails:
        for f in fails:
            print(f"  ✗ {f}")
        print(f"\n波形检查未通过：{len(fails)} 项")
        return 1
    print("  ✓ ASCII 行宽对齐、信号齐全、总线跳变位置可见")
    print("  ✓ HTML 自包含（无外部引用）、流按 ID 去重、时间戳严格递增")
    print("  ✓ dut.q@45ns = 2，与测试台时序一致；x 初值原样保留")
    print("\n波形检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
