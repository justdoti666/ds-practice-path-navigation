"""画布渲染组件 ``CanvasView``（P4 的核心交付物）。

这个文件只做「把图和数据画到屏幕上」这一件事，**不包含**任何寻路算法，
也不读取任何数据文件。P3 的窗口只需要：

    view = CanvasView(parent, graph)       # 放进任意容器
    view.bind_click(callback)             # 点站点时回调 callback(站点ID)
    view.show_path(result)                # 静态高亮一条路径
    view.animate(result, speed_level=2)   # 逐帧动画（1/2/4 倍速）
    view.reset()                          # 清掉路径、缩放、平移
    view.export("out.png")                # 导出 PNG / PS

坐标系统（P4-01 的关键）
-------------------------
* **世界坐标**：数据文件里的 x/y，单位是公里，y 轴朝上（数学坐标系）。
* **屏幕坐标**：Tkinter 画布的像素，y 轴朝下。
* 每次重绘都重新计算 ``世界 -> 屏幕``，并保持 x/y 同比例（等比缩放），
  因此窗口拉宽、拉高都不会把城市“压扁”。
* 视图状态只有三个：缩放倍率 ``zoom``、平移量 ``pan_x/pan_y``、
  视口尺寸 ``width/height``。鼠标缩放时以光标为中心，光标下的地点不动。

坐标变换的公式（``ViewTransform`` 里就这么写的）：

    screen_x = width / 2  + (world_x - center_x) * scale + pan_x
    screen_y = height / 2 - (world_y - center_y) * scale + pan_y

减号就是 y 翻转：世界坐标里越靠北（y 越大），屏幕上越靠上（screen_y 越小）。

渲染性能
--------
北京演示图有 134 个站点、7 条线路。为了不卡顿：

* 线路用一条折线画完，而不是每个区间画一条；
* 站名避让用「网格空间索引」，只检查附近几十像素内的矩形；
* 拖动、滚轮缩放时延迟 16 毫秒合并重绘；
* 窗口 ``<Configure>`` 事件做 120 毫秒防抖（避免初始 1x1 时算错坐标）。

自检接口
--------
``selfcheck.py`` 会读取下面这些公开字段来验证“站名零重叠”等指标：

* ``station_label_boxes``：每个已绘制站名的包围盒（dict，键是站点 ID）；
* ``segment_label_boxes``：路径每段的 km/min 标注包围盒（list）；
* ``hidden_station_labels`` / ``hidden_segment_labels``：空间不够而省略的数量。
"""

from __future__ import annotations

import math
import os
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import font as tkfont
from typing import Callable, Iterable

# Pillow 只用于 PNG 截图导出；没装也不影响画布和 PS 导出。
try:  # pragma: no cover - 取决于运行环境
    from PIL import ImageGrab
except Exception:  # pragma: no cover
    ImageGrab = None


# ----------------------------------------------------------------------
# 配色与尺寸常量：集中放在这里，换主题只改这一块
# ----------------------------------------------------------------------
BG = "#F5F7FB"            # 画布底色（很浅的蓝灰，像一张纸）
CARD = "#FFFFFF"          # 卡片、站点填充
INK = "#0F172A"           # 主文字（近黑）
SUB = "#64748B"           # 次要文字（灰蓝）
GRID = "#E9EFF7"          # 背景网格
BORDER = "#D8E1EE"        # 卡片描边
ACCENT = "#2563EB"        # 主色（蓝）
HIGHLIGHT = "#E11D48"     # 路径高亮（玫红）
HIGHLIGHT_BG = "#FFF1F2"  # 路径标注底色
TRANSFER = "#334155"      # 换乘站描边
ANIM = "#F59E0B"          # 动画中的“搜索波”橙色
START = "#059669"         # 起点绿
END = "#DC2626"           # 终点红
FEEDER_DASH = (6, 4)      # 接驳线虚线样式：6 像素实线 + 4 像素空白
FONT_FAMILY_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "Noto Sans CJK SC",
    "SimHei",
)

# 没有配置颜色的线路，按这个顺序自动取色，保证每条线都能区分
FALLBACK_COLORS = (
    "#C0392B", "#2980B9", "#27AE60", "#8E44AD",
    "#D4AC0D", "#16A085", "#E67E22", "#2C3E50",
)


# ----------------------------------------------------------------------
# 小工具函数
# ----------------------------------------------------------------------
def _clamp(value: float, low: float, high: float) -> float:
    """把 value 限制在 [low, high] 区间内。"""
    return max(low, min(high, value))


def _rect_intersect(a: tuple, b: tuple, pad: float = 0.0) -> bool:
    """判断两个矩形是否相交；pad 表示把 b 向外扩几像素（留一点安全距离）。"""
    return (
        a[0] < b[2] + pad
        and a[2] > b[0] - pad
        and a[1] < b[3] + pad
        and a[3] > b[1] - pad
    )


def _rect_inside(rect: tuple, width: float, height: float, margin: float = 3.0) -> bool:
    """矩形是否完整落在画布内（留 margin 像素边距）。"""
    return (
        rect[0] >= margin
        and rect[1] >= margin
        and rect[2] <= width - margin
        and rect[3] <= height - margin
    )


class _RectIndex:
    """矩形的网格索引，用来快速判断“新标签会不会压到旧标签”。

    如果每放一个站名都和前面所有站名逐一比较，134 个站名就要比较上万次；
    把画布切成 64 像素的小格子后，只检查新标签覆盖到的那几个格子即可。
    对 100 多个站点的图来说，这一步能把重绘时间从几十毫秒降到几毫秒。
    """

    def __init__(self, cell: float = 64.0) -> None:
        self.cell = float(cell)
        self.cells: dict[tuple[int, int], list[tuple]] = {}

    def add(self, rect: tuple) -> None:
        """把一个矩形放进它覆盖到的所有格子。"""
        c = self.cell
        x0 = int(math.floor(rect[0] / c))
        x1 = int(math.floor(rect[2] / c))
        y0 = int(math.floor(rect[1] / c))
        y1 = int(math.floor(rect[3] / c))
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                self.cells.setdefault((gx, gy), []).append(rect)

    def hits(self, rect: tuple, pad: float = 1.0) -> bool:
        """新矩形是否与索引里的某个矩形重叠。"""
        c = self.cell
        x0 = int(math.floor(rect[0] / c))
        x1 = int(math.floor(rect[2] / c))
        y0 = int(math.floor(rect[1] / c))
        y1 = int(math.floor(rect[3] / c))
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                for other in self.cells.get((gx, gy), ()):
                    if _rect_intersect(rect, other, pad):
                        return True
        return False


# ----------------------------------------------------------------------
# 查看用的数据模型：把 P1/P2 的对象“翻译”成画布好画的样子
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class DrawStation:
    """画布用的站点：只有画图需要的四个字段。"""

    id: str
    name: str
    x: float
    y: float


@dataclass(frozen=True)
class DrawLine:
    """画布用的线路：站序、颜色、是否环线、是否接驳线。"""

    id: str
    name: str
    color: str
    station_ids: tuple[str, ...]
    loop: bool = False
    feeder: bool = False


@dataclass
class DrawGraph:
    """画布用的整张图（P1 图对象的只读投影）。"""

    stations: dict[str, DrawStation]
    lines: list[DrawLine]
    station_lines: dict[str, list[str]]
    segments: dict[tuple[str, str], str]
    bbox: tuple[float, float, float, float]

    def is_transfer(self, station_id: str) -> bool:
        """一个站属于两条及以上线路，就是换乘站。"""
        return len(self.station_lines.get(station_id, ())) > 1

    def line_names(self, station_id: str) -> list[str]:
        """某站经过的线路名称，用于悬停提示。"""
        by_id = {line.id: line.name for line in self.lines}
        return [by_id.get(lid, lid) for lid in self.station_lines.get(station_id, ())]


@dataclass(frozen=True)
class PathLegView:
    """路径中的一段：ride = 乘车，transfer = 换乘。"""

    kind: str
    from_id: str
    to_id: str
    line_id: str
    km: float
    minutes: float


@dataclass
class PathView:
    """一次搜索结果的渲染视图。"""

    found: bool
    algorithm: str = ""
    stations: list[str] = field(default_factory=list)
    legs: list[PathLegView] = field(default_factory=list)
    distance_km: float = 0.0
    time_min: float = 0.0
    transfers: int = 0
    expanded: int = 0
    events: list[str] = field(default_factory=list)
    message: str = ""
    raw: object | None = None

    @property
    def metrics(self) -> "PathMetrics":
        return PathMetrics(
            algorithm=self.algorithm,
            found=self.found,
            distance_km=float(self.distance_km),
            time_min=float(self.time_min),
            transfers=int(self.transfers),
            expanded=int(self.expanded),
            station_count=len(self.stations),
        )


@dataclass(frozen=True)
class PathMetrics:
    """对外暴露的统计值；动画和“立即显示”用的是同一份数值。"""

    algorithm: str
    found: bool
    distance_km: float
    time_min: float
    transfers: int
    expanded: int
    station_count: int

    def as_dict(self) -> dict:
        # 保留 6 位小数，既避免浮点误差，又方便自检逐项比较
        return {
            "algorithm": self.algorithm,
            "found": self.found,
            "distance_km": round(self.distance_km, 6),
            "time_min": round(self.time_min, 6),
            "transfers": self.transfers,
            "expanded": self.expanded,
            "station_count": self.station_count,
        }


@dataclass
class LabelBox:
    """一个已经画到屏幕上的文字标签及其包围盒。"""

    text: str
    rect: tuple[float, float, float, float]
    station_id: str | None = None
    kind: str = "station"

    def overlaps(self, other: "LabelBox", pad: float = 0.0) -> bool:
        return _rect_intersect(self.rect, other.rect, pad)


