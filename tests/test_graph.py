#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1 数据层单测（T01–T15）。

运行：
    python -m unittest discover -s tests -t . -v

覆盖四块：
  * 图模型：邻接双向、bbox、换乘站归属；
  * 加载：JSON/TXT 对拍、6 类坏数据各一条、新增数据集可加载；
  * 精度与自洽：R1 边权不取整、R2 几何不自洽必须拒绝；
  * 动态增删：add/remove 后邻接表与线路归属都不留悬空引用。
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.graph import DataError, Edge, Station, load_graph  # noqa: E402
from tests import support                                   # noqa: E402

DEMO_JSON = ROOT / "data" / "demo_city.json"
DEMO_TXT = ROOT / "data" / "demo_city.txt"
TINY_FILE = ROOT / "data" / "tiny_hand.json"
EDGE_FILE = ROOT / "data" / "edge_cases.json"


def demo():
    return load_graph(DEMO_JSON)


def assert_bad(testcase, spec: dict, note: str) -> None:
    """坏 spec 必须走真实加载路径抛 DataError，且错误信息非空。"""
    try:
        support.load_spec(spec)
    except DataError as exc:
        testcase.assertTrue(str(exc), f"{note}: DataError 信息为空")
        return
    except Exception as exc:                              # noqa: BLE001
        testcase.fail(f"{note}: 抛的是 {type(exc).__name__}，应为 DataError：{exc}")
    testcase.fail(f"{note}: 没有抛 DataError")


class TestGraphModel(unittest.TestCase):
    def test_t01_demo_load_counts(self):
        """T01 示例城市：10 站 / 10 边 / 4 条线。"""
        g = demo()
        self.assertEqual(len(g.stations), 10)
        self.assertEqual(len(g.edges), 10)
        self.assertEqual(len(g.lines), 4)

    def test_t02_neighbors_is_bidirectional(self):
        """T02 neighbors() 双向：每条边在两端都能查到，且条数等于邻接表长度。"""
        g = demo()
        for e in g.edges:
            self.assertIn(e, g.neighbors(e.u))
            self.assertIn(e, g.neighbors(e.v))
            self.assertEqual(e.other(e.u), e.v)
            self.assertEqual(e.other(e.v), e.u)
        total = sum(len(g.neighbors(sid)) for sid in g.stations)
        self.assertEqual(total, 2 * len(g.edges))

    def test_t03_bbox_matches_coordinates(self):
        """T03 bbox() 等于所有站点坐标的极值（P4 画布自适应依赖它）。"""
        g = demo()
        xs = [s.x for s in g.stations.values()]
        ys = [s.y for s in g.stations.values()]
        self.assertEqual(g.bbox(), (min(xs), min(ys), max(xs), max(ys)))
        self.assertEqual(g.bbox(), (0, 0, 7, 4.5))

    def test_t04_lines_of_station_finds_transfers(self):
        """T04 换乘站识别：示例城市有 4 个站归属 ≥2 条线（S2/S4/S6/S11）。"""
        g = demo()
        lines_of = g.lines_of_station()
        transfer = {sid for sid, ls in lines_of.items() if len(ls) > 1}
        self.assertEqual(transfer, {"S2", "S4", "S6", "S11"})
        self.assertEqual(lines_of["S2"], {"L1", "L2"})

    def test_t05_json_and_txt_are_equivalent(self):
        """T05 两种格式加载出同一张图：站点/边数一致，且逐边 distance 与 time 相同。"""
        gj, gt = load_graph(DEMO_JSON), load_graph(DEMO_TXT)
        self.assertEqual(sorted(gj.stations), sorted(gt.stations))
        self.assertEqual(len(gj.edges), len(gt.edges))
        key = lambda g: sorted((min(e.u, e.v), max(e.u, e.v), e.line,
                                round(e.distance_km, 9), round(e.time_min, 9))
                               for e in g.edges)
        self.assertEqual(key(gj), key(gt))


class TestBadData(unittest.TestCase):
    """6 类坏数据逐一被拒（P1-02 验收第 2 条）。"""

    def test_t06_duplicate_station_id(self):
        """T06 重复站点 id → DataError。"""
        assert_bad(self, {
            "lines": [{"id": "T", "name": "线", "speed_kmh": 40}],
            "stations": [{"id": "A", "name": "甲", "x": 0, "y": 0},
                         {"id": "A", "name": "重复", "x": 1, "y": 0}],
            "edges": [],
        }, "重复站点 id")

    def test_t07_unknown_line(self):
        """T07 边引用未定义线路 → DataError。"""
        assert_bad(self, {
            "lines": [{"id": "T", "name": "线", "speed_kmh": 40}],
            "stations": [{"id": "A", "name": "甲", "x": 0, "y": 0},
                         {"id": "B", "name": "乙", "x": 1, "y": 0}],
            "edges": [{"u": "A", "v": "B", "line": "NOPE"}],
        }, "未知线路")

    def test_t08_negative_distance(self):
        """T08 负距离 → DataError。"""
        assert_bad(self, {
            "lines": [{"id": "T", "name": "线", "speed_kmh": 40}],
            "stations": [{"id": "A", "name": "甲", "x": 0, "y": 0},
                         {"id": "B", "name": "乙", "x": 1, "y": 0}],
            "edges": [{"u": "A", "v": "B", "line": "T", "distance_km": -3}],
        }, "负距离")

    def test_t09_self_loop(self):
        """T09 自环边（u == v）→ DataError。"""
        assert_bad(self, {
            "lines": [{"id": "T", "name": "线", "speed_kmh": 40}],
            "stations": [{"id": "A", "name": "甲", "x": 0, "y": 0},
                         {"id": "B", "name": "乙", "x": 1, "y": 0}],
            "edges": [{"u": "A", "v": "A", "line": "T"}],
        }, "自环边")

    def test_t10_duplicate_edge(self):
        """T10 同一 (u, v, line) 重复出现 → DataError。"""
        assert_bad(self, {
            "lines": [{"id": "T", "name": "线", "speed_kmh": 40}],
            "stations": [{"id": "A", "name": "甲", "x": 0, "y": 0},
                         {"id": "B", "name": "乙", "x": 1, "y": 0}],
            "edges": [{"u": "A", "v": "B", "line": "T"},
                      {"u": "B", "v": "A", "line": "T"}],
        }, "重复边")

    def test_t11_missing_field(self):
        """T11 站点缺字段（无 x）→ DataError。"""
        assert_bad(self, {
            "lines": [{"id": "T", "name": "线", "speed_kmh": 40}],
            "stations": [{"id": "A", "name": "甲", "x": 0, "y": 0},
                         {"id": "B", "name": "乙", "y": 0}],
            "edges": [{"u": "A", "v": "B", "line": "T"}],
        }, "站点缺字段")


