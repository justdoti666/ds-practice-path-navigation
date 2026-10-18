#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P2 单测矩阵（T01–T17，15+ 条）。

运行：
    python -m unittest discover -s tests -t . -v

判别力（任务书 P2-06 的硬要求）：每条断言都在实现写错时会失败。
其中最特别的是 T15 —— 它是"元测试"：注入一个**非可采纳**的启发式，
证明本套测试能抓住"h 写错导致结果非最优"这类最容易混过去的 bug。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import solver                       # noqa: E402
from core.graph import DataError, load_graph  # noqa: E402
from tests import support                     # noqa: E402

DEMO = ROOT / "data" / "demo_city.json"


def demo():
    return load_graph(DEMO)


def max_lines(g) -> int:
    return max((len(v) for v in g.lines_of_station().values()), default=1)


class TestBasicCorrectness(unittest.TestCase):
    def test_t01_tiny_hand_math(self):
        """T01 3 站手算图：A→C 直连 6 km / 6 min；绕行 A-B-C 为 10 / 10。"""
        g = support.tiny_hand()
        r = solver.dijkstra(g, "A", "C", "distance")
        self.assertEqual(r.path, ["A", "C"])
        self.assertAlmostEqual(r.distance_km, 6.0, places=6)
        r2 = solver.dijkstra(g, "A", "C", "time")
        self.assertAlmostEqual(r2.time_min, 6.0, places=6)
        km, tm, seq, sw = solver.analyze_path(g, ["A", "B", "C"])
        self.assertAlmostEqual(km, 10.0, places=6)
        self.assertAlmostEqual(tm, 10.0, places=6)
        self.assertEqual(sw, 0)
        self.assertEqual(len(seq), 2)

    def test_t22_json_with_bom(self):
        """T22 Windows 编辑器常给 JSON 写 BOM；加载器必须能读（utf-8-sig）。"""
        import json, tempfile
        from pathlib import Path as _P
        spec = {"lines": [{"id": "L", "name": "线", "speed_kmh": 40}],
                "stations": [{"id": "A", "name": "甲", "x": 0, "y": 0},
                             {"id": "B", "name": "乙", "x": 1, "y": 0}],
                "edges": [{"u": "A", "v": "B", "line": "L"}]}
        fd, path = tempfile.mkstemp(suffix=".json")
        with open(fd, "w", encoding="utf-8-sig") as fh:   # 关键：带 BOM 写
            json.dump(spec, fh, ensure_ascii=False)
        try:
            g = load_graph(path)
            self.assertEqual(len(g.stations), 2)
        finally:
            _P(path).unlink(missing_ok=True)

    def test_t02_dual_weight_diverges(self):
        """T02 双权重：慢线更短（6 km/36 min），快线最快（10 km/5 min）。"""
        g = support.dual_weight()
        short = solver.dijkstra(g, "S", "T", "distance")
        fast = solver.dijkstra(g, "S", "T", "time")
        self.assertAlmostEqual(short.distance_km, 6.0, places=3)
        self.assertAlmostEqual(short.time_min, 36.0, places=1)
        self.assertAlmostEqual(fast.distance_km, 10.0, places=3)
        self.assertAlmostEqual(fast.time_min, 5.0, places=1)
        self.assertNotEqual(fast.path, short.path, "两种口径不应给出同一条路径")
        self.assertLess(fast.time_min, short.time_min)
        self.assertLess(short.distance_km, fast.distance_km)


