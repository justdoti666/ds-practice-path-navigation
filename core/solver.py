#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""算法引擎（A 题）：Dijkstra / BFS（最少换乘）/ A*。

口径（与任务书 P01 一致，报告第 3 章直接引用）：
  * 最快路径 = Dijkstra，权重可选 time（时间）或 distance（距离）；
  * 最少换乘 = BFS，状态为 (站点, 当前线路)，同线移动 0 代价、换线 1 代价（0-1 BFS）；
  * 启发式 = A*，h 取欧几里得距离；按时间权重时 h 再除以**全网最大速度**，保证可采纳：
        h_time(n) = ‖P_n − P_goal‖ / v_max × 60 ≤ 真实用时（因 time = 距离 / v_line 且 v_line ≤ v_max）
  * **扩展（settle）计数口径**：出堆且首次访问记 1；`len(events) == expanded`（单测 T11 固化）；
  * **确定性**：堆键尾部带站点 id 兜底比较（Dijkstra `(dist, sid)`、A* `(f, g, sid)`、
    0-1 BFS 初始按 sorted(线路) 入队）⇒ 同一输入两次运行得到逐字段相同的结果（单测 T13）；
  * 异常：`PathNotFound`（两站不连通 / 站点无线路）、`DataError`（参数或站点非法）。