class TestPrecisionAndConsistency(unittest.TestCase):
    def test_t13_edge_keeps_full_precision(self):
        """T13（R1）边权不取整：无显式距离的边，distance_km 必须精确等于坐标欧氏距离。"""
        g = demo()
        edges = [e for e in g.edges if e.u == "S3" and e.v == "S4"
                 or e.u == "S4" and e.v == "S3"]
        self.assertTrue(edges, "示例城市应有 S3-S4 边")
        e = edges[0]
        exact = math.hypot(6 - 4, 1 - 0.5)          # S3(4,0.5) → S4(6,1)
        self.assertNotEqual(round(exact, 3), exact)  # 该值取整到 3 位会变，下面的严格相等才有判别力
        self.assertEqual(e.distance_km, exact)       # 严格相等：证明没有 round(..., 3)
        self.assertAlmostEqual(e.time_min, exact / 35 * 60, places=12)

    def test_t14_geometric_inconsistency_rejected(self):
        """T14（R2）显式 time_min 比"全网最快速度"还快 → DataError（否则 A* 会高估）。"""
        assert_bad(self, {
            "lines": [{"id": "L", "name": "慢线", "speed_kmh": 40},
                      {"id": "X", "name": "快线", "speed_kmh": 120}],
            "stations": [{"id": "A", "name": "甲", "x": 0, "y": 0},
                         {"id": "B", "name": "乙", "x": 10, "y": 0}],
            # 10 km 按 120 km/h 至少要 5 min，这里只给 0.1 min
            "edges": [{"u": "A", "v": "B", "line": "L", "time_min": 0.1}],
        }, "时间与速度不自洽")

    def test_t15_new_datasets_loadable(self):
        """T15 新增数据集可加载：tiny_hand 精确边权；edge_cases 含孤立站与长空档。"""
        tiny = load_graph(TINY_FILE)
        self.assertEqual(len(tiny.stations), 3)
        dists = sorted(round(e.distance_km, 9) for e in tiny.edges)
        self.assertEqual(dists, [5.0, 5.0, 6.0])
        self.assertEqual(sorted(round(e.time_min, 9) for e in tiny.edges), [5.0, 5.0, 6.0])

        edge = load_graph(EDGE_FILE)
        self.assertEqual(len(edge.stations), 5)
        self.assertEqual(edge.neighbors("Z1"), [])          # 孤立站：没有边
        self.assertEqual(len(edge.neighbors("Z1")), 0)
        longest = max(e.distance_km for e in edge.edges)
        self.assertGreater(longest, 90)                     # 长空档 ≥ 90 km


class TestMutation(unittest.TestCase):
    def test_t12_dynamic_add_and_remove(self):
        """T12 动态增删：加站加边可查；删站级联删边并返回条数；删完无悬空引用。"""
        g = support.tiny_hand()          # A-B、B-C、A-C 三条边
        self.assertEqual(len(g.edges), 3)

        g.add_station(Station("D", "丁站", 9.0, 0.0))
        g.add_edge(Edge("C", "D", "T", 3.0, 3.0))
        self.assertEqual(len(g.edges), 4)
        self.assertEqual(len(g.neighbors("D")), 1)
        self.assertEqual(g.lines_of_station()["D"], {"T"})

        removed = g.remove_station("C")  # C 连 A、B、D 三条边
        self.assertEqual(removed, 3)
        self.assertNotIn("C", g.stations)
        self.assertEqual(len(g.edges), 1)
        for e in g.edges:
            self.assertIn(e.u, g.stations)
            self.assertIn(e.v, g.stations)
        for sid in g.stations:            # 邻接表不得留下指向已删站点的边
            for e in g.neighbors(sid):
                self.assertNotIn("C", (e.u, e.v))

        self.assertEqual(g.remove_edge("A", "B", "T"), 1)
        self.assertEqual(g.edges, [])
        self.assertEqual(g.remove_edge("A", "B", "T"), 0)   # 再删一次返回 0，不抛错
        self.assertEqual(g.neighbors("A"), [])

        with self.assertRaises(DataError):
            g.remove_station("不存在")

    def test_t12b_add_rejects_bad_edge(self):
        """T12b 动态加边同样走校验：未知线路 / 自环 / 重复边都应被拒。"""
        g = support.tiny_hand()
        for bad_edge in (Edge("A", "B", "NOPE", 1.0, 1.0),
                         Edge("A", "A", "T", 1.0, 1.0),
                         Edge("A", "B", "T", 5.0, 5.0)):
            with self.assertRaises(DataError):
                g.add_edge(bad_edge)


if __name__ == "__main__":
    unittest.main(verbosity=2)
