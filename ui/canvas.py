"""P3 窗口使用的画布入口：包一层 P4 的 CanvasView。

P4 画布（vendored 到 ``ui/p4_canvas.py``）已经支持：
    bind_click / reset / show_path / animate / set_graph / refresh / set_zoom
P3 窗口还用到三个可选接口，这里补上：
    show_selection(start_id, end_id)  画起终点标记（挂在 "p3sel" tag 上）
    animation_duration_ms(result, speed_level)
    animate(..., on_done=...)          动画结束后回调
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

from ui.p4_canvas import CanvasView as _P4Canvas

START_COLOR = "#16a34a"
END_COLOR = "#f97316"


def pick_ui_font(root: tk.Misc) -> str:
    families = set(tkfont.families(root))
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                 "Noto Sans CJK SC", "SimHei"):
        if name in families:
            return name
    return "TkDefaultFont"


def _line_stations(graph) -> list[dict]:
    """从 edges 推出每条线的站序（P4 画布要求 lines[*].stations）。"""
    lines = []
    for lid, meta in graph.lines.items():
        edges = [e for e in graph.edges if e.line == lid]
        if not edges:
            continue
        adj: dict[str, list[str]] = {}
        for e in edges:
            adj.setdefault(e.u, []).append(e.v)
            adj.setdefault(e.v, []).append(e.u)
        ends = sorted(n for n, nb in adj.items() if len(nb) == 1)
        start = ends[0] if ends else sorted(adj)[0]
        seq, prev, cur = [], None, start
        while True:
            seq.append(cur)
            nxt = [n for n in adj[cur] if n != prev]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            if not ends and cur == start:      # 环线：回到起点为止
                seq.append(cur)
                break
        if len(seq) >= 2:
            lines.append({"id": lid, "name": meta.get("name", lid),
                          "stations": seq, "loop": not ends})
    return lines


class _CanvasGraph:
    """P4 normalise_graph 需要的只读投影。"""

    def __init__(self, graph) -> None:
        self.stations = graph.stations
        self.lines = _line_stations(graph)
        try:
            self._bbox = tuple(graph.bbox())
        except Exception:
            xs = [s.x for s in graph.stations.values()]
            ys = [s.y for s in graph.stations.values()]
            self._bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 1, 1)

    @property
    def bbox(self):
        return self._bbox


class CanvasView(_P4Canvas):
    def __init__(self, parent, graph, **kwargs):
        self._source_graph = graph
        super().__init__(parent, _CanvasGraph(graph), **kwargs)
        self._selection = (None, None)

    def set_graph(self, graph) -> None:      # P4 的签名：set_graph(self, graph)
        self._source_graph = graph
        super().set_graph(_CanvasGraph(graph))
        self._draw_selection()

    def show_selection(self, start_id, end_id) -> None:
        self._selection = (start_id, end_id)
        self._draw_selection()

    def _draw_selection(self) -> None:
        self.delete("p3sel")
        start_id, end_id = self._selection
        for sid, color, label in ((start_id, START_COLOR, "起"), (end_id, END_COLOR, "终")):
            station = self.draw_graph.stations.get(sid) if sid else None
            if station is None:
                continue
            try:
                x, y = self._transform.to_screen(station.x, station.y)
            except Exception:
                continue
            self.create_oval(x - 9, y - 9, x + 9, y + 9, outline=color, width=3, tags="p3sel")
            self.create_text(x, y - 18, text=label, fill=color,
                             font=(self._font_family, 9, "bold"), tags="p3sel")

    def refresh(self) -> None:
        super().refresh()
        self._draw_selection()

    def animation_duration_ms(self, result, speed_level: int = 1) -> int:
        count = len(getattr(result, "events", []) or [])
        speed = max(1, int(speed_level))
        return max(700, min(4000, (600 + 220 * count) // speed))

    def animate(self, result, speed_level: int = 1, on_done=None):
        super().animate(result, speed_level)
        if on_done is not None:
            self.after(self.animation_duration_ms(result, speed_level), on_done)
