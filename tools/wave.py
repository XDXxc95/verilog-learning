"""波形渲染：终端 ASCII + 自包含 HTML。

    from vcd import parse
    from wave import render_ascii, render_html

    dump = parse(Path("build/x.vcd"))
    print(render_ascii(dump))                    # 给我（Claude）快速判读
    render_html(dump, Path("build/x.html"))      # 给学习者交互查看

两种后端的分工：

  ASCII  轻量、可直接进对话上下文，用于"这一步时序对不对"的快速判断。
         一列一个采样点，**窄脉冲会被采样丢掉**，所以它只适合看大结构，
         不适合查 glitch。

  HTML   完整跳变数据按视口裁剪后画在 canvas 上，不丢任何边沿。
         完全自包含（不引 CDN、不联网），双击就能开。

HTML 的性能约束（VCD 动辄几十万次跳变，踩过就知道）：

  * **绝不按跳变建 DOM 节点**。整张图是一个 <canvas>，重画时只遍历
    视口内的跳变（二分定位起止），跳变再多也只画屏幕上那几个像素。
  * 跳变数据压成两条平行数组（时间 / 值）内联进 HTML，
    别名信号（同 ID 不同层次名）只存一份，用下标引用。
"""

from __future__ import annotations

import json
from pathlib import Path

import vcd as vcdmod
from vcd import VCD, Signal, format_time, to_display

# 值 → 颜色（与 HTML 里的 CSS 变量保持一致）
_ASCII_LOW = "_"
_ASCII_HIGH = "‾"      # ‾  OVERLINE：和 _ 视觉上对称
_ASCII_BUS_EDGE = "│"  # │
_ASCII_BUS_LINE = "─"  # ─

DEFAULT_COLUMNS = 96
DEFAULT_NAME_WIDTH = 26


# ============================================================ ASCII


def _sample_columns(sig: Signal, t0: int, t1: int, width: int) -> list[str | None]:
    """把信号在 [t0, t1] 上采样成 width 个值。

    每一列的取值 = 该列起始时刻的值（保持型采样）。窄脉冲会被吞掉，
    这是刻意的取舍：ASCII 用来一眼看清结构，细节交给 HTML。
    """
    if width <= 0:
        return []
    span = max(1, t1 - t0)
    out: list[str | None] = []
    for c in range(width):
        t = t0 + span * c // width
        out.append(sig.value_at(t))
    return out


def _scalar_row(values: list[str | None]) -> str:
    chars = []
    for v in values:
        if v is None:
            chars.append(" ")
        elif v == "0":
            chars.append(_ASCII_LOW)
        elif v == "1":
            chars.append(_ASCII_HIGH)
        elif v == "x":
            chars.append("x")
        elif v == "z":
            chars.append("z")
        else:
            chars.append("?")
    return "".join(chars)


def _bus_row(values: list[str | None], fmt) -> str:
    """总线行：`│<值>────│<值>────` —— 竖线标出跳变位置，值写在跳变之后。"""
    width = len(values)
    row = [" "] * width
    c = 0
    while c < width:
        v = values[c]
        end = c
        while end < width and values[end] == v:
            end += 1
        span = end - c
        if v is None:
            c = end
            continue
        label = fmt(v)
        row[c] = _ASCII_BUS_EDGE
        room = span - 1
        if room >= len(label):
            for k, ch in enumerate(label):
                row[c + 1 + k] = ch
            for k in range(c + 1 + len(label), end):
                row[k] = _ASCII_BUS_LINE
        elif room > 0:
            # 放不下完整值：只放得下多少放多少，末尾用 ~ 表示被截断
            for k in range(room):
                row[c + 1 + k] = label[k]
            if room >= 1:
                row[end - 1] = "~"
        # room <= 0：太窄，只留竖线
        c = end
    return "".join(row)