class TestOptimality(unittest.TestCase):
    def test_t03_astar_equals_dijkstra_on_demo(self):
        """T03 示例城市全点对：A* 与 Dijkstra 代价一致（可采纳 ⇒ 最优）。"""
        g = demo()
        pairs = support.all_pairs(g)
        total_astar = total_dij = 0
        for w in ("time", "distance"):
            key = "time_min" if w == "time" else "distance_km"
            for a, b in pairs:
                d = solver.dijkstra(g, a, b, w)
                s = solver.astar(g, a, b, w)
                self.assertAlmostEqual(getattr(d, key), getattr(s, key), places=9,
                                       msg=f"{w} {a}->{b} 代价不一致")
                total_astar += s.expanded
                total_dij += d.expanded
        self.assertLessEqual(total_astar, total_dij, "A* 总扩展数应不多于 Dijkstra")

    def test_t04_astar_equals_dijkstra_on_random(self):
        """T04 随机图（3 个种子）全点对同样一致 —— 防止只在示范数据上碰巧正确。"""
        for seed in (1, 2, 3):
            g = support.random_graph(seed, n=7, extra=3)
            for w in ("time", "distance"):
                key = "time_min" if w == "time" else "distance_km"
                for a, b in support.all_pairs(g):
                    d = solver.dijkstra(g, a, b, w)
                    s = solver.astar(g, a, b, w)
                    self.assertAlmostEqual(getattr(d, key), getattr(s, key), places=9,
                                           msg=f"seed={seed} {w} {a}->{b}")

    def test_t14_heuristic_admissible_strict(self):
        """T14a 严格可采纳性：权重可精确表示的小图上，h 必须 ≤ 真实最优（两种权重）。"""
        g = support.tiny_hand()
        for w in ("time", "distance"):
            for a, b in support.all_pairs(g):
                h = solver._heuristic(g, a, b, w)
                opt = support.raw_optimal_cost(g, a, b, w)
                self.assertLessEqual(h, opt + 1e-9, f"{w} {a}->{b} h={h} opt={opt}")

    def test_t14b_heuristic_admissible_tolerant(self):
        """T14b 容差可采纳性：数据层把边权取整到 2 位小数（每条边误差 ≤0.005），
        几何 h 可能有 ≤1% 的微小高估；允许 1% 相对容差，但仍拒绝量级错误。
        （典型量级错误：时间权重忘了除 v_max —— 会高估约 80/35≈2.3 倍。）"""
        graphs = [demo()] + [support.random_graph(s, n=6, extra=2) for s in (7, 8)]
        for g in graphs:
            for w in ("time", "distance"):
                for a, b in support.all_pairs(g):
                    h = solver._heuristic(g, a, b, w)
                    opt = support.raw_optimal_cost(g, a, b, w)
                    self.assertLessEqual(h, opt * 1.01 + 0.01,
                                         f"h 高估超过容差：{a}->{b} h={h} opt={opt}")
                    self.assertLessEqual(h, opt * 1.5 + 0.05,
                                         f"h 量级错误：{a}->{b} h={h} opt={opt}")
    def test_t15_inadmissible_heuristic_is_caught(self):
        """T15 元测试：注入曼哈顿类 h（非可采纳）后结果变差 ⇒ 测试确实有判别力。"""
        g = support.h_trap()
        good = solver.astar(g, "S", "T", "time")
        self.assertAlmostEqual(good.time_min, 10.0, places=3, msg="内置 h 应得到最优 10.0")
        bad = solver.astar(g, "S", "T", "time", heuristic=support.manhattan)
        # 次优路径 S→Q→T：Q(99,1)→T(100,0) 直线距离 √2 km，慢线 100 km/h ⇒ 0.85 min
        self.assertAlmostEqual(bad.time_min, 10.75, places=2,
                               msg="注入非可采纳 h 应落到次优 10.75（若这里相等，说明测试失去判别力）")
        self.assertGreater(bad.time_min, good.time_min)


class TestTransfers(unittest.TestCase):
    def test_t05_bfs_matches_bruteforce(self):
        """T05 最少换乘：与"枚举所有简单路径"的独立 oracle 对拍（示例图 + 随机图）。"""
        graphs = [demo()] + [support.random_graph(s, n=6, extra=2) for s in (11, 12)]
        checked = 0
        for g in graphs:
            for a, b in support.all_pairs(g):
                want = support.brute_min_transfers(g, a, b)
                got = solver.bfs_transfers(g, a, b).transfers
                self.assertEqual(want, got, f"{a}->{b} 暴力={want} vs BFS={got}")
                checked += 1
        self.assertGreaterEqual(checked, 30)

    def test_t07_parallel_lines_prefer_fewer_switches(self):
        """T07 平行线路：同一区间有两线时，DP 选"换乘更少"的组合而非更快的。"""
        g = support.parallel_lines()
        km, tm, seq, sw = solver.analyze_path(g, ["A", "B", "C"])
        self.assertEqual(seq, ["L1", "L1"], "应全程坐 L1（换乘 0 次）")
        self.assertEqual(sw, 0)
        # 加载器把每条边时间取整到 2 位：单段 60/35=1.714 → 1.71，两段 3.42
        self.assertAlmostEqual(tm, 2 * round(60 / 35, 2), places=6)
        # 更快但多换一次的方案：L2 + L1
        fast_mixed = 60 / 80 + 60 / 35
        self.assertLess(fast_mixed, tm, "L2 方案应更快，但换乘数更高")

    def test_t16_transfers_consistent_with_path(self):
        """T16 换乘数三处一致：BFS 结果 = analyze_path(其路径) = 暴力最小值。"""
        g = demo()
        for a, b in support.all_pairs(g):
            r = solver.bfs_transfers(g, a, b)
            _km, _tm, _seq, sw = solver.analyze_path(g, r.path)
            self.assertEqual(r.transfers, sw, f"{a}->{b} 路径换乘数不一致")
            self.assertEqual(r.transfers, support.brute_min_transfers(g, a, b), f"{a}->{b} 非最小")


