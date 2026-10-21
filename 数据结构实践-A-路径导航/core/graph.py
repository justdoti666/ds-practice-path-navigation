"""图数据层（P1 契约桩）。

本文件属于 P1（数据加载）的交付物。当前仓库中它是一个**可运行的占位实现**，
目的是让 P3 的界面在 P1 真实现就位前就能端到端跑通；P1 替换本文件时请保持
下列公开接口不变，界面层（ui/）不感知内部结构：

    from core.graph import load_graph, Graph, Station, Edge
    g = load_graph()                 # 默认读 data/stations.csv + data/edges.csv
    g.stations["S02"].name           # -> "人民广场"
    list(g.neighbors("S02"))         # -> [Edge(...), ...]
    g.edge_between("S01", "S02")     # -> Edge 或 None（用于界面显示线路名）
    g.iter_edges()                   # -> 去重后的 Edge 序列（画布绘制用）

约定：
* 站点 id 形如 ``S01``，站名是中文，坐标 x/y 为平面示意坐标（无量纲）。
* 边一律存双向（CSV 里一行代表双向的一条区间）。
* 数据错误抛 :class:`GraphDataError`，界面层会捕获并提示"数据错误"。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "GraphDataError",
    "Station",
    "Edge",
    "Graph",
    "load_graph",
    "DEFAULT_DATA_DIR",
]

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class GraphDataError(ValueError):
    """站点/区间数据不合法（缺列、重复 id、悬空边等）。"""


@dataclass(frozen=True)
class Station:
    """一个站点。"""

    id: str
    name: str
    x: float
    y: float
    lines: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """界面下拉框统一使用的 ``站名（ID）`` 文案。"""
        return f"{self.name}（{self.id}）"


@dataclass(frozen=True)
class Edge:
    """一条区间（无向，方向由遍历时决定）。"""

    u: str
    v: str
    line: str
    km: float
    minutes: float

    def other(self, sid: str) -> str:
        if sid == self.u:
            return self.v
        if sid == self.v:
            return self.u
        raise KeyError(f"站点 {sid} 不在区间 {self.u}-{self.v} 上")

    def cost(self, weight: str) -> float:
        if weight == "time":
            return self.minutes
        if weight == "distance":
            return self.km
        raise ValueError(f"未知权重：{weight!r}（只支持 'time' / 'distance'）")


class Graph:
    """邻接表 + 站点字典。"""

    def __init__(self) -> None:
        self.stations: dict[str, Station] = {}
        self._adj: dict[str, list[Edge]] = {}

    # ---- 构建 ----------------------------------------------------------
    def add_station(self, station: Station) -> None:
        if station.id in self.stations:
            raise GraphDataError(f"站点 id 重复：{station.id}")
        self.stations[station.id] = station
        self._adj[station.id] = []

    def add_edge(self, u: str, v: str, line: str, km: float, minutes: float) -> None:
        for sid in (u, v):
            if sid not in self.stations:
                raise GraphDataError(f"区间 {u}-{v} 引用了不存在的站点：{sid}")
        if u == v:
            raise GraphDataError(f"不允许自环：{u}")
        if km <= 0 or minutes <= 0:
            raise GraphDataError(f"区间 {u}-{v} 的里程/时间必须为正：{km}/{minutes}")
        edge = Edge(u, v, line, float(km), float(minutes))
        self._adj[u].append(edge)
        self._adj[v].append(edge)

    # ---- 查询 ----------------------------------------------------------
    def neighbors(self, sid: str) -> list[Edge]:
        try:
            return self._adj[sid]
        except KeyError:
            raise KeyError(f"未知站点：{sid}") from None

    def edge_between(self, u: str, v: str) -> Edge | None:
        for edge in self._adj.get(u, ()):  # type: ignore[arg-type]
            if edge.other(u) == v:
                return edge
        return None

    def iter_edges(self) -> list[Edge]:
        seen: set[tuple[str, str, str]] = set()
        out: list[Edge] = []
        for sid in self.stations:
            for edge in self._adj[sid]:
                key = (min(edge.u, edge.v), max(edge.u, edge.v), edge.line)
                if key in seen:
                    continue
                seen.add(key)
                out.append(edge)
        return out

    @property
    def is_empty(self) -> bool:
        return not self.stations

    def __len__(self) -> int:
        return len(self.stations)


def _read_rows(path: Path, required: tuple[str, ...]) -> list[dict[str, str]]:
    if not path.exists():
        raise GraphDataError(f"找不到数据文件：{path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            missing = [col for col in required if col not in (reader.fieldnames or [])]
            if missing:
                raise GraphDataError(f"{path.name} 缺少列：{', '.join(missing)}")
            rows = [dict(row) for row in reader]
    except UnicodeDecodeError as exc:
        raise GraphDataError(f"{path.name} 不是 UTF-8 编码：{exc}") from exc
    if not rows:
        raise GraphDataError(f"{path.name} 是空文件")
    return rows


def load_graph(data_dir: str | Path | None = None) -> Graph:
    """从 CSV 装载路网。

    参数
    ----
    data_dir: 存放 stations.csv / edges.csv 的目录，默认 ``data/``。
    """
    base = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
    station_rows = _read_rows(base / "stations.csv", ("id", "name", "x", "y"))
    edge_rows = _read_rows(base / "edges.csv", ("from", "to", "line", "km", "min"))

    graph = Graph()
    for row in station_rows:
        sid = (row.get("id") or "").strip()
        name = (row.get("name") or "").strip()
        if not sid or not name:
            raise GraphDataError(f"站点行缺少 id/name：{row!r}")
        try:
            x = float(row.get("x") or 0)
            y = float(row.get("y") or 0)
        except ValueError as exc:
            raise GraphDataError(f"站点 {sid} 坐标不是数字：{exc}") from exc
        lines = tuple(
            part.strip() for part in (row.get("lines") or "").split("|") if part.strip()
        )
        graph.add_station(Station(sid, name, x, y, lines))

    for row in edge_rows:
        u = (row.get("from") or "").strip()
        v = (row.get("to") or "").strip()
        line = (row.get("line") or "").strip()
        try:
            km = float(row.get("km") or 0)
            minutes = float(row.get("min") or 0)
        except ValueError as exc:
            raise GraphDataError(f"区间 {u}-{v} 里程/时间不是数字：{exc}") from exc
        graph.add_edge(u, v, line, km, minutes)

    return graph
