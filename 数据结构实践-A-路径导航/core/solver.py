"""最短路求解层（P2 契约桩）。

本文件属于 P2（算法）的交付物。当前是可运行的占位实现，保证 P3 界面与
``bench.csv`` 的数值来自**同一份代码**（不手抄、不写死）；P2 用真实现替换时
请保持接口与 :class:`Result` 字段不变：

    from core import solver
    result = solver.solve(g, "S01", "S07", "dijkstra", "time")

``Result`` 字段：algorithm / found / path / distance_km / time_min /
transfers / expanded / elapsed_ms / events
"""

from __future__ import annotations

import heapq
import math
import time
from collections import deque
from dataclasses import dataclass, field

from .graph import Graph

__all__ = [
    "Result",
    "solve",
    "ALGORITHMS",
    "WEIGHTS",
    "ALGORITHM_LABELS",
    "WEIGHT_LABELS",
]

ALGORITHMS = ("dijkstra", "astar", "bfs")
WEIGHTS = ("time", "distance")
ALGORITHM_LABELS = {"dijkstra": "Dijkstra", "astar": "A*", "bfs": "BFS"}
WEIGHT_LABELS = {"time": "时间最短", "distance": "距离最短"}


@dataclass
class Result:
    """一次求解的完整结果（界面只读这个对象）。"""

    algorithm: str
    found: bool
    path: list[str] = field(default_factory=list)
    distance_km: float = 0.0
    time_min: float = 0.0
    transfers: int = 0
    expanded: int = 0
    elapsed_ms: float = 0.0
    events: list[dict] = field(default_factory=list)

    @property
    def stops(self) -> int:
        """途经站点数（不含起点）。"""
        return max(0, len(self.path) - 1)


def _heuristic_scale(graph: Graph) -> tuple[float, float]:
    """返回 (km_per_unit 下界, 全网最大速度 km/min)，用于 A* 的可采纳启发式。"""
    km_per_unit = math.inf
    max_speed = 0.0
    for edge in graph.iter_edges():
        a, b = graph.stations[edge.u], graph.stations[edge.v]
        units = math.hypot(a.x - b.x, a.y - b.y)
        if units > 1e-9:
            km_per_unit = min(km_per_unit, edge.km / units)
        max_speed = max(max_speed, edge.km / edge.minutes)
    if not math.isfinite(km_per_unit):
        km_per_unit = 0.0
    if max_speed <= 0:
        max_speed = 1.0
    return km_per_unit, max_speed


def _make_heuristic(graph: Graph, weight: str, target: str):
    km_per_unit, max_speed = _heuristic_scale(graph)
    tx, ty = graph.stations[target].x, graph.stations[target].y

    def h(sid: str) -> float:
        station = graph.stations[sid]
        straight_km = math.hypot(station.x - tx, station.y - ty) * km_per_unit
        if weight == "distance":
            return straight_km
        return straight_km / max_speed

    return h


def _reconstruct(prev: dict[str, str | None], start: str, target: str) -> list[str]:
    path: list[str] = []
    node: str | None = target
    while node is not None:
        path.append(node)
        if node == start:
            break
        node = prev.get(node)
    path.reverse()
    return path


def _summarize(graph: Graph, path: list[str]) -> tuple[float, float, int]:
    """按路径重新累加 里程 / 时间 / 换乘次数（不依赖搜索时的临时量）。"""
    km = 0.0
    minutes = 0.0
    transfers = 0
    prev_line: str | None = None
    for u, v in zip(path, path[1:]):
        edge = graph.edge_between(u, v)
        if edge is None:
            raise RuntimeError(f"路径中出现不存在的区间：{u}-{v}")
        km += edge.km
        minutes += edge.minutes
        if prev_line is not None and edge.line != prev_line:
            transfers += 1
        prev_line = edge.line
    return round(km, 2), round(minutes, 1), transfers


def solve(graph: Graph, start: str, end: str, algorithm: str = "dijkstra", weight: str = "time") -> Result:
    """求 start→end 的最短路。

    algorithm ∈ {"dijkstra", "astar", "bfs"}；weight ∈ {"time", "distance"}。
    bfs 以"区间数"为代价，weight 参数仅用于结果标注，不影响选路。
    """
    if algorithm not in ALGORITHMS:
        raise ValueError(f"未知算法：{algorithm!r}")
    if weight not in WEIGHTS:
        raise ValueError(f"未知权重：{weight!r}")
    for sid in (start, end):
        if sid not in graph.stations:
            raise KeyError(f"未知站点：{sid}")

    t0 = time.perf_counter()
    events: list[dict] = []
    expanded = 0
    prev: dict[str, str | None] = {start: None}
    dist: dict[str, float] = {start: 0.0}

    if start == end:
        return Result(algorithm, True, [start], 0.0, 0.0, 0, 0, 0.0, [])

    if algorithm == "bfs":
        hops = {start: 0}
        queue: deque[str] = deque([start])
        seen = {start}
        while queue:
            node = queue.popleft()
            expanded += 1
            events.append({"type": "expand", "station": node, "cost": hops[node]})
            if node == end:
                break
            for edge in graph.neighbors(node):
                nxt = edge.other(node)
                if nxt in seen:
                    continue
                seen.add(nxt)
                hops[nxt] = hops[node] + 1
                prev[nxt] = node
                queue.append(nxt)
        found = end in seen
        path = _reconstruct(prev, start, end) if found else []
    else:
        heuristic = _make_heuristic(graph, weight, end) if algorithm == "astar" else None
        heap: list[tuple[float, float, str]] = [(0.0, 0.0, start)]
        settled: set[str] = set()
        while heap:
            _, g_cost, node = heapq.heappop(heap)
            if node in settled:
                continue
            settled.add(node)
            expanded += 1
            events.append({"type": "expand", "station": node, "cost": round(g_cost, 3)})
            if node == end:
                break
            for edge in graph.neighbors(node):
                nxt = edge.other(node)
                if nxt in settled:
                    continue
                ng = g_cost + edge.cost(weight)
                if ng < dist.get(nxt, math.inf) - 1e-12:
                    dist[nxt] = ng
                    prev[nxt] = node
                    est = ng + (heuristic(nxt) if heuristic else 0.0)
                    heapq.heappush(heap, (est, ng, nxt))
        found = end in settled
        path = _reconstruct(prev, start, end) if found else []

    elapsed = (time.perf_counter() - t0) * 1000.0
    if not found:
        return Result(algorithm, False, [], 0.0, 0.0, 0, expanded, round(elapsed, 3), events)

    km, minutes, transfers = _summarize(graph, path)
    events.append({"type": "path", "stations": list(path)})
    return Result(
        algorithm=algorithm,
        found=True,
        path=path,
        distance_km=km,
        time_min=minutes,
        transfers=transfers,
        expanded=expanded,
        elapsed_ms=round(elapsed, 3),
        events=events,
    )