def _time_axis(vcd: VCD, t0: int, t1: int, width: int, indent: int) -> list[str]:
    """时间轴：两行，第一行刻度文字，第二行刻度线。"""
    if width <= 0:
        return []
    span = max(1, t1 - t0)
    # 选一个让刻度大约每 12 列出现一次的间隔
    raw = span * 12 / width
    unit = vcdmod.display_unit(vcd)
    unit_fs = vcdmod.UNIT_FS[unit] // vcd.timescale_fs or 1
    step = max(unit_fs, round(raw / unit_fs) * unit_fs)

    label_row = [" "] * width
    tick_row = [" "] * width
    t = (t0 // step) * step
    while t <= t1:
        col = (t - t0) * width // span
        if 0 <= col < width:
            text = f"{int(t * vcd.timescale_fs // vcdmod.UNIT_FS[unit])}"
            start = col
            if start + len(text) <= width:
                for k, ch in enumerate(text):
                    label_row[start + k] = ch
            tick_row[col] = "|"
        t += step

    pad = " " * indent
    return [pad + "".join(label_row), pad + "".join(tick_row)]


def render_ascii(
    vcd: VCD,
    patterns: list[str] | None = None,
    columns: int | None = None,
    t0: int | None = None,
    t1: int | None = None,
    name_width: int = DEFAULT_NAME_WIDTH,
    base: int | None = None,
) -> str:
    """把波形画成终端可读的 ASCII。"""
    sigs = _select(vcd, patterns)
    if not sigs:
        return "（没有匹配的信号）"

    t0 = 0 if t0 is None else t0
    t1 = vcd.end_time if t1 is None else t1
    if t1 <= t0:
        t1 = t0 + 1

    width = columns or DEFAULT_COLUMNS
    width = max(24, width)

    lines: list[str] = []
    lines += _time_axis(vcd, t0, t1, width, name_width)

    for sig in sigs:
        values = _sample_columns(sig, t0, t1, width)
        tag = f" [{sig.width}]" if sig.is_bus else ""
        name = sig.path
        if len(name) + len(tag) > name_width - 1:
            name = "…" + name[-(name_width - len(tag) - 2):]
        head = f"{name}{tag}".ljust(name_width)

        if sig.is_bus:
            row = _bus_row(values, lambda v, s=sig: vcd.format_value(s, v, base))
        else:
            row = _scalar_row(values)
        lines.append(head + row)

    lines.append("")
    lines.append(
        f"时间单位 {vcdmod.display_unit(vcd)}，范围 "
        f"{format_time(vcd, t0)} ~ {format_time(vcd, t1)}，"
        f"每列约 {to_display(vcd, (t1 - t0)) / width:.2f}{vcdmod.display_unit(vcd)} "
        f"（窄脉冲会被采样丢掉，查细节请看 HTML 波形）"
    )
    return "\n".join(lines)


def _select(vcd: VCD, patterns: list[str] | None) -> list[Signal]:
    if not patterns:
        return list(vcd.signals)
    out: list[Signal] = []
    for p in patterns:
        for s in vcd.find(p):
            if s not in out:
                out.append(s)
    return out


# ============================================================ HTML

# 单个 HTML 里内联的跳变总数上限。
# 超过就明确拒绝，而不是生成一个几十 MB、浏览器打开就卡死的页面 ——
# "生成失败并说明原因"比"生成一个用不了的东西"好。
MAX_INLINE_CHANGES = 1_000_000


class WaveTooLarge(Exception):
    """跳变太多，不适合塞进单文件 HTML。"""


def render_html(
    vcd: VCD,
    out_path: Path,
    title: str | None = None,
    patterns: list[str] | None = None,
    max_changes: int = MAX_INLINE_CHANGES,
) -> Path:
    sigs = _select(vcd, patterns)
    if not sigs:
        raise WaveTooLarge("没有匹配的信号，无法生成波形")

    if vcd.n_changes() > max_changes:
        raise WaveTooLarge(
            f"跳变数 {vcd.n_changes()} 超过 {max_changes}，单文件 HTML 会过大且浏览器会卡。"
            f"建议缩窄 $dumpvars 的范围（只 dump 关心的层次），或缩短仿真时间。"
        )

    payload = _build_payload(vcd, sigs, title)
    html = _TEMPLATE.replace("__DATA__", _json_for_script(payload))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：Windows 上 write_text 默认会把 \n 翻成 \r\n，
    # 生成物本身无所谓，但会让"按行匹配"的下游脚本（含本仓库的检查脚本）
    # 莫名其妙地匹配不上。统一用 LF，省掉一整类环境相关的怪问题。
    out_path.write_text(html, encoding="utf-8", newline="\n")
    return out_path


def _json_for_script(obj) -> str:
    # `</script` 出现在字符串里会提前结束脚本块；`<` 转义掉最省事
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


# 打包上限：每个信号位占 2 bit，所以 2*width 必须落在 JS 双精度的精确整数
# 范围内（2^53）。52 bit -> width 26 本来也行，这里留点余量取 20。
_MAX_PACK_WIDTH = 20

# 每个信号位的 2 bit 编码
_BIT_CODE = {"0": 0, "1": 1, "x": 2, "z": 3}


def _pack_value(bits: str) -> int:
    """把位串打成整数：每 bit 用 2 bit 表示 0/1/x/z（低位在低地址）。

    16 位信号的一个值在 JSON 里是 `"0000000000000010"`（18 字符），
    打完之后是 `2`（1 字符）。教育场景里位宽越大越省，而位宽大的信号
    恰恰是最费空间的那些。单比特信号（时钟）不打 —— "0" 本来就只占 1 字符，
    打了反而要在绘制热路径上加一层解码。
    """
    n = 0
    for i, ch in enumerate(reversed(bits)):
        n |= _BIT_CODE.get(ch, 2) << (2 * i)
    return n


def _build_payload(vcd: VCD, sigs: list[Signal], title: str | None) -> dict:
    """把数据整理成 JS 侧最省事的形状。

    流（stream）按 ID 去重后单独存一份，信号只存一个下标 ——
    别名信号（同一条网线在不同层次下的名字）因此不会把跳变数据存两遍。
    """
    stream_key: dict[int, int] = {}
    streams: list[dict] = []

    def stream_index(sig: Signal) -> int:
        key = id(sig.stream)
        idx = stream_key.get(key)
        if idx is None:
            idx = len(streams)
            stream_key[key] = idx
            packed = (not sig.is_real) and 1 < sig.width <= _MAX_PACK_WIDTH
            streams.append({
                "t": sig.stream.times,
                "v": ([_pack_value(v) for v in sig.stream.values] if packed
                      else sig.stream.values),
            })
        return idx

    signals = []
    for s in sigs:
        packed = (not s.is_real) and 1 < s.width <= _MAX_PACK_WIDTH
        signals.append({
            "n": s.name,
            "p": s.path,
            "s": s.scope,
            "w": s.width,
            "r": 1 if s.is_real else 0,
            "k": s.kind,
            # pk=1：v 里存的是打包整数，取值时要先解回位串
            "pk": 1 if packed else 0,
            "i": stream_index(s),
        })

    scopes: list[str] = []
    for s in sigs:
        if s.scope not in scopes:
            scopes.append(s.scope)

    return {
        "title": title or Path(vcd.path).stem,
        "unit": vcdmod.display_unit(vcd),
        "unitFs": vcdmod.UNIT_FS[vcdmod.display_unit(vcd)],
        "tsFs": vcd.timescale_fs,
        "end": vcd.end_time,
        "nchg": sum(st["t"].__len__() for st in streams),
        "scopes": scopes,
        "signals": signals,
        "streams": streams,
        "notes": vcd.notes,
    }


# 超过这个跳变数就在页面上挂一条提示。不是错误 —— 是"页面会有点大，
# 想更流畅可以缩短仿真时间或缩窄 $dumpvars 的范围"。
_LARGE_WAVE_CHANGES = 300_000


# HTML 模板：刻意用 __DATA__ 占位而不是 f-string / .format()，
# 因为模板里满是 CSS 的 `{}`，用 format 会炸。
# JS 里也没有任何外部依赖。
_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>波形</title>
<style>
  :root {
    --bg: #ffffff; --fg: #1a1a1a; --dim: #6b7280; --grid: #e5e7eb;
    --panel: #f9fafb; --line: #d1d5db; --accent: #2563eb;
    --v0: #0f766e; --v1: #b45309; --vx: #dc2626; --vz: #7c3aed;
    --bus: #1f2937; --cursor: #ef4444;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1115; --fg: #e5e7eb; --dim: #9ca3af; --grid: #262b36;
      --panel: #161a21; --line: #2b313c; --accent: #60a5fa;
      --v0: #2dd4bf; --v1: #fbbf24; --vx: #f87171; --vz: #c084fc;
      --bus: #cbd5e1; --cursor: #f87171;
    }
  }
  html.dark { --bg:#0f1115; --fg:#e5e7eb; --dim:#9ca3af; --grid:#262b36;
    --panel:#161a21; --line:#2b313c; --accent:#60a5fa; --v0:#2dd4bf;
    --v1:#fbbf24; --vx:#f87171; --vz:#c084fc; --bus:#cbd5e1; --cursor:#f87171; }
  html.light { --bg:#ffffff; --fg:#1a1a1a; --dim:#6b7280; --grid:#e5e7eb;
    --panel:#f9fafb; --line:#d1d5db; --accent:#2563eb; --v0:#0f766e;
    --v1:#b45309; --vx:#dc2626; --vz:#7c3aed; --bus:#1f2937; --cursor:#ef4444; }

  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--fg);
         font: 13px/1.5 ui-sans-serif, system-ui, "Segoe UI", sans-serif; }
  header { padding: 10px 14px; border-bottom: 1px solid var(--line);
           display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
           background: var(--panel); position: sticky; top: 0; z-index: 5; }
  header h1 { font-size: 14px; margin: 0; font-weight: 600; }
  header .meta { color: var(--dim); font-size: 12px; }
  button { font: inherit; padding: 3px 10px; border-radius: 6px;
           border: 1px solid var(--line); background: var(--bg); color: var(--fg);
           cursor: pointer; }
  button:hover { border-color: var(--accent); color: var(--accent); }
  #wrap { position: relative; }
  canvas { display: block; width: 100%; }
  .hint { padding: 8px 14px; color: var(--dim); font-size: 12px;
          border-top: 1px solid var(--line); }
  .note { padding: 6px 14px; color: var(--vx); font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1 id="title">波形</h1>
  <span class="meta" id="meta"></span>
  <span style="flex:1"></span>
  <span class="meta" id="cursor">—</span>
  <button id="zin">放大 +</button>
  <button id="zout">缩小 −</button>
  <button id="zfit">全览</button>
  <button id="theme">明/暗</button>
</header>
<div id="wrap"><canvas id="cv"></canvas></div>
<div class="hint">
  滚轮缩放 · 拖动平移 · Shift+滚轮纵向滚动 · 单击层次名折叠/展开 · 鼠标移动显示该时刻的值
</div>
<div id="notes"></div>

<script>
"use strict";
const DATA = __DATA__;

// ---- 基础换算 ----
// VCD 里的时间戳是以 $timescale（这里 1ps）为单位的整数，
// 和 vcd.to_display() 同一条公式：显示值 = t * tsFs / unitFs。
// TS = "1 个显示单位等于几个内部时间戳"（ns → 1000 个 ps），所以是乘。
const TS = DATA.tsFs / DATA.unitFs;
const t2u = t => t * TS;
const u2t = u => Math.round(u / TS);

// ---- 值解码 / 格式化（与 tools/vcd.py 的 format_value 保持一致）----
// 2 bit 一档，低位在低地址；4^i 全部是 2 的幂，除法在双精度下是精确的，
// 所以这里的取位不需要 BigInt，也不会掉精度（位宽上限见 _MAX_PACK_WIDTH）。
const P4 = [];
for (let i = 0, p = 1; i <= 26; i++, p *= 4) P4.push(p);

function decode(sig, v) {
  if (v == null) return null;
  if (!sig.pk) return v;
  let s = "";
  for (let i = 0; i < sig.w; i++) s = "01xz"[Math.floor(v / P4[i]) % 4] + s;
  return s;
}

function fmtVal(sig, raw) {
  if (raw == null) return "-";
  if (sig.r) return raw;
  if (sig.w === 1) return raw;
  const bits = decode(sig, raw);
  if (/[xz]/.test(bits)) return bits;
  const n = BigInt("0b" + bits);
  if (sig.w <= 8) return n.toString(10);
  let h = n.toString(16);
  const need = Math.ceil(sig.w / 4);
  while (h.length < need) h = "0" + h;
  return h;
}

// ---- 布局 ----
const NAME_W = 250;
const ROW_H = 22;
const BUS_H = 30;
const HEAD_H = 26;
const PAD_TOP = 8;

const cv = document.getElementById("cv");
const ctx = cv.getContext("2d");
let css = getComputedStyle(document.documentElement);
const C = n => css.getPropertyValue(n).trim();

let groups = [];      // [{scope, sigs:[idx], collapsed}]
let rows = [];        // 展平后的行：{kind:'group'|'sig', ...}
let view0 = 0, view1 = DATA.end || 1;   // 显示单位下的可见区间
let scrollY = 0;
let cursorT = null;

function buildRows() {
  const byScope = new Map();
  DATA.signals.forEach((s, i) => {
    if (!byScope.has(s.s)) byScope.set(s.s, []);
    byScope.get(s.s).push(i);
  });
  groups = DATA.scopes.map(sc => ({ scope: sc, sigs: byScope.get(sc) || [], collapsed: false }));
  relayout();
}

function relayout() {
  rows = [];
  for (const g of groups) {
    rows.push({ kind: "group", g });
    if (g.collapsed) continue;
    for (const i of g.sigs) rows.push({ kind: "sig", i, h: DATA.signals[i].w > 1 || DATA.signals[i].r ? BUS_H : ROW_H });
  }
}

// ---- 值的二分查找：找最后一个 time <= t 的下标 ----
function idxAt(times, t) {
  let lo = 0, hi = times.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}
function valAt(sig, t) {
  const st = DATA.streams[sig.i];
  const k = idxAt(st.t, t);
  return k < 0 ? null : st.v[k];
}

// ---- 绘制 ----
function resize() {
  const w = cv.parentElement.clientWidth;
  const h = window.innerHeight - cv.getBoundingClientRect().top - 60;
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.max(320, w) * dpr;
  cv.height = Math.max(200, h) * dpr;
  cv.style.height = Math.max(200, h) + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  clampScroll();   // 窗口变高之后原来的滚动位置可能已经越界
  draw();
}

const xOf = t => NAME_W + (t2u(t) - view0) / (view1 - view0) * (cv.clientWidth - NAME_W);
const tOf = x => u2t(view0 + (x - NAME_W) / (cv.clientWidth - NAME_W) * (view1 - view0));

function draw() {
  const W = cv.clientWidth, H = cv.clientHeight;
  ctx.clearRect(0, 0, W, H);
  ctx.font = "12px ui-monospace, Consolas, monospace";
  ctx.textBaseline = "middle";

  const y0 = HEAD_H - scrollY;

  // ---- 时间轴 ----
  ctx.fillStyle = C("--panel");
  ctx.fillRect(0, 0, W, HEAD_H);
  ctx.strokeStyle = C("--line");
  ctx.beginPath(); ctx.moveTo(0, HEAD_H - .5); ctx.lineTo(W, HEAD_H - .5); ctx.stroke();

  const span = view1 - view0;
  const step = niceStep(span, (W - NAME_W) / 90);
  ctx.fillStyle = C("--dim");
  ctx.textAlign = "left";
  for (let u = Math.ceil(view0 / step) * step; u <= view1; u += step) {
    const x = NAME_W + (u - view0) / span * (W - NAME_W);
    if (x < NAME_W - 1) continue;
    ctx.strokeStyle = C("--grid");
    ctx.beginPath(); ctx.moveTo(x, HEAD_H); ctx.lineTo(x, H); ctx.stroke();
    ctx.fillText(fmtTime(u), x + 3, HEAD_H / 2);
  }

  // ---- 各行 ----
  // 分两趟：先画名字（不受裁剪影响），再把波形整体裁在 [NAME_W, W) 里画。
  // 否则平移/缩放后，落在视口左侧的跳变会一路画到名字那一列上，把名字糊掉。
  const drawRows = fn => {
    let y = PAD_TOP + y0;
    for (const row of rows) {
      const h = row.kind === "group" ? ROW_H : row.h;
      if (row.kind !== "group" && y + h >= HEAD_H && y <= H) fn(row, y, h);
      y += h;
    }
  };

  drawRows((row, y, h) => {
    ctx.fillStyle = C("--fg");
    ctx.textAlign = "left";
    drawName(DATA.signals[row.i], y, h);
  });

  ctx.save();
  ctx.beginPath(); ctx.rect(NAME_W, HEAD_H, W - NAME_W, H - HEAD_H); ctx.clip();
  drawRows((row, y, h) => {
    const sig = DATA.signals[row.i];
    const st = DATA.streams[sig.i];
    const lo = Math.max(0, idxAt(st.t, tOf(NAME_W)));
    const hi = Math.min(st.t.length - 1, idxAt(st.t, tOf(W)) + 1);
    if (sig.w > 1 || sig.r) drawBus(sig, st, lo, hi, y, h);
    else drawScalar(sig, st, lo, hi, y, h);
  });
  ctx.restore();

  // 层次标题行画在波形之上，full width
  let gy = PAD_TOP + y0;
  for (const row of rows) {
    if (row.kind !== "group") { gy += row.h; continue; }
    ctx.fillStyle = C("--panel");
    ctx.fillRect(0, gy, W, ROW_H);
    ctx.fillStyle = C("--accent");
    ctx.textAlign = "left";
    ctx.fillText((row.g.collapsed ? "▶ " : "▼ ") + row.g.scope, 8, gy + ROW_H / 2);
    ctx.strokeStyle = C("--line");
    ctx.beginPath(); ctx.moveTo(0, gy + ROW_H - .5); ctx.lineTo(W, gy + ROW_H - .5); ctx.stroke();
    gy += ROW_H;
  }

  // ---- 时间光标 ----
  if (cursorT != null) {
    const x = xOf(cursorT);
    if (x >= NAME_W) {
      ctx.save();
      ctx.beginPath(); ctx.rect(NAME_W, HEAD_H, W - NAME_W, H - HEAD_H); ctx.clip();
      ctx.strokeStyle = C("--cursor");
      ctx.beginPath(); ctx.moveTo(x, HEAD_H); ctx.lineTo(x, H); ctx.stroke();
      ctx.restore();
    }
  }
}

function drawName(sig, y, h) {
  const label = sig.p;
  ctx.save();
  ctx.beginPath(); ctx.rect(0, y, NAME_W - 8, h); ctx.clip();
  ctx.fillStyle = C("--fg");
  ctx.fillText(label, 8, y + h / 2);
  ctx.restore();
  // 光标时刻的值贴在名字右侧，省掉一个浮层
  if (cursorT != null) {
    const v = fmtVal(sig, valAt(sig, cursorT));
    ctx.fillStyle = C("--accent");
    ctx.textAlign = "right";
    ctx.fillText(v, NAME_W - 12, y + h / 2);
    ctx.textAlign = "left";
  }
}

function drawScalar(sig, st, lo, hi, y, h) {
  const top = y + 3, bot = y + h - 4;
  ctx.lineWidth = 1.6;
  let px = null, pv = null;
  // 视口左界到首个可见跳变之间也要有线，否则左边会空一截
  if (lo > 0) { px = NAME_W; pv = st.v[lo]; }
  for (let k = lo; k <= hi; k++) {
    const x = xOf(st.t[k]), v = st.v[k];
    if (pv !== null) {
      ctx.strokeStyle = colorOf(pv);
      ctx.beginPath();
      const py = yOf(pv, top, bot);
      ctx.moveTo(px, py); ctx.lineTo(x, py);
      ctx.stroke();
      if (pv !== v) {
        ctx.beginPath();
        ctx.moveTo(x, yOf(pv, top, bot)); ctx.lineTo(x, yOf(v, top, bot));
        ctx.stroke();
      }
    }
    px = x; pv = v;
  }
  // 最后一段延伸出视口右界
  if (pv !== null) {
    ctx.strokeStyle = colorOf(pv);
    ctx.beginPath();
    const py = yOf(pv, top, bot);
    ctx.moveTo(px, py); ctx.lineTo(cv.clientWidth, py);
    ctx.stroke();
  }
}

function yOf(v, top, bot) {
  if (v === "0") return bot;
  if (v === "1") return top;
  return (top + bot) / 2;   // x / z 画在中线
}

function colorOf(v) {
  if (v === "0") return C("--v0");
  if (v === "1") return C("--v1");
  if (v === "x") return C("--vx");
  if (v === "z") return C("--vz");
  return C("--bus");
}

function drawBus(sig, st, lo, hi, y, h) {
  const mid = y + h / 2;
  ctx.lineWidth = 1.4;
  for (let k = lo; k <= hi; k++) {
    const x0 = xOf(st.t[k]);
    const x1 = (k + 1 <= hi) ? xOf(st.t[k + 1]) : cv.clientWidth;
    if (x1 < NAME_W) continue;
    const v = st.v[k];
    const bits = decode(sig, v);
    ctx.strokeStyle = /[xz]/.test(bits) ? colorOf(bits[0]) : C("--bus");
    // 总线画成"六边形"：两端斜切，中间一条横线，和标量线一眼区分得开
    const d = 4;
    ctx.beginPath();
    if (k > lo) { ctx.moveTo(x0, mid - d); ctx.lineTo(x0 + d, mid); ctx.lineTo(x0, mid + d); }
    else { ctx.moveTo(x0, mid); }
    ctx.moveTo(Math.min(x0, NAME_W), mid); ctx.lineTo(x1, mid);
    ctx.stroke();
    const label = fmtVal(sig, v);
    if (ctx.measureText(label).width + 8 < Math.min(x1, cv.clientWidth) - x0) {
      ctx.fillStyle = C("--fg");
      ctx.fillText(label, x0 + 6, mid - 7);
    }
  }
}

function fmtTime(u) {
  const v = u;
  if (Math.abs(v - Math.round(v)) < 1e-9) return Math.round(v) + DATA.unit;
  return v.toFixed(3).replace(/\.?0+$/, "") + DATA.unit;
}

function niceStep(span, targetCols) {
  const raw = span / Math.max(1, targetCols);
  const mag = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-9))));
  for (const m of [1, 2, 5, 10]) if (mag * m >= raw) return mag * m;
  return mag * 10;
}

