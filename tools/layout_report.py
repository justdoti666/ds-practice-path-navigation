#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""显示层布局几何验证工具（T2 交付，纯标准库、不依赖 Tkinter）。

用途
----
读一个 ``path-nav`` 图 JSON（``core.graph.load_graph`` 加载），分别用

* **before（数据坐标）**：``graph.stations[*].x/y`` —— 算法/数据层真实坐标；
* **after（显示坐标）**：``ui.display_layout.station_positions(graph, line_sequences)``
  —— 画布实际绘制坐标（``--layout-source display|auto`` 时才有），

度量几何质量，输出 JSON + Markdown 对比报告到 ``_reports/``。

度量口径（阈值可在命令行调整，报告中会写明实际值）
--------------------------------------------------
屏幕像素等效：假定布局的 bbox 最长边映射到画布约 ``--ref-px``（默认 800）像素，
于是 ``1 屏幕像素 ≈ span / ref_px`` 个坐标单位；所有"看不清"阈值都折算成像素再
换成坐标单位，因此数据坐标（km）与显示坐标（|coord|≤1）可以**同口径**比较。

* **重合线段（overlap）**：两段近似共线（端点到对方直线的最大距离 ≤
  ``--collinear-px`` 像素等效），且沿线段方向的投影重叠长度 ≥ ``--overlap-min-px``
  像素等效。线路在换乘站处共享一个端点（只接触、不共线重叠）不算。
* **近重合线段（near_overlap）**：不满足"重合"定义，但两段最近距离 ≤
  ``max(--near-px 像素等效, --near-pct × 中位边长)``，且任一段有 ≥
  ``--near-frac``（默认 20%）的长度落在对方 ``--near-px`` 范围内 —— 即视觉上贴成
  一条线。建议口径：``--near-pct 2%``（按平均站间距归一化）。
* **交叉（crossing）**：两段严格相交（交点在两段内部；共享端点、T 形接触不算）。
* **站点坐标重合**：两个不同站点距离 ≤ ``--station-near-px`` 像素等效。
* **线段穿过非端点站点**：线段内部（离两端均 > 2 像素等效）经过某个非端点站点
  ``--through-px`` 像素范围内。
* **最小站间距 / 中位边长 / 超长边比例**：超长边 = 长度 > 4× 中位边长的线段。
* **并行走廊（corridor，已知限制）**：两条不同线路共用**同一对**站点（如 2/7 号线
  广州南站-石壁），画布上两段必然完全重合，任何"站点坐标"布局都无法分开。本工具
  单独列为 ``corridor_pairs``，**不计入**非相邻线段重合失败，但会在报告中提示。

用法
----
    # 默认：real_metro / stress_164 / demo_city，auto 选择显示坐标
    python tools/layout_report.py

    # 只测数据坐标（无显示层时的基线）
    python tools/layout_report.py --layout-source data

    # 指定数据集 + 输出路径
    python tools/layout_report.py --data data/real_metro.json --data data/demo_city.json \
        --md _reports/layout_report.md --json _reports/layout_metrics.json

    # 作为质量门：任一数据集验收失败则退出码 1
    python tools/layout_report.py --fail-on-acceptance

退出码：0 正常；1 验收失败（仅 --fail-on-acceptance 时）；2 输入/环境错误。
"""
from __future__ import annotations

import argparse
import html as _html
import json
import math
import re
import sys
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------- 路径
ROOT = Path(__file__).resolve().parent.parent            # path-nav/
WORKSPACE = ROOT.parent                                  # 工作区根
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.graph import load_graph                        # noqa: E402

DEFAULT_REPORTS_DIR = WORKSPACE / "_reports"
DEFAULT_DATASETS = [
    ROOT / "data" / "real_metro.json",
    ROOT / "data" / "stress_164.json",
    ROOT / "data" / "demo_city.json",
]

#: 屏幕像素等效假设：布局 bbox 最长边 ≈ 画布 800 px。
DEFAULT_REF_PX = 800.0
DEFAULT_NEAR_PX = 3.0          # "近重合"最近距离阈值（像素）
DEFAULT_NEAR_PCT = 0.02        # 近重合阈值下限：中位边长的 2%
DEFAULT_OVERLAP_MIN_PX = 2.0   # 共线投影重叠长度下限（像素）
DEFAULT_COLLINEAR_PX = 0.5     # 判定共线的横向偏差上限（像素）
DEFAULT_STATION_NEAR_PX = 1.5  # 站点近重合阈值（像素）
DEFAULT_THROUGH_PX = 2.0       # 线段穿过站点阈值（像素）
DEFAULT_NEAR_FRACTION = 0.2    # 近重合：任一段落在阈值内的长度占比下限
DEFAULT_JUNCTION_EXCLUDE_PX = 5.0  # 换乘站汇入豁免区：距共享站 <5px 的贴近属正常汇入

#: Lead 统一的「并行重叠」缺陷口径（归一化坐标，span=2，即 |coord|≤1）：
#: 不同线路、不共享站点的两段，夹角 < 12° 且最近距离 < 0.02 → 并行重叠（必须 0）。
DEFAULT_PARALLEL_ANGLE_DEG = 12.0
DEFAULT_PARALLEL_DIST_NORM = 0.02

#: 并行走廊（不同线路共用同一对相邻站）的策略：
#:   known = 视为允许项（渲染层已做并行偏移、视觉分开），报告中单列清单；
#:   defect = 视为待修缺陷（几何坐标层面要求归零）。
CORRIDOR_POLICIES = ("known", "defect")
DEFAULT_CORRIDOR_POLICY = "known"

#: 验收时 200 站以内布局耗时的报告上限（PLAN 口径：169 站 ≤ 1.5s）
LAYOUT_TIME_LIMIT_S = 1.5


# ======================================================================
# 1. 线路站序（与 ui/canvas.py::_line_stations、display_layout 同口径）
# ======================================================================
def reconstruct_line_sequences(graph) -> "OrderedDict[str, list[str]]":
    """从 edges 还原每条线的有序站序 ``{lid: [sid, ...]}``。

    与 ``ui/canvas.py::_line_stations`` 相同的走法：取端点（度=1）排序后从最小端点
    沿唯一邻居走；无端点（环线）从最小节点走回起点。只保留 ≥ 2 站的线。
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

    result: "OrderedDict[str, list[str]]" = OrderedDict()
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
        seen: set[str] = set()
        prev = None
        cur = start
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
# 2. 几何基元
# ======================================================================
class Segment:
    """一条线路的一段（相邻两站之间），带坐标与端点。"""

    __slots__ = ("line", "u", "v", "x1", "y1", "x2", "y2", "length")

    def __init__(self, line, u, v, p, q):
        self.line = line
        self.u = u
        self.v = v
        self.x1, self.y1 = p
        self.x2, self.y2 = q
        self.length = math.hypot(self.x2 - self.x1, self.y2 - self.y1)

    @property
    def bbox(self):
        return (min(self.x1, self.x2), min(self.y1, self.y2),
                max(self.x1, self.x2), max(self.y1, self.y2))

    def other(self, sid):
        return self.v if sid == self.u else self.u


