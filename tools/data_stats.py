#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据画像（P1-07）：把图规模与结构特征算成报告能直接引用的数字。

输出 5 项口径：
  1. 站点数 / 2. 线路数 / 3. 换乘站数（归属 ≥2 条线的站）
  4. 度分布（每个站连几条边） / 5. 平均度 · 最大度 · 最小度

用法：
    python tools/data_stats.py                          # 默认统计 demo_city + real_metro
    python tools/data_stats.py --data data/demo_city.json data/edge_cases.json
    python tools/data_stats.py --out tools/data_stats.csv

约定：报告里出现的这些数字**必须来自本脚本输出**，不允许手写（质量门第 4 条）。
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.graph import DataError, Graph, load_graph  # noqa: E402


def collect(g: Graph, name: str) -> tuple[dict, Counter]:
    """返回（汇总行, 度分布计数器）。"""
    lines_of = g.lines_of_station()
    degrees = {sid: len(g.neighbors(sid)) for sid in g.stations}
    values = list(degrees.values())
    row = {
        "dataset": name,
        "stations": len(g.stations),
        "lines": len(g.lines),
        "edges": len(g.edges),
        "transfer_stations": sum(1 for v in lines_of.values() if len(v) > 1),
        "isolated_stations": sum(1 for d in values if d == 0),
        "avg_degree": round(sum(values) / len(values), 3) if values else 0.0,
        "max_degree": max(values, default=0),
        "min_degree": min(values, default=0),
    }
    return row, Counter(values)


def print_detail(row: dict, dist: Counter) -> None:
    print(f"\n[{row['dataset']}]")
    print(f"  站点 {row['stations']} ｜ 线路 {row['lines']} ｜ 边 {row['edges']}")
    print(f"  换乘站 {row['transfer_stations']} ｜ 孤立站 {row['isolated_stations']}")
    print(f"  平均度 {row['avg_degree']} ｜ 最大度 {row['max_degree']} ｜ 最小度 {row['min_degree']}")
    print("  度分布：")
    for deg in sorted(dist):
        print(f"    度={deg}: {dist[deg]} 站")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="图数据画像（站点/线路/换乘/度分布）")
    ap.add_argument("--data", nargs="+", default=[
        str(ROOT / "data" / "demo_city.json"),
        str(ROOT / "data" / "real_metro.json"),
    ])
    ap.add_argument("--out", default=str(ROOT / "tools" / "data_stats.csv"))
    args = ap.parse_args(argv)

    rows, details = [], []
    for path in args.data:
        p = Path(path)
        if not p.exists():
            print(f"[跳过] 不存在: {p}", file=sys.stderr)
            continue
        try:
            g = load_graph(p)
        except DataError as exc:
            print(f"[跳过] 加载失败 {p}: {exc}", file=sys.stderr)
            continue
        row, dist = collect(g, p.stem)
        rows.append(row)
        details.append((row, dist))

    if not rows:
        print("没有任何数据集被成功统计", file=sys.stderr)
        return 1

    for row, dist in details:
        print_detail(row, dist)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        # csv 模块默认 lineterminator="\r\n"；显式 LF，保证与入库 CSV 同哈希
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV 已写出：{out}（{len(rows)} 个数据集）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
