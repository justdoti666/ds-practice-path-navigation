"""画布渲染器（P4 契约桩）。

本文件属于 P4（Canvas 渲染）的交付物。这里给出一个**可用的参考实现**，
让 P3 的界面在 P4 正式版就位前就能演示；P4 替换时保持下列接口即可，
``ui/window.py`` 里没有任何绘图代码：

    view = CanvasView(parent_frame, graph)
    view.bind_click(callback)              # callback(clicked_sid)，点空白不触发
    view.show_path(result)                 # 静态高亮
    view.animate(result, speed_level=1)    # 动画播放（可选 on_done 回调）
    view.reset()                           # 清除高亮/选中/动画

P3 额外用到的两个可选接口（P4 未实现时界面会自动降级，不报错）：

    view.show_selection(start_sid, end_sid)   # 画布上的起终点标记
    view.animation_duration_ms(result, speed_level)
"""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont

__all__ = ["CanvasView", "LINE_COLORS", "pick_ui_font"]

# 各线路配色（线路名 -> 颜色），未登记的线路用兜底色
LINE_COLORS = {
    "1号线": "#e5484d",
    "2号线": "#2563eb",
    "3号线": "#0f9d6e",
    "联络线": "#8b5cf6",
    "南环线": "#f59e0b",
    "S1线（在建）": "#94a3b8",
}
FALLBACK_LINE_COLOR = "#7c8798"

BG = "#fbfcfe"
EDGE_BASE = "#e2e8f2"
PATH_COLOR = "#1d4ed8"
PATH_HALO = "#ffffff"
TEXT = "#334155"
MUTED = "#8b95a5"
START_COLOR = "#16a34a"
END_COLOR = "#f97316"


def pick_ui_font(root: tk.Misc) -> str:
    """挑一个当前系统里存在的中文字体。"""
    families = set(tkfont.families(root))
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "SimHei"):
        if name in families:
            return name
    return "TkDefaultFont"


def _overlap(a, b, pad: float = 1.0) -> bool:
    """两个矩形（x1, y1, x2, y2）是否相交。"""
    return not (
        a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1]
    )


def _segment_hits_rect(segment, rect, steps: int = 20) -> bool:
    """线段是否穿过矩形（用采样近似，只为排版好看，不要求精确）。"""
    x1, y1, x2, y2 = segment
    for i in range(steps + 1):
        t = i / steps
        x = x1 + (x2 - x1) * t
        y = y1 + (y2 - y1) * t
        if rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
            return True
    return False


