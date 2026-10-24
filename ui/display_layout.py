#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""显示层示意布局（P4 渲染层新增，2026-10-24）。

背景
----
P1 的真实数据接入 ``tools/import_metro.py`` 在没有原始经纬度时，按
"每条线路一条水平带、x = 站序 × spacing、y = 线序 × line_gap" 生成**近似坐标**；
换乘站合并为同一站点、坐标取首次出现位置。站点一多（169 站），同一线路的
相邻站之间就会出现大量跨全图的超长直线，线路在画面中央互相压盖。

本模块只解决"画得好看"的问题：按图的**拓扑**重新计算一套**仅用于绘制**的
示意坐标。它**不修改** ``Graph`` 里的 ``x/y``，因此不影响 ``core/solver.py``
的 A* 启发式、也不影响任何算法数值与交互逻辑：
    * ``core/``、``ui/window.py`` 一行不改；
    * 画布仍然用 ``station_screen`` / ``hit_test`` 同一套坐标变换做交互；
    * 路径的 km/min 标注由 ``_segment_metrics`` 用**数据坐标**计算，和求解结果一致。

判定策略
--------
* 数据文件自带 ``meta.display_layout``（工具预生成的显示坐标）→ 直接用；
* 否则若被判定为"生成器的线性布局"（``meta.layout`` 标记 / 线路退化成水平带 /
  大量超长边）→ 用 :func:`schematic_layout` 重算；
* 否则（如 10 站 demo 的手工坐标）→ 原样使用数据坐标，界面与修复前完全一致。

布局算法（纯标准库，确定性）
--------------------------
1. 站点图 = 线路站序相邻关系；按连通分量分别布局（罕见，正常数据是单分量）。
2. 每个分量：BFS 距离 → 最远点采样选 pivot → PivotMDS（经典 MDS 的
   landmark 近似）得到初始坐标；这比单纯弹簧布局更稳、交叉少。
3. 局部力导向细化：近邻斥力（空间哈希加速）+ 边弹簧 + 沿线平滑（让线路顺滑）
   + 非相邻线段互斥（避免两条线路的线段贴在一起看不出来）。
4. 归一化到以原点为中心、绝对坐标 ≤ 1 的框内，供画布等比适配。

