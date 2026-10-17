#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""无界面自检（A 题）：算法正确性 + 数据校验 + 异常处理。

设计原则：**先红后绿**。每条检查都必须能在实现出 bug 时真的失败（不是恒真断言）：
  * 两种数据格式必须加载出同一张图（全部点对对拍）；
  * A* 与 Dijkstra 在**全部点对**上代价必须相同（可采纳性 ⇒ 最优性）；
  * BFS 的换乘次数必须与**独立暴力枚举**（所有简单路径）得到的最小值一致；
  * 不连通 / 非法数据必须抛指定异常，且错误信息非空。

用法：python selfcheck.py [--json 路径] [--txt 路径]
退出码：0=全过；1=有失败项。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.graph import DataError, Graph, load_graph  # noqa: E402
from core import solver  # noqa: E402

ROOT = Path(__file__).resolve().parent
DATA_JSON = ROOT / "data" / "demo_city.json"
DATA_TXT = ROOT / "data" / "demo_city.txt"


def pairs(g: Graph):
    ids = sorted(g.stations)
    for a in ids:
        for b in ids:
            if a != b:
                yield a, b


def brute_min_transfers(g: Graph, s: str, t: str):
    """独立实现：枚举所有简单路径，取最少换乘数（仅用于小图对拍）。"""
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
        seen: set = set()
        for e in g.neighbors(sid):
            v = e.other(sid)
            if v in visited or (v, e.line) in seen:
                continue
            seen.add((v, e.line))
            nxt = switches + (0 if prev_line is None or e.line == prev_line else 1)
            visited.add(v)
            walk(v, e.line, nxt, visited)
            visited.remove(v)

    for ln in sorted(lines_of[s]):
        for e in g.neighbors(s):
            if e.line != ln:
                continue
            v = e.other(s)
            walk(v, ln, 0, {s, v})
    return best[0]


