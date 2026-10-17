#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""城市交通路径导航系统（数据结构实践 · 选题 A）—— 统一入口。

用法：
    python main.py                        # 打开图形界面
    python main.py --check                # 无界面自检（算法正确性 + 异常处理），退出码 0/1
    python main.py --cli 大学城 机场        # 命令行对比三种算法（支持站点 id 或中文名）
    python main.py --cli S4 S11 --all-pairs  # 附带全部点对的耗时/扩展数分布
    python main.py --smoke out.ps         # 界面冒烟：建窗→求解→导出画布→销毁

数据：data/demo_city.json 或 data/demo_city.txt（两种格式等价，可用 --data 指定）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import solver                      # noqa: E402
from core.graph import DataError, Graph, load_graph  # noqa: E402

DEFAULT_DATA = ROOT / "data" / "demo_city.json"


def resolve(g: Graph, token: str) -> str:
    if token in g.stations:
        return token
    for st in g.stations.values():
        if st.name == token:
            return st.sid
    raise DataError(f"找不到站点：{token}（可用 id 或站点全名）")


def cli(g: Graph, start_token: str, goal_token: str, all_pairs: bool = False) -> int:
    start, goal = resolve(g, start_token), resolve(g, goal_token)
    name = {sid: st.name for sid, st in g.stations.items()}
    print(f"数据：{g.meta.get('name', '(未命名)')} ｜ {len(g.stations)} 站 / "
          f"{len(g.edges)} 边 / {len(g.lines)} 条线")
    print(f"查询：{name[start]}（{start}）→ {name[goal]}（{goal}）\n")
    header = f"{'算法':<18}{'距离 km':>9}{'时间 min':>10}{'换乘':>6}{'扩展':>6}{'耗时 ms':>9}"
    print(header)
    print("-" * len(header))
    jobs = [("Dijkstra(最快)", "dijkstra", "time"),
            ("A*(最快)", "astar", "time"),
            ("Dijkstra(最短)", "dijkstra", "distance"),
            ("A*(最短)", "astar", "distance"),
            ("BFS(最少换乘)", "bfs", "time")]
    for label, algo, weight in jobs:
        try:
            r = solver.solve(g, start, goal, algo, weight)
        except solver.PathNotFound:
            print(f"{label:<18}{'不可达':>9}")
            continue
        t = "—" if r.transfers is None else str(r.transfers)
        print(f"{label:<18}{r.distance_km:>9.2f}{r.time_min:>10.2f}{t:>6}{r.expanded:>6}{r.elapsed_ms:>9.2f}")
        print(f"   路径：{' → '.join(name[s] for s in r.path)}")
    print()
    fastest = solver.dijkstra(g, start, goal, "time")
    shortest = solver.dijkstra(g, start, goal, "distance")
    if fastest.path != shortest.path:
        print(f"※ 多准则：最快 {fastest.time_min:.2f} min（{fastest.distance_km:.2f} km）与 "
              f"最短 {shortest.distance_km:.2f} km（{shortest.time_min:.2f} min）不是同一条路径。")
    else:
        print("※ 本对起终点上，最快与最短是同一条路径。")

    if all_pairs:
        print("\n全部点对（时间权重）：")
        print(f"{'起点':<6}{'终点':<6}{'Dijkstra扩展':>14}{'A*扩展':>8}{'A*省下':>8}{'最小换乘':>10}")
        total_d = total_a = 0
        pairs = [(a, b) for a in sorted(g.stations) for b in sorted(g.stations) if a != b]
        for a, b in pairs:
            d = solver.dijkstra(g, a, b, "time")
            s = solver.astar(g, a, b, "time")
            bfs = solver.bfs_transfers(g, a, b)
            total_d += d.expanded
            total_a += s.expanded
            print(f"{a:<6}{b:<6}{d.expanded:>14}{s.expanded:>8}{d.expanded - s.expanded:>8}{bfs.transfers:>10}")
        n = max(len(pairs), 1)
        print(f"\n平均扩展节点：Dijkstra {total_d / n:.2f} ｜ A* {total_a / n:.2f} ｜ "
              f"平均省下 {(total_d - total_a) / n:.2f}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="城市交通路径导航系统（A 题）")
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--check", action="store_true", help="运行无界面自检")
    ap.add_argument("--cli", nargs=2, metavar=("起点", "终点"), help="命令行对比三种算法")
    ap.add_argument("--all-pairs", action="store_true", help="附全部点对统计")
    ap.add_argument("--smoke", metavar="OUT.ps", help="界面冒烟并导出画布 PostScript")
    ap.add_argument("--anim-ms", type=int, default=90, help="动画每帧间隔（毫秒）")
    args = ap.parse_args(argv)

    if args.check:
        import selfcheck
        return selfcheck.main(["--json", args.data, "--txt", str(ROOT / "data" / "demo_city.txt")])

    try:
        g = load_graph(args.data)
    except DataError as exc:
        print(f"[数据错误] {exc}", file=sys.stderr)
        return 2

    if args.cli:
        try:
            return cli(g, args.cli[0], args.cli[1], args.all_pairs)
        except DataError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 2

    if args.smoke:
        from ui.window import smoke
        out = Path(args.smoke)
        out.parent.mkdir(parents=True, exist_ok=True)
        rc = smoke(g, str(out))
        print(f"smoke ok: {out} ({out.stat().st_size if out.exists() else 0} bytes)")
        return rc

    from ui.window import run_gui
    run_gui(g)
    return 0


if __name__ == "__main__":
    sys.exit(main())