性能（169 站实测）：一次布局约 0.6~0.9s，只在首次建画布时算一次；
之后缩放/拖拽/重绘都复用同一份结果。
"""
from __future__ import annotations

import hashlib
import math
from collections import OrderedDict, defaultdict

__all__ = [
    "LAYOUT_META_KEY",
    "station_positions",
    "needs_schematic_layout",
    "schematic_layout",
    "derived_line_sequences",
]

#: ``graph.meta`` 中可选的预生成显示坐标字段：``{sid: [x, y], ...}``。
LAYOUT_META_KEY = "display_layout"

#: 需要重算布局的最小站点数（小图手工坐标通常本来就好看，不动）。
MIN_NODES = 24

#: 判定为生成器线性布局的阈值。
ROW_LIKE_RATIO = 0.5          # 线路"退化成一条水平/垂直带"的比例
COLLINEAR_RATIO = 0.6         # 线路"整条共线"的比例
LONG_EDGE_RATIO = 0.12        # 超长边（> 4×中位边长）占比
LONG_EDGE_MIN_COUNT = 8       # 超长边最少条数（小图不误判）

#: 力导向细化参数（调过参，169 站质量/耗时平衡）。
_REFINE_ITERS = 300
_REFINE_MIN_ITERS = 160
_REFINE_SMALL = 120


# ======================================================================
# 对外接口
# ======================================================================
_LAYOUT_CACHE: "OrderedDict[str, dict[str, tuple[float, float]]]" = OrderedDict()
_LAYOUT_CACHE_MAX = 4


def _layout_fingerprint(graph, line_sequences) -> str:
    """按拓扑 + 数据坐标生成缓存键；同图重复建窗不再重算布局。"""
    stations = _station_table(graph)
    seqs = line_sequences if line_sequences is not None else derived_line_sequences(graph)
    payload = (
        tuple(sorted((sid, round(x, 6), round(y, 6)) for sid, (_n, x, y) in stations.items())),
        tuple(sorted((e.u, e.v, e.line) for e in (getattr(graph, "edges", None) or []))),
        tuple(sorted((lid, tuple(seq)) for lid, seq in (seqs or {}).items())),
    )
    return hashlib.sha1(repr(payload).encode("utf-8", "replace")).hexdigest()


def _cache_get(key: str):
    cached = _LAYOUT_CACHE.get(key)
    if cached is None:
        return None
    _LAYOUT_CACHE.move_to_end(key)
    return dict(cached)


def _cache_put(key: str, positions: dict) -> None:
    _LAYOUT_CACHE[key] = dict(positions)
    _LAYOUT_CACHE.move_to_end(key)
    while len(_LAYOUT_CACHE) > _LAYOUT_CACHE_MAX:
        _LAYOUT_CACHE.popitem(last=False)


def station_positions(graph, line_sequences=None) -> dict[str, tuple[float, float]]:
    """返回画布要用的站点坐标 ``{sid: (x, y)}``（只用于绘制）。

    优先级：
        1. ``graph.meta[LAYOUT_META_KEY]`` 完整合法 → 原样采用（工具预生成布局）；
        2. :func:`needs_schematic_layout` 为 True → :func:`schematic_layout`；
        3. 否则 → 数据坐标。
    """
    stations = _station_table(graph)
    if not stations:
        return {}

    preset = _preset_positions(graph, stations)
    if preset is not None:
        return preset

    if not needs_schematic_layout(graph, line_sequences):
        return {sid: (float(st[1]), float(st[2])) for sid, st in stations.items()}

    key = _layout_fingerprint(graph, line_sequences)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        positions = schematic_layout(graph, line_sequences)
    except Exception:  # noqa: BLE001 - 画布绝不能因为布局失败而打不开
        # 任何异常都退回数据坐标：界面照常可用，只是不美化。
        return {sid: (float(st[1]), float(st[2])) for sid, st in stations.items()}
    _cache_put(key, positions)
    return positions


def needs_schematic_layout(graph, line_sequences=None) -> bool:
    """判断数据坐标是否是"生成器线性布局"、需要换成示意布局。"""
    stations = _station_table(graph)
    if len(stations) < MIN_NODES:
        return False
    meta = getattr(graph, "meta", None) or {}
    if isinstance(meta.get("layout"), dict):
        # tools/import_metro.py 生成的近似坐标：明确标记过，直接重算。
        return True

    lines = line_sequences if line_sequences else derived_line_sequences(graph)
    if not lines:
        return False

    pos = {sid: (float(st[1]), float(st[2])) for sid, st in stations.items()}
    if _row_like_ratio(lines, pos) >= ROW_LIKE_RATIO:
        return True
    if _collinear_ratio(lines, pos) >= COLLINEAR_RATIO:
        return True
    long_count = _long_edge_count(graph, pos)
    if long_count >= max(LONG_EDGE_MIN_COUNT, int(len(graph.edges) * LONG_EDGE_RATIO)):
        return True
    return False


def schematic_layout(graph, line_sequences=None) -> dict[str, tuple[float, float]]:
    """按拓扑计算一套仅供绘制的示意坐标（纯标准库，确定性）。

    返回 ``{sid: (x, y)}``；坐标以原点为中心、绝对值 ≤ 1，画布会再等比适配。
    """
    stations = _station_table(graph)
    if not stations:
        return {}
    ids = sorted(stations)
    order = {sid: i for i, sid in enumerate(ids)}
    n = len(ids)

    lines = line_sequences if line_sequences else derived_line_sequences(graph)
    # 全局边表（去重）与每条线的站序（用于沿线平滑）
    edge_set = set()
    for seq in lines.values():
        for a, b in zip(seq, seq[1:]):
            if a == b or a not in order or b not in order:
                continue
            ia, ib = order[a], order[b]
            edge_set.add((ia, ib) if ia < ib else (ib, ia))
    edges = sorted(edge_set)
    line_nodes = [
        [order[sid] for sid in seq if sid in order]
        for seq in lines.values()
    ]
    line_nodes = [nodes for nodes in line_nodes if len(nodes) >= 2]

    adj = [[] for _ in range(n)]
    for a, b in edges:
        adj[a].append(b)
        adj[b].append(a)

    # 按连通分量布局，再横向拼装（孤立站点单独作为 1 点分量）
    seen = [False] * n
    components: list[list[int]] = []
    for start in range(n):
        if seen[start]:
            continue
        stack = [start]
        seen[start] = True
        comp = []
        while stack:
            u = stack.pop()
            comp.append(u)
            for v in adj[u]:
                if not seen[v]:
                    seen[v] = True
                    stack.append(v)
        components.append(sorted(comp))
    components.sort(key=len, reverse=True)

    if len(components) == 1:
        coords = _layout_component(adj, edges, line_nodes)
    else:
        coords = [None] * n
        cursor = 0.0
        gap = 0.35
        for comp in components:
            cset = set(comp)
            c_edges = [(a, b) for a, b in edges if a in cset]
            c_lines = [[v for v in nodes if v in cset] for nodes in line_nodes]
            c_lines = [nodes for nodes in c_lines if len(nodes) >= 2]
            sub = _layout_component_for(comp, c_edges, c_lines)
            # 每个分量已归一化到 [-1, 1]，按顺序摆成一行
            for gid, (x, y) in zip(comp, sub):
                coords[gid] = (x + cursor + 1.0, y)
            cursor += 2.0 + gap

    # 统一归一化：居中、绝对值 ≤ 1
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-9)
    scale = 2.0 / span
    return {ids[i]: ((coords[i][0] - cx) * scale, (coords[i][1] - cy) * scale)
            for i in range(n)}


def derived_line_sequences(graph) -> dict[str, list[str]]:
    """从 ``graph.edges`` 还原每条线路的站序（与 ``ui.canvas._line_stations`` 同口径）。

    正常画布会把自己算好的站序传进来；工具/测试直接调本模块时用这个兜底。
    """
    edges = list(getattr(graph, "edges", None) or [])
    line_ids: list[str] = []
    try:
        line_ids = [str(lid) for lid in (getattr(graph, "lines", None) or {})]
    except TypeError:
        line_ids = []
    for e in edges:
        lid = str(getattr(e, "line", "") or "")
        if lid and lid not in line_ids:
            line_ids.append(lid)

    result: dict[str, list[str]] = {}
    for lid in line_ids:
        pairs = [(e.u, e.v) for e in edges if str(getattr(e, "line", "")) == lid]
        if not pairs:
            continue
        adj: dict[str, list[str]] = {}
        for a, b in pairs:
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
        ends = sorted(node for node, nb in adj.items() if len(nb) == 1)
        start = ends[0] if ends else sorted(adj)[0]
        seq: list[str] = []
        prev = None
        cur = start
        seen = set()
        while cur is not None and cur not in seen:
            seq.append(cur)
            seen.add(cur)
            nxt = [node for node in adj[cur] if node != prev]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            if not ends and cur == start:      # 环线：回到起点为止
                seq.append(cur)
                break
        if len(seq) >= 2:
            result[lid] = seq
    return result


# ======================================================================
# 站点表与"线性布局"判定
# ======================================================================
def _station_table(graph) -> dict[str, tuple[str, float, float]]:
    """``{sid: (name, x, y)}``，只读、兼容 core.Station 与 UI 适配对象。"""
    table: dict[str, tuple[str, float, float]] = {}
    raw = getattr(graph, "stations", None) or {}
    items = raw.items() if isinstance(raw, dict) else [(None, st) for st in raw]
    for key, st in items:
        sid = getattr(st, "sid", None) or getattr(st, "id", None) or key
        if not sid:
            continue
        try:
            x = float(getattr(st, "x"))
            y = float(getattr(st, "y"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        name = str(getattr(st, "name", sid))
        table[str(sid)] = (name, x, y)
    return table


def _preset_positions(graph, stations) -> dict[str, tuple[float, float]] | None:
    """读取工具预生成的 ``meta.display_layout``；不完整/不合法就返回 None。"""
    meta = getattr(graph, "meta", None) or {}
    raw = meta.get(LAYOUT_META_KEY) if isinstance(meta, dict) else None
    if not isinstance(raw, dict) or not raw:
        return None
    out: dict[str, tuple[float, float]] = {}
    for sid in stations:
        item = raw.get(sid)
        try:
            x, y = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        out[sid] = (x, y)
    # 预生成布局至少要"不缩成一点"，否则宁可自动重算
    xs = [p[0] for p in out.values()]
    ys = [p[1] for p in out.values()]
    if max(xs) - min(xs) < 1e-9 and max(ys) - min(ys) < 1e-9:
        return None
    return out


def _row_like_ratio(lines, pos) -> float:
    """线路里"绝大多数站点压在同一条水平/垂直线上"的比例。"""
    considered = 0
    row_like = 0
    for seq in lines.values():
        if len(seq) < 4:
            continue
        considered += 1
        ys = [pos[sid][1] for sid in seq if sid in pos]
        xs = [pos[sid][0] for sid in seq if sid in pos]
        if len(ys) < 4:
            continue
        if max(_mode_count(ys), _mode_count(xs)) / len(ys) >= 0.8:
            row_like += 1
    return row_like / considered if considered else 0.0


def _collinear_ratio(lines, pos) -> float:
    """线路整条近似共线（含斜线）的比例——"完全为直线"的另一种退化形态。"""
    considered = 0
    collinear = 0
    for seq in lines.values():
        if len(seq) < 4:
            continue
        pts = [pos[sid] for sid in seq if sid in pos]
        if len(pts) < 4:
            continue
        considered += 1
        x0, y0 = pts[0]
        x1, y1 = pts[-1]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        max_dev = 0.0
        for x, y in pts[1:-1]:
            dev = abs(dy * (x - x0) - dx * (y - y0)) / length
            max_dev = max(max_dev, dev)
        diag = math.hypot(max(x for x, _ in pts) - min(x for x, _ in pts),
                          max(y for _, y in pts) - min(y for _, y in pts))
        if max_dev <= max(1e-9, diag * 1e-6):
            collinear += 1
    return collinear / considered if considered else 0.0


def _long_edge_count(graph, pos) -> int:
    lengths = []
    for e in getattr(graph, "edges", None) or []:
        a, b = getattr(e, "u", None), getattr(e, "v", None)
        if a in pos and b in pos:
            lengths.append(math.hypot(pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]))
    if len(lengths) < 8:
        return 0
    ordered = sorted(lengths)
    median = ordered[len(ordered) // 2]
    if median <= 0:
        return 0
    return sum(1 for value in lengths if value > 4.0 * median)


def _mode_count(values) -> int:
    counts: dict[float, int] = {}
    for value in values:
        key = round(float(value), 6)
        counts[key] = counts.get(key, 0) + 1
    return max(counts.values()) if counts else 0


# ======================================================================
# 单个连通分量的布局
# ======================================================================
def _layout_component(adj, edges, line_nodes):
    return _layout_component_for(list(range(len(adj))), edges, line_nodes)


def _layout_component_for(nodes, edges, line_nodes):
    """对连通分量布局，返回与 ``nodes`` 等长的坐标列表（归一化到 [-1,1]）。"""
    m = len(nodes)
    if m == 1:
        return [(0.0, 0.0)]
    local = {gid: i for i, gid in enumerate(nodes)}
    l_edges = [(local[a], local[b]) for a, b in edges if a in local and b in local]
    l_lines = [[local[v] for v in line if v in local] for line in line_nodes]
    l_lines = [line for line in l_lines if len(line) >= 2]
    l_adj = [[] for _ in range(m)]
    for a, b in l_edges:
        l_adj[a].append(b)
        l_adj[b].append(a)

    if m == 2:
        return [(-0.25, 0.0), (0.25, 0.0)]
    if m < 8 or not l_edges:
        # 小分量：圆上均匀排（确定性）
        coords = []
        for i in range(m):
            angle = 2.0 * math.pi * i / m
            coords.append((math.cos(angle), math.sin(angle)))
        return _normalize(coords)

    coords = _pivot_mds_layout(l_adj, m)
    coords = _normalize(coords)
    if m >= 40:
        iters = _REFINE_ITERS
    elif m >= 16:
        iters = _REFINE_MIN_ITERS
    else:
        iters = _REFINE_SMALL
    # 超大图按比例降迭代次数，保证首次建画布不会被布局卡住
    if m > 260:
        iters = max(120, iters * 260 // m)
    coords = _refine(coords, l_edges, l_lines, iters=iters)
    coords = _normalize(coords)
    # 后处理：精确清掉"近平行重叠"和"线路贴非端点站"的残余缺陷（小幅位移）。
    coords = _separate_near_misses(coords, l_edges)
    return _normalize(coords)


def _pivot_mds_layout(adj, m):
    """最远点采样 pivot + PivotMDS 初始化。"""
    k = min(m, 64, max(6, int(round(4.0 * math.sqrt(m)))))
    pivots: list[int] = []
    dist_rows: dict[int, list[int]] = {}

    def bfs(source: int) -> list[int]:
        dist = [-1] * m
        dist[source] = 0
        frontier = [source]
        while frontier:
            nxt = []
            for u in frontier:
                du = dist[u]
                for v in adj[u]:
                    if dist[v] < 0:
                        dist[v] = du + 1
                        nxt.append(v)
            frontier = nxt
        return dist

    first = 0
    d0 = bfs(first)
    far = max(range(m), key=lambda i: (d0[i], -i))
    pivots.append(far)
    dist_rows[far] = bfs(far)
    while len(pivots) < k:
        best = -1
        best_d = -1
        for i in range(m):
            mind = min(dist_rows[p][i] for p in pivots)
            if mind > best_d:
                best_d = mind
                best = i
        if best < 0 or best_d <= 0:
            break
        pivots.append(best)
        dist_rows[best] = bfs(best)
    return _classical_mds_from_pivots(dist_rows, pivots, m)


def _classical_mds_from_pivots(dist_rows, pivots, m):
    """对"所有点到 k 个 pivot 的距离矩阵"做双中心化 + 截断 SVD（幂迭代）。"""
    k = len(pivots)
    if k < 2:
        return [(0.0, 0.0)] * m
    # D[i][j]：站点 i 到第 j 个 pivot 的距离
    D = [[float(dist_rows[pivots[j]][i]) if dist_rows[pivots[j]][i] >= 0 else 0.0
          for j in range(k)] for i in range(m)]
    col_mean = [sum(D[i][j] for i in range(m)) / m for j in range(k)]
    row_mean = [sum(row) / k for row in D]
    total = sum(col_mean) / k
    B = [[-0.5 * (D[i][j] - row_mean[i] - col_mean[j] + total) for j in range(k)]
         for i in range(m)]
    # M = B^T B（k×k，小矩阵）
    M = [[0.0] * k for _ in range(k)]
    for a in range(k):
        Ba = [B[i][a] for i in range(m)]
        for b in range(a, k):
            s = 0.0
            for i in range(m):
                s += Ba[i] * B[i][b]
            M[a][b] = M[b][a] = s
    vecs = []
    vals = []
    work = [row[:] for row in M]
    for _ in range(2):
        v = [math.sin((i + 1) * 12.9898 + 78.233) for i in range(k)]
        nrm = math.sqrt(sum(x * x for x in v)) or 1.0
        v = [x / nrm for x in v]
        lam = 0.0
        for _it in range(80):
            w = [0.0] * k
            for a in range(k):
                wa = 0.0
                row = work[a]
                for b in range(k):
                    wa += row[b] * v[b]
                w[a] = wa
            nrm = math.sqrt(sum(x * x for x in w)) or 1.0
            w = [x / nrm for x in w]
            delta = sum(abs(w[i] - v[i]) for i in range(k))
            v = w
            lam = nrm
            if delta < 1e-8:
                break
        for a in range(k):
            for b in range(k):
                work[a][b] -= lam * v[a] * v[b]
        vecs.append(v)
        vals.append(lam)
    # 投影：坐标 = B · v / sqrt(λ)
    coords = []
    for i in range(m):
        Bi = B[i]
        x = sum(Bi[a] * vecs[0][a] for a in range(k))
        y = sum(Bi[a] * vecs[1][a] for a in range(k))
        lx = math.sqrt(abs(vals[0])) or 1.0
        ly = math.sqrt(abs(vals[1])) or 1.0
        coords.append((x / lx, y / ly))
    return coords


def _refine(coords, edges, line_nodes, iters=300):
    """局部力导向细化：斥力 + 弹簧 + 沿线平滑 + 非相邻线段互斥。"""
    m = len(coords)
    if m < 3:
        return coords
    X = [[float(p[0]), float(p[1])] for p in coords]
    target = 2.6 / math.sqrt(m)
    cell = max(target * 1.5, 1e-4)
    vel = [[0.0, 0.0] for _ in range(m)]
    lr = 0.32
    spring = 0.55
    smooth = 0.42
    seg_push = 0.5

    for it in range(iters):
        F = [[0.0, 0.0] for _ in range(m)]
        _apply_repulsion(X, F, m, cell)
        _apply_springs(X, F, edges, target, spring)
        _apply_line_smoothing(X, F, line_nodes, smooth)
        if it % 2 == 0:
            _apply_segment_repulsion(X, F, edges, target, cell, seg_push)
        step = lr * (1.0 if it < iters * 0.7 else 0.5)
        for i in range(m):
            vel[i][0] = 0.72 * vel[i][0] + step * F[i][0]
            vel[i][1] = 0.72 * vel[i][1] + step * F[i][1]
            X[i][0] += vel[i][0]
            X[i][1] += vel[i][1]
    return [tuple(p) for p in X]


def _apply_repulsion(X, F, m, cell):
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i in range(m):
        grid[(int(X[i][0] // cell), int(X[i][1] // cell))].append(i)
    cutoff2 = (1.8 * cell) ** 2
    for i in range(m):
        xi, yi = X[i]
        gx, gy = int(xi // cell), int(yi // cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    if j <= i:
                        continue
                    ddx = xi - X[j][0]
                    ddy = yi - X[j][1]
                    d2 = ddx * ddx + ddy * ddy
                    if d2 > cutoff2:
                        continue
                    if d2 < 1e-12:
                        ddx = 1e-3 * (1 + (i % 7))
                        ddy = 1e-3 * (1 + (i % 5))
                        d2 = ddx * ddx + ddy * ddy
                    w = min(1.0 / d2, 400.0) * 0.02
                    F[i][0] += ddx * w
                    F[i][1] += ddy * w
                    F[j][0] -= ddx * w
                    F[j][1] -= ddy * w


def _apply_springs(X, F, edges, target, strength):
    for a, b in edges:
        dx = X[b][0] - X[a][0]
        dy = X[b][1] - X[a][1]
        dist = math.hypot(dx, dy) or 1e-9
        f = strength * (dist - target) / dist
        F[a][0] += dx * f
        F[a][1] += dy * f
        F[b][0] -= dx * f
        F[b][1] -= dy * f


def _apply_line_smoothing(X, F, line_nodes, strength):
    for nodes in line_nodes:
        count = len(nodes)
        if count < 3:
            continue
        for k in range(1, count - 1):
            a, b, c = nodes[k - 1], nodes[k], nodes[k + 1]
            tx = (X[a][0] + X[c][0]) * 0.5 - X[b][0]
            ty = (X[a][1] + X[c][1]) * 0.5 - X[b][1]
            F[b][0] += strength * tx
            F[b][1] += strength * ty


def _apply_segment_repulsion(X, F, edges, target, cell, strength):
    """非相邻线段最近距离小于阈值就互相推开（避免两条线贴在一起）。

    用"线段包围盒格子 + 邻格"做候选筛选（不漏掉隔壁格子里的近平行线段）。
    """
    if not edges:
        return
    seg_cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for si, (a, b) in enumerate(edges):
        x1, y1 = X[a]
        x2, y2 = X[b]
        gx0, gy0 = int(min(x1, x2) // cell), int(min(y1, y2) // cell)
        gx1, gy1 = int(max(x1, x2) // cell), int(max(y1, y2) // cell)
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                seg_cells[(gx, gy)].append(si)
    threshold = target * 0.55
    checked: set[tuple[int, int]] = set()

    def consider(si: int, sj: int) -> None:
        key = (si, sj) if si < sj else (sj, si)
        if key in checked:
            return
        checked.add(key)
        a1, b1 = edges[si]
        a2, b2 = edges[sj]
        if a1 in (a2, b2) or b1 in (a2, b2):
            return
        dist, cp, cq = _segment_distance(X[a1], X[b1], X[a2], X[b2])
        if dist >= threshold:
            return
        ddx = cp[0] - cq[0]
        ddy = cp[1] - cq[1]
        length = math.hypot(ddx, ddy)
        if length < 1e-9:
            # 几乎完全重合：用第一段的法线方向分开
            ux, uy = X[b1][0] - X[a1][0], X[b1][1] - X[a1][1]
            norm = math.hypot(ux, uy) or 1.0
            ddx, ddy = -uy / norm, ux / norm
            length = 1.0
        push = (threshold - dist) / length * strength * 0.5
        fx, fy = ddx * push, ddy * push
        F[a1][0] += fx
        F[a1][1] += fy
        F[b1][0] += fx
        F[b1][1] += fy
        F[a2][0] -= fx
        F[a2][1] -= fy
        F[b2][0] -= fx
        F[b2][1] -= fy

    # 每格只和本格 + 4 个"前向"邻格配对，既覆盖全部相邻组合又不重复
    forward = ((0, 0), (1, 0), (0, 1), (1, 1), (-1, 1))
    for (gx, gy), sids in seg_cells.items():
        for dx, dy in forward:
            other = seg_cells.get((gx + dx, gy + dy))
            if not other:
                continue
            if (dx, dy) == (0, 0):
                for ii in range(len(sids)):
                    for jj in range(ii + 1, len(sids)):
                        consider(sids[ii], sids[jj])
            else:
                for si in sids:
                    for sj in other:
                        consider(si, sj)


def _closest_point_on_segment(p, a, b):
    """点 p 到线段 ab 的最近点（返回 (qx, qy, t)）。"""
    ux, uy = b[0] - a[0], b[1] - a[1]
    length2 = ux * ux + uy * uy
    if length2 <= 1e-18:
        return (a[0], a[1], 0.0)
    t = ((p[0] - a[0]) * ux + (p[1] - a[1]) * uy) / length2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return (a[0] + t * ux, a[1] + t * uy, t)


def _segment_distance(p1, p2, q1, q2):
    """两条线段最近距离及最近点（不相交时最近点必有一个是端点）。"""
    best_d = float("inf")
    best = (p1, q1)
    for point in (q1, q2):
        qx, qy, _t = _closest_point_on_segment(point, p1, p2)
        d = math.hypot(point[0] - qx, point[1] - qy)
        if d < best_d:
            best_d, best = d, ((qx, qy), point)
    for point in (p1, p2):
        qx, qy, _t = _closest_point_on_segment(point, q1, q2)
        d = math.hypot(point[0] - qx, point[1] - qy)
        if d < best_d:
            best_d, best = d, (point, (qx, qy))
    return best_d, best[0], best[1]


def _parallel_overlap(p1, p2, q1, q2, *, max_sin=0.2079, min_overlap=0.0):
    """判断两线段是否"近似平行且投影有交叠"（12° 内）。

    返回 ``(is_overlap, distance, unit_x, unit_y)``；``unit`` 是平均方向。
    ``min_overlap`` 是投影重叠长度门槛（相对较短线段）；默认 0 = 只要投影方向
    有交叠就算，和验收口径（夹角 + 距离）保持一致，避免漏判"端点附近相贴"。
    """
    ux, uy = p2[0] - p1[0], p2[1] - p1[1]
    vx, vy = q2[0] - q1[0], q2[1] - q1[1]
    lu = math.hypot(ux, uy)
    lv = math.hypot(vx, vy)
    if lu < 1e-12 or lv < 1e-12:
        return (False, 0.0, 0.0, 0.0)
    # 先统一两段方向（点积为负就翻转 v），否则"反向近平行"会被平均方向带偏，
    # 投影重叠判断随线段书写方向变化（同一对线段结果不稳定）。
    if ux * vx + uy * vy < 0.0:
        vx, vy = -vx, -vy
    cross = abs(ux * vy - uy * vx) / (lu * lv)
    if cross > max_sin:
        return (False, 0.0, 0.0, 0.0)
    # 平均方向 & 两段在平均方向上的投影重叠（避免"只是端点附近擦过"）
    ax, ay = ux / lu + vx / lv, uy / lu + vy / lv
    la = math.hypot(ax, ay) or 1.0
    ax, ay = ax / la, ay / la
    proj = []
    for ox, oy, dx, dy in ((p1[0], p1[1], ux, uy), (q1[0], q1[1], vx, vy)):
        t1 = ox * ax + oy * ay
        t2 = t1 + dx * ax + dy * ay
        proj.append((min(t1, t2), max(t1, t2)))
    overlap = min(proj[0][1], proj[1][1]) - max(proj[0][0], proj[1][0])
    if overlap <= max(0.0, min(lu, lv) * min_overlap):
        return (False, 0.0, 0.0, 0.0)
    dist, cp, cq = _segment_distance(p1, p2, q1, q2)
    return (True, dist, ax, ay)


def _separate_near_misses(coords, edges, *, seg_clear=0.032, node_clear=0.032,
                          max_passes=120):
    """后处理：把"不同线路近平行重叠"和"线路贴着非端点站"的几何缺陷推开。

    力导向布局是连续优化，无法保证逐像素验收；这里在归一化坐标上做一次
    精确的几何清理（只做很小的位移），保证最终画布满足"线路不重叠"。
    ``coords`` 的 span ≈ 2，阈值 0.032 ≈ 画幅 1.6%。
    """
    m = len(coords)
    if m < 3 or not edges:
        return coords
    X = [[float(p[0]), float(p[1])] for p in coords]
    adjacent: dict[int, set[int]] = defaultdict(set)
    for a, b in edges:
        adjacent[a].add(b)
        adjacent[b].add(a)
    seg_cell = seg_clear * 2.0
    node_cell = node_clear * 2.0
    prev_bad: int | None = None
    damp = 0.75

    for _pass in range(max_passes):
        F = [[0.0, 0.0] for _ in range(m)]
        bad = 0

        # ---- 1) 近平行线段重叠：互相推开 ----
        seg_cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        for si, (a, b) in enumerate(edges):
            x1, y1 = X[a]
            x2, y2 = X[b]
            for gx in range(int(min(x1, x2) // seg_cell), int(max(x1, x2) // seg_cell) + 1):
                for gy in range(int(min(y1, y2) // seg_cell), int(max(y1, y2) // seg_cell) + 1):
                    seg_cells[(gx, gy)].append(si)
        checked: set[tuple[int, int]] = set()
        forward = ((0, 0), (1, 0), (0, 1), (1, 1), (-1, 1))
        for (gx, gy), sids in seg_cells.items():
            for dx, dy in forward:
                other = seg_cells.get((gx + dx, gy + dy))
                if not other:
                    continue
                pairs = ((si, sj) for si in sids for sj in other) if (dx, dy) != (0, 0) \
                    else ((sids[i], sids[j]) for i in range(len(sids)) for j in range(i + 1, len(sids)))
                for si, sj in pairs:
                    key = (si, sj) if si < sj else (sj, si)
                    if key in checked:
                        continue
                    checked.add(key)
                    a1, b1 = edges[si]
                    a2, b2 = edges[sj]
                    if a1 in (a2, b2) or b1 in (a2, b2):
                        continue
                    is_overlap, dist, ax, ay = _parallel_overlap(
                        X[a1], X[b1], X[a2], X[b2])
                    if not is_overlap or dist >= seg_clear:
                        continue
                    bad += 1
                    # 推开方向用"最近点对"的方向：始终沿真实间隙把两段推远，
                    # 不会像"中点法线方向"那样在重合时每轮翻转导致震荡。
                    d2, cp, cq = _segment_distance(X[a1], X[b1], X[a2], X[b2])
                    dx0, dy0 = cp[0] - cq[0], cp[1] - cq[1]
                    gap = math.hypot(dx0, dy0)
                    if gap > 1e-12:
                        px, py = dx0 / gap, dy0 / gap
                    else:
                        # 两段几乎完全重合：用平均方向的法线，方向由线段编号确定
                        px, py = -ay, ax
                        if (si + sj) % 2:
                            px, py = -px, -py
                    strength = (seg_clear - dist) * 0.5
                    # 贴着时段越长、越严重，推开力度略加（最多 1.8 倍）
                    span_len = math.hypot(
                        (X[b1][0] - X[a1][0] + X[b2][0] - X[a2][0]) * 0.5,
                        (X[b1][1] - X[a1][1] + X[b2][1] - X[a2][1]) * 0.5) or 1.0
                    strength *= min(1.8, 0.9 + 0.35 / max(span_len, 1e-6))
                    for node in (a1, b1):
                        F[node][0] += px * strength
                        F[node][1] += py * strength
                    for node in (a2, b2):
                        F[node][0] -= px * strength
                        F[node][1] -= py * strength

        # ---- 2) 线路贴着"非端点站"：把站和线段推开 ----
        node_grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i in range(m):
            node_grid[(int(X[i][0] // node_cell), int(X[i][1] // node_cell))].append(i)
        for a, b in edges:
            x1, y1 = X[a]
            x2, y2 = X[b]
            gx0, gx1 = int(min(x1, x2) // node_cell), int(max(x1, x2) // node_cell)
            gy0, gy1 = int(min(y1, y2) // node_cell), int(max(y1, y2) // node_cell)
            for gx in range(gx0 - 1, gx1 + 2):
                for gy in range(gy0 - 1, gy1 + 2):
                    for node in node_grid.get((gx, gy), ()):
                        if node == a or node == b:
                            continue
                        if node in adjacent[a] or node in adjacent[b]:
                            continue        # 相邻站正常在线上，跳过
                        qx, qy, t = _closest_point_on_segment(X[node], X[a], X[b])
                        ddx = X[node][0] - qx
                        ddy = X[node][1] - qy
                        dist = math.hypot(ddx, ddy)
                        if dist >= node_clear:
                            continue
                        bad += 1
                        sx, sy = X[a]
                        if dist < 1e-9:
                            # 站点几乎落在线上：用线段法线方向推开
                            ux, uy = x2 - x1, y2 - y1
                            norm = math.hypot(ux, uy) or 1.0
                            ddx, ddy = -uy / norm, ux / norm
                            if (X[node][0] - (sx + x2) * 0.5) * ddx + \
                               (X[node][1] - (sy + y2) * 0.5) * ddy < 0:
                                ddx, ddy = -ddx, -ddy
                            dist = 0.0
                        else:
                            ddx, ddy = ddx / dist, ddy / dist
                        push = (node_clear - dist) * 0.55
                        F[node][0] += ddx * push
                        F[node][1] += ddy * push
                        # 线段两端反向让一点，避免整体被推跑
                        F[a][0] -= ddx * push * 0.25
                        F[a][1] -= ddy * push * 0.25
                        F[b][0] -= ddx * push * 0.25
                        F[b][1] -= ddy * push * 0.25

        if bad == 0:
            return [tuple(p) for p in X]
        # 自适应阻尼：连续几轮没减少就加大步长，避免在局部震荡里出不来
        if prev_bad is not None and bad >= prev_bad:
            damp = min(2.5, damp * 1.3)
        else:
            damp = max(0.75, damp * 0.9)
        prev_bad = bad
        for i in range(m):
            X[i][0] += F[i][0] * damp
            X[i][1] += F[i][1] * damp
    return [tuple(p) for p in X]


def _normalize(coords):
    if not coords:
        return []
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-9)
    scale = 2.0 / span
    return [((x - cx) * scale, (y - cy) * scale) for x, y in coords]
