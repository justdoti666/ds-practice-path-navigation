"""程序入口：``python main.py`` 打开界面，``python main.py --check`` 跑自检。

自检（--check）覆盖 P3 的质量门：
* 数据能装载、最小路网规模正确；
* Dijkstra / A* / BFS 三条算法都能出结果，A* 与 Dijkstra 代价一致；
* 不连通、空图、未选点三条异常路径按预期抛出；
* ``bench.csv`` 与当前 solver 同源（重算后逐行比对）；
* ``ui/window.py`` 里没有 Canvas 绘图调用（P3-06 解耦证据）；
* 交互层测试文件存在且用例数 ≥ 6，截图包 ≥ 5 张。
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
SHOT_DIR = ROOT / "docs" / "截图"


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------
def run_ui(data_dir: Path | None, force_empty: bool) -> int:
    import tkinter as tk

    _enable_dpi_awareness()
    from ui.window import build_window

    root = tk.Tk()
    build_window(root, data_dir=data_dir, force_empty=force_empty)
    root.update_idletasks()
    width, height = 1180, 720
    x = max(0, (root.winfo_screenwidth() - width) // 2)
    y = max(0, (root.winfo_screenheight() - height) // 3)
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.lift()
    root.mainloop()
    return 0


def _enable_dpi_awareness() -> None:
    """让高分屏下的控件与截图保持清晰（失败不影响运行）。"""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
class CheckFailed(AssertionError):
    pass


def _expect(condition, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def _check_python() -> str:
    import tkinter

    _expect(sys.version_info >= (3, 10), f"Python 版本过低：{sys.version.split()[0]}")
    return f"Python {sys.version.split()[0]} / Tk {tkinter.TkVersion}"


def _check_data() -> str:
    from core.graph import load_graph

    graph = load_graph(DATA_DIR)
    _expect(len(graph.stations) >= 10, f"站点太少：{len(graph.stations)}")
    _expect(len(graph.iter_edges()) >= 10, f"区间太少：{len(graph.iter_edges())}")
    for sid, station in graph.stations.items():
        _expect(bool(station.name), f"站点 {sid} 没有名字")
    return f"{len(graph.stations)} 站 / {len(graph.iter_edges())} 区间"


def _check_routes() -> str:
    from core import solver
    from core.graph import load_graph

    graph = load_graph(DATA_DIR)
    result = solver.solve(graph, "S01", "S07", "dijkstra", "time")
    _expect(result.found, "S01->S07 应连通")
    _expect(abs(result.distance_km - 10.3) <= 0.05, f"里程不对：{result.distance_km}")
    _expect(abs(result.time_min - 22.0) <= 0.1, f"时间不对：{result.time_min}")
    _expect(result.transfers == 0, f"1 号线直达不应换乘：{result.transfers}")
    return f"S01->S07 {result.distance_km} km / {result.time_min} min / 扩展 {result.expanded}"


def _check_astar() -> str:
    from core import solver
    from core.graph import load_graph

    graph = load_graph(DATA_DIR)
    pairs = [("S01", "S17"), ("S08", "S12"), ("S13", "S07"), ("S12", "S16")]
    for weight in ("time", "distance"):
        for start, end in pairs:
            d = solver.solve(graph, start, end, "dijkstra", weight)
            a = solver.solve(graph, start, end, "astar", weight)
            _expect(d.found and a.found, f"{start}->{end} 应连通")
            key = "time_min" if weight == "time" else "distance_km"
            _expect(
                abs(getattr(d, key) - getattr(a, key)) <= 0.05,
                f"{start}->{end} 在 {weight} 下 A*={getattr(a, key)} 与 Dijkstra={getattr(d, key)} 不一致",
            )
    return f"{len(pairs)} 组 OD × 2 权重，A* 与 Dijkstra 代价一致"


def _check_bfs() -> str:
    from core import solver
    from core.graph import load_graph

    graph = load_graph(DATA_DIR)
    result = solver.solve(graph, "S12", "S16", "bfs", "time")
    _expect(result.found, "S12->S16 应连通")
    _expect(len(result.path) >= 2, "BFS 路径过短")
    _expect(result.path[0] == "S12" and result.path[-1] == "S16", "BFS 路径首尾不对")
    return f"BFS {len(result.path) - 1} 段 / 扩展 {result.expanded}"


def _check_disconnected() -> str:
    from core import solver
    from core.graph import load_graph

    graph = load_graph(DATA_DIR)
    result = solver.solve(graph, "S01", "S18", "dijkstra", "time")
    _expect(not result.found, "S01->S18 应不连通（S1 线在建）")
    return "S01->S18 正确返回不连通"


def _check_bench() -> str:
    from core import solver
    from core.graph import load_graph

    graph = load_graph(DATA_DIR)
    bench_path = DATA_DIR / "bench.csv"
    _expect(bench_path.exists(), "缺少 data/bench.csv，先运行 python tools/make_bench.py")
    rows = 0
    with bench_path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            rows += 1
            result = solver.solve(
                graph, row["start_id"], row["end_id"], row["algorithm"], row["weight"]
            )
            _expect(result.found, f"bench 行不连通：{row}")
            _expect(
                abs(result.distance_km - float(row["distance_km"])) <= 0.011,
                f"bench 里程不一致：{row['start_id']}->{row['end_id']} {row['algorithm']}",
            )
            _expect(
                abs(result.time_min - float(row["time_min"])) <= 0.051,
                f"bench 时间不一致：{row['start_id']}->{row['end_id']} {row['algorithm']}",
            )
            _expect(
                result.transfers == int(float(row["transfers"])),
                f"bench 换乘不一致：{row['start_id']}->{row['end_id']} {row['algorithm']}",
            )
            _expect(
                result.expanded == int(float(row["expanded"])),
                f"bench 扩展数不一致：{row['start_id']}->{row['end_id']} {row['algorithm']}",
            )
    _expect(rows >= 12, f"bench 行数太少：{rows}")
    return f"bench.csv {rows} 行与 solver 逐项一致"


def _check_ui_controller() -> str:
    from core import solver
    from core.graph import load_graph
    from ui.window import (
        Controller,
        DisconnectedError,
        EmptyGraphError,
        NoSelectionError,
        SearchState,
        parse_station_label,
    )

    _expect(parse_station_label("人民广场（S02）") == "S02", "站名（ID）解析失败")
    _expect(parse_station_label("") is None, "空文案应解析为 None")

    graph = load_graph(DATA_DIR)
    ctrl = Controller(graph)
    try:
        ctrl.begin_search()
    except NoSelectionError:
        pass
    else:
        raise CheckFailed("未选点时应抛 NoSelectionError")

    ctrl.set_start("S01")
    ctrl.set_end("S07")
    _expect(ctrl.state is SearchState.READY, f"选好点后应为 READY，实际 {ctrl.state}")
    token = ctrl.begin_search()
    _expect(ctrl.state is SearchState.COMPUTING, "开始搜索后应为 COMPUTING")
    for _ in range(4):
        _expect(ctrl.begin_search() == token, "连点应复用同一个 token")
    result = ctrl.perform(token)
    _expect(ctrl.solve_calls == 1, f"连点 5 次只应计算 1 次，实际 {ctrl.solve_calls}")
    ctrl.apply(token, result)
    _expect(ctrl.state is SearchState.RESULT, "结果落地后应为 RESULT")

    ctrl.set_end("S18")
    try:
        ctrl.run_search()
    except DisconnectedError:
        pass
    else:
        raise CheckFailed("不连通时应抛 DisconnectedError")

    empty = Controller(type(graph)())
    try:
        empty.begin_search()
    except EmptyGraphError:
        pass
    else:
        raise CheckFailed("空图时应抛 EmptyGraphError")
    return "4 条异常分支 + 连点防抖 + 状态流转正常"


def _check_decoupling() -> str:
    window_src = (ROOT / "ui" / "window.py").read_text(encoding="utf-8")
    forbidden = ("create_line(", "create_oval(", "create_rectangle(", "create_polygon(", "create_text(")
    hits = [name for name in forbidden if name in window_src]
    _expect(not hits, f"ui/window.py 里仍有绘图调用：{hits}")
    canvas_src = (ROOT / "ui" / "canvas.py").read_text(encoding="utf-8")
    _expect("create_line(" in canvas_src, "ui/canvas.py 应负责绘图")
    return "window.py 0 处绘图调用，绘图全部在 canvas.py"


def _check_tests() -> str:
    path = ROOT / "tests" / "test_ui.py"
    _expect(path.exists(), "缺少 tests/test_ui.py")
    source = path.read_text(encoding="utf-8")
    count = source.count("def test_")
    _expect(count >= 6, f"交互层用例应 ≥ 6 条，实际 {count}")
    return f"tests/test_ui.py 共 {count} 条用例"


def _check_screenshots() -> str:
    shots = sorted(SHOT_DIR.glob("*.png")) if SHOT_DIR.exists() else []
    _expect(len(shots) >= 5, f"截图应 ≥ 5 张，实际 {len(shots)}")
    tiny = [shot.name for shot in shots if shot.stat().st_size < 8_000]
    _expect(not tiny, f"截图疑似空白：{tiny}")
    return f"{len(shots)} 张截图"


CHECKS = [
    ("运行环境", _check_python),
    ("路网数据", _check_data),
    ("最短路径", _check_routes),
    ("A* 一致性", _check_astar),
    ("BFS 可达", _check_bfs),
    ("不连通判定", _check_disconnected),
    ("bench 同源", _check_bench),
    ("界面状态机", _check_ui_controller),
    ("绘图解耦", _check_decoupling),
    ("交互层测试", _check_tests),
    ("截图包", _check_screenshots),
]


def run_check() -> int:
    print("=" * 68)
    print(" 城市交通路径导航 / 自检（python main.py --check）")
    print("=" * 68)
    failures = 0
    for index, (name, func) in enumerate(CHECKS, start=1):
        try:
            detail = func()
        except CheckFailed as exc:
            failures += 1
            print(f"[{index:02d}/{len(CHECKS)}] FAIL  {name:<10} {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[{index:02d}/{len(CHECKS)}] FAIL  {name:<10} {type(exc).__name__}: {exc}")
        else:
            print(f"[{index:02d}/{len(CHECKS)}] OK    {name:<10} {detail}")
    print("-" * 68)
    if failures:
        print(f"自检未通过：{failures} 项失败")
        return 1
    print("全部通过")
    return 0


def _make_console_safe() -> None:
    """Windows 控制台多为 GBK：无法编码的字符用 ? 代替，避免自检自己崩在 print 上。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    _make_console_safe()
    parser = argparse.ArgumentParser(description="城市交通路径导航系统（P3 界面）")
    parser.add_argument("--check", action="store_true", help="运行自检，不打开窗口")
    parser.add_argument("--data", type=Path, default=None, help="数据目录（默认 data/）")
    parser.add_argument("--empty", action="store_true", help="演示用：强制空图，展示空态")
    args = parser.parse_args(argv)
    if args.check:
        return run_check()
    return run_ui(args.data, args.empty)


if __name__ == "__main__":
    raise SystemExit(main())
