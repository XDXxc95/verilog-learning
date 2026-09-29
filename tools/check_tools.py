"""按课程级别体检工具能力，报告「缺什么、缺了会怎样、装什么能补」。

用法：
    python tools/check_tools.py                 # 所有级别
    python tools/check_tools.py --level L2      # 只看 L2
    python tools/check_tools.py --json          # stdout 只要 JSON
    python tools/check_tools.py --software      # 顺带列出每个软件的探测结果

约定与 sim.py 一致：**stdout 是一个 JSON，stderr 是人看的摘要**。

本脚本**只报告，不安装、不改任何东西**。缺失项按影响分级列出，
由使用者自己决定装不装——缺工具不该阻塞开课（见 CLAUDE.md）。

退出码：
    0  该级别的能力全部可用
    1  有缺失（仅提示，不代表出错）
    4  清单本身有问题，或环境异常
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _console import init_console  # noqa: E402
from _manifest import (  # noqa: E402
    IMPACT_ICON,
    IMPACT_LABEL,
    IMPACT_ORDER,
    WINDOWS_LABEL,
    ManifestError,
    evaluate,
    load,
    probe_all,
    summarize_for_level,
    validate,
)

EXIT_OK = 0
EXIT_GAPS = 1
EXIT_ENV = 4

LEVELS = ["L1", "L2", "L3"]


def _print_software(manifest, probes) -> None:
    print("\n软件探测结果", file=sys.stderr)
    print("─" * 66, file=sys.stderr)
    for sw in manifest.software:
        p = probes[sw.id]
        if p.found and p.usable:
            mark = "✓"
            where = str(p.exe_path) if p.exe_path else "（目录存在）"
            ver = f"  {p.version}" if p.version else ""
            print(f"  {mark} {sw.name:<22} {where}{ver}", file=sys.stderr)
            if p.detail:
                print(f"      {p.detail}", file=sys.stderr)
        elif p.found:
            # 文件在、但真跑一次过不去。既不能打 ✓（会谎报它能补能力），
            # 也不能打 ✗ 未安装（它确实在）。单独一档，把原因原样带出来。
            print(f"  ⚠ {sw.name:<22} 已安装但不可用   {p.exe_path}", file=sys.stderr)
            if p.detail:
                print(f"      {p.detail}", file=sys.stderr)
        else:
            avail = WINDOWS_LABEL.get(sw.windows, sw.windows)
            size = f"  {sw.size_mb} MB" if sw.size_mb else ""
            print(f"  ✗ {sw.name:<22} 未安装   [{avail}{size}]", file=sys.stderr)


def _print_level(manifest, level, probes) -> dict:
    s = summarize_for_level(manifest, level, probes)
    total, ok = s["capabilities_total"], s["capabilities_ok"]

    if ok == total:
        print(f"\n{level}：{ok}/{total} 项能力可用 —— 无缺口 ✓", file=sys.stderr)
        return s

    print(f"\n{level}：{ok}/{total} 项能力可用，缺 {total - ok} 项", file=sys.stderr)
    print("─" * 66, file=sys.stderr)

    for item in s["unmet"]:
        icon = IMPACT_ICON.get(item["impact"], "?")
        label = IMPACT_LABEL.get(item["impact"], item["impact"])
        print(f"  {icon} {item['name']}   （{label}）", file=sys.stderr)
        if item["both_gaps"]:
            # 软件和代码两边都缺。**不能只说其中一句**：
            # 只报软件，使用者装完会以为齐了（而 integration 里写着「仓库有活干」）；
            # 只报代码，又会说成「装什么都没用」，可软件确实是卡口之一。
            names = ", ".join(item["missing_software"])
            print(f"     这条**两个都缺**：软件要装（{names}），", file=sys.stderr)
            print("     装完本仓库还得补接入代码 —— 只补一边都不算齐", file=sys.stderr)
        elif item["code_gap"]:
            print("     缺的是代码不是软件 —— 装什么都没用，由本仓库自己补", file=sys.stderr)
        else:
            names = ", ".join(item["missing_software"])
            print(f"     需要：{names}", file=sys.stderr)
            # 缺失软件里有 Windows 原生装不上的，必须当场说清楚，
            # 否则使用者会把它和「去 --install 就行」的那堆混为一谈
            if item["unavailable_software"]:
                note = "、".join(item["unavailable_software"])
                print(
                    f"     ⚠ 其中 {note} 在 Windows 原生装不了（要 WSL2）—— 本仓库默认不装",
                    file=sys.stderr,
                )
        if item["fallback"]:
            print(f"     没有它：{item['fallback']}", file=sys.stderr)

    if s["missing_software"]:
        print("\n  装上这些可补上面的缺口：", file=sys.stderr)
        for sw in s["missing_software"]:
            size = f"{sw['size_mb']} MB" if sw["size_mb"] else "体积未知"
            print(f"  {sw['id']:<22} {size:>10}   {WINDOWS_LABEL.get(sw['windows'], '')}", file=sys.stderr)
            if sw.get("caveat"):
                print(f"      注意：{sw['caveat']}", file=sys.stderr)
        print(
            "  安装：python tools/setup_tools.py --install "
            + " ".join(sw["id"] for sw in s["missing_software"])
            + " --lock",
            file=sys.stderr,
        )

    if s["unavailable_software"]:
        print("\n  下面这些本仓库默认装不了，缺的能力只能走「讲 + 手算 + 读样例」：", file=sys.stderr)
        for sw in s["unavailable_software"]:
            print(f"  ⚠ {sw['id']:<22} {WINDOWS_LABEL.get(sw['windows'], '')}", file=sys.stderr)

    if s["blocking"]:
        print(
            f"\n  ⚠ 其中 {len(s['blocking'])} 项是「整讲做不了」级别：{', '.join(s['blocking'])}",
            file=sys.stderr,
        )
    return s


def main(argv: list[str] | None = None) -> int:
    init_console()

    ap = argparse.ArgumentParser(
        description="按课程级别体检工具能力",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python tools/check_tools.py --level L2\n"
            "  python tools/check_tools.py --software --json > tools_report.json\n"
        ),
    )
    ap.add_argument("--level", choices=LEVELS, help="只体检某一级别（默认全部）")
    ap.add_argument("--json", action="store_true", help="stdout 只要 JSON，不打人读摘要")
    ap.add_argument("--software", action="store_true", help="顺带列出每个软件的探测结果")
    args = ap.parse_args(argv)

    # ---- 读清单。清单坏了必须大声报错，绝不能降级成空清单谎报「全部可用」
    try:
        manifest = load()
    except ManifestError as exc:
        print(f"清单加载失败：{exc}", file=sys.stderr)
        return EXIT_ENV

    problems = validate(manifest)
    if problems:
        print("清单自洽性校验未通过：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return EXIT_ENV

    probes = probe_all(manifest)

    levels = [args.level] if args.level else LEVELS
    per_level = {lv: summarize_for_level(manifest, lv, probes) for lv in levels}

    payload = {
        "schema": "vlearn.tools/1",
        "manifest_schema": manifest.schema,
        "manifest_updated": manifest.updated,
        "levels": per_level,
        "software": [
            {
                "id": sw.id,
                "name": sw.name,
                "windows": sw.windows,
                "found": probes[sw.id].found,
                "usable": probes[sw.id].usable,
                "exe": str(probes[sw.id].exe_path) if probes[sw.id].exe_path else None,
                "version": probes[sw.id].version,
                "detail": probes[sw.id].detail,
            }
            for sw in manifest.software
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if not args.json:
        if args.software:
            _print_software(manifest, probes)
        for lv in levels:
            _print_level(manifest, lv, probes)

    any_gap = any(
        v["capabilities_ok"] < v["capabilities_total"] for v in per_level.values()
    )
    return EXIT_GAPS if any_gap else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
