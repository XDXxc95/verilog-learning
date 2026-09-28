"""把生成 HTML 里的 JS 抠出来交给 node 做语法检查。

    python tests/_smoke/check_wave_js.py

为什么值得单独做：HTML 是**模板字符串拼出来的**，里面的 JS 我改起来没有
任何编辑器提示，一个手滑的括号在浏览器里表现为"白屏 + 控制台一行红字"，
而如果没人去点开页面，这个错误会一路带到学习者面前。

浏览器我给不了，但 js 的**语法**能用 node 验。检查不了的部分（canvas
绘制、事件绑定）靠眼睛，检查得了的部分就别靠眼睛。
"""

from __future__ import annotations

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


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("  ! 找不到 node，跳过 JS 语法检查（不是失败，但少了一层保护）")
        return 0

    dump = vcdmod.parse(ROOT / "build" / "_smoke_counter4.vcd")
    out = ROOT / "build" / "_smoke_wave.html"
    wavemod.render_html(dump, out)
    html = out.read_text(encoding="utf-8")

    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    if not scripts:
        print("  ✗ HTML 里没有 <script> 块")
        return 1

    fails = 0
    for n, js in enumerate(scripts):
        # 把 DATA 换成空壳，让 node 只做**解析**，不去真的跑（没有 document）
        probe = ROOT / "build" / f"_probe_{n}.js"
        probe.write_text(js, encoding="utf-8", newline="\n")
        p = subprocess.run([node, "--check", str(probe)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            fails += 1
            print(f"  ✗ 第 {n} 个 script 块语法有误：")
            for line in (p.stderr or "").strip().splitlines()[:12]:
                print(f"      {line}")
        else:
            print(f"  ✓ 第 {n} 个 script 块语法通过（{len(js)} 字符）")

    # 顺手查几个"漏了会静默出问题"的点
    checks = [
        ("canvas 元素存在", 'id="cv"' in html),
        ("滚轮缩放已绑定", "addEventListener(\"wheel\"" in html),
        ("拖动平移已绑定", "addEventListener(\"mousedown\"" in html),
        ("时间光标已绑定", "cursorT" in html),
        ("折叠用 relayout", "relayout()" in html),
        ("0/1/x/z 分色齐全", all(k in html for k in ["--v0", "--v1", "--vx", "--vz"])),
        ("明暗主题", "prefers-color-scheme" in html and "html.dark" in html),
        ("视口裁剪（二分查找）", "idxAt(" in html),
    ]
    for name, ok in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
        if not ok:
            fails += 1

    print()
    if fails:
        print(f"波形 JS 检查未通过：{fails} 项")
        return 1
    print("波形 JS 检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
