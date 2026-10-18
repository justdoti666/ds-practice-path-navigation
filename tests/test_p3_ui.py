"""P3 交互层测试：用假 solver 驱动状态机与错误分支，不弹真窗口。

运行::

    python -m unittest tests.test_ui -v

要点：本文件不创建 ``Tk()``，所有依赖都注入到 :class:`ui.window.Controller`，
因此可以在 CI / 无桌面环境里跑。
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import solver as real_solver  # noqa: E402
from core.graph import Edge, Station  # noqa: E402
from ui.p3_compat import Graph, load_graph  # noqa: E402
from ui.window import (  # noqa: E402
    Controller,
    DataError,
    DisconnectedError,
    EmptyGraphError,
    NoSelectionError,
    SearchState,
    load_bench_rows,
    parse_station_label,
    station_label,
)


# ---------------------------------------------------------------------------
# 假 solver：可控制"找到 / 不连通 / 直接抛异常"
# ---------------------------------------------------------------------------
@dataclass
class FakeResult:
    algorithm: str = "dijkstra"
    found: bool = True
    path: list[str] = field(default_factory=lambda: ["S01", "S02", "S07"])
    distance_km: float = 3.4
    time_min: float = 7.0
    transfers: int = 0
    expanded: int = 3
    elapsed_ms: float = 0.12
    events: list[dict] = field(default_factory=list)


class FakeSolver:
    def __init__(self, result: FakeResult | None = None, error: Exception | None = None) -> None:
        self.result = result if result is not None else FakeResult()
        self.error = error
        self.calls: list[tuple] = []

    def __call__(self, graph, start, end, algorithm, weight):
        self.calls.append((start, end, algorithm, weight))
        if self.error is not None:
            raise self.error
        return self.result


def make_graph() -> Graph:
    """用真实 Graph API 造一张 3 站小图（S01—S02—S07，共 3.4 km）。"""
    graph = Graph()
    graph.lines["1号线"] = {"name": "1号线", "speed_kmh": 60.0, "color": "#e5484d"}
    for sid, name, x in (("S01", "中央公园", 0.0), ("S02", "人民广场", 10.0), ("S07", "大学城", 20.0)):
        graph.add_station(Station(sid, name, x, 0.0))
    graph.add_edge(Edge("S01", "S02", "1号线", 1.8, 4.0))
    graph.add_edge(Edge("S02", "S07", "1号线", 1.6, 3.0))
    return graph


def ready_controller(fake: FakeSolver | None = None, graph: Graph | None = None) -> Controller:
    controller = Controller(graph or make_graph(), solve_fn=fake or FakeSolver())
    controller.set_start("S01")
    controller.set_end("S07")
    return controller


# ---------------------------------------------------------------------------
# 1. 状态机主流程
# ---------------------------------------------------------------------------
class TestStateMachine(unittest.TestCase):
    def test_happy_path_transitions(self) -> None:
        controller = ready_controller()
        self.assertIs(controller.state, SearchState.READY)

        token = controller.begin_search()
        self.assertIs(controller.state, SearchState.COMPUTING)

        result = controller.perform(token)
        self.assertIsNone(controller.apply(token, result))

        self.assertIs(controller.state, SearchState.RESULT)
        self.assertEqual(controller.result.path, ["S01", "S02", "S07"])
        self.assertEqual(controller.solve_calls, 1)

    def test_repeated_clicks_only_solve_once(self) -> None:
        fake = FakeSolver()
        controller = ready_controller(fake)

        tokens = [controller.begin_search() for _ in range(5)]
        self.assertEqual(len(set(tokens)), 1, "连点应复用同一个 token")
        result = controller.perform(tokens[0])
        controller.apply(tokens[0], result)

        self.assertEqual(len(fake.calls), 1, "连点 5 次只能触发 1 次计算")
        self.assertIs(controller.state, SearchState.RESULT)

    def test_clear_keeps_selection_but_drops_result(self) -> None:
        controller = ready_controller()
        controller.run_search()
        self.assertIs(controller.state, SearchState.RESULT)

        controller.clear()
        self.assertIs(controller.state, SearchState.READY)
        self.assertIsNone(controller.result)
        self.assertEqual((controller.start_id, controller.end_id), ("S01", "S07"))

    def test_swap_exchanges_endpoints(self) -> None:
        controller = ready_controller()
        controller.swap()
        self.assertEqual((controller.start_id, controller.end_id), ("S07", "S01"))

    def test_cancel_invalidates_late_result(self) -> None:
        controller = ready_controller()
        token = controller.begin_search()
        controller.cancel()
        self.assertIs(controller.state, SearchState.READY)

        late = FakeResult()
        self.assertIsNone(controller.perform(token), "取消后不应再执行计算")
        self.assertIsNone(controller.apply(token, late), "迟到的结果应被丢弃")
        self.assertIs(controller.state, SearchState.READY)
        self.assertIsNone(controller.result)

    def test_cancelled_result_from_other_token_is_ignored(self) -> None:
        controller = ready_controller()
        first = controller.begin_search()
        controller.cancel()
        second = controller.begin_search()
        self.assertNotEqual(first, second)

        self.assertIsNone(controller.apply(first, FakeResult()))
        self.assertIs(controller.state, SearchState.COMPUTING)
        controller.apply(second, FakeResult())
        self.assertIs(controller.state, SearchState.RESULT)


# ---------------------------------------------------------------------------
# 2. 四类异常
# ---------------------------------------------------------------------------
class TestErrorBranches(unittest.TestCase):
    def test_missing_selection_raises(self) -> None:
        controller = Controller(make_graph())
        with self.assertRaises(NoSelectionError) as ctx:
            controller.begin_search()
        self.assertIn("起点和终点", ctx.exception.message)
        self.assertTrue(ctx.exception.hint, "异常应带可执行的建议")
        self.assertIs(controller.state, SearchState.IDLE)

    def test_empty_graph_raises(self) -> None:
        controller = Controller(Graph())
        with self.assertRaises(EmptyGraphError):
            controller.begin_search()

    def test_same_station_raises(self) -> None:
        from ui.window import SameStationError

        controller = Controller(make_graph())
        controller.set_start("S01")
        controller.set_end("S01")
        with self.assertRaises(SameStationError):
            controller.begin_search()

    def test_disconnected_becomes_error_state(self) -> None:
        fake = FakeSolver(FakeResult(found=False, path=[]))
        controller = ready_controller(fake)
        token = controller.begin_search()
        result = controller.perform(token)

        error = controller.apply(token, result)
        self.assertIsInstance(error, DisconnectedError)
        self.assertIn("中央公园", error.message)
        self.assertIn("大学城", error.message)
        self.assertIs(controller.state, SearchState.ERROR)
        self.assertIsNone(controller.result)

    def test_solver_exception_maps_to_data_error(self) -> None:
        """故意让 solver 抛异常：这里必须变成 DataError，而不是把程序带崩。"""
        fake = FakeSolver(error=RuntimeError("CSV 第 42 行缺列"))
        controller = ready_controller(fake)
        token = controller.begin_search()

        with self.assertRaises(DataError) as ctx:
            controller.perform(token)
        self.assertIn("CSV 第 42 行缺列", ctx.exception.message)
        self.assertIs(controller.state, SearchState.COMPUTING)

        controller.fail(token, ctx.exception)
        self.assertIs(controller.state, SearchState.ERROR)
        self.assertIs(controller.last_error, ctx.exception)


# ---------------------------------------------------------------------------
# 3. 下拉框文案
# ---------------------------------------------------------------------------
class TestStationLabels(unittest.TestCase):
    def test_label_roundtrip(self) -> None:
        label = station_label("人民广场", "S02")
        self.assertEqual(label, "人民广场（S02）")
        self.assertEqual(parse_station_label(label), "S02")

    def test_label_with_ascii_parentheses(self) -> None:
        self.assertEqual(parse_station_label("人民广场(S02)"), "S02")

    def test_empty_label(self) -> None:
        self.assertIsNone(parse_station_label(""))
        self.assertIsNone(parse_station_label("   "))


# ---------------------------------------------------------------------------
# 4. 三算法对比与 bench 同源
# ---------------------------------------------------------------------------
@unittest.skipUnless((ROOT / "data" / "bench.csv").exists(), "缺少 data/bench.csv")
class TestComparePanel(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = load_graph(ROOT / "data")
        self.rows, _status = load_bench_rows(ROOT / "data" / "bench.csv")

    def test_compare_three_algorithms_match_bench(self) -> None:
        controller = Controller(self.graph, bench_rows=self.rows)
        controller.set_start("S01")
        controller.set_end("S07")
        rows = controller.compare()

        self.assertEqual([row.algorithm for row in rows], ["dijkstra", "astar", "bfs"])
        for row in rows:
            self.assertIsNotNone(row.result, f"{row.algorithm} 应找到路径")
            self.assertTrue(row.matched, f"{row.algorithm} 与 bench.csv 不一致")

    def test_compare_marks_bench_mismatch(self) -> None:
        tampered = dict(self.rows)
        key = ("S01", "S07", "time", "dijkstra")
        bad = dict(tampered[key])
        bad["distance_km"] += 1.0
        tampered[key] = bad

        controller = Controller(self.graph, bench_rows=tampered)
        controller.set_start("S01")
        controller.set_end("S07")
        rows = {row.algorithm: row for row in controller.compare()}
        self.assertFalse(rows["dijkstra"].matched, "被改坏的基准必须被抓出来")
        self.assertTrue(rows["astar"].matched)

    def test_compare_without_selection_raises(self) -> None:
        controller = Controller(self.graph, bench_rows=self.rows)
        with self.assertRaises(NoSelectionError):
            controller.compare()


# ---------------------------------------------------------------------------
# 5. 真 solver 冒烟（确保换实现后接口没断）
# ---------------------------------------------------------------------------
class TestRealSolverSmoke(unittest.TestCase):
    def test_real_solver_through_controller(self) -> None:
        graph = make_graph()
        controller = Controller(graph, solve_fn=real_solver.solve)
        controller.set_start("S01")
        controller.set_end("S07")
        result = controller.run_search()
        self.assertTrue(result.found)
        self.assertAlmostEqual(result.distance_km, 3.4, places=2)
        self.assertEqual(result.path[0], "S01")
        self.assertEqual(result.path[-1], "S07")


if __name__ == "__main__":
    unittest.main(verbosity=2)