class TestEdgeCases(unittest.TestCase):
    def test_t06_same_station(self):
        """T06 起终点相同：0 km / 0 min / 路径只有它自己。"""
        g = demo()
        for r in (solver.dijkstra(g, "S1", "S1"), solver.astar(g, "S1", "S1", "distance"),
                  solver.bfs_transfers(g, "S1", "S1")):
            self.assertEqual(r.path, ["S1"])
            self.assertAlmostEqual(r.distance_km, 0.0)
            self.assertAlmostEqual(r.time_min, 0.0)
        self.assertEqual(solver.bfs_transfers(g, "S1", "S1").transfers, 0)

    def test_t09_disconnected_raises(self):
        """T09 不连通：三种算法都必须抛 PathNotFound，且信息可读。"""
        g = support.disconnected()
        calls = [
            lambda: solver.dijkstra(g, "A", "Z"),
            lambda: solver.astar(g, "A", "Z"),
            lambda: solver.bfs_transfers(g, "A", "Z"),
            lambda: solver.bfs_transfers(g, "Z", "A"),   # 起点无线路
        ]
        for fn in calls:
            with self.assertRaises(solver.PathNotFound) as ctx:
                fn()
            self.assertTrue(str(ctx.exception), "异常信息不能为空")

    def test_t10_invalid_params(self):
        """T10 参数校验：未知站点/权重/算法 → DataError；别名可正常派发。"""
        g = demo()
        with self.assertRaises(DataError):
            solver.dijkstra(g, "S1", "NOT_A_STATION")
        with self.assertRaises(DataError):
            solver.dijkstra(g, "S1", "S2", "speed")
        with self.assertRaises(DataError):
            solver.solve(g, "S1", "S2", "nope")
        for alias in ("dijkstra", "d", "astar", "a*", "a", "bfs", "b"):
            self.assertTrue(solver.solve(g, "S1", "S2", alias).found, alias)

    def test_t08_analyze_path_sums_match(self):
        """T08 analyze_path 返回的 km/min 必须等于其所选线路序列上各边之和。"""
        g = demo()
        for a, b in support.all_pairs(g)[:12]:
            r = solver.dijkstra(g, a, b, "time")
            km, tm, seq, _sw = solver.analyze_path(g, r.path)
            self.assertEqual(len(seq), len(r.path) - 1)
            tot_km = tot_tm = 0.0
            for (u, v), line in zip(zip(r.path, r.path[1:]), seq):
                edge = next(e for e in g.edge_between(u, v) if e.line == line)
                tot_km += edge.distance_km
                tot_tm += edge.time_min
            self.assertAlmostEqual(km, tot_km, places=3)
            self.assertAlmostEqual(tm, tot_tm, places=2)

    def test_t17_degenerate_paths(self):
        """T17 退化路径：空/单点返回零值；相邻重复站点明确报错（契约文档化）。"""
        g = support.tiny_hand()
        self.assertEqual(solver.analyze_path(g, []), (0.0, 0.0, [], 0))
        self.assertEqual(solver.analyze_path(g, ["A"]), (0.0, 0.0, [], 0))
        with self.assertRaises(solver.PathNotFound):
            solver.analyze_path(g, ["A", "A"])