"""
from __future__ import annotations

import heapq
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .graph import DataError, Edge, Graph

WEIGHTS = ("time", "distance")


class PathNotFound(RuntimeError):
    """两站不连通 / 起点没有可乘线路。"""


@dataclass
class Result:
    """一次搜索的完整结果（跨层契约，见《技术方案》§6.2）。

    algorithm  形如 "Dijkstra(time)" / "A*(distance)" / "BFS(最少换乘)"
    path       站点 id 序列（含起终点）
    transfers  最低换乘数（Dijkstra/A* 为沿其路径的最少线路切换数，由 analyze_path 求得）
    expanded   扩展（settle）节点数，报告对比实验的主要指标之一
    events     [("settle", sid), …]，供界面层做搜索动画；算法层不感知界面
    """

    algorithm: str
    found: bool
    path: List[str] = field(default_factory=list)
    distance_km: float = 0.0
    time_min: float = 0.0
    transfers: Optional[int] = None
    expanded: int = 0
    elapsed_ms: float = 0.0
    events: List[Tuple[str, str]] = field(default_factory=list)
    note: str = ""

    def summary(self) -> str:
        if not self.found:
            return f"{self.algorithm}: 未找到路径（{self.note or '不连通'}）"
        t = "—" if self.transfers is None else str(self.transfers)
        return (f"{self.algorithm}: {self.distance_km:.2f} km / {self.time_min:.2f} min / "
                f"换乘 {t} / 扩展 {self.expanded} / {self.elapsed_ms:.2f} ms")


def _w(edge: Edge, weight: str) -> float:
    if weight == "time":
        return edge.time_min
    if weight == "distance":
        return edge.distance_km
    raise DataError(f"未知权重: {weight}")


def _vmax(g: Graph) -> float:
    """全网最大速度（km/h）；无线路或异常值时报错前兜底为 1.0。"""
    v = max((ln["speed_kmh"] for ln in g.lines.values()), default=1.0)
    return v if v > 0 else 1.0


def _heuristic(g: Graph, sid: str, goal: str, weight: str, vmax: Optional[float] = None) -> float:
    """可采纳启发式：距离权重用直线距离；时间权重再除以全网最大速度。

    vmax 允许外部传入：A* 每次求解只算一次，避免每个邻居都扫一遍线路表。
    """
    a, b = g.stations[sid], g.stations[goal]
    d = math.hypot(a.x - b.x, a.y - b.y)
    if weight == "distance":
        return d
    return d / (vmax if vmax else _vmax(g)) * 60.0


def analyze_path(g: Graph, path: List[str]) -> Tuple[float, float, List[str], int]:
    """沿站点序列选线路：先用 DP 让**换乘次数最少**，同分时让用时更短。

    返回 (km, min, 线路序列, 换乘次数)。平行线路（同站对多线）时取切换最少的组合。
    """
    if len(path) < 2:
        return 0.0, 0.0, [], 0
    hops: List[List[Edge]] = []
    for a, b in zip(path, path[1:]):
        cand = g.edge_between(a, b)
        if not cand:
            raise PathNotFound(f"路径中 {a} → {b} 无连接")
        hops.append(cand)
    # dp[line] = (换乘数, 用时, 距离, 线路序列)
    dp: Dict[str, Tuple[int, float, float, List[str]]] = {}
    for e in hops[0]:
        dp[e.line] = (0, e.time_min, e.distance_km, [e.line])
    for cand in hops[1:]:
        nxt: Dict[str, Tuple[int, float, float, List[str]]] = {}
        for e in cand:
            best = None
            for prev_line, (sw, tm, km, seq) in dp.items():
                cost = (sw + (0 if prev_line == e.line else 1), tm + e.time_min, km + e.distance_km)
                if best is None or cost < best[:3]:
                    best = (cost[0], cost[1], cost[2], seq + [e.line])
            if best is not None:
                cur = nxt.get(e.line)
                if cur is None or best[:3] < cur[:3]:
                    nxt[e.line] = best
        dp = nxt
    if not dp:
        raise PathNotFound("路径不可达")
    sw, tm, km, seq = min(dp.values(), key=lambda x: (x[0], x[1]))
    return round(km, 3), round(tm, 2), seq, sw


def _result(g: Graph, algorithm: str, path: List[str], expanded: int,
            elapsed_ms: float, events: List[Tuple[str, str]], note: str = "") -> Result:
    km, tm, _lines, sw = analyze_path(g, path)
    return Result(algorithm, True, path, km, tm, sw, expanded, round(elapsed_ms, 3), events, note)


# ------------------------------------------------------------------ Dijkstra
def dijkstra(g: Graph, start: str, goal: str, weight: str = "time") -> Result:
    """单源最短路（非负权重）：堆 + 懒惰删除。

    扩展计数：出堆且未访问过记 1（`expanded`），并同时写一条 settle 事件。
    确定性：堆键为 `(dist, sid)`，同代价时按站点 id 升序出堆 ⇒ 可复现。
    """
    _check(g, start, goal, weight)
    t0 = time.perf_counter()
    dist = {start: 0.0}
    prev: Dict[str, str] = {}
    done = set()
    expanded = 0
    events: List[Tuple[str, str]] = []
    heap = [(0.0, start)]
    while heap:
        d, u = heapq.heappop(heap)
        if u in done:
            continue
        done.add(u)
        expanded += 1
        events.append(("settle", u))
        if u == goal:
            break
        for e in g.neighbors(u):
            v = e.other(u)
            nd = d + _w(e, weight)
            if nd < dist.get(v, math.inf) - 1e-12:
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))
    elapsed = (time.perf_counter() - t0) * 1000.0
    if goal not in done:
        raise PathNotFound(f"{start} 与 {goal} 不连通")
    return _result(g, f"Dijkstra({weight})", _rebuild(prev, start, goal), expanded, elapsed, events)


# ---------------------------------------------------------------------- A*
def astar(g: Graph, start: str, goal: str, weight: str = "time",
          heuristic: Optional[Callable[[Graph, str, str], float]] = None) -> Result:
    """A*：`f(n) = g(n) + h(n)`，默认 h 为可采纳的欧几里得（时间权重再除 v_max）。

    参数 heuristic 是可选的**注入点**（向后兼容，默认 None 用内置 h）：
      * 报告里的"曼哈顿 vs 欧几里得"对比实验直接注入不同 h；
      * 单测用它注入**非可采纳** h，证明"h 写错会被测试抓住"（T15）。
    签名形式：heuristic(g, sid, goal) -> float。
    """
    _check(g, start, goal, weight)
    vmax = _vmax(g)
    if heuristic is None:
        def h(sid: str) -> float:
            return _heuristic(g, sid, goal, weight, vmax)
    else:
        def h(sid: str) -> float:
            return float(heuristic(g, sid, goal))

    t0 = time.perf_counter()
    gscore = {start: 0.0}
    prev: Dict[str, str] = {}
    done = set()
    expanded = 0
    events: List[Tuple[str, str]] = []
    heap = [(h(start), 0.0, start)]
    while heap:
        f, gs, u = heapq.heappop(heap)
        if u in done:
            continue
        done.add(u)
        expanded += 1
        events.append(("settle", u))
        if u == goal:
            break
        for e in g.neighbors(u):
            v = e.other(u)
            ng = gs + _w(e, weight)
            if ng < gscore.get(v, math.inf) - 1e-12:
                gscore[v] = ng
                prev[v] = u
                heapq.heappush(heap, (ng + h(v), ng, v))
    elapsed = (time.perf_counter() - t0) * 1000.0
    if goal not in done:
        raise PathNotFound(f"{start} 与 {goal} 不连通")
    return _result(g, f"A*({weight})", _rebuild(prev, start, goal), expanded, elapsed, events)


# ------------------------------------------------------------- BFS 最少换乘
def bfs_transfers(g: Graph, start: str, goal: str) -> Result:
    """0-1 BFS：状态 = (站点, 线路)。同线移动代价 0（入队首），换线代价 1（入队尾）。

    为什么不能直接对站点做 BFS：那样只会得到"最少站数"，不是"最少换乘"。
    正确性：0-1 BFS 的出队顺序按代价非降 ⇒ 首次弹出 goal 状态即为最小换乘数。
    扩展计数口径与 Dijkstra 一致（每弹出并处理一个状态记 1，注意单位是**状态**不是站点）。
    """
    _check(g, start, goal, "time")
    if start == goal:
        return Result("BFS(最少换乘)", True, [start], 0.0, 0.0, 0, 1, 0.0, [("settle", start)])
    t0 = time.perf_counter()
    station_lines = g.lines_of_station()
    if not station_lines.get(start):
        raise PathNotFound(f"站点 {start} 没有任何线路")
    best: Dict[Tuple[str, str], int] = {}
    parent: Dict[Tuple[str, str], Tuple[str, str]] = {}
    dq = deque()
    for ln in sorted(station_lines[start]):
        best[(start, ln)] = 0
        dq.appendleft((start, ln))
    goal_state: Optional[Tuple[str, str]] = None
    expanded = 0
    events: List[Tuple[str, str]] = []
    processed = set()                     # 同一状态只处理一次（0-1 BFS 的标准去重，防重复入队导致 expanded 虚高）
    while dq:
        sid, ln = dq.popleft()
        if (sid, ln) in processed:
            continue
        processed.add((sid, ln))
        cost = best[(sid, ln)]
        expanded += 1
        events.append(("settle", sid))
        if sid == goal:
            goal_state = (sid, ln)
            break
        for e in g.neighbors(sid):
            if e.line != ln:
                continue
            nxt = (e.other(sid), ln)
            if cost < best.get(nxt, 10 ** 9):
                best[nxt] = cost
                parent[nxt] = (sid, ln)
                dq.appendleft(nxt)
        for other in station_lines[sid]:
            if other == ln:
                continue
            nxt = (sid, other)
            if cost + 1 < best.get(nxt, 10 ** 9):
                best[nxt] = cost + 1
                parent[nxt] = (sid, ln)
                dq.append(nxt)
    elapsed = (time.perf_counter() - t0) * 1000.0
    if goal_state is None:
        raise PathNotFound(f"{start} 与 {goal} 不连通")
    # 回溯：把 (站点,线路) 序列压成站点序列（同站换线只保留一次）
    states: List[Tuple[str, str]] = [goal_state]
    while states[-1] in parent:
        states.append(parent[states[-1]])
    states.reverse()
    path: List[str] = []
    for sid, _ln in states:
        if not path or path[-1] != sid:
            path.append(sid)
    return _result(g, "BFS(最少换乘)", path, expanded, elapsed, events,
                   note=f"状态数 {len(best)}")


# ------------------------------------------------------------------ 工具函数
def _rebuild(prev: Dict[str, str], start: str, goal: str) -> List[str]:
    path = [goal]
    while path[-1] != start:
        path.append(prev[path[-1]])
    path.reverse()
    return path


def _check(g: Graph, start: str, goal: str, weight: str) -> None:
    if start not in g.stations or goal not in g.stations:
        raise DataError(f"起终点不在图中: {start} / {goal}")
    if weight not in WEIGHTS:
        raise DataError(f"权重只能是 {WEIGHTS}")


def solve(g: Graph, start: str, goal: str, algorithm: str, weight: str = "time") -> Result:
    """统一入口：algorithm ∈ {dijkstra, astar, bfs}（大小写与别名均容忍）。"""
    algo = algorithm.lower()
    if algo in ("dijkstra", "d"):
        return dijkstra(g, start, goal, weight)
    if algo in ("astar", "a*", "a"):
        return astar(g, start, goal, weight)
    if algo in ("bfs", "b"):
        return bfs_transfers(g, start, goal)
    raise DataError(f"未知算法: {algorithm}")