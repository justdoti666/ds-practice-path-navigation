#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图模型与数据加载（A 题：城市交通路径导航系统）。

设计要点（对应任务书 3.1 / 3.2）：
  * Graph（图结构）/ solver（算法引擎）/ ui（界面）三层解耦；
  * 站点 = 顶点（id / 名称 / 坐标），边 = 相邻两站的连接（line_id / 距离 km / 时间 min）；
  * 支持 .json 与 .txt 两种数据文件，两种格式加载出**同一张图**（自检项）；
  * 所有非法数据在**加载阶段**抛 DataError（带行号/字段），算法层不再兜底。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


class DataError(ValueError):
    """数据文件格式/内容错误。"""


@dataclass(frozen=True)
class Station:
    sid: str
    name: str
    x: float
    y: float

    @property
    def label(self) -> str:
        return f"{self.name}（{self.sid}）"


@dataclass(frozen=True)
class Edge:
    u: str
    v: str
    line: str
    distance_km: float
    time_min: float

    def other(self, sid: str) -> str:
        return self.v if sid == self.u else self.u


@dataclass
class Graph:
    stations: Dict[str, Station] = field(default_factory=dict)
    lines: Dict[str, dict] = field(default_factory=dict)
    edges: List[Edge] = field(default_factory=list)
    adj: Dict[str, List[Edge]] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ 查询
    def neighbors(self, sid: str) -> List[Edge]:
        return self.adj.get(sid, [])

    def lines_of_station(self) -> Dict[str, set]:
        out: Dict[str, set] = {sid: set() for sid in self.stations}
        for e in self.edges:
            out[e.u].add(e.line)
            out[e.v].add(e.line)
        return out

    def stations_of_line(self, line: str) -> List[str]:
        seen: List[str] = []
        for e in self.edges:
            if e.line != line:
                continue
            for sid in (e.u, e.v):
                if sid not in seen:
                    seen.append(sid)
        return seen

    def edge_between(self, u: str, v: str) -> List[Edge]:
        return [e for e in self.neighbors(u) if e.other(u) == v]

    def path_metrics(self, path: List[str], weight: str = "time") -> tuple:
        """沿站点序列累加指标；同站换乘按 0 距离/0 时间计（本版简化，见 README）。"""
        dist = time = 0.0
        for a, b in zip(path, path[1:]):
            if a == b:            # 同站换乘段：0 距离 / 0 时间
                continue
            cand = self.edge_between(a, b)
            if not cand:
                raise DataError(f"路径中 {a} → {b} 不存在连接")
            if weight == "distance":
                e = min(cand, key=lambda x: x.distance_km)
            else:
                e = min(cand, key=lambda x: x.time_min)
            dist += e.distance_km
            time += e.time_min
        return round(dist, 3), round(time, 2)

    def bbox(self) -> tuple:
        xs = [s.x for s in self.stations.values()]
        ys = [s.y for s in self.stations.values()]
        return (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 1.0, 1.0)

    # ------------------------------------------------------------------ 校验
    def validate(self) -> None:
        if not self.stations:
            raise DataError("站点列表为空")
        if len(self.stations) < 2:
            raise DataError("至少需要 2 个站点")
        names: dict = {}
        for sid, st in self.stations.items():
            if st.name in names:
                raise DataError(f"站名重复：{st.name}（{names[st.name]} 与 {sid}）")
            names[st.name] = sid
        for e in self.edges:
            if e.u not in self.stations or e.v not in self.stations:
                raise DataError(f"边引用了不存在的站点: {e.u} - {e.v}")
            if not (e.distance_km > 0 and math.isfinite(e.distance_km)):
                raise DataError(f"边 {e.u}-{e.v} 距离非法: {e.distance_km}")
            if not (e.time_min > 0 and math.isfinite(e.time_min)):
                raise DataError(f"边 {e.u}-{e.v} 时间非法: {e.time_min}")
            if e.line not in self.lines:
                raise DataError(f"边 {e.u}-{e.v} 的线路 {e.line} 未定义")


# ---------------------------------------------------------------- 组装工具
def _add_station(g: Graph, sid: str, name: str, x: float, y: float) -> None:
    if not sid or not name:
        raise DataError(f"站点 id/名称不能为空: {sid!r} {name!r}")
    if sid in g.stations:
        raise DataError(f"站点 id 重复: {sid}")
    g.stations[sid] = Station(sid, name, float(x), float(y))


def _add_line(g: Graph, lid: str, name: str, speed_kmh: float, color: str) -> None:
    if lid in g.lines:
        raise DataError(f"线路 id 重复: {lid}")
    if not (speed_kmh > 0):
        raise DataError(f"线路 {lid} 速度非法: {speed_kmh}")
    g.lines[lid] = {"name": name, "speed_kmh": float(speed_kmh), "color": color}