@dataclass
class _PlacedLabel:
    """内部使用的标签排版结果：框 + 字体 + 颜色 + 是否要画引线。"""

    text: str
    rect: tuple[float, float, float, float]
    station_id: str | None
    font_size: int
    bold: bool
    color: str
    leader: bool = False
    kind: str = "station"

    def to_box(self) -> LabelBox:
        return LabelBox(self.text, self.rect, self.station_id, self.kind)


# ----------------------------------------------------------------------
# 把任意 Graph / Result 对象翻译成画布模型
# ----------------------------------------------------------------------
def _get(obj: object, *names: str, default=None):
    """依次尝试多个字段名，兼容 dict 和普通对象两种写法。

    P1/P2 目前用的是 dataclass，但任务书里的接口可能写成 dict，
    所以画布不写死 ``obj.stations``，而是容忍几种常见命名。
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _as_station_id(item: object) -> str:
    """从字符串 / dict / 对象里取出站点 ID。

    兼容四种历史命名：``id``（本项目）、``station_id``、``sid``（P1/P2 的
    《技术方案》§6 与 `docs/交接/P1-收到的口径.md` 用的是 sid）以及
    ``station``（站台节点里“当前站点”的字段名）。
    """
    if item is None:
        return ""
    if isinstance(item, str):
        return item
    if isinstance(item, (tuple, list)):  # P2 的站台节点常写成 (站点, 线路)
        return str(item[0]) if item else ""
    if isinstance(item, dict):
        return str(
            item.get("id")
            or item.get("station_id")
            or item.get("sid")
            or item.get("station")
            or ""
        )
    return str(
        getattr(item, "id", None)
        or getattr(item, "station_id", None)
        or getattr(item, "sid", None)
        or getattr(item, "station", None)
        or ""
    )


#: 任务书/P2 的 ``events`` 常写成 ``(kind, sid)``；只有这些 kind 才按“事件”解析，
#: 否则 ``("S1", "L1")`` 这类站台节点元组会被误读成事件。
_EVENT_KINDS = frozenset(
    {"settle", "visit", "visited", "push", "pop", "expand", "relax", "transfer", "ride"}
)


def _as_event_station(item: object) -> str:
    """从一个动画事件里取出站点 ID，兼容任务书与 P2 的几种写法。

    * ``"S1"``                             → ``S1``；
    * ``("settle", "S1")``                 → ``S1``（任务书/P2 的 events）；
    * ``{"kind": "settle", "sid": "S1"}``  → ``S1``；
    * ``("S1", "L1")``                     → ``S1``（站台节点元组）。
    """
    if isinstance(item, (tuple, list)) and len(item) >= 2:
        kind = str(item[0]).strip().lower()
        if kind in _EVENT_KINDS:
            return _as_station_id(item[1])
        return _as_station_id(item[0])
    return _as_station_id(item)


def _looks_like_feeder(line: object, name: str) -> bool:
    """判断一条线是不是“接驳线/支线”，用于画虚线。

    优先看数据字段（feeder / kind）；没有字段时按名称识别，
    因为课程数据里通常会把支线命名为“××接驳线”“S1支线”等。
    """
    if bool(_get(line, "feeder", "is_feeder", default=False)):
        return True
    kind = str(_get(line, "kind", "type", default="") or "").lower()
    if kind in {"feeder", "shuttle", "bus", "connection"}:
        return True
    return any(word in name for word in ("接驳", "支线", "摆渡", "shuttle", "feeder"))


def normalise_graph(graph: object) -> DrawGraph:
    """把 P1 的图对象转换成画布模型。

    这里只读不写，不修改任何 P1 的对象；如果图对象缺字段，会抛出
    带中文说明的 ValueError，方便 P3 在界面上提示。
    """
    if graph is None:
        raise ValueError("CanvasView 需要一个图对象，但收到的是 None。")

    raw_stations = _get(graph, "stations", default={})
    if isinstance(raw_stations, dict):
        # dict 形式的 stations：允许“key 就是站点 ID、value 里没有 id 字段”
        station_items: list[tuple[object | None, object]] = list(raw_stations.items())
    else:
        station_items = [(None, item) for item in (raw_stations or [])]

    stations: dict[str, DrawStation] = {}
    for key, item in station_items:
        sid = _as_station_id(item) or (str(key) if key is not None else "")
        if not sid:
            continue
        try:
            x = float(_get(item, "x", "lon", "longitude"))
            y = float(_get(item, "y", "lat", "latitude"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"站点 {sid} 的坐标不是数字，无法绘制。") from exc
        name = str(_get(item, "name", "title", default=sid))
        stations[sid] = DrawStation(sid, name, x, y)

    if not stations:
        raise ValueError("图对象里没有站点（stations 为空），无法绘制。")

    raw_lines = _get(graph, "lines", default=[])
    if isinstance(raw_lines, dict):
        line_iter = raw_lines.values()
    else:
        line_iter = raw_lines or []

    lines: list[DrawLine] = []
    station_lines: dict[str, list[str]] = {sid: [] for sid in stations}
    segments: dict[tuple[str, str], str] = {}

    for index, item in enumerate(line_iter):
        lid = _as_station_id(_get(item, "id", "line_id", default=""))
        lid = lid or f"L{index + 1}"
        name = str(_get(item, "name", "title", default=lid))

        raw_ids = _get(item, "stations", "path", "stops", "station_ids", default=[])
        ids: list[str] = []
        for raw in raw_ids or []:
            sid = _as_station_id(raw)
            # 跳过不存在的站点，避免一条坏数据把整张图画崩
            if sid in stations and (not ids or ids[-1] != sid):
                ids.append(sid)
        if not ids:
            continue

        loop = bool(_get(item, "loop", "is_loop", "circular", default=False))
        feeder = _looks_like_feeder(item, name)
        color = str(_get(item, "color", "colour", default="") or "")
        if not color:
            color = FALLBACK_COLORS[index % len(FALLBACK_COLORS)]

        line = DrawLine(lid, name, color, tuple(ids), loop, feeder)
        lines.append(line)

        for sid in ids:
            if lid not in station_lines[sid]:
                station_lines[sid].append(lid)

        # 记录“相邻两站属于哪条线”，结果对象只给站点序列时用它补线路
        pairs = list(zip(ids, ids[1:]))
        if loop and len(ids) > 2:
            pairs.append((ids[-1], ids[0]))
        for a, b in pairs:
            segments.setdefault((a, b), lid)
            segments.setdefault((b, a), lid)

    if not lines:
        raise ValueError("图对象里没有线路（lines 为空），无法绘制。")

    # 优先使用图对象自带的 bbox；没有就按站点坐标现算
    raw_bbox = _get(graph, "bbox", "bounds", default=None)
    bbox: tuple[float, float, float, float]
    if raw_bbox is not None and len(tuple(raw_bbox)) == 4:
        min_x, min_y, max_x, max_y = (float(v) for v in raw_bbox)
    else:
        xs = [s.x for s in stations.values()]
        ys = [s.y for s in stations.values()]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
    if max_x - min_x < 1e-6:
        max_x = min_x + 1.0
    if max_y - min_y < 1e-6:
        max_y = min_y + 1.0

    return DrawGraph(stations, lines, station_lines, segments, (min_x, min_y, max_x, max_y))


def _segment_metrics(draw_graph: DrawGraph, graph: object, a: str, b: str) -> tuple[float, float]:
    """计算相邻两站的 (km, 分钟)。

    结果对象自带每段时间/里程时就以结果为准；只有在结果只给了站点序列时，
    才用这里的公式兜底。公式和 P2 的乘车时间一致：
        时间 = 距离 / 速度 * 60 + 停站时间
    """
    sa, sb = draw_graph.stations[a], draw_graph.stations[b]
    km = math.hypot(sa.x - sb.x, sa.y - sb.y)
    speed = float(_get(graph, "speed_kmh", "speed", default=35.0) or 35.0)
    dwell = float(_get(graph, "dwell_minutes", "dwell", default=0.6) or 0.6)
    minutes = km / speed * 60.0 + dwell if speed > 0 else km
    return km, minutes


def normalise_result(graph: object, draw_graph: DrawGraph, result: object) -> PathView:
    """把 P2 的搜索结果转换成画布视图。

    兼容两套字段名：
    * P2 当前实现：``stations / legs / total_distance / total_time / transfers``
    * 任务书接口：``path / distance_km / time_min / events``

    ``events`` 兼容 ``(kind, sid)`` 元组（任务书/P2 写法）、``{"kind":…, "sid":…}``
    字典与纯站点 ID 列表三种形式；解析见 :func:`_as_event_station`。
    """
    if result is None:
        return PathView(found=False, message="还没有搜索")

    found = bool(_get(result, "found", "success", default=True))
    algorithm = str(_get(result, "algorithm", "algo", "title", default="") or "")

    raw_stations = _get(result, "stations", "path", "station_ids", "route", default=[]) or []
    if isinstance(raw_stations, str):
        raw_stations = [raw_stations]
    stations: list[str] = []
    for item in raw_stations:
        sid = _as_station_id(item)
        if sid and sid in draw_graph.stations and (not stations or stations[-1] != sid):
            stations.append(sid)

    legs: list[PathLegView] = []
    for raw in _get(result, "legs", "segments", "steps", default=[]) or []:
        kind_raw = str(_get(raw, "kind", "type", "action", default="") or "").lower()
        u = _as_station_id(_get(raw, "from_station", "from_id", "u", "from", "source"))
        v = _as_station_id(_get(raw, "to_station", "to_id", "v", "to", "target"))
        line_id = str(_get(raw, "line_id", "line", default="") or "")
        if not u or not v:
            continue
        if not kind_raw:
            kind_raw = "transfer" if u == v else "ride"
        kind = "transfer" if "transfer" in kind_raw or "换乘" in kind_raw else "ride"

        km = _get(raw, "distance", "distance_km", "km")
        minutes = _get(raw, "time", "time_min", "minutes", "min")
        if km is None or minutes is None:
            seg_km, seg_min = _segment_metrics(draw_graph, graph, u, v) if u != v else (0.0, 0.0)
            km = seg_km if km is None else km
            minutes = seg_min if minutes is None else minutes
        if not line_id:
            line_id = draw_graph.segments.get((u, v), "")
        legs.append(PathLegView(kind, u, v, line_id, float(km), float(minutes)))

    # 结果只给了站点序列：根据相邻关系补出 ride 段
    if not legs and len(stations) >= 2:
        for a, b in zip(stations, stations[1:]):
            if a == b:
                continue
            km, minutes = _segment_metrics(draw_graph, graph, a, b)
            legs.append(PathLegView("ride", a, b, draw_graph.segments.get((a, b), ""), km, minutes))

    # 如果结果没给站点序列，就按段的顺序推出来
    if not stations and legs:
        for leg in legs:
            for sid in (leg.from_id, leg.to_id):
                if sid in draw_graph.stations and (not stations or stations[-1] != sid):
                    stations.append(sid)

    total_km = _get(result, "distance_km", "total_distance", "distance")
    total_min = _get(result, "time_min", "total_time", "time", "minutes")
    if total_km is None:
        total_km = sum(leg.km for leg in legs)
    if total_min is None:
        total_min = sum(leg.minutes for leg in legs)

    transfers = _get(result, "transfers", "transfer_count", "changes")
    if transfers is None:
        transfers = sum(1 for leg in legs if leg.kind == "transfer")
    expanded = _get(result, "expanded", "expanded_nodes", "visited_count", default=0) or 0

    raw_events = _get(result, "events", "visited_order", "settle_events", "visited", default=[]) or []
    events: list[str] = []
    for event in raw_events:
        sid = _as_event_station(event)
        if sid:
            events.append(sid)
    if not events:
        events = list(stations)

    message = str(_get(result, "message", "detail", default="") or "")
    return PathView(
        found=found,
        algorithm=algorithm,
        stations=stations,
        legs=legs,
        distance_km=float(total_km or 0.0),
        time_min=float(total_min or 0.0),
        transfers=int(transfers or 0),
        expanded=int(expanded),
        events=events,
        message=message,
        raw=result,
    )


# ----------------------------------------------------------------------
# 世界坐标 -> 屏幕坐标
# ----------------------------------------------------------------------
class ViewTransform:
    """负责“世界坐标 <-> 屏幕坐标”的换算，以及缩放和平移。

    单独做成一个类的好处：不依赖 Tkinter 也能用 selfcheck 验证数学正确性。
    """

    MIN_ZOOM = 0.5   # 相对“适配窗口”的缩放范围：0.5x ~ 3x
    MAX_ZOOM = 3.0

    def __init__(
        self,
        bbox: tuple[float, float, float, float],
        width: int = 1,
        height: int = 1,
        margin: float = 56.0,
    ) -> None:
        self.min_x, self.min_y, self.max_x, self.max_y = map(float, bbox)
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.margin = float(margin)
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        # 世界坐标的几何中心：缩放时以它作为“不动的参考点”
        self.center_x = (self.min_x + self.max_x) / 2.0
        self.center_y = (self.min_y + self.max_y) / 2.0
        self.fit_scale = 1.0
        self._recompute_fit()

    # ---- 基本属性 ----
    @property
    def scale(self) -> float:
        """当前每公里对应多少像素。"""
        return self.fit_scale * self.zoom

    def _recompute_fit(self) -> None:
        """根据视口大小算出“刚好放下整张图”的比例。"""
        available_w = max(self.width - 2.0 * self.margin, 40.0)
        available_h = max(self.height - 2.0 * self.margin, 40.0)
        world_w = max(self.max_x - self.min_x, 1e-6)
        world_h = max(self.max_y - self.min_y, 1e-6)
        # 取两个方向里较小的缩放比 => 等比缩放，不会把图拉变形
        self.fit_scale = max(0.01, min(available_w / world_w, available_h / world_h))

    def resize(self, width: int, height: int) -> None:
        """画布尺寸变化时更新视口（缩放倍率和平移量保持不变）。"""
        width = max(1, int(width))
        height = max(1, int(height))
        if (width, height) == (self.width, self.height):
            return
        self.width, self.height = width, height
        self._recompute_fit()
        self.clamp()

    # ---- 正反变换 ----
    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        """世界坐标 -> 屏幕像素。注意 y 前面的减号就是“y 翻转”。"""
        scale = self.scale
        return (
            self.width / 2.0 + (x - self.center_x) * scale + self.pan_x,
            self.height / 2.0 - (y - self.center_y) * scale + self.pan_y,
        )

    def to_world(self, screen_x: float, screen_y: float) -> tuple[float, float]:
        """屏幕像素 -> 世界坐标（to_screen 的逆运算）。"""
        scale = self.scale
        return (
            self.center_x + (screen_x - self.width / 2.0 - self.pan_x) / scale,
            self.center_y - (screen_y - self.height / 2.0 - self.pan_y) / scale,
        )

    # ---- 缩放 / 平移 ----
    def zoom_at(self, screen_x: float, screen_y: float, factor: float) -> bool:
        """以光标为中心缩放；返回是否真的改变了缩放倍率。

        做法：先记下光标处的世界坐标，改完比例后重新算平移量，
        让同一个世界坐标仍然落在光标下面。
        """
        if factor <= 0 or not math.isfinite(factor):
            return False
        old_zoom = self.zoom
        new_zoom = _clamp(old_zoom * factor, self.MIN_ZOOM, self.MAX_ZOOM)
        if abs(new_zoom - old_zoom) < 1e-9:
            return False

        world_x, world_y = self.to_world(screen_x, screen_y)
        self.zoom = new_zoom
        scale = self.scale
        self.pan_x = screen_x - self.width / 2.0 - (world_x - self.center_x) * scale
        self.pan_y = screen_y - self.height / 2.0 + (world_y - self.center_y) * scale
        self.clamp()
        return True

    def set_zoom(self, zoom: float, anchor: tuple[float, float] | None = None) -> bool:
        """设置绝对缩放倍率（用于“双击复位”“按钮放大”等）。"""
        target = _clamp(float(zoom), self.MIN_ZOOM, self.MAX_ZOOM)
        if abs(target - self.zoom) < 1e-9:
            return False
        if anchor is None:
            anchor = (self.width / 2.0, self.height / 2.0)
        return self.zoom_at(anchor[0], anchor[1], target / self.zoom)

    def pan_by(self, dx: float, dy: float) -> None:
        """按屏幕像素平移视图。"""
        self.pan_x += float(dx)
        self.pan_y += float(dy)
        self.clamp()

    def reset(self) -> None:
        """回到“刚打开时”的适配状态。"""
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0

    def clamp(self) -> None:
        """限制平移范围：无论怎么拖，图都不能被拖出屏幕看不见。"""
        left, top = self.to_screen(self.min_x, self.max_y)
        right, bottom = self.to_screen(self.max_x, self.min_y)
        keep_x = min(90.0, max(24.0, self.width * 0.15))
        keep_y = min(90.0, max(24.0, self.height * 0.15))

        if right < keep_x:                      # 图整个跑到左边了
            self.pan_x += keep_x - right
        elif left > self.width - keep_x:        # 图整个跑到右边了
            self.pan_x -= left - (self.width - keep_x)
        if bottom < keep_y:                     # 图整个跑到上边了
            self.pan_y += keep_y - bottom
        elif top > self.height - keep_y:        # 图整个跑到下边了
            self.pan_y -= top - (self.height - keep_y)


# ----------------------------------------------------------------------
# 主组件
# ----------------------------------------------------------------------
class CanvasView(tk.Canvas):
    """地铁网络画布：绘制、交互、路径高亮、动画、导出。

    参数
    ----
    parent : 任意 Tk 容器（通常是 P3 窗口里的 Frame）。
    graph  : P1 的图对象（只读使用 stations / lines / bbox）。
    show_labels : 是否显示站名（默认 True）。
    """

    CONFIGURE_DEBOUNCE_MS = 120   # 窗口尺寸变化后等 120ms 再重绘
    HIT_RADIUS = 12               # 点击命中站点的像素半径（验收要求 ≤12px）

    def __init__(
        self,
        parent: tk.Misc,
        graph: object,
        *,
        width: int = 1000,
        height: int = 700,
        show_labels: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            width=width,
            height=height,
            background=BG,
            highlightthickness=0,
            bd=0,
            takefocus=1,
            **kwargs,
        )

        # --- 数据与视图状态 ---
        self._graph_raw = graph
        self.draw_graph = normalise_graph(graph)
        self.show_labels = bool(show_labels)
        self._transform = ViewTransform(self.draw_graph.bbox, width, height)

        # --- 交互状态 ---
        self._redraw_job: str | None = None
        self._hover_job: str | None = None
        self._click_callbacks: list[Callable[[str], None]] = []
        self._hover_sid: str | None = None
        self._pointer: tuple[int, int] | None = None
        self._press: tuple[int, int] | None = None
        self._drag_last: tuple[int, int] | None = None
        self._dragged = False

        # --- 路径与动画状态 ---
        self._current_path: PathView | None = None
        self._metrics: PathMetrics | None = None
        self._anim_job: str | None = None
        self._animating = False
        self._anim_paused = False
        self._anim_view: PathView | None = None
        self._anim_visited = 0
        self._anim_legs = 0
        self._anim_speed = 1
        self._animation_end_callbacks: list[Callable[[], None]] = []

        # --- 布局结果（供 selfcheck 读取）---
        self._screen: dict[str, tuple[float, float]] = {}
        self._reserved: list[tuple] = []
        self._marker_boxes: list[tuple] = []
        self._legend_rect: tuple | None = None
        self._legend_shown = 0
        self._info_rect: tuple | None = None
        self._badge_rect: tuple | None = None
        self.station_label_boxes: dict[str, LabelBox] = {}
        self.segment_label_boxes: list[LabelBox] = []
        self.hidden_station_labels: list[str] = []
        self.hidden_segment_labels = 0

        # --- 字体 ---
        self._font_cache: dict[tuple[int, bool], tkfont.Font] = {}
        # 文字尺寸缓存：站名/标注的宽度在缩放、平移时不会变，缓存后可省掉
        # 大量 Tk 字体测量调用（134 站地图重绘从 ~200ms 降到 ~50ms）。
        self._text_size_cache: dict[tuple[str, int, bool], tuple[float, float]] = {}
        families = set(tkfont.families(self))
        self._font_family = next(
            (name for name in FONT_FAMILY_CANDIDATES if name in families),
            tkfont.nametofont("TkDefaultFont").actual("family"),
        )

        self._bind_events()
        # 首次绘制稍等片刻：Tk 还没完成布局时 winfo_width() 是 1
        self._redraw_job = self.after(40, self._redraw)

    # ==================================================================
    # 对外接口（P3 只认这几个方法）
    # ==================================================================
    def bind_click(self, callback: Callable[[str], None]) -> None:
        """注册“点击站点”回调；回调参数是站点 ID。"""
        if callable(callback) and callback not in self._click_callbacks:
            self._click_callbacks.append(callback)

    def show_path(self, result: object) -> dict:
        """静态高亮一条路径，返回统计值（字典）。"""
        self._cancel_animation()
        view = normalise_result(self._graph_raw, self.draw_graph, result)
        self._current_path = view
        self._metrics = view.metrics
        self._schedule_redraw(0)
        return self._metrics.as_dict()

    def animate(self, result: object, speed_level: int = 1) -> dict:
        """逐帧播放搜索/路径动画。

        speed_level 支持 1、2、4 倍速；动画只改变“画到什么程度”，
        所有数值在动画开始前就已经确定，因此动画结果一定与立即显示一致。
        """
        self._cancel_animation()
        view = normalise_result(self._graph_raw, self.draw_graph, result)
        self._metrics = view.metrics

        # 没找到路径时没有可动画的站点，直接按静态结果展示
        if not view.found or not view.stations:
            self._current_path = view
            self._schedule_redraw(0)
            return self._metrics.as_dict()

        self._current_path = None       # 动画过程中不画完整路径
        self._anim_view = view
        self._animating = True
        self._anim_paused = False
        self._anim_visited = 0
        self._anim_legs = 0
        try:
            self._anim_speed = min((1, 2, 4), key=lambda v: abs(v - int(speed_level)))
        except (TypeError, ValueError):
            self._anim_speed = 1

        self._redraw()
        self._anim_job = self.after(self._interval(30), self._anim_tick)
        return self._metrics.as_dict()

    def reset(self) -> None:
        """清空路径和视图状态，回到刚打开画布的样子。"""
        self._cancel_animation()
        self._current_path = None
        self._metrics = None
        self._hover_sid = None
        self._transform.reset()
        self._schedule_redraw(0)

    def reset_view(self) -> None:
        """只复位缩放和平移，保留当前路径。"""
        self._transform.reset()
        self._schedule_redraw(0)

    def refresh(self) -> None:
        """请求一次重画（公开方法，供 P3/宿主在数据或状态变化后调用）。

        P3 不要直接调用私有的 ``_redraw``：它绕过了防抖与“是否正在动画”
        的保护逻辑；``refresh()`` 走统一的调度入口，等价于安全的整图重绘。
        """
        self._schedule_redraw(0)

    def export(self, path: str) -> str:
        """导出当前画布。

        * ``.png / .jpg``：截取屏幕上的画布（用 Pillow 的 ImageGrab）；
        * ``.ps / .eps``：Tkinter 原生 PostScript，矢量图，放进报告更清晰。
        """
        target = os.path.abspath(path)
        folder = os.path.dirname(target)
        if folder:
            os.makedirs(folder, exist_ok=True)
        ext = os.path.splitext(target)[1].lower()

        if ext in {".ps", ".eps"}:
            self.update_idletasks()
            self.postscript(
                file=target,
                colormode="color",
                x=0,
                y=0,
                width=max(self.winfo_width(), 1),
                height=max(self.winfo_height(), 1),
            )
            return target

        if ext not in {".png", ".jpg", ".jpeg"}:
            raise ValueError(f"不支持的导出格式：{ext}（可选 .png/.jpg/.ps）")
        if ImageGrab is None:
            raise RuntimeError("导出 PNG 需要 Pillow：pip install pillow")

        self.update_idletasks()
        self.update()
        if not self.winfo_ismapped():
            raise RuntimeError("画布还没有显示出来，无法截图导出。")
        # Windows 的窗口合成（DWM）比 Tk 的 update() 慢半拍：
        # 刚创建的窗口如果立刻截屏，可能截到它后面的桌面。这里短暂地
        # 泵一会儿事件循环，等窗口真正出现在屏幕上再抓图。
        deadline = time.perf_counter() + 0.35
        while time.perf_counter() < deadline:
            try:
                self.update()
            except tk.TclError:
                break
            time.sleep(0.01)
        x, y = self.winfo_rootx(), self.winfo_rooty()
        width, height = max(self.winfo_width(), 1), max(self.winfo_height(), 1)
        try:
            image = ImageGrab.grab(
                bbox=(x, y, x + width, y + height), all_screens=True
            )
        except TypeError:
            # 个别平台/旧版 Pillow 不认 all_screens 参数
            image = ImageGrab.grab(bbox=(x, y, x + width, y + height))
        if ext != ".png":
            image = image.convert("RGB")
        image.save(target)
        return target

    # ---- 其它便捷方法（P3 的按钮会用到）----
    def get_metrics(self) -> dict:
        """当前路径的统计值；没有路径时返回空字典。"""
        return self._metrics.as_dict() if self._metrics else {}

    def zoom_level(self) -> float:
        return self._transform.zoom

    def set_zoom(self, zoom: float) -> None:
        if self._transform.set_zoom(zoom):
            self._schedule_redraw(0)

    def pan_by(self, dx: float, dy: float) -> None:
        self._transform.pan_by(dx, dy)
        self._schedule_redraw(0)

    def world_to_screen(self, x: float, y: float) -> tuple[float, float]:
        return self._transform.to_screen(x, y)

    def screen_to_world(self, x: float, y: float) -> tuple[float, float]:
        return self._transform.to_world(x, y)

    def station_screen(self, station_id: str) -> tuple[float, float] | None:
        return self._screen.get(station_id)

    def hit_test(self, screen_x: float, screen_y: float, radius: float | None = None) -> str | None:
        """返回距离 (screen_x, screen_y) 最近的站点 ID（超过半径就返回 None）。"""
        limit = self.HIT_RADIUS if radius is None else float(radius)
        best_id: str | None = None
        best_dist = limit + 1e-9
        for sid, station in self.draw_graph.stations.items():
            px, py = self._transform.to_screen(station.x, station.y)
            dist = math.hypot(px - screen_x, py - screen_y)
            if dist <= best_dist:
                best_id = sid
                best_dist = dist
        return best_id

    def set_graph(self, graph: object) -> None:
        """换一张图（P3 重新加载数据时调用）。"""
        self._cancel_animation()
        self._graph_raw = graph
        self.draw_graph = normalise_graph(graph)
        self._text_size_cache.clear()
        self._transform = ViewTransform(
            self.draw_graph.bbox, max(self.winfo_width(), 1), max(self.winfo_height(), 1)
        )
        self._current_path = None
        self._metrics = None
        self._hover_sid = None
        self._schedule_redraw(0)

    def set_show_labels(self, show: bool) -> None:
        self.show_labels = bool(show)
        self._schedule_redraw(0)

    def on_animation_end(self, callback: Callable[[], None]) -> None:
        if callable(callback) and callback not in self._animation_end_callbacks:
            self._animation_end_callbacks.append(callback)

    @property
    def is_animating(self) -> bool:
        return self._animating

    @property
    def is_paused(self) -> bool:
        return self._anim_paused

    def toggle_pause(self) -> bool:
        """暂停/继续动画；返回 True 表示当前是“暂停”状态。"""
        if not self._animating:
            return False
        self._anim_paused = not self._anim_paused
        if self._anim_paused:
            self._cancel_anim_job()
        else:
            self._anim_job = self.after(self._interval(10), self._anim_tick)
        self._schedule_redraw(0)
        return self._anim_paused

    def skip_animation(self) -> None:
        """跳过动画，直接显示最终结果。"""
        if self._animating:
            self._finish_animation()

    # ==================================================================
    # 事件绑定
    # ==================================================================
    def _bind_events(self) -> None:
        self.bind("<Configure>", self._on_configure)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Double-Button-1>", self._on_double_click)
        self.bind("<MouseWheel>", self._on_mousewheel)
        # Linux 下滚轮是 Button-4/5 事件
        self.bind("<Button-4>", self._on_mousewheel)
        self.bind("<Button-5>", self._on_mousewheel)
        # 键盘快捷键（画布获得焦点时生效）
        self.bind("<KeyPress-plus>", lambda _e: self.set_zoom(self._transform.zoom * 1.2))
        self.bind("<KeyPress-equal>", lambda _e: self.set_zoom(self._transform.zoom * 1.2))
        self.bind("<KeyPress-minus>", lambda _e: self.set_zoom(self._transform.zoom / 1.2))
        self.bind("<KeyPress-0>", lambda _e: self.reset_view())
        self.bind("<space>", lambda _e: self.toggle_pause())
        self.bind("<KeyPress-s>", lambda _e: self.skip_animation())

    def _on_configure(self, event: tk.Event) -> None:
        """窗口尺寸变化：先更新视口，再防抖重绘。

        为什么防抖？窗口拖动时 Tk 会连续发很多 Configure 事件，
        每个都重绘会很卡；而程序刚启动时窗口常常还是 1x1，
        马上按 1x1 算坐标会一直错。等 120ms 稳定后再画就没事了。
        """
        if event.width <= 2 or event.height <= 2:
            return
        self._transform.resize(event.width, event.height)
        self._schedule_redraw(self.CONFIGURE_DEBOUNCE_MS)

    def _on_mousewheel(self, event: tk.Event) -> None:
        num = getattr(event, "num", None)
        if num == 4:
            delta = 120
        elif num == 5:
            delta = -120
        else:
            delta = getattr(event, "delta", 0)
        if not delta:
            return
        # 120 的滚动量约等于 1.2 倍；指数写法让触控板的小增量也平滑
        factor = 1.0015 ** float(delta)
        if self._transform.zoom_at(event.x, event.y, factor):
            self._schedule_redraw(16)

    def _on_motion(self, event: tk.Event) -> None:
        self._pointer = (event.x, event.y)
        # 悬停判断也做一点延迟，避免鼠标快速划过时疯狂重绘
        if self._hover_job is not None:
            try:
                self.after_cancel(self._hover_job)
            except tk.TclError:
                pass
        self._hover_job = self.after(16, self._update_hover)

    def _on_leave(self, _event: tk.Event) -> None:
        self._pointer = None
        if self._hover_sid is not None:
            self._hover_sid = None
            self.delete("hover")
            self.configure(cursor="")

    def _on_press(self, event: tk.Event) -> None:
        self.focus_set()
        self._press = (event.x, event.y)
        self._drag_last = (event.x, event.y)
        self._dragged = False

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_last is None:
            return
        dx = event.x - self._drag_last[0]
        dy = event.y - self._drag_last[1]
        if self._press and math.hypot(event.x - self._press[0], event.y - self._press[1]) > 3:
            self._dragged = True
            self.configure(cursor="fleur")
            self.delete("hover")
        self._drag_last = (event.x, event.y)
        if not self._dragged:
            return

        # 拖动时只“整体平移”已经画好的图形，不重新排站名。
        # 134 站全量重绘约 200ms，平移只需几毫秒，拖起来才跟手。
        old_x, old_y = self._transform.pan_x, self._transform.pan_y
        self._transform.pan_by(dx, dy)
        move_x = self._transform.pan_x - old_x
        move_y = self._transform.pan_y - old_y
        if abs(move_x) < 0.01 and abs(move_y) < 0.01:
            return
        for tag in ("network", "station", "path", "label", "anim"):
            self.move(tag, move_x, move_y)
        # 同步屏幕坐标缓存，动画/悬停/松手后的重绘都靠它
        self._screen = {
            sid: (px + move_x, py + move_y)
            for sid, (px, py) in self._screen.items()
        }

    def _on_release(self, event: tk.Event) -> None:
        was_dragging = self._dragged
        self._press = None
        self._drag_last = None
        self._dragged = False
        self.configure(cursor="")

        if was_dragging:
            # 松手后补一次完整重绘：确认边界、站名、图例都是最终位置
            self._schedule_redraw(0)
            return
        sid = self.hit_test(event.x, event.y)
        if sid:
            self._hover_sid = sid
            self._draw_hover()
            for callback in list(self._click_callbacks):
                callback(sid)

    def _on_double_click(self, event: tk.Event) -> None:
        """双击空白处 / 站点都复位视图（P3 的“复位”按钮同义）。"""
        self.reset_view()

    def _update_hover(self) -> None:
        self._hover_job = None
        if self._pointer is None:
            return
        sid = self.hit_test(self._pointer[0], self._pointer[1])
        if sid != self._hover_sid:
            self._hover_sid = sid
            self._draw_hover()
            self.configure(cursor="hand2" if sid else "")

    def _draw_hover(self) -> None:
        """只重画悬停层，不重画整张图，鼠标移动才跟手。"""
        self.delete("hover")
        if not self._hover_sid:
            return
        station = self.draw_graph.stations.get(self._hover_sid)
        if not station:
            return
        px, py = self._transform.to_screen(station.x, station.y)
        radius = max(8.0, self._station_radius() * 1.9)
        self.create_oval(
            px - radius, py - radius, px + radius, py + radius,
            outline=ACCENT, width=2.0, tags=("hover", "hoverline"),
        )

        # 深色小提示框：站名 + 经过的线路
        line_text = " / ".join(self.draw_graph.line_names(station.id))
        text = station.name + (f"  ·  {line_text}" if line_text else "")
        font = self._font(9, True)
        tw, th = self._text_size(text, 9, True)
        th = max(th, 13)
        x0, y0 = px + 16, py + 14
        if x0 + tw + 14 > self.winfo_width():
            x0 = px - tw - 24
        if y0 + th + 12 > self.winfo_height():
            y0 = py - th - 24
        x0 = max(6, x0)
        y0 = max(6, y0)
        self.create_rectangle(
            x0, y0, x0 + tw + 14, y0 + th + 10,
            fill="#0F172A", outline="#334155", width=1, tags=("hover",),
        )
        self.create_text(
            x0 + 7, y0 + 5, text=text, anchor="nw",
            fill="#F8FAFC", font=font, tags=("hover",),
        )
        self.tag_raise("hover")

    # ==================================================================
    # 重绘主流程
    # ==================================================================
    def _schedule_redraw(self, delay_ms: int = 0) -> None:
        """合并多次重绘请求，只保留最后一次。"""
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except tk.TclError:
                pass
        self._redraw_job = self.after(max(0, int(delay_ms)), self._redraw)

    def _font(self, size: int, bold: bool = False) -> tkfont.Font:
        """按 (字号, 是否加粗) 缓存字体对象，避免每次重绘都新建。"""
        key = (int(size), bool(bold))
        if key not in self._font_cache:
            self._font_cache[key] = tkfont.Font(
                self,
                family=self._font_family,
                size=int(size),
                weight="bold" if bold else "normal",
            )
        return self._font_cache[key]

    def _text_size(self, text: str, size: int, bold: bool = False) -> tuple[float, float]:
        """返回文字的 (像素宽, 像素高)，只测量一次。

        Tk 的 ``font.measure`` 每次都要跨进程调用 Tcl，134 个站名每帧量一遍
        会明显拖慢拖动/缩放。文字内容不变时尺寸也不变，所以缓存起来。
        """
        key = (text, int(size), bool(bold))
        cached = self._text_size_cache.get(key)
        if cached is None:
            font = self._font(size, bold)
            cached = (float(font.measure(text)), float(font.metrics("linespace")))
            self._text_size_cache[key] = cached
        return cached

    def _redraw(self) -> None:
        """完整重绘：先算坐标，再按“底图 -> 路径 -> 站点 -> 文字 -> 图例”的顺序画。"""
        self._redraw_job = None
        if not self.winfo_exists():
            return
        width, height = self.winfo_width(), self.winfo_height()
        if width < 80 or height < 80:
            # 窗口还没真正显示出来，等下一轮
            self._schedule_redraw(80)
            return

        self._transform.resize(width, height)
        self.delete("all")

        # 重置每一帧的布局状态
        self._screen = {}
        self._marker_boxes = []
        self._reserved = []
        self._legend_rect = None
        self._info_rect = None
        self._badge_rect = None
        self.station_label_boxes = {}
        self.segment_label_boxes = []
        self.hidden_station_labels = []
        self.hidden_segment_labels = 0

        for sid, station in self.draw_graph.stations.items():
            self._screen[sid] = self._transform.to_screen(station.x, station.y)

        path = self._current_path
        # 提前算好图例/信息卡/缩放提示占用的区域，站名排版会避开它们
        self._legend_rect = self._plan_legend_rect(width, height)
        self._info_rect = self._plan_info_rect(width, height, path)
        self._badge_rect = self._plan_badge_rect(width, height)
        self._reserved = [r for r in (self._legend_rect, self._info_rect, self._badge_rect) if r]

        self._draw_grid(width, height)
        self._draw_lines()
        if path is not None and path.found and not self._animating:
            self._draw_path_line(path)
        self._draw_stations()
        if path is not None and not self._animating:
            self._draw_path_markers(path)
        if self._animating and self._anim_view is not None:
            self._paint_anim_items()
        # 先排“每段 km/min”标注（靠近路径），再排站名。
        # 站名排版会把分段标注当作障碍物，所以两类文字仍然零重叠；
        # 否则密集路网里站名会先把路径两侧的位置占满，分段标注全被省略。
        self._draw_segment_labels(path)
        self._draw_station_labels(path)
        self._draw_legend()
        self._draw_info_card(path)
        self._draw_badge()
        self._draw_hover()

        # 文字层永远在最上面，避免被后画的动画线条盖住
        self.tag_raise("label")
        self.tag_raise("overlay")
        self.tag_raise("hover")

    # ------------------------------------------------------------------
    # 底图：网格、线路、站点
    # ------------------------------------------------------------------
    def _draw_grid(self, width: int, height: int) -> None:
        """很浅的方格背景，让画布有“图纸”的感觉，不影响读图。"""
        step = 64
        for x in range(0, width, step):
            self.create_line(x, 0, x, height, fill=GRID, tags=("background",))
        for y in range(0, height, step):
            self.create_line(0, y, width, y, fill=GRID, tags=("background",))

    def _line_width(self, line: DrawLine) -> float:
        """线宽随缩放略微增大，但限制范围，避免放大后糊成一片。"""
        base = 3.0 if line.feeder else 3.8
        factor = _clamp(self._transform.zoom ** 0.35, 0.8, 1.6)
        return max(1.4, base * factor)

    def _station_radius(self) -> float:
        return _clamp(3.5 * (self._transform.zoom ** 0.35), 2.4, 5.6)

    def _path_width(self) -> float:
        return _clamp(6.0 * (self._transform.zoom ** 0.35), 4.0, 10.0)

    def _draw_lines(self) -> None:
        """画所有线路。

        * 先画白色“描边”，再画彩色线，交叉处更容易分辨；
        * 环线首尾相连；
        * 接驳线/支线用虚线。
        """
        # 环线、接驳线先画，普通线后画，保证主要线路压在上面
        ordered = sorted(self.draw_graph.lines, key=lambda l: (not l.loop, not l.feeder))
        for line in ordered:
            if len(line.station_ids) < 2:
                continue
            points: list[float] = []
            for sid in line.station_ids:
                if sid not in self._screen:
                    continue
                points.extend(self._screen[sid])
            if len(points) < 4:
                continue
            if line.loop and line.station_ids:
                first = self._screen.get(line.station_ids[0])
                if first:
                    points.extend(first)

            width = self._line_width(line)
            common = {
                "capstyle": "round",
                "joinstyle": "round",
                "tags": ("network", f"line:{line.id}"),
            }
            # 白色描边让线路在网格和交叉处更清楚
            self.create_line(*points, fill=CARD, width=width + 2.6, **common)
            if line.feeder:
                self.create_line(*points, fill=line.color, width=width, dash=FEEDER_DASH, **common)
            else:
                self.create_line(*points, fill=line.color, width=width, **common)

    def _draw_stations(self) -> None:
        """站点：普通站画圆点，换乘站画菱形（和线路圆点区分开）。"""
        radius = self._station_radius()
        for sid, station in self.draw_graph.stations.items():
            px, py = self._screen[sid]
            if self.draw_graph.is_transfer(sid):
                r = radius * 1.35
                self.create_polygon(
                    px, py - r, px + r, py, px, py + r, px - r, py,
                    fill=CARD, outline=TRANSFER, width=1.8,
                    tags=("station", f"station:{sid}"),
                )
            else:
                self.create_oval(
                    px - radius, py - radius, px + radius, py + radius,
                    fill=CARD, outline="#7C8DA6", width=1.2,
                    tags=("station", f"station:{sid}"),
                )

    # ------------------------------------------------------------------
    # 路径高亮、起终点、换乘标记
    # ------------------------------------------------------------------
    def _path_polyline(self, path: PathView) -> list[float]:
        """把路径的站点序列转成屏幕折线（去掉相邻重复点）。"""
        points: list[float] = []
        last_sid: str | None = None
        for sid in path.stations:
            if sid == last_sid or sid not in self._screen:
                continue
            points.extend(self._screen[sid])
            last_sid = sid
        return points

    def _draw_path_line(self, path: PathView) -> None:
        """路径主体：白色粗描边 + 玫红粗线，压在线路之上、站点之下。"""
        points = self._path_polyline(path)
        if len(points) < 4:
            return
        width = self._path_width()
        self.create_line(
            *points, fill=CARD, width=width + 5.0,
            capstyle="round", joinstyle="round", tags=("path", "pathhalo"),
        )
        self.create_line(
            *points, fill=HIGHLIGHT, width=width,
            capstyle="round", joinstyle="round", tags=("path",),
        )

    def _draw_path_markers(self, path: PathView) -> None:
        """路径上的小圆点、换乘站橙色菱形、起点/终点徽标。"""
        if not path.found or not path.stations:
            return
        start, end = path.stations[0], path.stations[-1]

        # 1) 途经站点的小圆点
        for sid in path.stations:
            if sid in (start, end) or sid not in self._screen:
                continue
            px, py = self._screen[sid]
            r = 3.2 * _clamp(self._transform.zoom ** 0.3, 0.9, 1.4)
            self.create_oval(
                px - r, py - r, px + r, py + r,
                fill=HIGHLIGHT, outline=CARD, width=1.0, tags=("path",),
            )

        # 2) 换乘站：橙色菱形 + “换”字，比普通换乘站更醒目
        transfer_ids: list[str] = []
        for leg in path.legs:
            if leg.kind == "transfer" and leg.from_id not in transfer_ids:
                transfer_ids.append(leg.from_id)
        for sid in transfer_ids:
            if sid not in self._screen:
                continue
            px, py = self._screen[sid]
            r = 8.4
            # 橙色大菱形 + 白底：和图例里的“换乘站（菱形）”对应。
            # 字号太小会把“换”字糊成一团，所以这里只用形状区分。
            self.create_polygon(
                px, py - r, px + r, py, px, py + r, px - r, py,
                fill=CARD, outline=ANIM, width=2.8, tags=("path", "transfermark"),
            )

        # 3) 起点 / 终点徽标
        if start == end:
            pair = [(start, "#7C3AED", "起终")]
        else:
            pair = [(start, START, "起"), (end, END, "终")]
        for sid, color, text in pair:
            if sid not in self._screen:
                continue
            px, py = self._screen[sid]
            r = 11.0
            self.create_oval(
                px - r, py - r, px + r, py + r,
                fill=color, outline=CARD, width=2.4, tags=("path", "endpoint"),
            )
            self.create_text(
                px, py, text=text, fill="#FFFFFF",
                font=self._font(7 if len(text) > 1 else 9, True),
                tags=("path", "endpoint"),
            )
            # 记录徽标占用区域，站名排版会避开
            self._marker_boxes.append((px - r - 1, py - r - 1, px + r + 1, py + r + 1))
            if self._transform.zoom >= 1.6:
                self._marker_boxes.append((px - 18, py - 18, px + 18, py + 18))

    # ------------------------------------------------------------------
    # 站名避让
    # ------------------------------------------------------------------
    def _label_candidates(
        self, px: float, py: float, text_w: float, text_h: float, min_gap: float = 9.0
    ) -> list[tuple[tuple[float, float, float, float], bool]]:
        """生成“站名可以放哪里”的候选矩形。

        先试站点四周的近距离位置（看起来最自然），再按一圈圈半径往外试。
        往外放时会画一条细引线，保证读者知道名字属于哪个站。
        """
        candidates: list[tuple[tuple[float, float, float, float], bool]] = []

        def box(dx: float, dy: float, align: str) -> tuple[float, float, float, float]:
            if align == "w":
                x0 = px + dx
                y0 = py + dy - text_h / 2
            elif align == "e":
                x0 = px + dx - text_w
                y0 = py + dy - text_h / 2
            else:  # 居中
                x0 = px + dx - text_w / 2
                y0 = py + dy - text_h / 2
            return (x0, y0, x0 + text_w, y0 + text_h)

        gap = max(6.0, min_gap)
        near = [
            (gap, 0, "w"), (-gap, 0, "e"),
            (0, -gap - 5, "c"), (0, gap + 5, "c"),
            (gap, -gap - 2, "w"), (-gap, -gap - 2, "e"),
            (gap, gap + 2, "w"), (-gap, gap + 2, "e"),
            (gap, -gap - 12, "w"), (-gap, -gap - 12, "e"),
            (gap, gap + 12, "w"), (-gap, gap + 12, "e"),
        ]
        for dx, dy, align in near:
            candidates.append((box(dx, dy, align), False))

        # 远处候选：8 个方向 × 多圈半径
        for radius in (26.0, 36.0, 48.0, 62.0, 78.0, 96.0):
            for k in range(8):
                angle = math.radians(k * 45)
                dx = math.cos(angle) * radius
                dy = -math.sin(angle) * radius
                if abs(dx) < 5:
                    align = "c"
                elif dx > 0:
                    align = "w"
                else:
                    align = "e"
                candidates.append((box(dx, dy, align), True))
        return candidates

    def _path_obstacle_rects(self, path: PathView | None) -> list[tuple]:
        """把路径主干线转成一组“细长矩形”，给站名排版当障碍物。

        演示图底部的路径是一条横线，如果站名还放在站点正右方，就会正好压在
        粗红线上。把路径线段也加入避让集合后，站名会自动挪到线上方/下方。
        """
        rects: list[tuple] = []
        if path is None or not path.found:
            return rects
        last: str | None = None
        for sid in path.stations:
            if last is None:
                last = sid
                continue
            if sid == last or sid not in self._screen or last not in self._screen:
                last = sid
                continue
            x1, y1 = self._screen[last]
            x2, y2 = self._screen[sid]
            pad = max(4.0, self._path_width() / 2.0 + 1.5)
            rects.append((
                min(x1, x2) - pad, min(y1, y2) - pad,
                max(x1, x2) + pad, max(y1, y2) + pad,
            ))
            last = sid
        return rects

    def _layout_station_labels(
        self, font_size: int, path: PathView | None
    ) -> tuple[list[_PlacedLabel], list[str]]:
        """尝试给所有站名找位置；返回 (排好的标签, 放不下的站点 ID)。"""
        width, height = self.winfo_width(), self.winfo_height()
        index = _RectIndex()

        # 1) 图例/信息卡/起终点徽标：这些区域不能被站名压住
        for rect in self._reserved + self._marker_boxes:
            index.add(rect)

        # 2) 路径粗线也避开，否则横线会从站名中间穿过去
        for rect in self._path_obstacle_rects(path):
            index.add(rect)

        # 3) 已经排好的分段标注：站名不能压住它们
        for box in self.segment_label_boxes:
            index.add(box.rect)

        # 4) 底图线路同样避开：站名压在线路上会看不清（文字没有描边）
        for line in self.draw_graph.lines:
            ids = list(line.station_ids)
            pairs = list(zip(ids, ids[1:]))
            if line.loop and len(ids) > 2:
                pairs.append((ids[-1], ids[0]))
            for a, b in pairs:
                if a not in self._screen or b not in self._screen:
                    continue
                x1, y1 = self._screen[a]
                x2, y2 = self._screen[b]
                pad = 2.5
                index.add((min(x1, x2) - pad, min(y1, y2) - pad,
                           max(x1, x2) + pad, max(y1, y2) + pad))

        # 5) 站点小圆点也当作障碍物，避免站名盖住别的站
        dot_r = max(4.2, self._station_radius() + 1.2)
        for px, py in self._screen.values():
            index.add((px - dot_r, py - dot_r, px + dot_r, py + dot_r))

        path_ids = set(path.stations) if path and path.found else set()
        path_order = {sid: i for i, sid in enumerate(path.stations)} if path_ids else {}
        start = path.stations[0] if path_ids else None
        end = path.stations[-1] if path_ids else None

        def priority(sid: str):
            # 数字越小越先排：起点/终点 > 路径上的站 > 换乘站 > 普通站
            if sid == start:
                return (0, 0, 0)
            if sid == end:
                return (1, 0, 0)
            if sid in path_ids:
                return (2, path_order.get(sid, 0), 0)
            degree = len(self.draw_graph.station_lines.get(sid, ()))
            return (3, -degree, sid)

        placed: list[_PlacedLabel] = []
        hidden: list[str] = []
        for sid in sorted(self.draw_graph.stations, key=priority):
            station = self.draw_graph.stations[sid]
            px, py = self._screen[sid]
            is_transfer = self.draw_graph.is_transfer(sid)
            on_path = sid in path_ids
            bold = is_transfer or sid in (start, end)
            measured_w, measured_h = self._text_size(station.name, font_size, bold)
            text_w = measured_w + 4.0
            text_h = max(measured_h, 11.0) + 1.0

            if on_path and sid in (start, end):
                color = HIGHLIGHT
            elif on_path:
                color = "#9F1239"
            elif is_transfer:
                color = INK
            else:
                color = SUB

            chosen: tuple[float, float, float, float] | None = None
            leader = False
            for rect, far in self._label_candidates(px, py, text_w, text_h):
                if not _rect_inside(rect, width, height):
                    continue
                if index.hits(rect, pad=1.5):
                    continue
                chosen = rect
                leader = far
                break

            if chosen is None:
                hidden.append(sid)
                continue
            placed.append(
                _PlacedLabel(station.name, chosen, sid, font_size, bold, color, leader)
            )
            index.add(chosen)

        return placed, hidden

    def _draw_station_labels(self, path: PathView | None) -> None:
        """画站名：先排版，再统一画文字和引线。

        中文字体在小字号下也占不少宽度，10 站演示图一次就能放下；
        北京图很密，8 号字放不下就自动降到 7、6 号，尽量多显示几个。
        实在放不下的站名会被省略（放大视图后会重新出现），保证任何尺寸下
        **已画出的站名之间零重叠**。
        """
        self.station_label_boxes = {}
        self.hidden_station_labels = []
        if not self.show_labels or not self.draw_graph.stations:
            return

        best: tuple[list[_PlacedLabel], list[str], int] | None = None
        # 空间够用时 8 号字只排一次；只有省略比例较高（>12%）才降字号重试，
        # 否则每次平移都要排三遍，134 站地图会明显卡顿。
        tolerant = max(6, int(len(self.draw_graph.stations) * 0.12))
        for size in (8, 7, 6):
            placed, hidden = self._layout_station_labels(size, path)
            if best is None or len(hidden) < len(best[1]):
                best = (placed, hidden, size)
            if not hidden or len(hidden) <= tolerant:
                break
        if best is None:
            return

        placed, hidden, size = best
        self.hidden_station_labels = hidden
        for label in placed:
            station = self.draw_graph.stations.get(label.station_id or "")
            if station is None:
                continue
            px, py = self._screen[station.id]
            if label.leader:
                # 引线：从站点圆点到标签矩形最近的一条边
                lx = min(max(px, label.rect[0]), label.rect[2])
                ly = min(max(py, label.rect[1]), label.rect[3])
                self.create_line(
                    px, py, lx, ly, fill="#CBD5E1", width=1, dash=(2, 3),
                    tags=("label", "leader"),
                )
            font = self._font(label.font_size, label.bold)
            self.create_text(
                label.rect[0] + 2.0, (label.rect[1] + label.rect[3]) / 2.0,
                text=label.text, anchor="w", fill=label.color,
                font=font, tags=("label", f"label:{label.station_id}"),
            )
            self.station_label_boxes[label.station_id or ""] = label.to_box()

    # ------------------------------------------------------------------
    # 每段 km/min 标注
    # ------------------------------------------------------------------
    def _draw_segment_labels(self, path: PathView | None) -> None:
        """在路径每一段旁边标上“x.x km · x.x min”。

        标注优先放在线段中点的垂直方向，避免压住站名、站点和起终点徽标；
        密度太高放不下时宁可省略，也不让文字互相压盖。
        """
        self.segment_label_boxes = []
        self.hidden_segment_labels = 0
        if path is None or not path.found or self._animating or not path.legs:
            return

        width, height = self.winfo_width(), self.winfo_height()
        index = _RectIndex()
        for rect in self._reserved + self._marker_boxes:
            index.add(rect)
        dot_r = max(4.2, self._station_radius() + 1.2)
        for px, py in self._screen.values():
            index.add((px - dot_r, py - dot_r, px + dot_r, py + dot_r))

        font = self._font(7, False)
        for leg in path.legs:
            if leg.kind == "transfer" or leg.from_id == leg.to_id:
                continue
            if leg.from_id not in self._screen or leg.to_id not in self._screen:
                continue
            p1, p2 = self._screen[leg.from_id], self._screen[leg.to_id]
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            length = math.hypot(dx, dy)
            if length < 3:
                continue
            nx, ny = -dy / length, dx / length       # 线段法向量
            mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
            text = f"{leg.km:.1f} km · {leg.minutes:.1f} min"
            measured_w, measured_h = self._text_size(text, 7, False)
            text_w = measured_w + 10.0
            text_h = max(measured_h, 11.0) + 4.0

            chosen = None
            leader = False
            # 沿着法向量两侧由近到远尝试；再沿线段方向挪一挪
            for offset in (12.0, -12.0, 20.0, -20.0, 30.0, -30.0, 40.0, -40.0):
                for along in (0.0, -0.18, 0.18):
                    cx = mx + dx * along + nx * offset
                    cy = my + dy * along + ny * offset
                    rect = (cx - text_w / 2, cy - text_h / 2,
                            cx + text_w / 2, cy + text_h / 2)
                    if not _rect_inside(rect, width, height):
                        continue
                    if index.hits(rect, pad=1.5):
                        continue
                    chosen = rect
                    leader = abs(offset) > 20.0
                    break
                if chosen:
                    break

            if chosen is None:
                self.hidden_segment_labels += 1
                continue

            if leader:
                lx = min(max(mx, chosen[0]), chosen[2])
                ly = min(max(my, chosen[1]), chosen[3])
                self.create_line(
                    mx, my, lx, ly, fill="#E2E8F0", width=1, dash=(2, 3),
                    tags=("label", "segleader"),
                )
            self.create_rectangle(
                chosen[0], chosen[1], chosen[2], chosen[3],
                fill=HIGHLIGHT_BG, outline="#FECDD3", width=1,
                tags=("label", "seglabel"),
            )
            self.create_text(
                chosen[0] + 5.0, (chosen[1] + chosen[3]) / 2.0,
                text=text, anchor="w", fill="#9F1239", font=font,
                tags=("label", "seglabel"),
            )
            box = LabelBox(text, chosen, None, "segment")
            self.segment_label_boxes.append(box)
            index.add(chosen)

    # ------------------------------------------------------------------
    # 图例、信息卡、缩放提示
    # ------------------------------------------------------------------
    def _legend_rows(self) -> list[tuple[str, str, bool, str]]:
        """图例里线路条目：(线路 ID, 名称, 是否虚线, 颜色)。"""
        rows: list[tuple[str, str, bool, str]] = []
        for line in self.draw_graph.lines:
            name = line.name + ("（环线）" if line.loop else "")
            rows.append((line.id, name, line.feeder, line.color))
        return rows

    def _plan_legend_rect(self, width: int, height: int) -> tuple[float, float, float, float] | None:
        """计算图例卡片的位置和大小；太小或太矮时干脆不画图例。

        图例放在**右上角**：左上角已经被结果摘要卡占用，右下角是缩放提示，
        左下角如果放图例，会挡住“起点在西南角”这类路网边缘的站名。
        """
        if width < 760 or height < 320:
            return None
        rows = self._legend_rows()
        max_rows = max(1, int((height - 150) // 18))
        shown = min(len(rows), max_rows, 10)
        # 标题区 36 + 每行 18 + 分隔线 6+12 + 路径/换乘/虚线 3 行 54 + 余量
        card_h = 36 + shown * 18 + 6 + 12 + 54 + 10
        if len(rows) > shown:
            card_h += 18  # “其余 N 条”那一行
        card_w = 194
        self._legend_shown = shown
        x1 = width - 14.0
        return (x1 - card_w, 14.0, x1, 14.0 + card_h)

    def _draw_legend(self) -> None:
        """左下角图例：线路颜色、路径示例、换乘站符号、虚线含义。"""
        rect = self._legend_rect
        if rect is None:
            return
        x0, y0, x1, y1 = rect
        self.create_rectangle(
            x0, y0, x1, y1, fill=CARD, outline=BORDER, width=1,
            tags=("overlay", "legend"),
        )
        self.create_rectangle(
            x0, y0, x0 + 4, y1, fill=ACCENT, outline="", tags=("overlay", "legend"),
        )
        self.create_text(
            x0 + 14, y0 + 8, text="图例", anchor="nw", fill=INK,
            font=self._font(9, True), tags=("overlay", "legend"),
        )

        rows = self._legend_rows()
        shown = min(len(rows), max(1, getattr(self, "_legend_shown", len(rows))))
        y = y0 + 36
        for _lid, name, feeder, color in rows[:shown]:
            if feeder:
                self.create_line(
                    x0 + 14, y, x0 + 34, y, fill=color, width=2.4,
                    dash=FEEDER_DASH, tags=("overlay", "legend"),
                )
            else:
                self.create_line(
                    x0 + 14, y, x0 + 34, y, fill=color, width=3.2,
                    capstyle="round", tags=("overlay", "legend"),
                )
            self.create_text(
                x0 + 40, y, text=name, anchor="w", fill=SUB,
                font=self._font(8, False), tags=("overlay", "legend"),
            )
            y += 18

        if len(rows) > shown:
            self.create_text(
                x0 + 40, y, text=f"…… 其余 {len(rows) - shown} 条线路",
                anchor="w", fill="#94A3B8", font=self._font(7, False),
                tags=("overlay", "legend"),
            )
            y += 18

        y += 6
        self.create_line(x0 + 14, y, x1 - 12, y, fill=BORDER, tags=("overlay", "legend"))
        y += 12
        # 路径示例
        self.create_line(
            x0 + 14, y, x0 + 34, y, fill=HIGHLIGHT, width=5,
            capstyle="round", tags=("overlay", "legend"),
        )
        self.create_text(
            x0 + 40, y, text="搜索路径", anchor="w", fill=SUB,
            font=self._font(8, False), tags=("overlay", "legend"),
        )
        y += 18
        # 换乘站示例
        cx, cy, r = x0 + 24, y, 5.5
        self.create_polygon(
            cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy,
            fill=CARD, outline=TRANSFER, width=1.6, tags=("overlay", "legend"),
        )
        self.create_text(
            x0 + 40, y, text="换乘站（菱形）", anchor="w", fill=SUB,
            font=self._font(8, False), tags=("overlay", "legend"),
        )
        y += 18
        self.create_text(
            x0 + 14, y, text="虚线 = 接驳线 / 支线", anchor="w",
            fill="#94A3B8", font=self._font(7, False),
            tags=("overlay", "legend"),
        )

    def _plan_info_rect(self, width: int, height: int, path: PathView | None) -> tuple | None:
        """左上角结果摘要卡的占位（有搜索结果时显示）。"""
        if path is None or width < 420:
            return None
        if not path.found:
            text = "未找到路径"
            card_w = self._text_size(text, 10, True)[0] + 120
            card_w = min(max(card_w, 220), max(220, width - 36))
            return (14.0, 14.0, 14.0 + card_w, 72.0)
        m = path.metrics
        text1 = f"{m.algorithm or '搜索结果'} · {m.time_min:.1f} 分钟 · {m.distance_km:.1f} km"
        text2 = f"换乘 {m.transfers} 次 · 扩展 {m.expanded} 个站台 · 途经 {m.station_count} 站"
        card_w = max(self._text_size(text1, 10, True)[0],
                     self._text_size(text2, 8, False)[0]) + 32
        card_w = min(max(card_w, 240), max(240, width - 36))
        card_h = 58.0
        return (14.0, 14.0, 14.0 + card_w, 14.0 + card_h)

    def _draw_info_card(self, path: PathView | None) -> None:
        """左上角显示算法、耗时、里程、换乘次数等，截图时一眼能看到结论。"""
        rect = self._info_rect
        if rect is None or path is None:
            return
        x0, y0, x1, y1 = rect
        self.create_rectangle(
            x0, y0, x1, y1, fill=CARD, outline=BORDER, width=1,
            tags=("overlay", "info"),
        )
        self.create_rectangle(
            x0, y0, x0 + 4, y1, fill=HIGHLIGHT if path.found else END,
            outline="", tags=("overlay", "info"),
        )
        if not path.found:
            self.create_text(
                x0 + 14, y0 + 12, text="未找到路径",
                anchor="nw", fill=END, font=self._font(10, True),
                tags=("overlay", "info"),
            )
            self.create_text(
                x0 + 14, y0 + 37, text=path.message or "这两个站点之间不连通",
                anchor="nw", fill=SUB, font=self._font(8, False),
                tags=("overlay", "info"),
            )
            return
        m = path.metrics
        self.create_text(
            x0 + 14, y0 + 9,
            text=f"{m.algorithm or '搜索结果'} · {m.time_min:.1f} 分钟 · {m.distance_km:.1f} km",
            anchor="nw", fill=INK, font=self._font(10, True),
            tags=("overlay", "info"),
        )
        self.create_text(
            x0 + 14, y0 + 33,
            text=f"换乘 {m.transfers} 次 · 扩展 {m.expanded} 个站台 · 途经 {m.station_count} 站",
            anchor="nw", fill=SUB, font=self._font(8, False),
            tags=("overlay", "info"),
        )

    def _badge_text(self) -> str:
        zoom = self._transform.zoom
        if self._animating:
            if self._anim_paused:
                return "动画已暂停 · 空格继续 · S 跳过"
            return f"动画中 {self._anim_speed}× · 空格暂停 · S 跳过"
        return f"×{zoom:.1f}   滚轮缩放 · 拖拽平移 · 双击复位"

    def _plan_badge_rect(self, width: int, height: int) -> tuple | None:
        if width < 320 or height < 120:
            return None
        text = self._badge_text()
        text_w, text_h = self._text_size(text, 7, False)
        pad_x, pad_y = 10, 6
        card_w = text_w + pad_x * 2
        card_h = max(text_h, 11) + pad_y * 2
        return (width - card_w - 12.0, height - card_h - 12.0, width - 12.0, height - 12.0)

    def _draw_badge(self) -> None:
        """右下角：当前缩放倍数与操作提示（动画时显示动画状态）。"""
        rect = self._badge_rect
        if rect is None:
            return
        x0, y0, x1, y1 = rect
        self.create_rectangle(
            x0, y0, x1, y1, fill=CARD, outline=BORDER, width=1,
            tags=("overlay", "badge"),
        )
        self.create_text(
            (x0 + x1) / 2, (y0 + y1) / 2, text=self._badge_text(),
            anchor="center", fill=SUB, font=self._font(7, False),
            tags=("overlay", "badge"),
        )

    # ------------------------------------------------------------------
    # 动画
    # ------------------------------------------------------------------
    def _interval(self, base_ms: float) -> int:
        """按速度档换算定时器间隔（1× 最慢，4× 最快）。"""
        return max(4, int(base_ms / max(1, self._anim_speed)))

    def _cancel_anim_job(self) -> None:
        if self._anim_job is not None:
            try:
                self.after_cancel(self._anim_job)
            except tk.TclError:
                pass
            self._anim_job = None

    def _cancel_animation(self) -> None:
        """停掉旧动画并清理残留帧；切换算法时先调用它。"""
        self._cancel_anim_job()
        self._animating = False
        self._anim_paused = False
        self._anim_view = None
        self._anim_visited = 0
        self._anim_legs = 0
        try:
            self.delete("anim")
        except tk.TclError:
            pass

    def _anim_tick(self) -> None:
        """定时器回调：先逐帧点亮“搜索过的站”，再逐段画出最终路径。"""
        self._anim_job = None
        if not self._animating or self._anim_paused or self._anim_view is None:
            return
        view = self._anim_view
        events = view.events or view.stations

        if self._anim_visited < len(events):
            # 把事件分组成最多 ~36 帧，134 个扩展节点也不会播太久
            step = max(1, math.ceil(len(events) / 36))
            self._anim_visited = min(len(events), self._anim_visited + step)
            self._paint_anim_items()
            self._anim_job = self.after(self._interval(30), self._anim_tick)
            return

        if self._anim_legs < len(view.legs):
            self._anim_legs += 1
            self._paint_anim_items()
            self._anim_job = self.after(self._interval(80), self._anim_tick)
            return

        self._finish_animation()

    def _paint_anim_items(self) -> None:
        """重画动画层（标签 "anim"）：已点亮站点 + 已走完的路径段。"""
        self.delete("anim")
        view = self._anim_view
        if view is None:
            return
        events = view.events or view.stations

        # 1) 搜索波：用橙色空心圆表示算法“看过”的站点
        r = max(4.0, self._station_radius() * 1.5)
        for sid in events[: self._anim_visited]:
            if sid not in self._screen:
                continue
            px, py = self._screen[sid]
            self.create_oval(
                px - r, py - r, px + r, py + r,
                outline=ANIM, width=1.2, tags=("anim", "animwave"),
            )

        # 2) 已确定的路径段：一边播放一边加粗
        width = self._path_width()
        for leg in view.legs[: self._anim_legs]:
            if leg.kind == "transfer":
                if leg.from_id in self._screen:
                    px, py = self._screen[leg.from_id]
                    rr = 7.0
                    self.create_polygon(
                        px, py - rr, px + rr, py, px, py + rr, px - rr, py,
                        outline=ANIM, width=2.0, fill="", tags=("anim",),
                    )
                continue
            if leg.from_id not in self._screen or leg.to_id not in self._screen:
                continue
            p1, p2 = self._screen[leg.from_id], self._screen[leg.to_id]
            self.create_line(
                *p1, *p2, fill=CARD, width=width + 5.0,
                capstyle="round", tags=("anim",),
            )
            self.create_line(
                *p1, *p2, fill=HIGHLIGHT, width=width,
                capstyle="round", tags=("anim",),
            )

        # 3) 当前“播放头”：路径末端的橙色圆环
        if self._anim_legs > 0:
            last_leg = view.legs[min(self._anim_legs, len(view.legs)) - 1]
            tip = last_leg.to_id
            if tip in self._screen:
                px, py = self._screen[tip]
                rr = width * 0.9
                self.create_oval(
                    px - rr, py - rr, px + rr, py + rr,
                    outline=ANIM, width=2.0, tags=("anim", "animtip"),
                )

        # 动画层要压在文字下面，但必须在底图之上；画完后把文字抬回来
        self.tag_raise("label")
        self.tag_raise("overlay")
        self.tag_raise("hover")

    def _finish_animation(self) -> None:
        """动画结束/被跳过：落到最终静态路径，统计数据一个都不改。"""
        self._cancel_anim_job()
        self._animating = False
        self._anim_paused = False
        self._current_path = self._anim_view
        self._anim_view = None
        try:
            self.delete("anim")
            self._redraw()
        except tk.TclError:
            # 宿主窗口可能在最后一帧前被销毁（如用户直接关窗），此时画布
            # 的 Tcl 命令已不存在；动画收尾不应把异常抛给调用方。
            pass
        for callback in list(self._animation_end_callbacks):
            try:
                callback()
            except Exception:
                # 回调属于 P3，渲染组件不应该因为它的异常而崩掉
                pass
