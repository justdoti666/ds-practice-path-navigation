"""P3 UI 与真实 P1/P2 之间的薄适配层（本地代写整合）。

P3 的窗口按“契约桩”写的，依赖：
  * ``GraphDataError`` 这个名字；
  * ``graph.is_empty / iter_edges() / edge_between()->单条边``；
  * ``edge.line / edge.km / edge.minutes`` 三个展示字段。

本模块只做只读适配，不改 P1/P2 的真实实现。
"""
from __future__ import annotations

from pathlib import Path

from core.graph import DataError, Graph as _Graph, Station, load_graph as _load

GraphDataError = DataError
DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data" / "demo_city.json"


class CompatEdge:
    """把 P1 的 Edge 映射成 P3 期望的展示字段（line / km / minutes）。"""

    def __init__(self, edge) -> None:
        self._edge = edge

    @property
    def line(self):
        return self._edge.line

    @property
    def km(self):
        return self._edge.distance_km

    @property
    def minutes(self):
        return self._edge.time_min

    def __getattr__(self, name):
        return getattr(self._edge, name)


class Graph(_Graph):
    """在真实 Graph 上补 P3 需要的三个只读方法。"""

    @property
    def is_empty(self) -> bool:
        return not self.stations

    def iter_edges(self):
        return list(self.edges)

    # 注意：不要覆盖 edge_between —— P2 的 solver.analyze_path 依赖它返回**列表**。
    # P3 窗口的单边展示在 window._render_path 里做 min(time) 归一化。


def _as_compat(graph) -> Graph:
    if isinstance(graph, Graph):
        return graph
    out = Graph()
    out.__dict__.update(graph.__dict__)
    return out


def load_graph(path=None) -> Graph:
    """接受 文件 / 目录 / None；目录时优先 demo_city.json。"""
    p = Path(path) if path else DEFAULT_DATA
    if p.is_dir():
        for name in ("demo_city.json", "real_metro.json", "tiny_hand.json"):
            candidate = p / name
            if candidate.exists():
                p = candidate
                break
        else:
            raise DataError(f"目录里没有可加载的数据文件: {p}")
    return _as_compat(_load(str(p)))
