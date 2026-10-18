#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""基准与对比实验（P2-07）：一条命令产出可进报告表的 CSV。

用法：
    python tools/bench.py --out tools/bench.csv --repeats 5 --pairs 5
    python tools/bench.py --scale 15,60,240 --out tools/bench_scale.csv      # 规模曲线
    python tools/bench.py --check-cv 15                                      # 噪声闸门（超限退出码 2）

设计要点：
  * 数据集 = 真实的 data/demo_city.json + **内置合成网格图**（不写 data/，尊重 P1 的文件所有权）；
  * 合成图保持几何自洽：time_min 由加载器按 distance/speed 计算 ⇒ 内置 h 仍然可采纳；
  * 计时用算法内部的 `Result.elapsed_ms`（perf_counter），**先热身 1 次**再取 repeats 次中位数；
  * 同时输出原始 CSV 与汇总 CSV；`--check-cv` 用变异系数（stdev/mean）把"噪声太大"变成红灯。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import solver              # noqa: E402
from core.graph import load_graph    # noqa: E402

FIELDS = ["dataset", "nodes", "edges", "lines", "start", "goal", "algorithm", "weight",
          "rounds", "expanded", "elapsed_ms", "distance_km", "time_min", "transfers", "path_nodes"]


# ---------------------------------------------------------------- 合成数据集
def grid_spec(rows: int, cols: int, seed: int = 0) -> dict:
    """rows×cols 网格：横线 R（40 km/h）、竖线 C（40 km/h）、少量快线 X（160 km/h）。"""
    stations = [{"id": f"G{r}_{c}", "name": f"{r}-{c}", "x": float(c), "y": float(r)}
                for r in range(rows) for c in range(cols)]
    edges = []
    for r in range(rows):
        for c in range(cols - 1):
            edges.append({"u": f"G{r}_{c}", "v": f"G{r}_{c + 1}", "line": "R"})
    for c in range(cols):
        for r in range(rows - 1):
            edges.append({"u": f"G{r}_{c}", "v": f"G{r + 1}_{c}", "line": "C"})
    seen = {(min(e["u"], e["v"]), max(e["u"], e["v"]), e["line"]) for e in edges}
    rng = random.Random(seed)
    for _ in range(max(1, rows * cols // 12)):        # 快线跳连（机场快线那种）
        r, c = rng.randrange(rows), rng.randrange(cols - 2)
        u, v = f"G{r}_{c}", f"G{r}_{c + 2}"
        key = (min(u, v), max(u, v), "X")
        if key not in seen:
            seen.add(key)
            edges.append({"u": u, "v": v, "line": "X"})
    return {
        "meta": {"name": f"grid-{rows}x{cols}"},
        "lines": [{"id": "R", "name": "横线", "speed_kmh": 40},
                  {"id": "C", "name": "竖线", "speed_kmh": 40},
                  {"id": "X", "name": "快线", "speed_kmh": 160}],
        "stations": stations,
        "edges": edges,
    }


def load_spec(spec: dict, cache_dir: Path):
    path = cache_dir / f"{spec['meta']['name']}.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return load_graph(path)


def pick_pairs(g, k: int):
    """确定性地取 k 对起终点（首尾、对角、中心等），保证可复现。"""
    ids = sorted(g.stations)
    n = len(ids)
    if n < 2:
        return []
    cand = [
        (ids[0], ids[-1]),
        (ids[0], ids[n // 2]),
        (ids[n // 2], ids[-1]),
        (ids[1], ids[-2]) if n > 3 else (ids[0], ids[-1]),
        (ids[n // 4], ids[3 * n // 4]) if n > 4 else (ids[0], ids[-1]),
    ]
    out = []
    for p in cand:
        if p[0] != p[1] and p not in out:
            out.append(p)
    return out[:k]


# ---------------------------------------------------------------- 计时
def time_call(fn, repeats: int, target_ms: float = 50.0):
    """批量计时：热身 1 次 → 估每次耗时 → 每轮跑 batch 次外部计时取均值 → repeats 轮取中位数。

    为什么不用单次 `Result.elapsed_ms` 求统计：微秒级单次测量受调度抖动影响，
    CV 会到 50%–150%，完全不能进报告。批量后每轮 ≥≈50 ms，CV 通常 <15%。
    """
    first = fn()                                  # 热身（不进统计）
    est = max(first.elapsed_ms, 1e-4)             # 毫秒
    batch = int(min(5000, max(1, math.ceil(target_ms / est))))
    samples = []
    last = first
    for _ in range(repeats):
        t0 = time.perf_counter()
        for _ in range(batch):
            last = fn()
        samples.append((time.perf_counter() - t0) * 1000.0 / batch)
    med = statistics.median(samples)
    cv = (statistics.pstdev(samples) / med * 100.0) if med > 0 and len(samples) > 1 else 0.0
    return med, cv, last, batch


def measure(g, pairs, repeats: int, dataset: str, cache: list):
    jobs = [("dijkstra", "time"), ("dijkstra", "distance"),
            ("astar", "time"), ("astar", "distance"),
            ("bfs", "—")]
    for start, goal in pairs:
        for algo, weight in jobs:
            def run(algo=algo, weight=weight, s=start, t=goal):
                if algo == "bfs":
                    return solver.bfs_transfers(g, s, t)
                return solver.solve(g, s, t, algo, weight)
            med, cv, last, batch = time_call(run, repeats)
            cache.append({
                "dataset": dataset, "nodes": len(g.stations), "edges": len(g.edges),
                "lines": len(g.lines), "start": start, "goal": goal,
                "algorithm": algo, "weight": weight, "rounds": repeats,
                "expanded": last.expanded, "elapsed_ms": round(med, 4),
                "distance_km": last.distance_km, "time_min": last.time_min,
                "transfers": "—" if last.transfers is None else last.transfers,
                "path_nodes": len(last.path), "_median": med, "_cv": cv, "_batch": batch,
            })

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A 题基准：Dijkstra / A* / BFS")
    ap.add_argument("--out", default=str(ROOT / "_smoke" / "bench.csv"),
                    help="原始 CSV 输出；默认 _smoke/bench.csv（入库版请显式写 tools/）")
    ap.add_argument("--summary-out", default="")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--pairs", type=int, default=5)
    ap.add_argument("--scale", default="",
                    help="规模模式：逗号分隔的站点数（只用内置合成图，忽略 --data），如 15,60,240")
    ap.add_argument("--check-cv", type=float, default=0.0, help="变异系数上限（%），超限退出码 2")
    ap.add_argument("--data", default=str(ROOT / "data" / "demo_city.json"))
    args = ap.parse_args(argv)

    rows: list = []
    cache_dir = Path(tempfile.mkdtemp(prefix="bench_"))
    print(f"[bench] repeats={args.repeats} pairs={args.pairs} scale={args.scale or '—'}")

    if args.scale:
        for n in [int(x) for x in args.scale.split(",") if x.strip()]:
            cols = max(3, math.ceil(n / max(3, math.ceil(math.sqrt(n)))))
            rows_n = max(3, math.ceil(n / cols))
            spec = grid_spec(rows_n, cols, seed=rows_n * cols)
            g = load_spec(spec, cache_dir)
            dataset = f"grid-{len(g.stations)}"
            pairs = pick_pairs(g, args.pairs)
            measure(g, pairs, args.repeats, dataset, rows)
            print(f"  {dataset}: {len(g.stations)} 站 / {len(g.edges)} 边 / {len(g.lines)} 线，{len(pairs)} 对起终点")
    else:
        g = load_graph(args.data)
        measure(g, pick_pairs(g, args.pairs), args.repeats, "demo_city", rows)
        for size in (15, 60, 240):
            spec = grid_spec(max(3, math.ceil(size / 10)), 10, seed=size)
            g2 = load_spec(spec, cache_dir)
            measure(g2, pick_pairs(g2, args.pairs), args.repeats, f"grid-{len(g2.stations)}", rows)

    if not rows:
        print("[bench] 没有样本", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)   # 默认写到 _smoke/，首次运行自动建目录
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.DictWriter(fh, fieldnames=FIELDS + ["_median", "_cv"], extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow({k: r.get(k, "") for k in FIELDS + ["_median", "_cv"]})
    print(f"[bench] 原始数据 -> {out}  ({len(rows)} 行)")

    # 汇总：按 (dataset, algorithm, weight) 聚合
    groups: dict = {}
    for r in rows:
        key = (r["dataset"], r["algorithm"], r["weight"])
        groups.setdefault(key, []).append(r)
    summary_path = Path(args.summary_out) if args.summary_out else out.with_name(out.stem + "_summary.csv")
    cv_list = []
    worst_cv, worst_label = 0.0, ""
    with summary_path.open("w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.writer(fh)
        wr.writerow(["dataset", "nodes", "edges", "algorithm", "weight",
                     "median_ms", "cv_pct", "avg_expanded", "avg_distance_km", "avg_transfers"])
        print(f"\n{'数据集':<14}{'算法':<9}{'权重':<9}{'中位耗时ms':>12}{'CV%':>8}{'平均扩展':>9}")
        for (dataset, algo, weight), rs in sorted(groups.items()):
            med = rs[0]["_median"]
            cv = rs[0]["_cv"]
            cv_list.append(cv)
            if cv > worst_cv:
                worst_cv, worst_label = cv, f"{dataset}/{algo}/{weight}"
            avg_exp = sum(r["expanded"] for r in rs) / len(rs)
            avg_km = sum(float(r["distance_km"]) for r in rs) / len(rs)
            tr = [int(r["transfers"]) for r in rs if r["transfers"] != "—"]
            avg_tr = sum(tr) / len(tr) if tr else ""
            wr.writerow([dataset, rs[0]["nodes"], rs[0]["edges"], algo, weight,
                         round(med, 4), round(cv, 2), round(avg_exp, 2), round(avg_km, 3),
                         "" if avg_tr == "" else round(avg_tr, 2)])
            print(f"{dataset:<14}{algo:<9}{weight:<9}{med:>12.4f}{cv:>8.2f}{avg_exp:>9.1f}")
    print(f"\n[bench] 汇总 -> {summary_path}")

    median_cv = statistics.median(cv_list) if cv_list else 0.0
    print(f"\n[bench] 计时抖动：CV 中位 {median_cv:.2f}% ｜ 最大 {worst_cv:.2f}%"
          f"（{worst_label or '—'}）")
    if args.check_cv:
        # 闸门用**中位 CV**（鲁棒）：共享桌面上亚毫秒组的最大 CV 会被调度抖动放大，
        # 用最大值当闸门会让 CI 必然红灯；最大值仍打印出来供人工判断。
        if median_cv > args.check_cv:
            print(f"[bench] 噪声超限：CV 中位 {median_cv:.2f}% > {args.check_cv}%", file=sys.stderr)
            return 2
        print(f"[bench] 噪声闸门通过：CV 中位 {median_cv:.2f}% ≤ {args.check_cv}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())