def _add_edge(g: Graph, u: str, v: str, line: str,
              distance_km=None, time_min=None, where: str = "") -> None:
    if u not in g.stations or v not in g.stations:
        raise DataError(f"{where}边引用了不存在的站点: {u} - {v}")
    if u == v:
        raise DataError(f"{where}自环边: {u}")
    if line not in g.lines:
        raise DataError(f"{where}线路未定义: {line}")
    su, sv = g.stations[u], g.stations[v]
    auto_dist = math.hypot(su.x - sv.x, su.y - sv.y)
    dist = float(distance_km) if distance_km is not None else auto_dist
    if dist <= 0:
        raise DataError(f"{where}距离必须为正: {u}-{v} -> {dist}")
    speed = g.lines[line]["speed_kmh"]
    t = float(time_min) if time_min is not None else dist / speed * 60.0
    if t <= 0:
        raise DataError(f"{where}时间必须为正: {u}-{v} -> {t}")
    key = (min(u, v), max(u, v), line)
    if any((min(e.u, e.v), max(e.u, e.v), e.line) == key for e in g.edges):
        raise DataError(f"{where}重复边: {u}-{v} ({line})")
    e = Edge(u, v, line, round(dist, 3), round(t, 2))
    g.edges.append(e)
    g.adj.setdefault(u, []).append(e)
    g.adj.setdefault(v, []).append(e)


def _finish(g: Graph) -> Graph:
    for sid in g.stations:
        g.adj.setdefault(sid, [])
    g.validate()
    return g


# ---------------------------------------------------------------- JSON 加载
def load_json(path: Path) -> Graph:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise DataError(f"JSON 解析失败（{path}）: {exc}") from exc
    g = Graph(meta=dict(raw.get("meta", {})))
    for i, ln in enumerate(raw.get("lines", []), 1):
        _add_line(g, str(ln.get("id", "")).strip(), ln.get("name", ""),
                  ln.get("speed_kmh", 35), ln.get("color", "#888888"))
        if not g.lines[list(g.lines)[-1]]["name"]:
            raise DataError(f"第 {i} 条线路缺少名称")
    for i, st in enumerate(raw.get("stations", []), 1):
        try:
            _add_station(g, str(st["id"]).strip(), st["name"], st["x"], st["y"])
        except KeyError as exc:
            raise DataError(f"第 {i} 个站点缺字段: {exc}") from exc
    for i, ed in enumerate(raw.get("edges", []), 1):
        try:
            _add_edge(g, ed["u"], ed["v"], ed["line"],
                      ed.get("distance_km"), ed.get("time_min"), where=f"第 {i} 条边 ")
        except KeyError as exc:
            raise DataError(f"第 {i} 条边缺字段: {exc}") from exc
    return _finish(g)


# ---------------------------------------------------------------- TXT 加载
def load_txt(path: Path) -> Graph:
    """分节文本：
    [meta]      name=示例城市（可省略）
    [lines]     L1 红线 35 #e4572e
    [stations]  S1 中央车站 0 0
    [edges]     S1 S2 L1 [distance_km] [time_min]
    以 # 开头的行为注释；空行忽略。
    """
    g = Graph()
    section = None
    text = Path(path).read_text(encoding="utf-8-sig")
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        parts = line.split()
        where = f"第 {lineno} 行: "
        if section == "meta":
            if "=" in line:
                k, v = line.split("=", 1)
                g.meta[k.strip()] = v.strip()
        elif section == "lines":
            if len(parts) < 3:
                raise DataError(f"{where}线路格式应为 id 名称 速度 [颜色]")
            _add_line(g, parts[0], parts[1], float(parts[2]), parts[3] if len(parts) > 3 else "#888888")
        elif section == "stations":
            if len(parts) < 4:
                raise DataError(f"{where}站点格式应为 id 名称 x y")
            _add_station(g, parts[0], parts[1], float(parts[2]), float(parts[3]))
        elif section == "edges":
            if len(parts) < 3:
                raise DataError(f"{where}边格式应为 u v line [distance_km] [time_min]")
            _add_edge(g, parts[0], parts[1], parts[2],
                      float(parts[3]) if len(parts) > 3 else None,
                      float(parts[4]) if len(parts) > 4 else None, where=where)
        else:
            raise DataError(f"{where}未知分节（需要 [meta]/[lines]/[stations]/[edges]）")
    return _finish(g)


def load_graph(path) -> Graph:
    p = Path(path)
    if not p.exists():
        raise DataError(f"数据文件不存在: {p}")
    suffix = p.suffix.lower()
    if suffix == ".json":
        return load_json(p)
    if suffix in (".txt", ".text"):
        return load_txt(p)
    raise DataError(f"不支持的数据格式: {suffix}（只支持 .json / .txt）")