def check(out) -> int:
    fails = 0

    def case(name: str, fn):
        nonlocal fails
        try:
            detail = fn()
            print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
        except AssertionError as exc:
            fails += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # 非断言异常也是失败
            fails += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")

    print("\n[1] 数据加载：JSON 与 TXT 必须等价")
    gj = load_graph(DATA_JSON)
    gt = load_graph(DATA_TXT)

    def same_graph():
        assert sorted(gj.stations) == sorted(gt.stations), "站点集合不同"
        assert len(gj.edges) == len(gt.edges), f"边数不同 {len(gj.edges)} vs {len(gt.edges)}"
        for a, b in pairs(gj):
            for w in ("time", "distance"):
                rj = solver.dijkstra(gj, a, b, w)
                rt = solver.dijkstra(gt, a, b, w)
                assert abs(rj.time_min - rt.time_min) < 1e-9, f"{a}-{b} 时间不等"
                assert abs(rj.distance_km - rt.distance_km) < 1e-9, f"{a}-{b} 距离不等"
        return f"{len(gj.stations)} 站 / {len(gj.edges)} 边 / {len(gj.lines)} 线"

    case("两种格式实现同一张图（全点对 × 两权重）", same_graph)

    print("\n[2] 手算基线：3 站点小图")
    tmp = Path(tempfile.mkdtemp()) / "tiny.json"
    tmp.write_text(json.dumps({
        "lines": [{"id": "T", "name": "测试线", "speed_kmh": 60}],
        "stations": [{"id": "A", "name": "甲", "x": 0, "y": 0},
                     {"id": "B", "name": "乙", "x": 3, "y": 4},
                     {"id": "C", "name": "丙", "x": 6, "y": 0}],
        "edges": [{"u": "A", "v": "B", "line": "T"},
                  {"u": "B", "v": "C", "line": "T"},
                  {"u": "A", "v": "C", "line": "T"}],
    }, ensure_ascii=False), encoding="utf-8")
    tiny = load_graph(tmp)

    def hand_math():
        r = solver.dijkstra(tiny, "A", "C", "distance")
        assert abs(r.distance_km - 6.0) < 1e-9, f"距离应为 6.0，实得 {r.distance_km}"
        r = solver.dijkstra(tiny, "A", "C", "time")
        assert abs(r.time_min - 6.0) < 1e-9, f"时间应为 6.0，实得 {r.time_min}"
        # 走 A->B->C 的代价：10 km / 10 min（故意绕远）
        km, tm, seq, sw = solver.analyze_path(tiny, ["A", "B", "C"])
        assert abs(km - 10.0) < 1e-9 and abs(tm - 10.0) < 1e-9, f"绕行代价 {(km, tm)}"
        return "A-C 直连 6.00 km/6.00 min；绕行 10.00/10.00"

    case("距离=坐标欧氏、时间=距离/速度", hand_math)

    print("\n[3] A* 最优性：全部点对与 Dijkstra 同代价")
    def astar_optimal():
        fewer = 0
        total = 0
        for w in ("time", "distance"):
            for a, b in pairs(gj):
                d = solver.dijkstra(gj, a, b, w)
                s = solver.astar(gj, a, b, w)
                key = "time_min" if w == "time" else "distance_km"
                assert abs(getattr(d, key) - getattr(s, key)) < 1e-9, \
                    f"{w} {a}->{b}: Dijkstra {getattr(d, key)} vs A* {getattr(s, key)}"
                total += 1
                if s.expanded <= d.expanded:
                    fewer += 1
        return f"{total} 个点对同代价；A* 扩展不多于 Dijkstra 的有 {fewer}/{total}"

    case("A* 与 Dijkstra 代价逐点对一致", astar_optimal)

    print("\n[4] BFS 最少换乘：与暴力枚举对拍")
    def bfs_vs_brute():
        bad = []
        for a, b in pairs(gj):
            want = brute_min_transfers(gj, a, b)
            got = solver.bfs_transfers(gj, a, b).transfers
            if want != got:
                bad.append(f"{a}->{b} 暴力{want} vs BFS{got}")
        assert not bad, "; ".join(bad[:3])
        return f"{len(list(pairs(gj)))} 个点对全部一致"

    case("换乘数 = 暴力枚举最小值", bfs_vs_brute)

    print("\n[5] 多准则：最快路径 ≠ 最短路径")
    def multicriteria():
        t = solver.dijkstra(gj, "S4", "S11", "time")
        d = solver.dijkstra(gj, "S4", "S11", "distance")
        assert t.path != d.path, f"两条路径相同：{t.path}"
        assert d.distance_km < t.distance_km, "最短距离应小于最快路径的距离"
        assert t.time_min < d.time_min, "最快时间应小于最短路径的时间"
        return f"最快 {t.time_min:.2f}min/{t.distance_km:.2f}km vs 最短 {d.time_min:.2f}min/{d.distance_km:.2f}km"

    case("同一起终点两种口径给出不同解", multicriteria)

    print("\n[6] 边界与异常")
    def same_station():
        r = solver.dijkstra(gj, "S1", "S1", "time")
        assert r.path == ["S1"] and r.distance_km == 0.0 and r.time_min == 0.0, f"{r.path} {r.distance_km}"
        return "起终点相同 → 0 距离 0 时间"

    case("起终点相同", same_station)

    def disconnected():
        p = Path(tempfile.mkdtemp()) / "disc.json"
        p.write_text(json.dumps({
            "lines": [{"id": "T", "name": "线", "speed_kmh": 40}],
            "stations": [{"id": "A", "name": "甲", "x": 0, "y": 0},
                         {"id": "B", "name": "乙", "x": 1, "y": 0},
                         {"id": "Z", "name": "孤岛", "x": 9, "y": 9}],
            "edges": [{"u": "A", "v": "B", "line": "T"}],
        }, ensure_ascii=False), encoding="utf-8")
        g = load_graph(p)
        for fn in (lambda: solver.dijkstra(g, "A", "Z"), lambda: solver.astar(g, "A", "Z"),
                   lambda: solver.bfs_transfers(g, "A", "Z")):
            try:
                fn()
            except solver.PathNotFound as exc:
                assert str(exc), "PathNotFound 应带说明"
            else:
                raise AssertionError("不连通却没有抛 PathNotFound")
        return "三种算法都抛 PathNotFound"

    case("两站不连通", disconnected)

    def bad_data():
        base = {"lines": [{"id": "T", "name": "线", "speed_kmh": 40}],
                "stations": [{"id": "A", "name": "甲", "x": 0, "y": 0},
                             {"id": "B", "name": "乙", "x": 1, "y": 0}],
                "edges": [{"u": "A", "v": "B", "line": "T"}]}
        variants = {
            "站点重复": dict(base, stations=base["stations"] + [{"id": "A", "name": "重复", "x": 2, "y": 2}]),
            "线路未定义": dict(base, edges=[{"u": "A", "v": "B", "line": "NOPE"}]),
            "距离为负": dict(base, edges=[{"u": "A", "v": "B", "line": "T", "distance_km": -1}]),
            "站点不存在": dict(base, edges=[{"u": "A", "v": "Q", "line": "T"}]),
        }
        msgs = []
        for name, raw in variants.items():
            p = Path(tempfile.mkdtemp()) / "bad.json"
            p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            try:
                load_graph(p)
            except DataError as exc:
                assert str(exc), f"{name}: 错误信息为空"
                msgs.append(name)
            else:
                raise AssertionError(f"{name}: 没有抛 DataError")
        return f"{len(msgs)} 类非法数据都被拒绝"

    case("非法数据在加载阶段被拒", bad_data)

    def bad_args():
        for fn, label in (
            (lambda: solver.solve(gj, "S1", "S2", "nope"), "未知算法"),
            (lambda: solver.solve(gj, "S1", "S2", "dijkstra", "nope"), "未知权重"),
            (lambda: solver.dijkstra(gj, "S1", "XX"), "站点不存在"),
        ):
            try:
                fn()
            except DataError as exc:
                assert str(exc), f"{label}: 错误信息为空"
            else:
                raise AssertionError(f"{label}: 没有抛 DataError")
        return "3 类参数错误都被拒绝"

    case("非法参数", bad_args)

    print(f"\nRESULT: {'ALL PASS' if fails == 0 else str(fails) + ' FAILED'}")
    return fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A 题算法自检")
    ap.add_argument("--json", default=str(DATA_JSON))
    ap.add_argument("--txt", default=str(DATA_TXT))
    args = ap.parse_args(argv)
    globals()["DATA_JSON"] = Path(args.json)
    globals()["DATA_TXT"] = Path(args.txt)
    print(f"data: {args.json} / {args.txt}")
    return 0 if check(sys.stdout) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())