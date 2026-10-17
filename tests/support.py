#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P2 单测的图构造器与**独立 oracle**。

设计约定：
  * 所有图都通过 `load_graph()` 走真实加载路径（不直接拼 Graph 对象），顺便验证契约；
  * 图数据保持"几何自洽"：time_min = distance_km / speed_kmh * 60，且 speed <= 全网最大速度,
    这样内置的欧几里得启发式才**可采纳**（否则 T14/T15 的结论不成立）；
  * `brute_min_transfers` 故意用"枚举所有简单路径"的笨办法，与被测的 0-1 BFS 相互独立——
    两套实现一致才算数（不要用被测代码验证被测代码）。
"""
from __future__ import annotations

import json
import math
import random
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.graph import Graph, load_graph  # noqa: E402


def load_spec(spec: dict) -> Graph:
    """把内存里的 JSON spec 落到临时文件再走 load_graph（真实加载路径）。"""
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with open(fd, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False)
        return load_graph(path)
    finally:
        Path(path).unlink(missing_ok=True)


# ------------------------------------------------------------------ 固定小图
def tiny_hand() -> Graph:
    """3 站手算图：A(0,0) B(3,4) C(6,0)，60 km/h。

    A-B = 5 km、B-C = 5 km、A-C = 6 km ⇒ A→C 最短 6 km / 6 min；绕行 A-B-C 为 10 km / 10 min。
    """
    return load_spec({
        "meta": {"name": "tiny_hand"},
        "lines": [{"id": "T", "name": "测试线", "speed_kmh": 60}],
        "stations": [
            {"id": "A", "name": "甲", "x": 0, "y": 0},
            {"id": "B", "name": "乙", "x": 3, "y": 4},
            {"id": "C", "name": "丙", "x": 6, "y": 0},
        ],
        "edges": [
            {"u": "A", "v": "B", "line": "T"},
            {"u": "B", "v": "C", "line": "T"},
            {"u": "A", "v": "C", "line": "T"},
        ],
    })


def dual_weight() -> Graph:
    """双权重视角：慢线更短、快线更远。

    慢线 SLOW（10 km/h）：S-M-T，各 3 km ⇒ 6 km / 36 min（最短）
    快线 FAST（120 km/h）：S-F-T，各 5 km ⇒ 10 km / 5 min（最快）
    """
    return load_spec({
        "meta": {"name": "dual_weight"},
        "lines": [
            {"id": "SLOW", "name": "慢线", "speed_kmh": 10},
            {"id": "FAST", "name": "快线", "speed_kmh": 120},
        ],
        "stations": [
            {"id": "S", "name": "起点", "x": 0, "y": 0},
            {"id": "M", "name": "中间", "x": 3, "y": 0},
            {"id": "T", "name": "终点", "x": 6, "y": 0},
            {"id": "F", "name": "绕行", "x": 3, "y": 4},
        ],
        "edges": [
            {"u": "S", "v": "M", "line": "SLOW"},
            {"u": "M", "v": "T", "line": "SLOW"},
            {"u": "S", "v": "F", "line": "FAST"},
            {"u": "F", "v": "T", "line": "FAST"},
        ],
    })


def parallel_lines() -> Graph:
    """平行线路：A-B 同时有 L1/L2，B-C 只有 L1。

    analyze_path 应在 A-B 段选 **L1**（多 0.96 min 但换乘 0 次），而不是更快的 L2。
    """
    return load_spec({
        "meta": {"name": "parallel_lines"},
        "lines": [
            {"id": "L1", "name": "普通线", "speed_kmh": 35},
            {"id": "L2", "name": "快速线", "speed_kmh": 80},
        ],
        "stations": [
            {"id": "A", "name": "甲", "x": 0, "y": 0},
            {"id": "B", "name": "乙", "x": 1, "y": 0},
            {"id": "C", "name": "丙", "x": 2, "y": 0},
        ],
        "edges": [
            {"u": "A", "v": "B", "line": "L1"},
            {"u": "B", "v": "C", "line": "L1"},
            {"u": "A", "v": "B", "line": "L2"},
        ],
    })


def h_trap() -> Graph:
    """非可采纳启发式的陷阱图（几何自洽，默认 h 仍可采纳）。

    最优：S→P→T，50 km + 50 km，快线 600 km/h ⇒ 10 min。
    次优：S→Q→T，99.005 km 快线 + 1 km 慢线 ⇒ 10.50 min。
    Q 离 T 近 ⇒ 曼哈顿类 h 会高估 P（150），于是先弹出 Q、过早得到 10.50 的次优解。
    """
    return load_spec({
        "meta": {"name": "h_trap"},
        "lines": [
            {"id": "FAST", "name": "快线", "speed_kmh": 600},
            {"id": "SLOW", "name": "慢线", "speed_kmh": 100},
        ],
        "stations": [
            {"id": "S", "name": "起点", "x": 0, "y": 0},
            {"id": "P", "name": "正路", "x": 50, "y": 0},
            {"id": "T", "name": "终点", "x": 100, "y": 0},
            {"id": "Q", "name": "岔路", "x": 99, "y": 1},
        ],
        "edges": [
            {"u": "S", "v": "P", "line": "FAST"},
            {"u": "P", "v": "T", "line": "FAST"},
            {"u": "S", "v": "Q", "line": "FAST"},
            {"u": "Q", "v": "T", "line": "SLOW"},
        ],
    })


def disconnected() -> Graph:
    """A-B 连通；Z 是孤立站（没有任何线路）。"""
    return load_spec({
        "meta": {"name": "disconnected"},
        "lines": [{"id": "T", "name": "线", "speed_kmh": 40}],
        "stations": [
            {"id": "A", "name": "甲", "x": 0, "y": 0},
            {"id": "B", "name": "乙", "x": 1, "y": 0},
            {"id": "Z", "name": "孤岛", "x": 9, "y": 9},
        ],
        "edges": [{"u": "A", "v": "B", "line": "T"}],
    })


# ---------------------------------------------------------------- 随机图
def random_graph(seed: int, n: int = 7, extra: int = 3) -> Graph:
    """连通随机图：环 + 额外边，3 条线路轮转分配，速度 <= 最大速度（几何自洽）。"""
    rng = random.Random(seed)
    speeds = [20, 40, 80]
    coords = []
    for i in range(n):
        # 保证坐标互不重合
        coords.append((round(rng.uniform(0, 20), 3), round(rng.uniform(0, 20), 3) + i * 0.01))
    stations = [{"id": f"N{i}", "name": f"站{i}", "x": x, "y": y} for i, (x, y) in enumerate(coords)]
    edges = []
    for i in range(n):                       # 环：保证连通
        edges.append({"u": f"N{i}", "v": f"N{(i + 1) % n}", "line": f"L{i % 3 + 1}"})
    seen = set()
    for e in edges:                        # 环边先去重登记
        a, b = int(e["u"][1:]), int(e["v"][1:])
        seen.add((min(a, b), max(a, b), e["line"]))
    tries = 0
    while len(edges) < n + extra and tries < 200:
        tries += 1
        a, b = rng.sample(range(n), 2)
        line = f"L{rng.randint(1, 3)}"
        key = (min(a, b), max(a, b), line)
        if key in seen:                    # 同一条 (u,v,line) 不能重复（加载器会拒绝）
            continue
        seen.add(key)
        edges.append({"u": f"N{a}", "v": f"N{b}", "line": line})
    return load_spec({
        "meta": {"name": f"random-{seed}"},
        "lines": [{"id": f"L{i + 1}", "name": f"线{i + 1}", "speed_kmh": s}
                  for i, s in enumerate(speeds)],
        "stations": stations,
        "edges": edges,
    })


# ---------------------------------------------------------------- oracle
def all_pairs(g: Graph) -> List[Tuple[str, str]]:
    ids = sorted(g.stations)
    return [(a, b) for a in ids for b in ids if a != b]


def brute_min_transfers(g: Graph, s: str, t: str):
    """独立 oracle：枚举所有简单路径，取最少换乘数。"""
    if s == t:
        return 0
    best = [None]
    lines_of = g.lines_of_station()

    def walk(sid: str, prev_line, switches: int, visited: set):
        if best[0] is not None and switches >= best[0]:
            return
        if sid == t:
            best[0] = switches
            return
        seen = set()
        for e in g.neighbors(sid):
            v = e.other(sid)
            if v in visited or (v, e.line) in seen:
                continue
            seen.add((v, e.line))
            visited.add(v)
            walk(v, e.line, switches + (0 if prev_line is None or e.line == prev_line else 1), visited)
            visited.remove(v)

    for ln in sorted(lines_of.get(s, ())):
        for e in g.neighbors(s):
            if e.line != ln:
                continue
            v = e.other(s)
            walk(v, ln, 0, {s, v})
    return best[0]


def raw_optimal_cost(g: Graph, s: str, t: str, weight: str = "time") -> float:
    """独立的最短路 oracle（**不取整**）：用于验证 h 的可采纳性。

    为什么需要它：`Result.time_min/distance_km` 是 analyze_path 取整后的展示值，
    直接拿它当"真实最优代价"会把 0.005 的取整误差误判成"h 高估"。
    """
    import heapq

    def w(e) -> float:
        return e.time_min if weight == "time" else e.distance_km

    dist = {s: 0.0}
    heap = [(0.0, s)]
    done = set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in done:
            continue
        done.add(u)
        if u == t:
            return d
        for e in g.neighbors(u):
            v = e.other(u)
            nd = d + w(e)
            if nd < dist.get(v, float("inf")) - 1e-12:
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    raise AssertionError(f"{s} 与 {t} 不连通")


def manhattan(g: Graph, sid: str, goal: str) -> float:
    """故意做坏的启发式：曼哈顿距离 × 3（会高估，破坏可采纳性）。"""
    a, b = g.stations[sid], g.stations[goal]
    return 3.0 * (abs(a.x - b.x) + abs(a.y - b.y))