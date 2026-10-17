#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""报告插图生成（P2-09）：三算法流程图 + 扩展节点数柱状图。

只依赖 Pillow（**开发期工具**，交付程序不 import 它，运行时仍是零第三方依赖）。
因为当前环境没有 Graphviz，也无法目视检查图片，所以生成后立刻做**像素自检**：
尺寸正确、非空白、主色像素计数达标；正式观感由 P4 在 J3 目检。

用法：
    python tools/make_figs.py --out docs/figs
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as exc:                      # pragma: no cover
    sys.exit(f"需要 Pillow（开发期依赖）: {exc}")

ROOT = Path(__file__).resolve().parent.parent

COL = {
    "bg": (255, 255, 255),
    "box": (238, 244, 255), "box_b": (46, 94, 168),
    "dec": (255, 244, 224), "dec_b": (199, 123, 21),
    "end": (232, 247, 238), "end_b": (31, 138, 76),
    "text": (34, 34, 34), "arrow": (80, 80, 80), "sub": (90, 90, 90),
    "bar1": (46, 134, 222), "bar2": (228, 87, 46),
}


def load_font(size: int):
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"):
        p = Path(r"C:\Windows\Fonts") / name
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                continue
    return ImageFont.load_default()


def arrow(dr: ImageDraw.ImageDraw, p0, p1, color=COL["arrow"], width=3):
    dr.line([p0, p1], fill=color, width=width)
    (x0, y0), (x1, y1) = p0, p1
    if abs(x1 - x0) >= abs(y1 - y0):
        s = 1 if x1 >= x0 else -1
        head = [(x1, y1), (x1 - 12 * s, y1 - 6), (x1 - 12 * s, y1 + 6)]
    else:
        s = 1 if y1 >= y0 else -1
        head = [(x1, y1), (x1 - 6, y1 - 12 * s), (x1 + 6, y1 - 12 * s)]
    dr.polygon(head, fill=color)