// ---- 交互 ----
let dragging = false, dragX = 0, dragView0 = 0;

// 纵向可滚范围：没有上限的话能把整幅波形滚到标题栏上面去，
// 屏幕上只剩一片空白，而且没有任何东西提示"往上滚就回来了"。
function maxScroll() {
  let content = PAD_TOP;
  for (const r of rows) content += (r.kind === "group" ? ROW_H : r.h);
  const view = cv.clientHeight - PAD_TOP;
  return Math.max(0, content - view);
}
function clampScroll() {
  scrollY = Math.min(Math.max(0, scrollY), maxScroll());
}

cv.addEventListener("wheel", e => {
  e.preventDefault();
  if (e.shiftKey) { scrollY += e.deltaY; clampScroll(); draw(); return; }
  const scale = Math.exp(e.deltaY * 0.0015);
  const anchor = tOf(Math.max(NAME_W, e.offsetX));
  view0 = anchor - (anchor - view0) * scale;
  view1 = anchor + (view1 - anchor) * scale;
  clampView();
  draw();
}, { passive: false });

cv.addEventListener("mousedown", e => {
  dragging = true; dragX = e.offsetX; dragView0 = view0;
});
window.addEventListener("mouseup", () => { dragging = false; });
cv.addEventListener("mousemove", e => {
  if (dragging) {
    const dt = (dragX - e.offsetX) / (cv.clientWidth - NAME_W) * (view1 - view0);
    view0 = dragView0 + dt; view1 = view0 + (view1 - view0);
    clampView(); draw(); return;
  }
  if (e.offsetX < NAME_W) { cursorT = null; showCursor(); draw(); return; }
  cursorT = tOf(e.offsetX);
  showCursor();
  draw();
});
cv.addEventListener("mouseleave", () => { cursorT = null; showCursor(); draw(); });