def _orient(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _point_seg_dist2(px, py, ax, ay, bx, by) -> tuple[float, float]:
    """点到线段距离平方 + 投影参数 t∈[0,1]。"""
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 <= 0.0:
        return (px - ax) ** 2 + (py - ay) ** 2, 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / l2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    qx, qy = ax + dx * t, ay + dy * t
    return (px - qx) ** 2 + (py - qy) ** 2, t


def _proper_intersect(s1: Segment, s2: Segment) -> bool:
    """严格内部相交（共享端点 / T 形接触 / 共线都不算）。"""
    ax, ay, bx, by = s1.x1, s1.y1, s1.x2, s1.y2
    cx, cy, dx_, dy_ = s2.x1, s2.y1, s2.x2, s2.y2
    d1 = _orient(ax, ay, bx, by, cx, cy)
    d2 = _orient(ax, ay, bx, by, dx_, dy_)
    d3 = _orient(cx, cy, dx_, dy_, ax, ay)
    d4 = _orient(cx, cy, dx_, dy_, bx, by)
    eps = 1e-12
    return ((d1 > eps) != (d2 > eps)) and ((d3 > eps) != (d4 > eps)) \
        and d1 != 0.0 and d2 != 0.0 and d3 != 0.0 and d4 != 0.0


def _seg_seg_min_dist2(s1: Segment, s2: Segment, proper: bool) -> float:
    if proper:
        return 0.0
    best = min(
        _point_seg_dist2(s1.x1, s1.y1, s2.x1, s2.y1, s2.x2, s2.y2)[0],
        _point_seg_dist2(s1.x2, s1.y2, s2.x1, s2.y1, s2.x2, s2.y2)[0],
        _point_seg_dist2(s2.x1, s2.y1, s1.x1, s1.y1, s1.x2, s1.y2)[0],
        _point_seg_dist2(s2.x2, s2.y2, s1.x1, s1.y1, s1.x2, s1.y2)[0],
    )
    return max(best, 0.0)


def _collinear_overlap(s1: Segment, s2: Segment, tol: float) -> tuple[float, float]:
    """近似共线判定；返回（投影重叠长度, 最大横向偏差）。不共线返回 (0, 偏差)。"""
    if s1.length <= 1e-12 or s2.length <= 1e-12:
        return 0.0, float("inf")
    ax, ay = s1.x1, s1.y1
    bax, bay = s1.x2 - s1.x1, s1.y2 - s1.y1
    dev_c = abs(_orient(ax, ay, s1.x2, s1.y2, s2.x1, s2.y1)) / s1.length
    dev_d = abs(_orient(ax, ay, s1.x2, s1.y2, s2.x2, s2.y2)) / s1.length
    cx, cy = s2.x1, s2.y1
    dcx, dcy = s2.x2 - s2.x1, s2.y2 - s2.y1
    dev_a = abs(_orient(cx, cy, s2.x2, s2.y2, s1.x1, s1.y1)) / s2.length
    dev_b = abs(_orient(cx, cy, s2.x2, s2.y2, s1.x2, s1.y2)) / s2.length
    max_dev = max(dev_a, dev_b, dev_c, dev_d)
    if max_dev > tol:
        return 0.0, max_dev
    l2 = bax * bax + bay * bay
    t_c = ((s2.x1 - ax) * bax + (s2.y1 - ay) * bay) / l2
    t_d = ((s2.x2 - ax) * bax + (s2.y2 - ay) * bay) / l2
    lo = max(0.0, min(t_c, t_d))
    hi = min(1.0, max(t_c, t_d))
    return max(0.0, hi - lo) * s1.length, max_dev


def _sample_dists(src: Segment, dst: Segment, samples: int) -> list[tuple[float, float]]:
    """在 src 上均匀采样，返回 [(t, 到 dst 的距离), ...]。"""
    out: list[tuple[float, float]] = []
    for k in range(samples):
        t = k / (samples - 1)
        px = src.x1 + (src.x2 - src.x1) * t
        py = src.y1 + (src.y2 - src.y1) * t
        d2, _ = _point_seg_dist2(px, py, dst.x1, dst.y1, dst.x2, dst.y2)
        out.append((t, math.sqrt(max(d2, 0.0))))
    return out


def _profile_fracs(dists, shared_dists, tol: float, exclude_len: float) -> tuple[float, float]:
    """返回（全体采样中 ≤tol 的占比, 距共享站 >exclude_len 的采样中 ≤tol 的占比）。"""
    n = len(dists)
    if not n:
        return 0.0, 0.0
    hits = sum(1 for d in dists if d <= tol)
    far_hits = far_total = 0
    for d, sd in zip(dists, shared_dists):
        if sd >= exclude_len:
            far_total += 1
            if d <= tol:
                far_hits += 1
    return hits / n, (far_hits / far_total if far_total else 0.0)


def _near_profile(s1: Segment, s2: Segment, tols: list[float], *,
                  shared_xy=None, exclude_len: float = 0.0,
                  samples: int = 33) -> dict:
    """多阈值近重合剖面。

    返回 ``{tol: (all_frac, far_frac)}``；``shared_xy`` 不为 None 时（两段共享一个
    换乘站），``far_frac`` 只统计距共享站 ≥ ``exclude_len`` 的采样——换乘站附近
    两条线汇入同一个点是正常画法，不算"并线压盖"。
    """
    if samples < 2:
        samples = 2
    prof: dict = {}
    d_ab = _sample_dists(s1, s2, samples)
    d_ba = _sample_dists(s2, s1, samples)
    if shared_xy is not None:
        sx, sy = shared_xy

        def shared_dist(seg: Segment) -> list[float]:
            out = []
            for k in range(samples):
                t = k / (samples - 1)
                px = seg.x1 + (seg.x2 - seg.x1) * t
                py = seg.y1 + (seg.y2 - seg.y1) * t
                out.append(math.hypot(px - sx, py - sy))
            return out

        sd_ab = shared_dist(s1)
        sd_ba = shared_dist(s2)
    else:
        inf = float("inf")
        sd_ab = [inf] * samples
        sd_ba = [inf] * samples
    for tol in tols:
        all_a, far_a = _profile_fracs([d for _, d in d_ab], sd_ab, tol, exclude_len)
        all_b, far_b = _profile_fracs([d for _, d in d_ba], sd_ba, tol, exclude_len)
        prof[tol] = (max(all_a, all_b), max(far_a, far_b))
    return prof


def _bbox_gap(s1: Segment, s2: Segment) -> float:
    """两个线段 bbox 的最小间隙（不相交时 > 0）。"""
    a = s1.bbox
    b = s2.bbox
    dx = max(0.0, max(a[0] - b[2], b[0] - a[2]))
    dy = max(0.0, max(a[1] - b[3], b[1] - a[3]))
    return math.hypot(dx, dy)


# ======================================================================
# 3. 阈值 / 站点表 / 度量
# ======================================================================
def station_table(graph) -> dict[str, tuple[str, float, float]]:
    """``{sid: (name, data_x, data_y)}``，只读。"""
    table: dict[str, tuple[str, float, float]] = {}
    for sid, st in (getattr(graph, "stations", None) or {}).items():
        name = str(getattr(st, "name", sid))
        try:
            x, y = float(getattr(st, "x")), float(getattr(st, "y"))
        except (TypeError, ValueError):
            continue
        table[str(sid)] = (name, x, y)
    return table


def normalize_positions(pos) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for sid, xy in (pos or {}).items():
        try:
            x, y = float(xy[0]), float(xy[1])
        except (TypeError, ValueError, IndexError):
            continue
        out[str(sid)] = (x, y)
    return out


def build_segments(positions, line_sequences) -> list[Segment]:
    segs: list[Segment] = []
    for lid, seq in line_sequences.items():
        for a, b in zip(seq, seq[1:]):
            if a not in positions or b not in positions:
                continue
            segs.append(Segment(lid, a, b, positions[a], positions[b]))
    return segs


def unit_span_positions(positions) -> dict[str, tuple[float, float]]:
    """把坐标等比缩放到 ``|coord| ≤ 1``（span=2，与 display_layout 的归一化一致）。

    用于 Lead 统一口径的「并行重叠」判定：距离阈值 0.02 直接是归一化坐标单位。
    """
    if not positions:
        return {}
    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    span = max(max(xs) - min(xs), max(ys) - min(ys), 0.0)
    if span <= 0:
        return {sid: (0.0, 0.0) for sid in positions}
    scale = 2.0 / span
    return {sid: ((x - cx) * scale, (y - cy) * scale)
            for sid, (x, y) in positions.items()}


def _angle_deg(s1: Segment, s2: Segment) -> float:
    """两段方向的锐角夹角（0~90°）。"""
    v1x, v1y = s1.x2 - s1.x1, s1.y2 - s1.y1
    v2x, v2y = s2.x2 - s2.x1, s2.y2 - s2.y1
    n1 = math.hypot(v1x, v1y)
    n2 = math.hypot(v2x, v2y)
    if n1 <= 0 or n2 <= 0:
        return 0.0
    cosv = abs(v1x * v2x + v1y * v2y) / (n1 * n2)
    cosv = 1.0 if cosv > 1.0 else (0.0 if cosv < 0.0 else cosv)
    return math.degrees(math.acos(cosv))


def parallel_overlap_metrics(positions, line_sequences, names,
                             angle_deg: float = DEFAULT_PARALLEL_ANGLE_DEG,
                             dist_norm: float = DEFAULT_PARALLEL_DIST_NORM,
                             detail_limit: int = 60) -> dict:
    """Lead 统一口径：不同线路、不共享站点、**非严格相交(proper crossing)** 的两段，
    夹角 < 12° 且最近距离 < 0.02（归一化坐标）→ 并行重叠（必须 0）。

    proper crossing（线路正常交叉）属允许画法，不计入并行重叠。

    另外统计：
      * ``min_nonadjacent_segment_pair``：非相邻（不共享站、不属同站对走廊）线段中
        **不相交** 的最近距离对（含夹角），供报告"最近贴近"证据；
      * ``corridor_pairs``：不同线路共用同一对相邻站的走廊（单列，不算缺陷）。
    """
    norm = unit_span_positions(positions)
    segs = build_segments(norm, line_sequences)
    defects: list[dict] = []
    min_pair = None
    close_count = 0
    for i in range(len(segs)):
        s1 = segs[i]
        for j in range(i + 1, len(segs)):
            s2 = segs[j]
            if s1.line == s2.line:
                continue
            shared = {s1.u, s1.v} & {s2.u, s2.v}
            if shared:
                continue                      # 共享换乘站：相邻/接触，允许
            proper = _proper_intersect(s1, s2)
            angle = _angle_deg(s1, s2)
            dmin = math.sqrt(max(_seg_seg_min_dist2(s1, s2, proper), 0.0))
            rec = {
                "line_a": s1.line, "line_b": s2.line,
                "seg_a": [s1.u, s1.v], "seg_b": [s2.u, s2.v],
                "name_a": [names.get(s1.u, ""), names.get(s1.v, "")],
                "name_b": [names.get(s2.u, ""), names.get(s2.v, "")],
                "angle_deg": angle, "dist_norm": dmin, "proper_crossing": proper,
            }
            if dmin < dist_norm:
                close_count += 1
            if not proper:
                if min_pair is None or dmin < min_pair["dist_norm"]:
                    min_pair = dict(rec)
            # 正常交叉（proper crossing）允许，不算并行重叠
            if not proper and angle < angle_deg and dmin < dist_norm:
                defects.append(rec)
    defects.sort(key=lambda r: (r["dist_norm"], r["angle_deg"]))
    for r in defects:
        r["angle_deg"] = round(r["angle_deg"], 2)
        r["dist_norm"] = round(r["dist_norm"], 6)
    if min_pair:
        min_pair["angle_deg"] = round(min_pair["angle_deg"], 2)
        min_pair["dist_norm"] = round(min_pair["dist_norm"], 6)
    return {
        "parallel_overlap_pairs": len(defects),
        "parallel_overlap_details": defects[:detail_limit],
        "parallel_angle_deg": angle_deg,
        "parallel_dist_norm": dist_norm,
        "close_nonadjacent_pairs_below_dist": close_count,
        "min_nonadjacent_segment_pair": min_pair,
    }


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def derive_thresholds(positions, seg_lengths, *, ref_px: float = DEFAULT_REF_PX,
                      near_px: float = DEFAULT_NEAR_PX,
                      near_pct: float = DEFAULT_NEAR_PCT,
                      overlap_min_px: float = DEFAULT_OVERLAP_MIN_PX,
                      collinear_px: float = DEFAULT_COLLINEAR_PX,
                      station_near_px: float = DEFAULT_STATION_NEAR_PX,
                      through_px: float = DEFAULT_THROUGH_PX,
                      near_fraction: float = DEFAULT_NEAR_FRACTION,
                      junction_exclude_px: float = DEFAULT_JUNCTION_EXCLUDE_PX) -> dict:
    """按当前坐标空间的尺度换算像素等效阈值（详见模块 docstring）。"""
    if positions:
        xs = [p[0] for p in positions.values()]
        ys = [p[1] for p in positions.values()]
        span = max(max(xs) - min(xs), max(ys) - min(ys), 0.0)
    else:
        span = 0.0
    median_edge = _median(seg_lengths)
    unit_per_px = (span / ref_px) if span > 0 else 0.0
    near_tol = max(near_px * unit_per_px, near_pct * median_edge, 1e-12)
    return {
        "ref_px": ref_px,
        "span": span,
        "median_edge": median_edge,
        "unit_per_px": unit_per_px,
        "near_px": near_px,
        "near_pct": near_pct,
        "near_tol": near_tol,
        "near_tol_pct_of_median_edge": (near_tol / median_edge) if median_edge > 0 else 0.0,
        "overlap_min_len": max(overlap_min_px * unit_per_px, 1e-12),
        "collinear_tol": max(collinear_px * unit_per_px, 1e-12),
        "station_near_tol": max(station_near_px * unit_per_px, 1e-12),
        "through_tol": max(through_px * unit_per_px, 1e-12),
        "through_end_margin": max(2.0 * unit_per_px, 1e-12),
        "near_fraction": near_fraction,
        "junction_exclude_px": junction_exclude_px,
        "junction_exclude_len": max(junction_exclude_px * unit_per_px, 1e-12),
        "scan_px": max(near_px, 5.0),
    }


def _components(graph, positions, segments) -> list[int]:
    parent: dict[str, str] = {sid: sid for sid in graph.stations}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for seg in segments:
        if seg.u in parent and seg.v in parent:
            union(seg.u, seg.v)
    buckets: dict[str, int] = {}
    for sid in graph.stations:
        root = find(sid)
        buckets[root] = buckets.get(root, 0) + 1
    return sorted(buckets.values(), reverse=True)


def _station_pairs(graph, positions, station_near_tol):
    """站点坐标重合（exact / near）、最小站间距。"""
    ids = list(positions)
    exact: list[dict] = []
    near: list[dict] = []
    names = {sid: st.name for sid, st in (getattr(graph, "stations", None) or {}).items()}
    best = (-1.0, None)
    tol2 = station_near_tol * station_near_tol
    for i in range(len(ids)):
        ax, ay = positions[ids[i]]
        for j in range(i + 1, len(ids)):
            bx, by = positions[ids[j]]
            d2 = (ax - bx) ** 2 + (ay - by) ** 2
            if best[1] is None or d2 < best[0]:
                best = (d2, [ids[i], ids[j]])
            if d2 <= 1e-18:
                exact.append({"a": ids[i], "b": ids[j], "name_a": names.get(ids[i], ""),
                              "name_b": names.get(ids[j], "")})
            elif d2 <= tol2:
                near.append({"a": ids[i], "b": ids[j], "dist": math.sqrt(d2),
                             "name_a": names.get(ids[i], ""),
                             "name_b": names.get(ids[j], "")})
    min_pair = None
    if best[1] is not None:
        min_pair = {"a": best[1][0], "b": best[1][1], "dist": math.sqrt(max(best[0], 0.0)),
                    "name_a": names.get(best[1][0], ""),
                    "name_b": names.get(best[1][1], "")}
    return exact, near, min_pair


def _through_stations(segments, positions, names, through_tol, end_margin) -> list[dict]:
    out: list[dict] = []
    tol2 = through_tol * through_tol
    for seg in segments:
        if seg.length <= 1e-12:
            continue
        for sid, (px, py) in positions.items():
            if sid == seg.u or sid == seg.v:
                continue
            d2, t = _point_seg_dist2(px, py, seg.x1, seg.y1, seg.x2, seg.y2)
            if d2 > tol2:
                continue
            if t * seg.length <= end_margin or (1.0 - t) * seg.length <= end_margin:
                continue
            out.append({"line": seg.line, "u": seg.u, "v": seg.v,
                        "station": sid, "dist": math.sqrt(max(d2, 0.0)),
                        "name_u": names.get(seg.u, ""), "name_v": names.get(seg.v, ""),
                        "name_station": names.get(sid, "")})
    return out


def _classify_pairs(segments, thresholds, names) -> dict:
    """线段两两分类：重合 / 近重合 / 交叉 / 正常接触。"""
    near_tol = thresholds["near_tol"]
    scan_tol = max(near_tol, thresholds["scan_px"] * thresholds["unit_per_px"], 1e-12)
    collinear_tol = thresholds["collinear_tol"]
    overlap_min = thresholds["overlap_min_len"]
    frac_min = thresholds["near_fraction"]
    exclude_len = thresholds["junction_exclude_len"]
    sens_px = (1.0, 2.0, 3.0, 5.0)

    records = []
    for i in range(len(segments)):
        s1 = segments[i]
        for j in range(i + 1, len(segments)):
            s2 = segments[j]
            same_line = s1.line == s2.line
            shared = {s1.u, s1.v} & {s2.u, s2.v}
            if same_line and shared:
                continue                      # 同线相邻段：正常，跳过
            gap = _bbox_gap(s1, s2)
            if gap > scan_tol:
                continue
            proper = _proper_intersect(s1, s2)
            overlap_len, max_dev = _collinear_overlap(s1, s2, collinear_tol)
            dmin = math.sqrt(max(_seg_seg_min_dist2(s1, s2, proper), 0.0))
            same_pair = (s1.u == s2.u and s1.v == s2.v) or \
                        (s1.u == s2.v and s1.v == s2.u)
            shared_station = None
            shared_xy = None
            if len(shared) == 1 and not same_pair:
                shared_station = next(iter(shared))
                shared_xy = (s1.x1, s1.y1) if shared_station == s1.u else (s1.x2, s1.y2)
            rec = {
                "i": i, "j": j, "proper": proper,
                "same_line": same_line, "same_pair": same_pair,
                "shared_station": shared_station, "shared_xy": shared_xy,
                "overlap_len": overlap_len, "max_dev": max_dev,
                "dmin": dmin, "near_all": 0.0, "near_far": 0.0, "sens": {},
                "line_a": s1.line, "line_b": s2.line,
                "ua": s1.u, "va": s1.v, "ub": s2.u, "vb": s2.v,
            }
            if dmin <= scan_tol and overlap_len < overlap_min:
                unit = thresholds["unit_per_px"]
                tols = sorted(set([near_tol] + [px * unit for px in sens_px]))
                prof = _near_profile(s1, s2, tols, shared_xy=shared_xy,
                                     exclude_len=exclude_len)
                rec["near_all"], rec["near_far"] = prof[near_tol]
                rec["sens"] = {px: prof[px * unit] for px in sens_px}
            records.append(rec)

    def detail(r):
        return {
            "line_a": r["line_a"], "line_b": r["line_b"],
            "seg_a": [r["ua"], r["va"]], "seg_b": [r["ub"], r["vb"]],
            "name_a": [names.get(r["ua"], ""), names.get(r["va"], "")],
            "name_b": [names.get(r["ub"], ""), names.get(r["vb"], "")],
            "shared_station": r["shared_station"],
            "shared_station_name": names.get(r["shared_station"], "")
            if r["shared_station"] else "",
            "dmin": r["dmin"],
            "overlap_len": r["overlap_len"],
            "near_fraction": r["near_all"],
            "far_fraction": r["near_far"],
        }

    crossings = [r for r in records if r["proper"]]
    overlap_records = [r for r in records
                       if not r["proper"] and r["overlap_len"] >= overlap_min]
    # 近重合候选：全体采样贴近比例达标；共享换乘站的对还要看"远离换乘站"后是否仍贴近
    near_candidates = [r for r in records
                       if not r["proper"]
                       and r["overlap_len"] < overlap_min
                       and r["dmin"] <= near_tol
                       and r["near_all"] >= frac_min]
    cross_line_near: list[dict] = []
    same_line_near: list[dict] = []
    junction_records: list[dict] = []
    for r in near_candidates:
        if r["same_line"]:
            same_line_near.append(r)
            continue
        if r["same_pair"]:
            continue                      # 同站对走廊单独统计
        if r["shared_station"] is not None and r["near_far"] < frac_min:
            # 只在换乘站附近汇入（<junction_exclude_px），属正常汇入画法
            junction_records.append(r)
            continue
        cross_line_near.append(r)

    # ---- 并行走廊（不同线路共用同一对车站）：待修缺陷（渲染层需做并行偏移）
    corridor_groups: dict[tuple[str, str], dict] = {}
    for r in overlap_records:
        if r["same_pair"] and not r["same_line"]:
            key = tuple(sorted([r["ua"], r["va"]]))
            grp = corridor_groups.setdefault(key, {
                "u": key[0], "v": key[1],
                "name_u": names.get(key[0], ""), "name_v": names.get(key[1], ""),
                "lines": [], "length": r["overlap_len"],
            })
            for lid in (r["line_a"], r["line_b"]):
                if lid not in grp["lines"]:
                    grp["lines"].append(lid)
            grp["length"] = max(grp["length"], r["overlap_len"])
    corridor_pairs = sorted(corridor_groups.values(),
                            key=lambda g: (-len(g["lines"]), g["u"], g["v"]))

    cross_line_overlap = [r for r in overlap_records
                          if not r["same_pair"] and not r["same_line"]]
    same_line_overlap = [r for r in overlap_records if r["same_line"]]

    # ---- 正常接触：不同线在换乘站只共享一个端点、不共线重叠、也不算近重合
    normal_touch = sum(1 for r in records
                       if r["shared_station"] is not None and not r["same_line"]
                       and r["overlap_len"] < overlap_min
                       and r["near_all"] < frac_min)

    # ---- 近重合阈值敏感性：换几个像素阈值看计数（换乘站汇入同样豁免）
    sensitivity = {}
    for px in sens_px:
        tol = px * thresholds["unit_per_px"]
        n_cross = 0
        for r in records:
            if r["same_line"] or r["proper"] or r["overlap_len"] >= overlap_min:
                continue
            if r["dmin"] > tol:
                continue
            all_frac, far_frac = r["sens"].get(px, (0.0, 0.0))
            if all_frac < frac_min:
                continue
            if r["shared_station"] is not None and far_frac < frac_min:
                continue
            n_cross += 1
        sensitivity["%gpx" % px] = n_cross

    def sort_key(r):
        return (r["dmin"], -r["overlap_len"])

    return {
        "crossings_total": len(crossings),
        "crossings_cross_line": sum(1 for r in crossings if not r["same_line"]),
        "crossings_same_line": sum(1 for r in crossings if r["same_line"]),
        "cross_line_overlap_pairs": len(cross_line_overlap),
        "same_line_overlap_pairs": len(same_line_overlap),
        "cross_line_near_overlap_pairs": len(cross_line_near),
        "same_line_near_overlap_pairs": len(same_line_near),
        "junction_convergence_pairs": len(junction_records),
        "corridor_pairs": corridor_pairs,
        "normal_shared_endpoint_touches": normal_touch,
        "near_sensitivity": sensitivity,
        "cross_line_overlap_details": [detail(r) for r in
                                       sorted(cross_line_overlap, key=sort_key)],
        "same_line_overlap_details": [detail(r) for r in
                                      sorted(same_line_overlap, key=sort_key)],
        "cross_line_near_details": [detail(r) for r in
                                    sorted(cross_line_near, key=sort_key)],
        "junction_convergence_details": [detail(r) for r in
                                         sorted(junction_records, key=sort_key)],
        "crossings_details": [detail(r) for r in sorted(crossings, key=sort_key)],
    }


def measure_layout(graph, positions, *, source_label: str, layout_time_s=None,
                   threshold_kwargs=None, detail_limit: int = 60) -> dict:
    """对一套站点坐标做全部几何度量，返回 JSON 友好 dict。"""
    positions = normalize_positions(positions)
    data_pos = {sid: (x, y) for sid, (name, x, y) in station_table(graph).items()}
    names = {sid: name for sid, (name, _, _) in station_table(graph).items()}
    sequences = reconstruct_line_sequences(graph)
    segments = build_segments(positions, sequences)
    lengths = [s.length for s in segments]
    kw = dict(threshold_kwargs or {})
    thresholds = derive_thresholds(positions, lengths, **kw)

    missing = [sid for sid in graph.stations if sid not in positions]
    extra = [sid for sid in positions if sid not in graph.stations]
    finite = all(math.isfinite(x) and math.isfinite(y) for x, y in positions.values())

    # 与数据坐标的位移：先把两套坐标各自缩放到 span=2（|coord|≤1）再比较，
    # 否则显示坐标（|coord|≤1）与数据坐标（km）量纲不同、数字没有意义。
    norm_pos = unit_span_positions(positions)
    norm_data = unit_span_positions(data_pos)
    common = [sid for sid in norm_pos if sid in norm_data]
    if common:
        diffs = [math.hypot(norm_pos[sid][0] - norm_data[sid][0],
                            norm_pos[sid][1] - norm_data[sid][1]) for sid in common]
        max_disp = max(diffs)
        mean_disp = sum(diffs) / len(diffs)
    else:
        max_disp = mean_disp = 0.0
    span = thresholds["span"]
    changed = max_disp > 1e-6

    exact_coll, near_coll, min_pair = _station_pairs(
        graph, positions, thresholds["station_near_tol"])
    through = _through_stations(segments, positions, names,
                                thresholds["through_tol"], thresholds["through_end_margin"])
    pair_metrics = _classify_pairs(segments, thresholds, names)
    parallel = parallel_overlap_metrics(positions, sequences, names,
                                        detail_limit=detail_limit)

    for key in ("cross_line_overlap_details", "same_line_overlap_details",
                "cross_line_near_details", "crossings_details",
                "junction_convergence_details"):
        if len(pair_metrics[key]) > detail_limit:
            pair_metrics[key] = pair_metrics[key][:detail_limit]

    if lengths:
        ordered = sorted(lengths)
        p10 = ordered[int(len(ordered) * 0.1)]
        p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
        median = _median(lengths)
        long_ratio = (sum(1 for v in lengths if v > 4.0 * median) / len(lengths)
                      if median > 0 else 0.0)
        min_edge = {
            "length": ordered[0],
            "line": min(segments, key=lambda s: s.length).line,
            "u": min(segments, key=lambda s: s.length).u,
            "v": min(segments, key=lambda s: s.length).v,
            "name_u": names.get(min(segments, key=lambda s: s.length).u, ""),
            "name_v": names.get(min(segments, key=lambda s: s.length).v, ""),
        }
        max_edge = {
            "length": ordered[-1],
            "line": max(segments, key=lambda s: s.length).line,
            "u": max(segments, key=lambda s: s.length).u,
            "v": max(segments, key=lambda s: s.length).v,
            "name_u": names.get(max(segments, key=lambda s: s.length).u, ""),
            "name_v": names.get(max(segments, key=lambda s: s.length).v, ""),
        }
    else:
        p10 = p90 = median = long_ratio = 0.0
        min_edge = max_edge = None

    # 线路直方图（每条线的站数/边数/段长统计）
    line_stats = []
    for lid, seq in sequences.items():
        lid_segs = [s for s in segments if s.line == lid]
        lid_lengths = [s.length for s in lid_segs]
        meta = (getattr(graph, "lines", None) or {}).get(lid, {})
        line_stats.append({
            "id": lid,
            "name": str(meta.get("name", lid)),
            "color": str(meta.get("color", "")),
            "stations": len(seq),
            "edges": len(lid_segs),
            "len_min": min(lid_lengths) if lid_lengths else 0.0,
            "len_median": _median(lid_lengths),
            "len_max": max(lid_lengths) if lid_lengths else 0.0,
        })

    zero_len = sum(1 for s in segments if s.length <= 1e-12)
    metrics = {
        "source": source_label,
        "layout_time_s": layout_time_s,
        "n_stations": len(graph.stations),
        "n_lines": len(graph.lines),
        "n_edges": len(graph.edges),
        "n_segments": len(segments),
        "n_zero_length_segments": zero_len,
        "coverage": {
            "positions": len(positions),
            "missing": missing,
            "extra": extra,
            "all_finite": finite,
            "ok": not missing and not extra and finite,
        },
        "layout_changed_vs_data": changed,
        "displacement": {"max": max_disp, "mean": mean_disp,
                         "max_over_span": (max_disp / 2.0) if span > 0 else 0.0},
        "displacement_note": "先把两套坐标各自缩放到 span=2（|coord|≤1）再算欧氏位移",
        "bbox": ([min(x for x, _ in positions.values()),
                  min(y for _, y in positions.values()),
                  max(x for x, _ in positions.values()),
                  max(y for _, y in positions.values())] if positions else []),
        "span": span,
        "components": _components(graph, positions, segments),
        "station_collisions_exact": exact_coll,
        "near_station_pairs": near_coll,
        "min_station_pair": min_pair,
        "min_edge": min_edge,
        "max_edge": max_edge,
        "segment_length": {
            "min": min_edge["length"] if min_edge else 0.0,
            "median": median,
            "p10": p10,
            "p90": p90,
            "max": max_edge["length"] if max_edge else 0.0,
        },
        "long_edge_ratio": long_ratio,
        "segments_through_station": through,
        "line_stats": line_stats,
        "thresholds": thresholds,
        "pair_metrics": pair_metrics,
        "parallel": parallel,
    }
    # 把常用计数提到顶层，便于对比与压测复用
    for key in ("crossings_total", "crossings_cross_line", "crossings_same_line",
                "cross_line_overlap_pairs", "same_line_overlap_pairs",
                "cross_line_near_overlap_pairs", "same_line_near_overlap_pairs",
                "junction_convergence_pairs",
                "corridor_pairs", "normal_shared_endpoint_touches",
                "near_sensitivity",
                "cross_line_overlap_details", "cross_line_near_details",
                "crossings_details", "same_line_overlap_details",
                "junction_convergence_details"):
        metrics[key] = pair_metrics[key]
    for key in ("parallel_overlap_pairs", "parallel_overlap_details",
                "min_nonadjacent_segment_pair", "close_nonadjacent_pairs_below_dist",
                "parallel_angle_deg", "parallel_dist_norm"):
        metrics[key] = parallel[key]
    return metrics


# ======================================================================
# 4. 验收判定 / 前后对比
# ======================================================================
def evaluate_acceptance(metrics: dict, *, expect_layout_change: bool,
                        layout_time_limit_s=None,
                        corridor_policy: str = DEFAULT_CORRIDOR_POLICY) -> dict:
    """按验收口径检查一套布局。

    ``corridor_policy``：
        * ``defect``（默认）：不同线路共用同一对相邻站的「并行走廊」视为**待修缺陷**，
          要求 0 处（渲染层做并行偏移后应达标）；
        * ``known``：视为已知限制，不计入失败，只给 warning。
    """
    checks = OrderedDict()
    checks["坐标完整且有限"] = bool(metrics["coverage"]["ok"])
    checks["无站点坐标重合"] = (not metrics["station_collisions_exact"]
                                and not metrics["near_station_pairs"])
    lead_def = "不同线路并行重叠为0（不共享站、非交叉、夹角<%g°且距离<%g归一化）" % (
        metrics["parallel_angle_deg"], metrics["parallel_dist_norm"])
    checks[lead_def] = metrics["parallel_overlap_pairs"] == 0
    checks["不同线路非相邻线段无共线重合"] = metrics["cross_line_overlap_pairs"] == 0
    checks["不同线路非相邻线段无近重合(3px)"] = metrics["cross_line_near_overlap_pairs"] == 0
    checks["无线段穿过非端点站点"] = len(metrics["segments_through_station"]) == 0
    checks["最小站间距>0"] = (metrics["min_station_pair"] or {}).get("dist", 0.0) > 0
    checks["布局变化符合预期"] = bool(metrics["layout_changed_vs_data"]) == bool(expect_layout_change)
    if layout_time_limit_s is not None and metrics.get("layout_time_s") is not None:
        checks["布局耗时≤%.2fs" % layout_time_limit_s] = \
            metrics["layout_time_s"] <= layout_time_limit_s
    corridor_count = len(metrics["corridor_pairs"])
    if corridor_policy == "defect":
        checks["并行重合走廊为0（defect 口径）"] = corridor_count == 0
    warnings: list[str] = []
    if metrics["same_line_overlap_pairs"]:
        warnings.append(f"同线非相邻线段重合 {metrics['same_line_overlap_pairs']} 对")
    if metrics["same_line_near_overlap_pairs"]:
        warnings.append(f"同线非相邻线段近重合 {metrics['same_line_near_overlap_pairs']} 对")
    if corridor_count:
        names = "；".join(f"{g['name_u']}-{g['name_v']}（{'+'.join(g['lines'])}号线）"
                         for g in metrics["corridor_pairs"][:5])
        tag = ("待修缺陷（defect 口径要求 0）" if corridor_policy == "defect"
               else "允许项（渲染层并行偏移，视觉上分开；清单单列）")
        warnings.append(f"并行走廊 {corridor_count} 处{tag}：{names}")
    if metrics["junction_convergence_pairs"]:
        warnings.append(f"换乘站汇入贴近 {metrics['junction_convergence_pairs']} 对"
                        f"（距共享站 <{metrics['thresholds']['junction_exclude_px']:g}px 豁免，属正常画法）")
    if metrics["n_zero_length_segments"]:
        warnings.append(f"零长度线段 {metrics['n_zero_length_segments']} 条")
    return {
        "checks": checks,
        "pass": all(checks.values()),
        "failed": [k for k, v in checks.items() if not v],
        "warnings": warnings,
        "corridor_policy": corridor_policy,
    }


COMPARE_KEYS = [
    ("parallel_overlap_pairs", "不同线路并行重叠(夹角<12°,距离<0.02)(对)"),
    ("cross_line_overlap_pairs", "跨线非相邻线段共线重合(对)"),
    ("cross_line_near_overlap_pairs", "跨线非相邻线段近重合(3px)(对)"),
    ("corridor_pairs", "并行走廊重合(处，渲染层偏移)"),
    ("min_nonadjacent_segment_pair", "最小非相邻线段距离(归一化)"),
    ("crossings_total", "线段交叉(处)"),
    ("station_collisions_exact", "站点坐标重合(对)"),
    ("near_station_pairs", "站点近重合(对)"),
    ("segments_through_station", "线段穿站(处)"),
    ("junction_convergence_pairs", "换乘站汇入贴近(对，正常)"),
    ("long_edge_ratio", "超长边比例(>4×中位)"),
    ("min_edge", "最短线段(坐标单位)"),
    ("min_station_pair", "最小站间距(坐标单位)"),
]


def _metric_value(metrics, key):
    if key in ("min_edge", "min_station_pair"):
        obj = metrics.get(key) or {}
        return obj.get("dist") if key == "min_station_pair" else obj.get("length")
    if key == "min_nonadjacent_segment_pair":
        return (metrics.get(key) or {}).get("dist_norm")
    if key in ("segments_through_station", "station_collisions_exact",
               "near_station_pairs", "corridor_pairs"):
        return len(metrics.get(key) or [])
    return metrics.get(key)


def compare_metrics(before: dict, after: dict) -> dict:
    rows = []
    for key, label in COMPARE_KEYS:
        b = _metric_value(before, key)
        a = _metric_value(after, key) if after else None
        rows.append({"key": key, "label": label, "before": b, "after": a})
    return {"rows": rows}


# ======================================================================
# 5. 显示层模块加载
# ======================================================================
def load_display_module(project_root: Path):
    """返回 (module|None, info dict)；不因缺模块而抛异常。"""
    info = {"available": False, "path": None, "error": None, "functions": [],
            "sequences_match": None, "sequences_note": None}
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    try:
        import importlib
        mod = importlib.import_module("ui.display_layout")
    except Exception as exc:                       # noqa: BLE001 - 缺模块要能降级
        info["error"] = f"{type(exc).__name__}: {exc}"
        return None, info
    info["available"] = True
    info["path"] = str(getattr(mod, "__file__", "") or "")
    for fn in ("station_positions", "needs_schematic_layout", "schematic_layout",
               "derived_line_sequences"):
        if hasattr(mod, fn):
            info["functions"].append(fn)
    return mod, info


def display_positions(mod, graph, sequences):
    """调 station_positions；返回 (positions, seconds, call_form)。"""
    fn = getattr(mod, "station_positions", None)
    if fn is None:
        raise RuntimeError("ui.display_layout 缺少 station_positions()")
    t0 = time.perf_counter()
    call_form = "station_positions(graph, line_sequences)"
    try:
        pos = fn(graph, dict(sequences))
    except TypeError:
        call_form = "station_positions(graph)"
        pos = fn(graph)
    dt = time.perf_counter() - t0
    return normalize_positions(pos), dt, call_form


def expect_layout_change(mod, graph, sequences) -> bool:
    """按显示层 needs_schematic_layout 判断该数据集是否应重算布局（demo 应不变）。"""
    if mod is not None and hasattr(mod, "needs_schematic_layout"):
        try:
            return bool(mod.needs_schematic_layout(graph, dict(sequences)))
        except TypeError:
            try:
                return bool(mod.needs_schematic_layout(graph))
            except Exception:                  # noqa: BLE001
                pass
        except Exception:                      # noqa: BLE001
            pass
    # 兜底：>=24 站且有 meta.layout 标记 → 认为应重算（与 PLAN 口径一致）
    return len(graph.stations) >= 24 and isinstance(graph.meta.get("layout"), dict)


# ======================================================================
# 6. 报告生成
# ======================================================================
def _r(value, nd=6):
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round(value, nd)
    return value


def _fmt(value, nd=3):
    if value is None:
        return "—"
    if isinstance(value, float):
        return ("%." + str(nd) + "f") % value
    return str(value)


def _json_ready(obj):
    if isinstance(obj, dict):
        return {str(k): _json_ready(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_ready(v) for v in obj]
    if isinstance(obj, float):
        return _r(obj)
    return obj


def _detail_line(entry: dict) -> str:
    shared = entry.get("shared_station_name") or ""
    shared_txt = f" 共享站={shared}" if shared else ""
    return ("{la}({na0}-{na1}) × {lb}({nb0}-{nb1}) "
            "dmin={d:.4g} overlap={o:.4g} near_frac={f:.2f} far_frac={ff:.2f}{ss}").format(
        la=entry["line_a"], lb=entry["line_b"],
        na0=entry["name_a"][0], na1=entry["name_a"][1],
        nb0=entry["name_b"][0], nb1=entry["name_b"][1],
        d=entry["dmin"], o=entry["overlap_len"], f=entry["near_fraction"],
        ff=entry.get("far_fraction", 0.0), ss=shared_txt)


def _parallel_detail_line(entry: dict) -> str:
    return ("{la}({na0}-{na1}) × {lb}({nb0}-{nb1}) 夹角={ang:.2f}° "
            "距离={d:.5f}(归一化) proper_crossing={pc}").format(
        la=entry["line_a"], lb=entry["line_b"],
        na0=entry["name_a"][0], na1=entry["name_a"][1],
        nb0=entry["name_b"][0], nb1=entry["name_b"][1],
        ang=entry["angle_deg"], d=entry["dist_norm"],
        pc="是" if entry.get("proper_crossing") else "否")


def build_markdown(report: dict) -> str:
    lines: list[str] = []
    ap = lines.append
    ap("# 169/164 站显示层布局几何验证报告")
    ap("")
    ap(f"- 生成时间：{report['generated_at']}")
    ap(f"- 工具：`path-nav/tools/layout_report.py`（纯标准库，不依赖 Tkinter）")
    ap(f"- 坐标源：`--layout-source {report['layout_source']}`")
    mod = report.get("module") or {}
    if mod.get("available"):
        ap(f"- 显示层模块：`{mod.get('path')}`（可用函数：{', '.join(mod.get('functions') or [])}）")
        if mod.get("call_form"):
            ap(f"- 调用形式：`{mod.get('call_form')}`")
        if mod.get("sequences_note"):
            ap(f"- 站序一致性：{mod.get('sequences_note')}")
    else:
        ap(f"- 显示层模块：**不可用**（{mod.get('error')}）——本报告仅有数据坐标基线")
    th = report["thresholds"]
    ap(f"- 屏幕像素等效：bbox 最长边 ≈ {_fmt(th['ref_px'], 0)} px；"
       f"1 px ≈ span/{_fmt(th['ref_px'], 0)} 坐标单位")
    ap(f"- 近重合阈值：`max({_fmt(th['near_px'], 1)}px等效, {_fmt(th['near_pct'] * 100, 1)}% × 中位边长)`"
       f"（各数据集实际值见明细）")
    ap(f"- 换乘站汇入豁免：两段共享换乘站时，距该站 <{_fmt(th.get('junction_exclude_px', DEFAULT_JUNCTION_EXCLUDE_PX), 1)}px 的贴近"
       f"视为正常汇入（单列 `junction_convergence`，不计失败）")
    ap(f"- **并行重叠（Lead 统一口径，必须 0）**：不同线路、不共享站点、非严格相交的两段，"
       f"夹角 < {_fmt(DEFAULT_PARALLEL_ANGLE_DEG, 0)}° 且最近距离 < {DEFAULT_PARALLEL_DIST_NORM} "
       f"（归一化坐标，span=2；约 1% 画幅）→ 记为并行重叠缺陷；正常交叉(proper crossing)不判")
    ap(f"- 并行走廊策略：`{report.get('corridor_policy', DEFAULT_CORRIDOR_POLICY)}`"
       f"（known = 允许项：渲染层已做并行偏移、视觉分开，报告单列清单；defect = 几何层要求归零）")
    ap(f"- 判定函数：见 `path-nav/tools/layout_report.py` 模块 docstring")
    ap("")

    # ---- 汇总表
    ap("## 1. 汇总（before = 数据坐标，after = 显示坐标）")
    ap("")
    ap("| 数据集 | 站/线/段 | 并行重叠 before→after | 跨线共线重合 before→after | 交叉 before→after | 站点重合 before→after | 穿站 before→after | 最小非相邻距离 before→after | 最小站间距 before→after | 走廊(单列) | 布局耗时 | 结论 |")
    ap("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for ds in report["datasets"]:
        b = ds.get("before") or {}
        a = ds.get("after")
        eff = ds["effective"]
        verdict = "✅ 通过" if ds["acceptance"]["pass"] else "❌ 未通过"
        if not a:
            verdict = "— 基线（无显示坐标）"
        def cell(key, nd=3):
            bv = _fmt(_metric_value(b, key), nd)
            av = _fmt(_metric_value(a, key), nd) if a else "—"
            return f"{bv} → {av}"
        ap("| {name} | {st}/{ln}/{sg} | {pv} | {ov} | {cr} | {sc} | {th} | {md} | {mg} | {cv} | {tm} | {vd} |".format(
            name=ds["name"], st=ds["n_stations"], ln=ds["n_lines"], sg=ds["n_segments"],
            pv=cell("parallel_overlap_pairs"),
            ov=cell("cross_line_overlap_pairs"),
            cr=cell("crossings_total", 0),
            sc=cell("station_collisions_exact"),
            th=cell("segments_through_station"),
            md=cell("min_nonadjacent_segment_pair", 4),
            mg=cell("min_station_pair", 4),
            cv=cell("corridor_pairs"),
            tm=("—" if a is None else _fmt(a.get("layout_time_s"), 3) + "s"),
            vd=verdict))
    ap("")
    ap("> 「站点重合」为精确坐标碰撞对数（近重合对数见明细）；「交叉」= 严格内部相交（正常画法，"
       "不判失败）；「并行重叠」= Lead 统一口径（不同线路、不共享站、非交叉，夹角<12° 且归一化距离<0.02，必须 0）；"
       "「走廊(单列)」= 不同线路共用同一对相邻站，渲染层已做并行偏移、视觉上分开，仅单列清单。")
    ap("")

    # ---- 各数据集
    ap("## 2. 各数据集明细")
    ap("")
    for idx, ds in enumerate(report["datasets"]):
        b = ds.get("before") or {}
        a = ds.get("after")
        eff = ds["effective"]
        ap(f"### 2.{idx + 1} {ds['name']}（{ds['n_stations']} 站 / {ds['n_lines']} 线）")
        ap("")
        ap(f"- 文件：`{ds['path']}`")
        if a is not None:
            ap(f"- 显示布局耗时：**{_fmt(a.get('layout_time_s'), 3)} s**"
               f"（阈值 {_fmt(eff['thresholds']['near_tol'], 6)} 坐标单位 = "
               f"近重合 {_fmt(eff['thresholds']['near_tol_px_equiv'], 2)} px 等效）")
        expect = ds.get("expect_layout_change")
        ap(f"- 布局变化：before→after {'已重算' if (a and a['layout_changed_vs_data']) else '未变化'}"
           f"（预期{'应重算' if expect else '保持不变'}）")
        ap(f"- 最大位移（归一化）：{_fmt((a or b)['displacement']['max'], 4)}"
           f"（两套坐标各自缩放到 span=2 后比较；均值 {_fmt((a or b)['displacement']['mean'], 4)}）")
        mn = eff.get("min_nonadjacent_segment_pair")
        if mn:
            ap(f"- 最小非相邻线段距离（归一化，排除相交对与共享站对）：**{_fmt(mn['dist_norm'], 5)}**"
               f"（夹角 {_fmt(mn['angle_deg'], 1)}°，{mn['line_a']} 线 {mn['name_a'][0]}-{mn['name_a'][1]}"
               f" × {mn['line_b']} 线 {mn['name_b'][0]}-{mn['name_b'][1]}）")
        else:
            ap("- 最小非相邻线段距离：无（所有非相邻线段对都相交或不存在）")
        ap("")
        ap("**指标对比**")
        ap("")
        ap("| 指标 | before（数据坐标） | after（显示坐标） | 变化 |")
        ap("|---|---|---|---|")
        for row in ds["comparison"]["rows"]:
            bv = row["before"]
            av = row["after"]
            if isinstance(bv, float) and isinstance(av, float) and bv not in (0.0,) and row["key"] not in (
                    "min_edge", "min_station_pair"):
                delta = ("%+.1f%%" % ((av - bv) / bv * 100.0)) if bv else ("%+g" % (av - bv))
            elif row["key"] == "min_edge":
                delta = ("%+.2f%%" % ((av - bv) / bv * 100.0)) if (bv and av is not None) else "—"
            elif row["key"] == "min_station_pair":
                delta = ("%+.2f%%" % ((av - bv) / bv * 100.0)) if (bv and av is not None) else "—"
            else:
                delta = "—"
            ap(f"| {row['label']} | {_fmt(bv)} | {_fmt(av)} | {delta} |")
        ap("")
        ap("**验收检查（after）**" if a is not None else "**验收检查（before，基线）**")
        ap("")
        acc = ds["acceptance"]
        for name, ok in acc["checks"].items():
            ap(f"- [{'x' if ok else ' '}] {name}")
        for w in acc["warnings"]:
            ap(f"- ⚠️ {w}")
        ap("")
        # 失败/风险明细
        eff_metrics = eff
        par = eff_metrics.get("parallel_overlap_details") or []
        if par:
            ap(f"**不同线路并行重叠（失败项，Lead 口径：夹角<12° 且归一化距离<0.02）"
               f"：{len(par)} 对（最多列 15 条）**")
            ap("")
            for entry in par[:15]:
                ap(f"- {_parallel_detail_line(entry)}")
            ap("")
        for key, title in (
                ("cross_line_overlap_details", "跨线非相邻线段共线重合（失败项）"),
                ("cross_line_near_details", "跨线非相邻线段近重合 3px（失败项）"),
                ("same_line_overlap_details", "同线非相邻线段重合"),
        ):
            items = eff_metrics.get(key) or []
            if items:
                ap(f"**{title}**：{len(items)} 条（最多列 15 条）")
                ap("")
                for entry in items[:15]:
                    ap(f"- {_detail_line(entry)}")
                ap("")
        junction = eff_metrics.get("junction_convergence_details") or []
        if junction:
            ap(f"**换乘站汇入贴近（正常，不算失败）**：{len(junction)} 对（最多列 8 条）")
            ap("")
            for entry in junction[:8]:
                ap(f"- {_detail_line(entry)}")
            ap("")
        through = eff_metrics.get("segments_through_station") or []
        if through:
            ap(f"**线段穿过非端点站点**：{len(through)} 处（最多列 15 条）")
            ap("")
            for entry in through[:15]:
                ap(f"- {entry['line']} 线 {entry['name_u']}-{entry['name_v']} "
                   f"穿过「{entry['name_station']}」 d={entry['dist']:.4g}")
            ap("")
        corridors = eff_metrics.get("corridor_pairs") or []
        if corridors:
            policy = report.get("corridor_policy", DEFAULT_CORRIDOR_POLICY)
            title = ("**并行重合走廊（单列清单，允许项：渲染层并行偏移、视觉上分开）**"
                     if policy != "defect" else
                     "**并行重合走廊（defect 口径的待修缺陷）**")
            ap(title)
            ap("")
            for g in corridors:
                ap(f"- {g['name_u']} — {g['name_v']}：线路 {', '.join(g['lines'])} "
                   f"共用同一对相邻站（站点 id: {g['u']}, {g['v']}），"
                   f"坐标层重合长度 {_fmt(g['length'], 4)}；渲染层已做并行偏移")
            ap("")
        collisions = eff_metrics.get("station_collisions_exact") or []
        if collisions:
            ap("**站点坐标完全重合（失败项）**")
            ap("")
            for c in collisions[:15]:
                ap(f"- {c['name_a']} ↔ {c['name_b']}")
            ap("")
        near_st = eff_metrics.get("near_station_pairs") or []
        if near_st:
            ap(f"**站点近重合**：{len(near_st)} 对（最多列 10 条）")
            ap("")
            for c in near_st[:10]:
                ap(f"- {c['name_a']} ↔ {c['name_b']} d={c['dist']:.4g}")
            ap("")
        # 近重合阈值敏感性
        sens = eff_metrics.get("near_sensitivity") or {}
        if sens:
            ap("**跨线近重合阈值敏感性（跨线线段对数）**：" +
               "，".join(f"{k}等效: {v}" for k, v in sens.items()))
            ap("")
        # 线路直方图
        ap("**线路直方图（after）**")
        ap("")
        ap("| 线路 | 名称 | 站数 | 段数 | 段长 min / median / max（坐标单位） |")
        ap("|---|---|---|---|---|")
        for row in eff_metrics["line_stats"]:
            ap("| {id} | {name} | {stations} | {edges} | {lmin} / {lmed} / {lmax} |".format(
                id=row["id"], name=row["name"], stations=row["stations"],
                edges=row["edges"], lmin=_fmt(row["len_min"], 4),
                lmed=_fmt(row["len_median"], 4), lmax=_fmt(row["len_max"], 4)))
        ap("")

    # ---- 已知限制
    ap("## 3. 已知限制与说明")
    ap("")
    ap("- **并行重合走廊（待修缺陷）**：两条不同线路共用同一对相邻站（如 2/7 号线"
       "广州南站-石壁）时，站点坐标层面两段端点完全相同、必然完全重合；"
       "需渲染层给并行线路做横向偏移（小弧线），偏移后本项应归零。")
    ap("- **共享换乘站汇入**：不同线路在换乘站共享同一个端点，两段在该站附近 <5px 的"
       "贴近属于正常汇入画法，单列为 `junction_convergence`，不计失败；远离换乘站后仍"
       "并线贴近则计入 `跨线近重合`。")
    ap("- **像素等效阈值**：用 bbox 最长边 / ref_px 把画面尺度折算成屏幕像素，"
       "是一种与画布尺寸无关的近似；实际画布尺寸变化时阈值应按比例调整。")
    ap("- **数据坐标（before）** 的“近重合”阈值同样按像素等效折算，因此与显示坐标可比；"
       "数据坐标单位是近似 km（线性布局），不是真实经纬度。")
    ap("")
    return "\n".join(lines) + "\n"


def build_html(markdown_text: str, title: str = "169/164 站显示层布局几何验证报告") -> str:
    """把本工具生成的 Markdown 转成可独立打开的 HTML（纯标准库、零依赖）。"""
    out: list[str] = []
    in_table = False
    in_list = False

    def inline(text: str) -> str:
        text = _html.escape(text)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        return text

    for raw in markdown_text.splitlines():
        line = raw.rstrip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and all(c and set(c) <= set("-: ") for c in cells):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{inline(c)}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line:
            continue
        if line.startswith("### "):
            out.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("> "):
            out.append(f"<blockquote>{inline(line[2:])}</blockquote>")
        else:
            out.append(f"<p>{inline(line)}</p>")
    if in_table:
        out.append("</table>")
    if in_list:
        out.append("</ul>")
    style = ("body{font-family:'Microsoft YaHei',system-ui,sans-serif;margin:28px auto;"
             "max-width:1560px;color:#1f2937;line-height:1.55;padding:0 18px}"
             "h1{font-size:22px;border-bottom:2px solid #334155;padding-bottom:6px}"
             "h2{font-size:18px;margin-top:30px;color:#0f172a}"
             "h3{font-size:15px;margin-top:20px;color:#334155}"
             "table{border-collapse:collapse;margin:10px 0;font-size:13px;width:100%}"
             "th,td{border:1px solid #cbd5e1;padding:5px 8px;text-align:left;vertical-align:top}"
             "th{background:#e2e8f0}tr:nth-child(even) td{background:#f8fafc}"
             "code{background:#f1f5f9;padding:1px 4px;border-radius:3px;"
             "font-family:Consolas,monospace}blockquote{border-left:4px solid #94a3b8;"
             "margin:8px 0;padding:4px 12px;color:#475569;background:#f8fafc}"
             "ul{margin:6px 0 6px 22px}")
    return ("<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<title>{_html.escape(title)}</title><style>{style}</style></head><body>\n"
            + "\n".join(out) + "\n</body></html>\n")


# ======================================================================
# 7. 主流程
# ======================================================================
def process_dataset(path: Path, *, layout_source: str, mod, module_info,
                    threshold_kwargs, corridor_policy: str = DEFAULT_CORRIDOR_POLICY) -> dict:
    graph = load_graph(path)
    sequences = reconstruct_line_sequences(graph)
    data_positions = {sid: (float(st.x), float(st.y))
                      for sid, st in graph.stations.items()}
    ns = {
        "name": path.stem,
        "path": str(path),
        "n_stations": len(graph.stations),
        "n_lines": len(graph.lines),
        "expect_layout_change": expect_layout_change(mod, graph, sequences),
        "warnings": [],
    }
    if not sequences:
        ns["warnings"].append("未能从 edges 还原出任何线路站序（图可能没有边）")

    before = measure_layout(graph, data_positions, source_label="data",
                            threshold_kwargs=threshold_kwargs)
    after = None
    module_call_form = None
    if layout_source == "data":
        pass
    elif mod is None:
        if layout_source == "display":
            raise SystemExit("--layout-source display 需要 ui/display_layout.py，但导入失败："
                             + str(module_info.get("error")))
        ns["warnings"].append("ui.display_layout 不可用，已退化为只测数据坐标（before）")
    else:
        try:
            pos, dt, module_call_form = display_positions(mod, graph, sequences)
            after = measure_layout(graph, pos, source_label="display",
                                   layout_time_s=dt, threshold_kwargs=threshold_kwargs)
        except Exception as exc:                   # noqa: BLE001
            msg = f"调用 station_positions 失败：{type(exc).__name__}: {exc}"
            if layout_source == "display":
                raise SystemExit(msg)
            ns["warnings"].append(msg + "（已退化为只测 before）")

    effective = after if after is not None else before
    # 阈值明细补充“像素等效数值”，便于报告阅读
    for m in (before, after):
        if not m:
            continue
        th = m["thresholds"]
        unit = th["unit_per_px"]
        th["near_tol_px_equiv"] = (th["near_tol"] / unit) if unit > 0 else 0.0
        th["station_near_tol_px_equiv"] = (th["station_near_tol"] / unit) if unit > 0 else 0.0
        th["through_tol_px_equiv"] = (th["through_tol"] / unit) if unit > 0 else 0.0

    acceptance = evaluate_acceptance(
        effective,
        expect_layout_change=ns["expect_layout_change"],
        layout_time_limit_s=(LAYOUT_TIME_LIMIT_S if after is not None else None),
        corridor_policy=corridor_policy)
    ns["acceptance"] = acceptance
    ns["effective"] = effective
    ns["effective_source"] = effective["source"]
    ns["n_segments"] = effective["n_segments"]
    ns["comparison"] = compare_metrics(before, after) if after is not None else {
        "rows": compare_metrics(before, None)["rows"]}
    ns["before"] = before
    ns["after"] = after
    ns["module_call_form"] = module_call_form
    return ns


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                              # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(
        description="显示层布局几何验证：before(数据坐标) vs after(显示坐标)")
    ap.add_argument("--data", action="append", default=None,
                    help="图 JSON 路径，可重复；缺省 real_metro/stress_164/demo_city")
    ap.add_argument("--layout-source", choices=["auto", "data", "display"],
                    default="auto", help="after 坐标来源（auto=有显示层就用）")
    ap.add_argument("--out-dir", default=str(DEFAULT_REPORTS_DIR))
    ap.add_argument("--md", default=None, help="Markdown 报告路径（默认 <out-dir>/layout_report.md）")
    ap.add_argument("--json", dest="json_path", default=None,
                    help="JSON 指标路径（默认 <out-dir>/layout_metrics.json）")
    ap.add_argument("--html", default=None,
                    help="HTML 报告路径（默认 <out-dir>/layout_report.html）")
    ap.add_argument("--ref-px", type=float, default=DEFAULT_REF_PX)
    ap.add_argument("--near-px", type=float, default=DEFAULT_NEAR_PX)
    ap.add_argument("--near-pct", type=float, default=DEFAULT_NEAR_PCT)
    ap.add_argument("--overlap-min-px", type=float, default=DEFAULT_OVERLAP_MIN_PX)
    ap.add_argument("--collinear-px", type=float, default=DEFAULT_COLLINEAR_PX)
    ap.add_argument("--station-near-px", type=float, default=DEFAULT_STATION_NEAR_PX)
    ap.add_argument("--through-px", type=float, default=DEFAULT_THROUGH_PX)
    ap.add_argument("--near-frac", type=float, default=DEFAULT_NEAR_FRACTION)
    ap.add_argument("--junction-exclude-px", type=float, default=DEFAULT_JUNCTION_EXCLUDE_PX,
                    help="换乘站汇入豁免区半径（像素等效，默认 5）")
    ap.add_argument("--corridor-policy", choices=CORRIDOR_POLICIES,
                    default=DEFAULT_CORRIDOR_POLICY,
                    help="并行走廊口径：defect=待修缺陷（默认，渲染层偏移后应归零）；known=已知限制")
    ap.add_argument("--detail-limit", type=int, default=60)
    ap.add_argument("--fail-on-acceptance", action="store_true",
                    help="任一数据集验收失败时退出码 1")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    paths = [Path(p) for p in (args.data or [str(p) for p in DEFAULT_DATASETS])]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print(f"[错误] 数据文件不存在: {missing}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = Path(args.md) if args.md else out_dir / "layout_report.md"
    html_path = Path(args.html) if args.html else out_dir / "layout_report.html"
    json_path = Path(args.json_path) if args.json_path else out_dir / "layout_metrics.json"

    mod, module_info = load_display_module(ROOT)
    sequences_note = None
    if mod is not None:
        # 校验本工具的站序重建与显示层 derived_line_sequences 是否一致
        try:
            matching = []
            probe_graph = load_graph(paths[0])
            seqs = reconstruct_line_sequences(probe_graph)
            if hasattr(mod, "derived_line_sequences"):
                theirs = mod.derived_line_sequences(probe_graph)
                same = set(seqs) == set(theirs) and all(seqs[k] == theirs.get(k) for k in seqs)
                module_info["sequences_match"] = bool(same)
                sequences_note = ("一致（与 derived_line_sequences 逐线相同）" if same
                                  else "**不一致**：canvas 传参前请核对")
                matching.append(same)
            else:
                sequences_note = "显示层无 derived_line_sequences，跳过一致性检查"
        except Exception as exc:                   # noqa: BLE001
            sequences_note = f"一致性检查失败（忽略）：{type(exc).__name__}: {exc}"
    module_info["sequences_note"] = sequences_note
    module_info["call_form"] = None

    threshold_kwargs = {
        "ref_px": args.ref_px, "near_px": args.near_px, "near_pct": args.near_pct,
        "overlap_min_px": args.overlap_min_px, "collinear_px": args.collinear_px,
        "station_near_px": args.station_near_px, "through_px": args.through_px,
        "near_fraction": args.near_frac,
        "junction_exclude_px": args.junction_exclude_px,
    }

    datasets = []
    for path in paths:
        if not args.quiet:
            print(f"[度量] {path.name} ...", flush=True)
        try:
            ds = process_dataset(path, layout_source=args.layout_source, mod=mod,
                                 module_info=module_info,
                                 threshold_kwargs=threshold_kwargs,
                                 corridor_policy=args.corridor_policy)
        except SystemExit:
            raise
        except Exception as exc:                   # noqa: BLE001
            print(f"[失败] {path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        # 明细裁剪
        for m in (ds.get("before"), ds.get("after")):
            if not m:
                continue
            for key in ("cross_line_overlap_details", "same_line_overlap_details",
                        "cross_line_near_details", "crossings_details"):
                if len(m.get(key) or []) > args.detail_limit:
                    m[key] = m[key][:args.detail_limit]
        datasets.append(ds)

    first_th = datasets[0]["effective"]["thresholds"] if datasets else {}
    report = {
        "tool": "path-nav/tools/layout_report.py",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "layout_source": args.layout_source,
        "corridor_policy": args.corridor_policy,
        "module": module_info,
        "project_root": str(ROOT),
        "thresholds": {
            "ref_px": args.ref_px,
            "near_px": args.near_px,
            "near_pct": args.near_pct,
            "overlap_min_px": args.overlap_min_px,
            "collinear_px": args.collinear_px,
            "station_near_px": args.station_near_px,
            "through_px": args.through_px,
            "near_fraction": args.near_frac,
            "junction_exclude_px": args.junction_exclude_px,
            "parallel_overlap": {
                "angle_deg": DEFAULT_PARALLEL_ANGLE_DEG,
                "dist_norm": DEFAULT_PARALLEL_DIST_NORM,
                "normalization": "span=2（|coord|≤1，与 display_layout 一致）",
            },
            "note": ("1 屏幕像素 ≈ span/ref_px 坐标单位；近重合阈值 = "
                     "max(near_px 像素等效, near_pct × 中位边长)；"
                     "并行重叠 = 不同线路、不共享站、非严格相交的两段，"
                     "夹角<12° 且归一化最近距离<0.02。"),
            "first_dataset_effective": first_th,
        },
        "datasets": datasets,
        "summary": {
            "datasets": len(datasets),
            "after_available": sum(1 for d in datasets if d.get("after") is not None),
            "acceptance_pass": sum(1 for d in datasets if d["acceptance"]["pass"]),
            "acceptance_failed": [d["name"] for d in datasets
                                  if not d["acceptance"]["pass"]],
            "corridor_total": sum(len(d["effective"].get("corridor_pairs") or [])
                                  for d in datasets),
            "parallel_overlap_total": sum(d["effective"].get("parallel_overlap_pairs") or 0
                                          for d in datasets),
        },
    }

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(_json_ready(report), ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8", newline="\n")
    md_text = build_markdown(report)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md_text, encoding="utf-8", newline="\n")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(build_html(md_text), encoding="utf-8", newline="\n")

    if not args.quiet:
        print("")
        print("== 汇总（before 数据坐标 → after 显示坐标）==")
        for ds in datasets:
            b = ds["before"]
            a = ds.get("after")
            fmt = lambda m, key: _fmt(_metric_value(m, key))
            line = (f"  {ds['name']}: {ds['n_stations']} 站 | "
                    f"并行重叠 {fmt(b, 'parallel_overlap_pairs')} → "
                    f"{fmt(a, 'parallel_overlap_pairs') if a else '—'} | "
                    f"跨线共线重合 {fmt(b, 'cross_line_overlap_pairs')} → "
                    f"{fmt(a, 'cross_line_overlap_pairs') if a else '—'} | "
                    f"交叉 {fmt(b, 'crossings_total')} → {fmt(a, 'crossings_total') if a else '—'} | "
                    f"穿站 {fmt(b, 'segments_through_station')} → "
                    f"{fmt(a, 'segments_through_station') if a else '—'} | "
                    f"走廊 {fmt(b, 'corridor_pairs')} → {fmt(a, 'corridor_pairs') if a else '—'}")
            if a is not None:
                line += f" | {a['layout_time_s']:.3f}s"
            else:
                line += " | 基线"
            print(line)
            print(f"    验收：{'通过' if ds['acceptance']['pass'] else '未通过 ' + str(ds['acceptance']['failed'])}")
        print(f"  JSON: {json_path}")
        print(f"  MD  : {md_path}")
        print(f"  HTML: {html_path}")

    if args.fail_on_acceptance:
        failed = [d["name"] for d in datasets if not d["acceptance"]["pass"]]
        if failed:
            print(f"[验收失败] {failed}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