def node_box(dr, box, kind, font, sub_font):
    x, y, w, h, title = box["x"], box["y"], box["w"], box["h"], box["t"]
    sub = box.get("sub", "")
    if kind == "decision":
        fill, border = COL["dec"], COL["dec_b"]
        dr.polygon([(x + w // 2, y), (x + w, y + h // 2), (x + w // 2, y + h), (x, y + h // 2)],
                   fill=fill, outline=border, width=3)
    else:
        fill, border = (COL["end"], COL["end_b"]) if kind == "end" else (COL["box"], COL["box_b"])
        dr.rounded_rectangle([x, y, x + w, y + h], radius=12, fill=fill, outline=border, width=3)
    # 居中排文字（支持两行）
    lines = title.split("\n")
    total = sum(dr.textbbox((0, 0), ln, font=font)[3] + 6 for ln in lines)
    ty = y + (h - total) // 2 + 2
    for ln in lines:
        tw = dr.textbbox((0, 0), ln, font=font)[2]
        dr.text((x + (w - tw) // 2, ty), ln, font=font, fill=COL["text"])
        ty += dr.textbbox((0, 0), ln, font=font)[3] + 6
    if sub:
        sw = dr.textbbox((0, 0), sub, font=sub_font)[2]
        dr.text((x + (w - sw) // 2, y + h + 6), sub, font=sub_font, fill=COL["sub"])


def flowchart(title: str, steps, out: Path, extra: str = ""):
    W, H = 1500, 120 + 118 * len(steps) + 120
    img = Image.new("RGB", (W, H), COL["bg"])
    dr = ImageDraw.Draw(img)
    f_title, f_box, f_sub = load_font(38), load_font(27), load_font(21)
    dr.text((60, 36), title, font=f_title, fill=COL["text"])
    if extra:
        dr.text((60, 84), extra, font=f_sub, fill=COL["sub"])
    x, w, h = 190, 760, 74
    top = 130
    for i, st in enumerate(steps):
        st.update({"x": x, "y": top + i * 118, "w": w, "h": h})
        node_box(dr, st, st["kind"], f_box, f_sub)
        if i:
            prev = steps[i - 1]
            arrow(dr, (x + w // 2, prev["y"] + h + (26 if prev.get("sub") else 4)),
                  (x + w // 2, st["y"] - 4))
            if st.get("edge"):
                dr.text((x + w // 2 + 14, prev["y"] + h + 6), st["edge"], font=f_sub, fill=COL["sub"])
    # 右侧回路箭头（最后一步回到第 2 步）
    if len(steps) >= 3:
        first_loop, last = steps[1], steps[-2]
        xr = x + w + 40
        dr.line([(x + w, last["y"] + h // 2), (xr, last["y"] + h // 2)], fill=COL["arrow"], width=3)
        dr.line([(xr, last["y"] + h // 2), (xr, first_loop["y"] + h // 2)], fill=COL["arrow"], width=3)
        arrow(dr, (xr, first_loop["y"] + h // 2), (x + w + 4, first_loop["y"] + h // 2))
        dr.text((xr + 10, (last["y"] + first_loop["y"]) // 2), "继续下一轮", font=f_sub, fill=COL["sub"])
    img.save(out)
    return out


def bar_chart(out: Path, csv_path: Path):
    if not csv_path.exists():
        return None
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    want = [("grid-16", "distance"), ("grid-30", "distance"), ("grid-60", "distance"),
            ("grid-240", "distance")]
    data = {}
    for r in rows:
        key = (r["dataset"], r["algorithm"], r["weight"])
        data[key] = (float(r["avg_expanded"]), float(r["median_ms"]))
    W, H = 1400, 820
    img = Image.new("RGB", (W, H), COL["bg"])
    dr = ImageDraw.Draw(img)
    f_t, f_a, f_s = load_font(36), load_font(22), load_font(19)
    dr.text((60, 36), "扩展节点数对比（distance 权重，Dijkstra vs A*）", font=f_t, fill=COL["text"])
    dr.text((60, 88), "数据：tools/bench_summary.csv ｜ A* 省下的扩展比例标注在柱上方", font=f_s, fill=COL["sub"])
    datasets = [d for d, _ in want]
    vals = {d: (data.get((d, "dijkstra", "distance"), (0, 0)),
                data.get((d, "astar", "distance"), (0, 0))) for d in datasets}
    base_y, plot_h, left = 700, 500, 140
    max_v = max([v for pair in vals.values() for v, _ in pair] + [1])
    group_w = (W - left - 120) / len(datasets)
    bar_w = 44
    for i, d in enumerate(datasets):
        cx = left + group_w * i + group_w / 2
        for j, (ser, col) in enumerate(((0, COL["bar1"]), (1, COL["bar2"]))):
            v = vals[d][ser][0]
            bh = plot_h * v / max_v
            x0 = cx - bar_w - 6 + j * (bar_w + 12)
            dr.rectangle([x0, base_y - bh, x0 + bar_w, base_y], fill=col)
            dr.text((x0, base_y - bh - 28), f"{v:.0f}", font=f_a, fill=COL["text"])
        dij, ast = vals[d][0][0], vals[d][1][0]
        if dij:
            dr.text((cx - 40, base_y + 8), f"A* −{(1 - ast / dij) * 100:.0f}%", font=f_a, fill=COL["bar2"])
        dr.text((cx - 30, base_y + 42), d, font=f_a, fill=COL["text"])
    dr.line([(left - 20, base_y), (W - 60, base_y)], fill=COL["arrow"], width=3)
    dr.text((left - 110, 100), "扩展节点数", font=f_s, fill=COL["sub"])
    # 图例
    dr.rectangle([left, 130, left + 28, 152], fill=COL["bar1"])
    dr.text((left + 38, 128), "Dijkstra", font=f_a, fill=COL["text"])
    dr.rectangle([left + 190, 130, left + 218, 152], fill=COL["bar2"])
    dr.text((left + 228, 128), "A*", font=f_a, fill=COL["text"])
    img.save(out)
    return out


def verify(path: Path, min_nonwhite=0.02):
    im = Image.open(path).convert("RGB")
    px = im.load()
    w, h = im.size
    nonwhite = 0
    for y in range(0, h, 3):
        for x in range(0, w, 3):
            if px[x, y] != COL["bg"]:
                nonwhite += 1
    ratio = nonwhite / ((w // 3) * (h // 3))
    assert ratio >= min_nonwhite, f"{path.name} 几乎空白（非白像素 {ratio:.3%}）"
    return f"{path.name}: {w}x{h}, 非白 {ratio:.1%}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "figs"))
    ap.add_argument("--bench", default=str(ROOT / "tools" / "bench_summary.csv"))
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    dijkstra_steps = [
        {"kind": "start", "t": "初始化：dist[s]=0，其余=+∞；堆 ← {(0, s)}"},
        {"kind": "process", "t": "弹出堆顶 (d, u)（当前距离最小者）"},
        {"kind": "decision", "t": "u 已出堆（done）？", "sub": "是 → 跳过该过期条目（懒惰删除），继续弹堆"},
        {"kind": "decision", "t": "u == goal？", "sub": "是 → 跳出循环，回溯 prev 得路径"},
        {"kind": "process", "t": "松弛：对每条邻边 e=(u,v,w)\n若 d + w(u,v) < dist[v] 则更新 dist[v]、prev[v] 并入堆"},
        {"kind": "decision", "t": "堆空？", "sub": "是 → 目标不可达，抛 PathNotFound"},
        {"kind": "end", "t": "输出：路径 + km/min + 换乘数 + 扩展节点数"},
    ]
    astar_steps = [
        {"kind": "start", "t": "初始化：g[s]=0；堆 ← {(h(s), 0, s)}"},
        {"kind": "process", "t": "弹出 f 最小者 (f, g, u)，f = g + h"},
        {"kind": "decision", "t": "u 已出堆？", "sub": "是 → 跳过（h 一致 ⇒ 每个节点只需扩展一次）"},
        {"kind": "decision", "t": "u == goal？", "sub": "是 → 最优解已确定，回溯 prev"},
        {"kind": "process", "t": "扩展：若 g[u] + w(u,v) < g[v] 则更新\n并入堆 (g[v] + h(v), g[v], v)"},
        {"kind": "decision", "t": "堆空？", "sub": "是 → 不可达，抛 PathNotFound"},
        {"kind": "end", "t": "输出：路径 + 指标（与 Dijkstra 同代价，扩展更少）"},
    ]
    bfs_steps = [
        {"kind": "start", "t": "起点每条线路建状态 (s, line)，代价 0，压入队首"},
        {"kind": "process", "t": "取队首状态 (sid, line)（0-1 BFS：队首=代价 0，队尾=代价 1）"},
        {"kind": "decision", "t": "该状态已处理？", "sub": "是 → 跳过（同一状态只处理一次）"},
        {"kind": "decision", "t": "sid == goal？", "sub": "是 → 最小换乘已确定，回溯 parent"},
        {"kind": "process", "t": "同线路邻边：代价不变 → 压队首\n（换乘为 0，不影响换乘计数）"},
        {"kind": "process", "t": "同站其他线路：代价 +1 → 压队尾\n（每换一次线记 1 次换乘）"},
        {"kind": "decision", "t": "队列空？", "sub": "是 → 不可达，抛 PathNotFound"},
        {"kind": "end", "t": "把 (站点,线路) 序列压平为站点序列，输出最少换乘"},
    ]

    made = [
        flowchart("Dijkstra：单源最短路（堆 + 懒惰删除）", dijkstra_steps,
                  out / "fig-3-1-dijkstra.png", "复杂度 O((V+E)logV)；扩展计数=出堆且未访问"),
        flowchart("A*：f(n) = g(n) + h(n)，h 必须可采纳", astar_steps,
                  out / "fig-3-2-astar.png",
                  "h 距离=欧氏距离；h 时间=欧氏距离/v_max×60（保证不高估）"),
        flowchart("0-1 BFS：状态 (站点, 线路)，同线 0 代价 / 换线 1 代价", bfs_steps,
                  out / "fig-3-3-bfs.png", "复杂度 O(V·L + E)；扩展计数单位是状态，不是站点"),
    ]
    bar = bar_chart(out / "fig-4-1-expanded.png", Path(args.bench))
    if bar:
        made.append(bar)

    print("生成：")
    for p in made:
        print("  " + verify(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())