class TestLinesAndResolve(unittest.TestCase):
    def test_t19_result_lines_match_analyze_path(self):
        """T19 Result.lines 与 analyze_path 一致，且按 lines 累计的指标等于 Result 指标。"""
        g = demo()
        for algo in ("dijkstra", "astar"):
            for a, b in support.all_pairs(g)[:10]:
                r = solver.solve(g, a, b, algo, "time")
                _km, _tm, seq, sw = solver.analyze_path(g, r.path)
                self.assertEqual(r.lines, seq)
                self.assertEqual(len(r.lines), len(r.path) - 1)
                self.assertEqual(r.transfers, sw)
                tot = 0.0
                for (u, v), ln in zip(zip(r.path, r.path[1:]), r.lines):
                    edge = next(e for e in g.edge_between(u, v) if e.line == ln)
                    tot += edge.distance_km
                self.assertAlmostEqual(r.distance_km, tot, places=3)

    def test_t20_duplicate_names_and_resolve_ambiguity(self):
        """T20 站名重复必须被拒；resolve 遇到歧义要报错（不能静默取第一个）。"""
        spec = {"lines": [{"id": "L", "name": "线", "speed_kmh": 40}],
                "stations": [{"id": "A", "name": "同名", "x": 0, "y": 0},
                             {"id": "B", "name": "同名", "x": 1, "y": 0}],
                "edges": [{"u": "A", "v": "B", "line": "L"}]}
        with self.assertRaises(DataError):
            support.load_spec(spec)
        from main import resolve
        from core.graph import Graph, Station
        g = Graph()
        g.stations = {"A": Station("A", "同名", 0.0, 0.0), "B": Station("B", "同名", 1.0, 0.0)}
        with self.assertRaises(DataError):
            resolve(g, "同名")
        self.assertEqual(resolve(g, "A"), "A")

    def test_t21_path_metrics_skips_transfer_hops(self):
        """T21 path_metrics 对“同站换乘”段按 0 距离/0 时间处理，不再抛异常。"""
        g = demo()
        self.assertEqual(g.path_metrics(["S1"]), (0.0, 0.0))
        km2, tm2 = g.path_metrics(["S2", "S2", "S3"])
        km, tm = g.path_metrics(["S2", "S3"])
        self.assertAlmostEqual(km2, km, places=6)
        self.assertAlmostEqual(tm2, tm, places=6)


class TestPickPair(unittest.TestCase):
    def test_t18_pick_connected_pair(self):
        """T18 冒烟选点：优先 S4→S11；否则取首个连通分量里字典序最小的两站；全孤立返回 None。"""
        g = demo()
        self.assertEqual(solver.pick_connected_pair(g), ("S4", "S11"))
        tiny = support.tiny_hand()          # A-B-C 三角，无 S4/S11
        self.assertEqual(solver.pick_connected_pair(tiny), ("A", "B"))
        orphan = support.load_spec({
            "meta": {"name": "orphan"},
            "lines": [{"id": "L", "name": "线", "speed_kmh": 40}],
            "stations": [{"id": "A", "name": "甲", "x": 0, "y": 0},
                         {"id": "B", "name": "乙", "x": 1, "y": 0}],
            "edges": [],
        })
        self.assertIsNone(solver.pick_connected_pair(orphan))

class TestMetrics(unittest.TestCase):
    def test_t11_events_semantics(self):
        """T11 事件语义：数量等于 expanded、末事件是终点、路径节点都被扩展过。"""
        g = demo()
        for r in (solver.dijkstra(g, "S1", "S11"),
                  solver.astar(g, "S1", "S11", "distance"),
                  solver.bfs_transfers(g, "S1", "S11")):
            self.assertEqual(len(r.events), r.expanded)
            self.assertTrue(all(kind == "settle" for kind, _ in r.events))
            self.assertEqual(r.events[-1][1], "S11")
            settled = {sid for _, sid in r.events}
            self.assertTrue(set(r.path) <= settled, "路径上的站点都应被扩展过")

    def test_t12_expanded_bounds(self):
        """T12 扩展计数落在合理区间：≥1；Dijkstra/A* ≤ V；BFS ≤ V×平均线路数。"""
        g = demo()
        v = len(g.stations)
        for a, b in support.all_pairs(g)[:20]:
            d = solver.dijkstra(g, a, b)
            s = solver.astar(g, a, b)
            f = solver.bfs_transfers(g, a, b)
            for name, r, upper in (("dijkstra", d, v), ("astar", s, v),
                                   ("bfs", f, v * max_lines(g))):
                self.assertGreaterEqual(r.expanded, 1, name)
                self.assertLessEqual(r.expanded, upper, f"{name} 扩展数异常：{r.expanded}>{upper}")

    def test_t13_deterministic(self):
        """T13 确定性：同一输入连跑两次，路径/扩展数/指标逐字段一致。"""
        g = demo()
        for fn, args in ((solver.dijkstra, ("S4", "S11", "time")),
                         (solver.astar, ("S4", "S11", "distance")),
                         (solver.bfs_transfers, ("S1", "S10"))):
            a = fn(g, *args)
            b = fn(g, *args)
            self.assertEqual(a.path, b.path, fn.__name__)
            self.assertEqual(a.expanded, b.expanded, fn.__name__)
            self.assertEqual(a.distance_km, b.distance_km, fn.__name__)
            self.assertEqual(a.time_min, b.time_min, fn.__name__)
            self.assertEqual(a.transfers, b.transfers, fn.__name__)
            self.assertEqual(len(a.events), len(b.events), fn.__name__)


if __name__ == "__main__":
    unittest.main(verbosity=2)