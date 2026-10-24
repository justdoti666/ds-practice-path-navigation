#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""显示层示意布局（ui/display_layout.py）的单元测试。

这个模块只改"画布坐标"，所以测试重点是：
  * 什么数据该重排、什么数据必须保持原样（demo 不能变）；
  * 重排后的几何质量（站点不重合、不同线路的非相邻线段不重合/不贴近）；
  * 鲁棒性（不连通、斜直线退化、手工 Graph、确定性、耗时上限）。

不涉及 Tkinter，可在无显示环境跑。
"""
from __future__ import annotations

import math
import time
import unittest
from pathlib import Path

from core.graph import load_graph
from ui.display_layout import (MIN_NODES, needs_schematic_layout,
                               schematic_layout, station_positions)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def line_sequences(graph) -> dict:
    """与 ui.canvas.py::_line_stations 同口径从 edges 还原每条线站序。

    直接调用纯布局模块的 ``derived_line_sequences``，避免测试依赖 Tkinter
    （Linux CI 可能没装 Tk，界面用例已有 HAS_TK 守卫）。
    """
    from ui.display_layout import derived_line_sequences

    return derived_line_sequences(graph)


def _point_seg_distance(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _segment_distance(p1, p2, q1, q2, samples=20) -> float:
    best = min(_point_seg_distance(p1, q1, q2), _point_seg_distance(p2, q1, q2),
               _point_seg_distance(q1, p1, p2), _point_seg_distance(q2, p1, p2))
    for i in range(samples + 1):
        t = i / samples
        p = (p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t)
        best = min(best, _point_seg_distance(p, q1, q2))
    return best


def _segments_cross(p1, p2, q1, q2) -> bool:
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1 = orient(p1, p2, q1)
    d2 = orient(p1, p2, q2)
    d3 = orient(q1, q2, p1)
    d4 = orient(q1, q2, p2)
    return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)


def _angle_between(p1, p2, q1, q2) -> float:
    a1 = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
    a2 = math.atan2(q2[1] - q1[1], q2[0] - q1[0])
    diff = abs(math.degrees(a1 - a2)) % 180.0
    return min(diff, 180.0 - diff)


def assert_no_segment_overlap(case: unittest.TestCase, graph, positions, *,
                              min_sep: float = 0.02,
                              parallel_deg: float = 12.0) -> None:
    """不同线路、不共享端点的线段不能**并行重叠/贴近**。

    口径说明（验收依据）：
      * 两条线路在换乘站交叉（proper crossing）是正常地铁图现象，不算重叠；
      * 判定为缺陷的是"两条线段近似平行（夹角 < 12°）且最近距离 < 0.02
        （归一化坐标，约 2% 画幅；在 1× 缩放下约 2px）"——这种会看成一条线；
      * 共享同一对车站的并行走廊由渲染层做轨道偏移（见 ui/p4_canvas.py
        ``_shared_corridors``），不在此处判失败。
    """
    edges = [(e.u, e.v) for e in graph.edges]
    bad = []
    near = []
    for i in range(len(edges)):
        u1, v1 = edges[i]
        p1, p2 = positions[u1], positions[v1]
        for j in range(i + 1, len(edges)):
            u2, v2 = edges[j]
            if {u1, v1} & {u2, v2}:
                continue
            q1, q2 = positions[u2], positions[v2]
            if _segments_cross(p1, p2, q1, q2):
                continue                       # 正常交叉，不算重叠
            dist = _segment_distance(p1, p2, q1, q2)
            if dist >= min_sep:
                continue
            angle = _angle_between(p1, p2, q1, q2)
            near.append((u1, v1, u2, v2, round(dist, 5), round(angle, 1)))
            if angle < parallel_deg:
                bad.append((u1, v1, u2, v2, round(dist, 5), round(angle, 1)))
    case.assertFalse(bad, f"存在并行重合线段 {len(bad)} 处，前 5 个: {bad[:5]}")
    case.assertLessEqual(len(near), 6,
                         f"过近线段过多（{len(near)} 处）: {near[:6]}")


class TestDisplayLayout(unittest.TestCase):
    def test_demo_keeps_data_coordinates(self):
        """10 站 demo 是手工坐标：不触发重排，画面必须与修复前一致。"""
        g = load_graph(DATA / "demo_city.json")
        seqs = line_sequences(g)
        self.assertFalse(needs_schematic_layout(g, seqs))
        pos = station_positions(g, seqs)
        self.assertEqual(set(pos), set(g.stations))
        for sid, station in g.stations.items():
            self.assertAlmostEqual(pos[sid][0], station.x, places=9)
            self.assertAlmostEqual(pos[sid][1], station.y, places=9)

    def test_tiny_and_edge_cases_untouched(self):
        for name in ("tiny_hand.json", "edge_cases.json"):
            g = load_graph(DATA / name)
            self.assertFalse(needs_schematic_layout(g), f"{name} 不该被重排")

    def test_metro_detected_and_relaid(self):
        """169 站真实数据：判定为线性布局，重排后站点不重合、线段不重合。"""
        g = load_graph(DATA / "real_metro.json")
        seqs = line_sequences(g)
        self.assertGreaterEqual(len(g.stations), MIN_NODES)
        self.assertTrue(needs_schematic_layout(g, seqs))
        pos = station_positions(g, seqs)
        self.assertEqual(set(pos), set(g.stations))
        for sid, (x, y) in pos.items():
            self.assertTrue(math.isfinite(x) and math.isfinite(y), sid)
        # 站点两两不重合（1e-6）
        coords = list(pos.values())
        for i in range(len(coords)):
            for j in range(i + 1, len(coords)):
                self.assertGreater(math.hypot(coords[i][0] - coords[j][0],
                                              coords[i][1] - coords[j][1]), 1e-6)
        assert_no_segment_overlap(self, g, pos)

    def test_deterministic(self):
        g = load_graph(DATA / "real_metro.json")
        seqs = line_sequences(g)
        first = station_positions(g, seqs)
        second = station_positions(g, seqs)
        self.assertEqual(first, second)

    def test_meta_display_layout_takes_precedence(self):
        """数据自带 meta.display_layout 时直接用它（工具预生成布局的注入口）。"""
        g = load_graph(DATA / "real_metro.json")
        preset = {sid: [float(i), float(-i)] for i, sid in enumerate(sorted(g.stations))}
        g.meta["display_layout"] = preset
        pos = station_positions(g)
        self.assertEqual(pos, {sid: (xy[0], xy[1]) for sid, xy in preset.items()})

    def test_collinear_synthetic_layout_is_detected(self):
        """斜直线上排开的 30 站长链：属于"完全为直线"退化，必须重排。"""
        from core.graph import Graph, _add_edge, _add_line, _add_station, _finish

        g = Graph(meta={"name": "synthetic-straight"})
        _add_line(g, "L1", "测试线", 40.0, "#123456")
        for i in range(30):
            _add_station(g, f"S{i:02d}", f"站{i}", float(i) * 1.0, float(i) * 0.5)
        for i in range(29):
            _add_edge(g, f"S{i:02d}", f"S{i + 1:02d}", "L1", 1.0, 1.5)
        _finish(g)

        self.assertTrue(needs_schematic_layout(g))
        pos = station_positions(g)
        self.assertEqual(set(pos), set(g.stations))
        # 重排后不能再整条共线
        deviations = [abs(pos[f"S{i:02d}"][1] - pos["S00"][1] - 0.5 * (pos[f"S{i:02d}"][0] - pos["S00"][0]))
                      for i in range(30)]
        self.assertGreater(max(deviations), 1e-3)

    def test_disconnected_components_are_packed(self):
        """不连通图也要能出坐标：各分量横向排开、站点不重合。"""
        from core.graph import Graph, _add_edge, _add_line, _add_station, _finish

        g = Graph(meta={"layout": {}})
        _add_line(g, "A", "甲线", 40.0, "#111111")
        _add_line(g, "B", "乙线", 40.0, "#222222")
        for i in range(5):
            _add_station(g, f"A{i}", f"甲{i}", float(i), 0.0)
            _add_station(g, f"B{i}", f"乙{i}", 50.0 + i, 50.0)
            if i:
                _add_edge(g, f"A{i - 1}", f"A{i}", "A", 1.0, 1.5)
                _add_edge(g, f"B{i - 1}", f"B{i}", "B", 1.0, 1.5)
        _finish(g)
        pos = station_positions(g)
        self.assertEqual(set(pos), set(g.stations))
        # 两个分量不应叠在一起
        ax = [pos[f"A{i}"][0] for i in range(5)]
        bx = [pos[f"B{i}"][0] for i in range(5)]
        self.assertLess(max(ax), min(bx))

    def test_edges_only_graph_gets_derived_line_order(self):
        """工具/测试直接调模块、不传 line_sequences 时也能从 edges 还原站序。"""
        from core.graph import Graph, _add_edge, _add_line, _add_station, _finish

        g = Graph(meta={"layout": {}})
        _add_line(g, "L", "线", 40.0, "#333333")
        for i in range(26):
            _add_station(g, f"N{i:02d}", f"节点{i}", float(i) * 1.0, 0.0)
            if i:
                _add_edge(g, f"N{i - 1:02d}", f"N{i:02d}", "L", 1.0, 1.5)
        _finish(g)
        pos = station_positions(g)
        self.assertEqual(len(pos), 26)
        ys = {round(y, 6) for _, y in pos.values()}
        self.assertGreater(len(ys), 1, "重排后不应还是一条水平直线")

    def test_164_stress_dataset(self):
        """164 站验收数据：同样要求线段不重合（用户明确的验收规模）。"""
        path = DATA / "stress_164.json"
        if not path.exists():
            self.skipTest("stress_164.json 尚未生成")
        g = load_graph(path)
        self.assertEqual(len(g.stations), 164)
        seqs = line_sequences(g)
        self.assertTrue(needs_schematic_layout(g, seqs))
        pos = station_positions(g, seqs)
        self.assertEqual(set(pos), set(g.stations))
        assert_no_segment_overlap(self, g, pos)

    def test_layout_is_cached(self):
        """同一张图重复调用 station_positions 不应重复跑布局。"""
        from unittest import mock
        from ui import display_layout

        g = load_graph(DATA / "real_metro.json")
        seqs = line_sequences(g)
        display_layout._LAYOUT_CACHE.clear()      # 避免被前面用例的缓存干扰
        with mock.patch.object(display_layout, "schematic_layout",
                               wraps=display_layout.schematic_layout) as spied:
            first = display_layout.station_positions(g, seqs)
            second = display_layout.station_positions(g, seqs)
        self.assertEqual(first, second)
        self.assertEqual(spied.call_count, 1, "同图第二次调用应命中缓存")

    def test_layout_time_budget(self):
        """169 站布局必须足够快（首次建画布用），留足余量防性能回退。"""
        g = load_graph(DATA / "real_metro.json")
        seqs = line_sequences(g)
        started = time.perf_counter()
        station_positions(g, seqs)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 5.0, f"布局耗时 {elapsed:.2f}s 超过 5s 上限")


if __name__ == "__main__":
    unittest.main()