cv.addEventListener("click", e => {
  if (e.offsetX >= NAME_W) return;
  let y = PAD_TOP - scrollY;
  for (const row of rows) {
    const h = row.kind === "group" ? ROW_H : row.h;
    if (e.offsetY >= y && e.offsetY < y + h) {
      if (row.kind === "group") { row.g.collapsed = !row.g.collapsed; relayout(); clampScroll(); draw(); }
      return;
    }
    y += h;
  }
});

function showCursor() {
  const el = document.getElementById("cursor");
  el.textContent = cursorT == null ? "—" : "t = " + fmtTime(t2u(cursorT));
}

function clampView() {
  const minSpan = 1e-6;
  if (view1 - view0 < minSpan) { const c = (view0 + view1) / 2; view0 = c - minSpan / 2; view1 = c + minSpan / 2; }
  const end = t2u(DATA.end || 1);
  if (view0 < -end * 0.05) { const s = view1 - view0; view0 = -end * 0.05; view1 = view0 + s; }
  if (view1 > end * 1.05) { const s = view1 - view0; view1 = end * 1.05; view0 = view1 - s; }
}

document.getElementById("zin").onclick = () => { zoomBy(0.6); };
document.getElementById("zout").onclick = () => { zoomBy(1 / 0.6); };
// "全览"同时复位横向与纵向 —— 只复位横向的话，纵向滚过头之后
// 按全览会觉得"按钮坏了"（波形还是一片空白）
document.getElementById("zfit").onclick = () => {
  view0 = 0; view1 = t2u(DATA.end || 1); scrollY = 0; draw();
};
document.getElementById("theme").onclick = () => {
  const cur = document.documentElement.className;
  const sysDark = matchMedia("(prefers-color-scheme: dark)").matches;
  const isDark = cur === "dark" || (cur === "" && sysDark);
  document.documentElement.className = isDark ? "light" : "dark";
  css = getComputedStyle(document.documentElement);
  draw();
};
function zoomBy(f) {
  const c = (view0 + view1) / 2, s = (view1 - view0) * f;
  view0 = c - s / 2; view1 = c + s / 2; clampView(); draw();
}

// ---- 初始化 ----
document.getElementById("title").textContent = DATA.title;
document.getElementById("meta").textContent =
  `${DATA.signals.length} 个信号 · ${DATA.nchg} 次跳变 · 时间单位 ${DATA.unit}`
  + ` · 0 ~ ${fmtTime(t2u(DATA.end))}`;
const msgs = DATA.notes.slice();
if (DATA.nchg > 300000) {
  msgs.push(`本次波形有 ${DATA.nchg} 次跳变，页面偏大。`
    + `想更流畅可以缩短仿真时间，或把 $dumpvars 缩到只看关心的层次`);
}
if (msgs.length) {
  const n = document.getElementById("notes");
  n.className = "note";
  n.textContent = "提示：" + msgs.join("；");
}
view1 = t2u(DATA.end || 1);
buildRows();
resize();
window.addEventListener("resize", resize);
</script>
</body>
</html>
"""