class CanvasView(tk.Frame):
    """把 Graph 画成示意图，并响应点选 / 高亮 / 动画。"""

    NODE_R = 6.0
    HIT_RADIUS = 13.0

    def __init__(self, parent_frame: tk.Misc, graph) -> None:
        super().__init__(parent_frame, bg=BG, highlightthickness=0)
        self.graph = graph
        self.canvas = tk.Canvas(
            self, bg=BG, highlightthickness=0, bd=0, cursor="arrow", takefocus=1
        )
        self.canvas.pack(fill="both", expand=True)

        family = pick_ui_font(self)
        self.font_station = tkfont.Font(family=family, size=9)
        self.font_badge = tkfont.Font(family=family, size=7, weight="bold")
        self.font_legend = tkfont.Font(family=family, size=8)

        self._click_cb = None
        self._selection: tuple[str | None, str | None] = (None, None)
        self._path: list[str] = []
        self._highlight_path = False
        self._hover_sid: str | None = None
        self._after_ids: list[str] = []
        self._anim_dot = None
        self._scale = 1.0
        self._origin = (0.0, 0.0)
        self._label_boxes: dict[str, tuple[float, float, float, float]] = {}

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.bind("<Destroy>", self._on_destroy)

    # ------------------------------------------------------------------
    # 公开接口（D1 冻结的契约）
    # ------------------------------------------------------------------
    def bind_click(self, callback) -> None:
        """注册点名回调：``callback(sid)``；点到空白处不回调。"""
        self._click_cb = callback

    def show_path(self, result) -> None:
        """静态高亮一条路径（不播放动画）。"""
        self._cancel_animation()
        self._path = list(getattr(result, "path", []) or []) if getattr(result, "found", False) else []
        self._highlight_path = True
        self.redraw()

    def animate(self, result, speed_level: int = 1, on_done=None) -> None:
        """沿路径逐段高亮，并以移动圆点示意行进方向。"""
        self._cancel_animation()
        path = list(getattr(result, "path", []) or []) if getattr(result, "found", False) else []
        if len(path) < 2:
            self._path = path
            self._highlight_path = True
            self.redraw()
            if on_done is not None:
                self.after(60, on_done)
            return

        self._path = path
        self._progress = 1
        self._highlight_path = True
        self._anim_speed = speed_level
        self._anim_done = on_done
        self.canvas.delete("path")
        self.canvas.delete("anim")
        self._draw_edges()
        self.redraw()
        delay = max(70, min(240, 1100 // max(1, len(path) - 1) // max(1, speed_level)))
        self._step_animation(1, delay)

    def reset(self) -> None:
        """清除路径高亮、选中标记与进行中的动画。"""
        self._cancel_animation()
        self._path = []
        self._highlight_path = False
        self._selection = (None, None)
        self.redraw()

    # ---- 可选扩展接口 ------------------------------------------------
    def show_selection(self, start_sid: str | None, end_sid: str | None) -> None:
        """画布上标出当前起点（绿）与终点（橙）。"""
        self._selection = (start_sid, end_sid)
        self.redraw()

    def animation_duration_ms(self, result, speed_level: int = 1) -> int:
        path = list(getattr(result, "path", []) or [])
        if len(path) < 2:
            return 120
        delay = max(70, min(240, 1100 // (len(path) - 1) // max(1, speed_level)))
        return delay * (len(path) - 1) + 120

    def station_screen_pos(self, sid: str) -> tuple[float, float] | None:
        """站点在画布上的像素坐标（测试/截图脚本用）。"""
        station = self.graph.stations.get(sid)
        if station is None:
            return None
        return self._to_screen(station.x, station.y)

    def pick_station_at(self, px: float, py: float) -> str | None:
        """返回像素坐标附近最近的站点 id，超出热区返回 None。"""
        best_sid, best_d = None, self.HIT_RADIUS
        for sid, station in self.graph.stations.items():
            sx, sy = self._to_screen(station.x, station.y)
            d = math.hypot(px - sx, py - sy)
            if d <= best_d:
                best_sid, best_d = sid, d
        return best_sid

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _on_configure(self, _event=None) -> None:
        self.redraw()

    def _on_click(self, event) -> None:
        sid = self.pick_station_at(event.x, event.y)
        if sid is not None and self._click_cb is not None:
            self._click_cb(sid)

    def _on_motion(self, event) -> None:
        sid = self.pick_station_at(event.x, event.y)
        if sid != self._hover_sid:
            self._hover_sid = sid
            self.canvas.configure(cursor="hand2" if sid else "arrow")
            self._draw_hover()

    def _on_destroy(self, event) -> None:
        if event.widget is self:
            self._cancel_animation()

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def _layout(self) -> None:
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        stations = list(self.graph.stations.values())
        if not stations:
            self._scale, self._origin = 1.0, (0.0, 0.0)
            return
        pad = 52.0
        xs = [s.x for s in stations]
        ys = [s.y for s in stations]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        span_x = max(xmax - xmin, 1e-6)
        span_y = max(ymax - ymin, 1e-6)
        scale = min((width - 2 * pad) / span_x, (height - 2 * pad) / span_y)
        scale = max(scale, 0.5)
        ox = (width - span_x * scale) / 2 - xmin * scale
        oy = (height - span_y * scale) / 2 - ymin * scale
        self._scale, self._origin = scale, (ox, oy)

    def _to_screen(self, x: float, y: float) -> tuple[float, float]:
        ox, oy = self._origin
        return (ox + x * self._scale, oy + y * self._scale)

    def redraw(self) -> None:
        self._layout()
        self.canvas.delete("all")
        if self.graph.is_empty:
            self._draw_empty()
            return
        self._draw_edges()
        self._draw_path()
        self._draw_stations()
        self._draw_labels()
        self._draw_selection()
        self._draw_legend()

    def _draw_empty(self) -> None:
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        top = height * 0.26
        self.canvas.create_text(
            width / 2,
            top,
            text="暂无路网数据",
            fill=MUTED,
            font=(self.font_station.cget("family"), 12, "bold"),
        )
        self.canvas.create_text(
            width / 2,
            top + 26,
            text="请检查 data/stations.csv 与 data/edges.csv",
            fill=MUTED,
            font=self.font_station,
        )

    def _edge_screen(self, edge):
        a = self.graph.stations[edge.u]
        b = self.graph.stations[edge.v]
        return (*self._to_screen(a.x, a.y), *self._to_screen(b.x, b.y))

    def _draw_edges(self) -> None:
        for edge in self.graph.iter_edges():
            x1, y1, x2, y2 = self._edge_screen(edge)
            dim = self._highlight_path and self._path
            self.canvas.create_line(
                x1, y1, x2, y2,
                fill=EDGE_BASE,
                width=8 if not dim else 6,
                capstyle=tk.ROUND,
                tags=("edge",),
            )
            color = LINE_COLORS.get(edge.line, FALLBACK_LINE_COLOR)
            self.canvas.create_line(
                x1, y1, x2, y2,
                fill=color,
                width=3,
                capstyle=tk.ROUND,
                stipple="" if not dim else "gray50",
                tags=("edge",),
            )

    def _path_points(self) -> list[tuple[float, float]]:
        pts = []
        for sid in self._path:
            station = self.graph.stations.get(sid)
            if station is None:
                continue
            pts.append(self._to_screen(station.x, station.y))
        return pts

    def _draw_path(self, upto: int | None = None) -> None:
        self.canvas.delete("path")
        if not self._path:
            return
        pts = self._path_points()
        if len(pts) < 2:
            return
        limit = len(pts) if upto is None else max(2, min(upto, len(pts)))
        segment_points = pts[:limit]
        flat = [coord for point in segment_points for coord in point]
        self.canvas.create_line(
            *flat,
            fill=PATH_HALO,
            width=11,
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
            tags=("path",),
        )
        self.canvas.create_line(
            *flat,
            fill=PATH_COLOR,
            width=6,
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
            tags=("path",),
        )
        for px, py in segment_points[1:]:
            self.canvas.create_oval(
                px - 4, py - 4, px + 4, py + 4,
                fill="#ffffff", outline=PATH_COLOR, width=2, tags=("path",),
            )
        for pid in ("anim",):
            self.canvas.delete(pid)
        # 路径要压在边线之上、站点与站名之下（动画重绘时也要保持这个层级）
        if self.canvas.find_withtag("edge"):
            self.canvas.tag_raise("path", "edge")

    def _draw_stations(self) -> None:
        self.canvas.delete("station")
        on_path = set(self._path)
        for sid, station in self.graph.stations.items():
            sx, sy = self._to_screen(station.x, station.y)
            r = self.NODE_R + (1.5 if len(station.lines) > 1 else 0)
            if sid in on_path:
                self.canvas.create_oval(
                    sx - r - 4, sy - r - 4, sx + r + 4, sy + r + 4,
                    fill="#dbeafe", outline="", tags=("station",),
                )
            self.canvas.create_oval(
                sx - r, sy - r, sx + r, sy + r,
                fill="#ffffff", outline="#1f2937", width=1.6, tags=("station",),
            )
            if len(station.lines) > 1:
                self.canvas.create_oval(
                    sx - r - 3, sy - r - 3, sx + r + 3, sy + r + 3,
                    outline="#1f2937", width=1.2, tags=("station",),
                )

    def _label_candidates(self, sx: float, sy: float, label_w: float):
        """围绕站点的 8 个候选位置：(锚点 x, 锚点 y, anchor, 外接矩形)。"""
        h = 14.0
        offsets = [
            (sx, sy - 13, "s", (sx - label_w / 2, sy - 13 - h, sx + label_w / 2, sy - 13)),
            (sx, sy + 13, "n", (sx - label_w / 2, sy + 13, sx + label_w / 2, sy + 13 + h)),
            (sx + 13, sy, "w", (sx + 13, sy - 8, sx + 13 + label_w, sy + 8)),
            (sx - 13, sy, "e", (sx - 13 - label_w, sy - 8, sx - 13, sy + 8)),
            (sx + 11, sy - 11, "sw", (sx + 11, sy - 11 - h, sx + 11 + label_w, sy - 11)),
            (sx - 11, sy - 11, "se", (sx - 11 - label_w, sy - 11 - h, sx - 11, sy - 11)),
            (sx + 11, sy + 11, "nw", (sx + 11, sy + 11, sx + 11 + label_w, sy + 11 + h)),
            (sx - 11, sy + 11, "ne", (sx - 11 - label_w, sy + 11, sx - 11, sy + 11 + h)),
        ]
        return offsets

    def _draw_labels(self) -> None:
        self.canvas.delete("label")
        self._label_boxes = {}
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        obstacles: list[tuple[float, float, float, float]] = []
        legend = self._legend_rect()
        if legend is not None:
            obstacles.append(legend)
        for station in self.graph.stations.values():
            sx, sy = self._to_screen(station.x, station.y)
            obstacles.append((sx - 10, sy - 10, sx + 10, sy + 10))
        start, end = self._selection
        for sid in (start, end):
            station = self.graph.stations.get(sid) if sid else None
            if station is not None:
                bx, by = self._to_screen(station.x, station.y)
                obstacles.append((bx + 17 - 11, by - 17 - 11, bx + 17 + 11, by - 17 + 11))

        segments = [self._edge_screen(edge) for edge in self.graph.iter_edges()]
        ordered = sorted(self.graph.stations.values(), key=lambda s: (s.y, s.x))
        for station in ordered:
            sx, sy = self._to_screen(station.x, station.y)
            on_path = station.id in self._path
            family = self.font_station.cget("family")
            font = (family, 9, "bold") if on_path else (family, 9, "normal")
            label_w = self.font_station.measure(station.name) + 8 + (6 if on_path else 0)

            best = None
            best_penalty = None
            for cx, cy, anchor, box in self._label_candidates(sx, sy, label_w):
                if box[0] < 2 or box[2] > width - 2 or box[1] < 2 or box[3] > height - 2:
                    continue
                if any(_overlap(box, other) for other in obstacles):
                    continue
                penalty = sum(
                    1 for seg in segments if _segment_hits_rect(seg, box)
                )
                if best_penalty is None or penalty < best_penalty:
                    best, best_penalty = (cx, cy, anchor, box), penalty
                if penalty == 0:
                    break

            if best is None:
                cx, cy, anchor = sx, sy - 13, "s"
                box = (sx - label_w / 2, sy - 27, sx + label_w / 2, sy - 13)
                best = (cx, cy, anchor, box)
            cx, cy, anchor, box = best
            obstacles.append(box)
            self._label_boxes[station.id] = box
            # 用 4 向偏移的"描边字"代替不透明底板，线路从字缝间仍然可见
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                self.canvas.create_text(
                    cx + dx, cy + dy, text=station.name, anchor=anchor, fill=BG,
                    font=font, tags=("label",),
                )
            self.canvas.create_text(
                cx,
                cy,
                text=station.name,
                anchor=anchor,
                fill=TEXT if not on_path else "#0f2b60",
                font=font,
                tags=("label",),
            )

    def _draw_selection(self) -> None:
        self.canvas.delete("selection")
        start, end = self._selection
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        taken = list(self._label_boxes.values())
        legend = self._legend_rect()
        if legend is not None:
            taken.append(legend)
        path_points = self._path_points()
        path_segments = list(zip(path_points, path_points[1:]))
        for sid, color, char in ((start, START_COLOR, "起"), (end, END_COLOR, "终")):
            if not sid or sid not in self.graph.stations:
                continue
            station = self.graph.stations[sid]
            sx, sy = self._to_screen(station.x, station.y)
            offsets = [(17, -17), (-17, -17), (17, 17), (-17, 17), (23, 0), (-23, 0)]
            bx, by = sx + 17, sy - 17
            for dx, dy in offsets:
                cx, cy = sx + dx, sy + dy
                box = (cx - 11, cy - 11, cx + 11, cy + 11)
                if box[0] < 2 or box[2] > width - 2 or box[1] < 2 or box[3] > height - 2:
                    continue
                if any(_overlap(box, other) for other in taken):
                    continue
                if any(
                    _segment_hits_rect((a[0], a[1], b[0], b[1]), box)
                    for a, b in path_segments
                ):
                    continue
                bx, by = cx, cy
                break
            self.canvas.create_oval(
                bx - 10, by - 10, bx + 10, by + 10,
                fill=color, outline="#ffffff", width=2, tags=("selection",),
            )
            self.canvas.create_text(
                bx, by, text=char, fill="#ffffff", font=self.font_badge, tags=("selection",),
            )
            taken.append((bx - 11, by - 11, bx + 11, by + 11))

    def _draw_hover(self) -> None:
        self.canvas.delete("hover")
        if not self._hover_sid or self._hover_sid not in self.graph.stations:
            return
        station = self.graph.stations[self._hover_sid]
        sx, sy = self._to_screen(station.x, station.y)
        self.canvas.create_oval(
            sx - 15, sy - 15, sx + 15, sy + 15,
            outline="#60a5fa", width=2, dash=(3, 2), tags=("hover",),
        )

    def _legend_lines(self) -> list[str]:
        lines: list[str] = []
        for edge in self.graph.iter_edges():
            if edge.line not in lines:
                lines.append(edge.line)
        return lines

    def _legend_rect(self) -> tuple[float, float, float, float] | None:
        lines = self._legend_lines()
        if not lines:
            return None
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        row_h = 17
        text_w = max(self.font_legend.measure(line) for line in lines)
        box_w = text_w + 48
        box_h = 16 + row_h * len(lines)
        x1 = width - box_w - 12
        y1 = height - box_h - 10
        if x1 < 4 or y1 < 4:
            return None
        return (x1, y1, x1 + box_w, y1 + box_h)

    def _draw_legend(self) -> None:
        self.canvas.delete("legend")
        lines = self._legend_lines()
        rect = self._legend_rect()
        if not lines or rect is None:
            return
        x1, y1, x2, y2 = rect
        self.canvas.create_rectangle(
            x1, y1, x2, y2,
            fill="#ffffff", outline="#e6ebf3", width=1, tags=("legend",),
        )
        for i, line in enumerate(lines):
            cy = y1 + 14 + i * 17
            self.canvas.create_line(
                x1 + 10, cy, x1 + 30, cy,
                fill=LINE_COLORS.get(line, FALLBACK_LINE_COLOR), width=3,
                capstyle=tk.ROUND, tags=("legend",),
            )
            self.canvas.create_text(
                x1 + 36, cy, text=line, anchor="w",
                fill=TEXT, font=self.font_legend, tags=("legend",),
            )

    # ------------------------------------------------------------------
    # 动画
    # ------------------------------------------------------------------
    def _step_animation(self, progress: int, delay: int) -> None:
        self._progress = progress
        self._draw_path(upto=progress)
        px, py = self._path_points()[progress - 1]
        self._anim_dot = self.canvas.create_oval(
            px - 7, py - 7, px + 7, py + 7,
            fill=PATH_COLOR, outline="#ffffff", width=2, tags=("anim",),
        )
        total = len(self._path)
        if progress >= total:
            self.canvas.delete("anim")
            self._anim_dot = None
            if self._anim_done is not None:
                callback = self._anim_done
                self._anim_done = None
                self.after(80, callback)
            return
        after_id = self.after(delay, lambda: self._step_animation(progress + 1, delay))
        self._after_ids.append(after_id)

    def _cancel_animation(self) -> None:
        for after_id in self._after_ids:
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
        self._after_ids.clear()
        self._anim_done = None
        self._anim_dot = None
        self._progress = 0
