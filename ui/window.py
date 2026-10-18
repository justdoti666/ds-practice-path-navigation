#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tkinter 界面（A 题）：可视化站点网络、点选/下拉选起终点、高亮路径、搜索过程动画。

界面与算法完全解耦：本模块只消费 core.solver 的 Result（路径 / 指标 / settle 事件序列）。
动画用 after() 定时器逐帧推进，**不在主线程 sleep**（任务书 P03/P12 的口径）。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Dict, List, Optional

from core import solver
from core.graph import DataError, Graph
from core.solver import PathNotFound, Result

BG = "#f7f7f5"
FG = "#22223b"
HILITE = "#ff7f0e"
FONT = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 10, "bold")

ALGOS = [
    ("最快路径 · Dijkstra", "dijkstra"),
    ("启发式 · A*", "astar"),
    ("最少换乘 · BFS", "bfs"),
]
WEIGHTS = [("时间（min）", "time"), ("距离（km）", "distance")]


class App(tk.Tk):
    def __init__(self, graph: Graph, animate_ms: int = 90):
        super().__init__()
        self.g = graph
        self.animate_ms = animate_ms
        self.pts: Dict[str, tuple] = {}
        self.current: Optional[Result] = None
        self._anim_job = None
        self._anim_events: List[str] = []
        self._anim_idx = 0
        self._pending_path: List[str] = []

        self.title(f"城市交通路径导航模拟器 ｜ {graph.meta.get('name', '示例城市')}")
        self.geometry("1180x720")
        self.configure(bg=BG)

        self._build_layout()
        self._draw_graph()
        self.log("就绪：选择起终点后点「搜索（动画）」或「立即求解」。")

    # ------------------------------------------------------------ 布局
    def _build_layout(self):
        self.canvas = tk.Canvas(self, bg="white", highlightthickness=1,
                                highlightbackground="#dcdcdc")
        self.canvas.pack(side="left", fill="both", expand=True, padx=(10, 6), pady=10)
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<Configure>", self._on_resize)
        self._resize_job = None

        side = tk.Frame(self, bg=BG, width=330)
        side.pack(side="right", fill="y", padx=(0, 10), pady=10)
        side.pack_propagate(False)

        tk.Label(side, text="① 选择起终点", bg=BG, fg=FG, font=FONT_B).pack(anchor="w")
        names = [f"{s.name}（{s.sid}）" for s in self.g.stations.values()]
        self.start_box = self._combo(side, "起点", names)
        self.end_box = self._combo(side, "终点", names)

        self.pick_mode = tk.StringVar(value="start")
        row = tk.Frame(side, bg=BG)
        row.pack(anchor="w", pady=(2, 8))
        tk.Label(row, text="点选：", bg=BG, fg=FG, font=FONT).pack(side="left")
        for text, val in (("起点", "start"), ("终点", "end")):
            tk.Radiobutton(row, text=text, value=val, variable=self.pick_mode,
                           bg=BG, fg=FG, font=FONT, activebackground=BG).pack(side="left")

        tk.Label(side, text="② 算法与权重", bg=BG, fg=FG, font=FONT_B).pack(anchor="w", pady=(4, 2))
        self.algo_box = self._combo(side, "算法", [a[0] for a in ALGOS], 0)
        self.weight_box = self._combo(side, "权重", [w[0] for w in WEIGHTS], 0)
        self.algo_box.bind("<<ComboboxSelected>>", lambda _e: self._sync_weight_state())

        tk.Label(side, text="③ 执行", bg=BG, fg=FG, font=FONT_B).pack(anchor="w", pady=(8, 2))
        btns = tk.Frame(side, bg=BG)
        btns.pack(fill="x")
        self.btn_anim = tk.Button(btns, text="搜索（动画）", command=lambda: self.search(True),
                                  bg="#2e86de", fg="white", font=FONT, relief="flat", padx=8, pady=4)
        self.btn_anim.pack(side="left", padx=(0, 6))
        self.btn_now = tk.Button(btns, text="立即求解", command=lambda: self.search(False),
                                 bg="#10ac84", fg="white", font=FONT, relief="flat", padx=8, pady=4)
        self.btn_now.pack(side="left", padx=(0, 6))
        self.btn_cmp = tk.Button(btns, text="三算法对比", command=self.compare_all,
                                 bg="#8e44ad", fg="white", font=FONT, relief="flat", padx=8, pady=4)
        self.btn_cmp.pack(side="left")

        tk.Button(side, text="清除高亮", command=self.clear, bg="#eee", fg=FG, font=FONT,
                  relief="flat", padx=8, pady=3).pack(anchor="w", pady=(6, 8))

        tk.Label(side, text="④ 结果 / 日志", bg=BG, fg=FG, font=FONT_B).pack(anchor="w")
        self.out = tk.Text(side, height=18, font=("Consolas", 9), bg="white", fg=FG,
                           relief="flat", wrap="word", padx=6, pady=6)
        self.out.pack(fill="both", expand=True, pady=(2, 0))
        self.status = tk.Label(side, text="—", bg=BG, fg="#666", font=FONT, anchor="w")
        self.status.pack(fill="x", pady=(6, 0))

        self._sync_weight_state()

    def _combo(self, parent, label: str, values: List[str], default: int = 0) -> ttk.Combobox:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, width=5, anchor="w", bg=BG, fg=FG, font=FONT).pack(side="left")
        box = ttk.Combobox(row, values=values, state="readonly", font=FONT)
        box.current(default)
        box.pack(side="left", fill="x", expand=True)
        return box

    # ------------------------------------------------------------ 绘图
    def _draw_graph(self):
        self.canvas.delete("all")
        self.update_idletasks()
        w = max(self.canvas.winfo_width(), 600)
        h = max(self.canvas.winfo_height(), 420)
        x0, y0, x1, y1 = self.g.bbox()
        pad = 60
        sx = (w - 2 * pad) / max(x1 - x0, 1e-6)
        sy = (h - 2 * pad) / max(y1 - y0, 1e-6)
        s = min(sx, sy)
        ox = pad + ((w - 2 * pad) - (x1 - x0) * s) / 2
        oy = pad + ((h - 2 * pad) - (y1 - y0) * s) / 2

        def to_screen(x, y):
            return ox + (x - x0) * s, h - (oy + (y - y0) * s)

        self.pts = {sid: to_screen(st.x, st.y) for sid, st in self.g.stations.items()}
        # 边
        for e in self.g.edges:
            xa, ya = self.pts[e.u]
            xb, yb = self.pts[e.v]
            color = self.g.lines[e.line]["color"]
            self.canvas.create_line(xa, ya, xb, yb, fill=color, width=4,
                                    dash=(6, 4) if e.line == "L4" else None)
        # 站点与站名
        for sid, (px, py) in self.pts.items():
            st = self.g.stations[sid]
            self.canvas.create_oval(px - 6, py - 6, px + 6, py + 6, fill="white",
                                    outline="#444", width=2, tags=("station", sid))
            self.canvas.create_text(px + 9, py - 10, text=st.name, anchor="w",
                                    fill="#333", font=("Microsoft YaHei UI", 9))
        # 图例
        ly = 18
        self.canvas.create_text(14, ly - 4, text="线路：", anchor="w", fill="#555",
                                font=("Microsoft YaHei UI", 9, "bold"))
        lx = 62
        for lid, meta in self.g.lines.items():
            self.canvas.create_line(lx, ly, lx + 26, ly, fill=meta["color"], width=4)
            self.canvas.create_text(lx + 32, ly, text=meta["name"], anchor="w", fill="#555",
                                    font=("Microsoft YaHei UI", 9))
            lx += 26 + 32 + 22 * len(meta["name"]) // 4

    def _mark_endpoints(self):
        for sid, color in ((self._sid(self.start_box), "#27ae60"), (self._sid(self.end_box), "#c0392b")):
            if sid in self.pts:
                px, py = self.pts[sid]
                self.canvas.create_oval(px - 11, py - 11, px + 11, py + 11, outline=color, width=3)

    def _draw_path(self, path: List[str]):
        for a, b in zip(path, path[1:]):
            candidates = self.g.edge_between(a, b)
            if candidates:
                e = min(candidates, key=lambda x: x.time_min)
                xa, ya = self.pts[a]
                xb, yb = self.pts[b]
                self.canvas.create_line(xa, ya, xb, yb, fill=HILITE, width=7,
                                        capstyle="round", tags="path")
                mid = ((xa + xb) / 2, (ya + yb) / 2)
                self.canvas.create_text(mid[0], mid[1] - 11,
                                        text=f"{e.distance_km:.1f}km/{e.time_min:.1f}min",
                                        fill="#b45309", font=("Microsoft YaHei UI", 8), tags="path")
        for sid in path:
            if sid in self.pts:
                px, py = self.pts[sid]
                self.canvas.create_oval(px - 5, py - 5, px + 5, py + 5, fill=HILITE,
                                        outline="white", width=1, tags="path")
        self._mark_endpoints()

    # ------------------------------------------------------------ 交互
    def _on_resize(self, _event=None):
        """窗口大小变化后重绘（防抖），否则初始 1x1 时算出的坐标会一直用下去。"""
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(120, self._redraw)

    def _redraw(self):
        self._resize_job = None
        self._draw_graph()
        self._mark_endpoints()
        if self.current and self.current.found:
            self._draw_path(self.current.path)

    def _sid(self, box: ttk.Combobox) -> str:
        text = box.get()
        return text.rsplit("（", 1)[-1].rstrip("）") if text else ""

    def on_canvas_click(self, event):
        for sid, (px, py) in self.pts.items():
            if abs(event.x - px) <= 12 and abs(event.y - py) <= 12:
                box = self.start_box if self.pick_mode.get() == "start" else self.end_box
                box.set(f"{self.g.stations[sid].name}（{sid}）")
                self.log(f"点选{'起点' if self.pick_mode.get() == 'start' else '终点'}：{sid}")
                self.clear(keep_log=True)
                return
        return None

    def _sync_weight_state(self):
        is_bfs = ALGOS[self.algo_box.current()][1] == "bfs"
        self.weight_box.configure(state="disabled" if is_bfs else "readonly")

    def _algo(self) -> str:
        return ALGOS[self.algo_box.current()][1]

    def _weight(self) -> str:
        return WEIGHTS[self.weight_box.current()][1]

    # ------------------------------------------------------------ 执行
    def search(self, animate: bool = False, silent: bool = False):
        """执行一次搜索并展示。

        silent=True 时不弹模态框（冒烟/自动化用）：否则数据不匹配时会卡在对话框上，
        外层还可能把"弹了框"当成"跑成功"。交互式点击保持 silent=False。
        """
        start, end = self._sid(self.start_box), self._sid(self.end_box)
        if not start or not end:
            self.log("请先选择起点和终点")
            if not silent:
                messagebox.showwarning("提示", "请先选择起点和终点")
            return
        algo = self._algo()
        self.clear(keep_log=True)
        try:
            result = solver.solve(self.g, start, end, algo, self._weight())
        except PathNotFound as exc:
            self.log(f"❌ 不可达：{exc}\n   两站不连通（任务书要求展示的异常处理）。")
            if not silent:
                messagebox.showerror("不连通", f"{start} 与 {end} 之间没有可达路径。\n{exc}")
            return
        except DataError as exc:
            self.log(f"❌ 数据错误：{exc}")
            if not silent:
                messagebox.showerror("数据错误", str(exc))
            return

        self.current = result
        self.log(result.summary())
        self.log("  路径：" + " → ".join(f"{self.g.stations[s].name}" for s in result.path))
        self.status.configure(text=f"{result.algorithm} ｜ 扩展 {result.expanded} 节点 ｜ "
                                   f"{result.elapsed_ms:.2f} ms")
        self._mark_endpoints()
        if animate:
            self._anim_events = [sid for kind, sid in result.events if kind == "settle"]
            self._pending_path = result.path
            self._anim_idx = 0
            self._step_animation()
        else:
            self._draw_path(result.path)

    def _step_animation(self):
        if self._anim_idx < len(self._anim_events):
            sid = self._anim_events[self._anim_idx]
            if sid in self.pts:
                px, py = self.pts[sid]
                self.canvas.create_oval(px - 8, py - 8, px + 8, py + 8,
                                        outline="#2e86de", width=2, tags="anim")
                self.canvas.create_text(px + 9, py + 8, text=str(self._anim_idx + 1),
                                        fill="#2e86de", font=("Consolas", 8), tags="anim")
            self._anim_idx += 1
            self._anim_job = self.after(self.animate_ms, self._step_animation)
        else:
            self._anim_job = None
            if self._pending_path:
                self._draw_path(self._pending_path)

    def compare_all(self):
        start, end = self._sid(self.start_box), self._sid(self.end_box)
        self.clear(keep_log=True)
        self.log(f"三算法对比：{start} → {end}")
        self.log(f"{'算法':<22}{'距离km':>9}{'时间min':>10}{'换乘':>6}{'扩展':>6}{'耗时ms':>9}")
        for name, key in ALGOS:
            try:
                r = solver.solve(self.g, start, end, key, self._weight() if key != "bfs" else "time")
                self.log(f"{name:<22}{r.distance_km:>9.2f}{r.time_min:>10.2f}"
                         f"{r.transfers:>6}{r.expanded:>6}{r.elapsed_ms:>9.2f}")
            except PathNotFound:
                self.log(f"{name:<22}{'—':>9}{'不可达':>10}")
        self.log("\n注：BFS 只优化换乘次数，距离/时间是其路径的附带指标。")

    def clear(self, keep_log: bool = False):
        self.current = None
        if self._anim_job is not None:
            self.after_cancel(self._anim_job)
            self._anim_job = None
        self.canvas.delete("path")
        self.canvas.delete("anim")
        self.canvas.delete("all")
        self._draw_graph()
        self._mark_endpoints()
        if not keep_log:
            self.out.delete("1.0", "end")

    def log(self, text: str):
        self.out.insert("end", text + "\n")
        self.out.see("end")


def run_gui(graph: Graph):
    app = App(graph)
    app.mainloop()


def smoke(graph: Graph, out_ps: str) -> int:
    """无人工干预的界面冒烟：自动挑选连通起终点 → 求解 → 导出画布 → 销毁。

    必须做到两件事，否则就是"假通过"：
    * 起终点从**当前图**自动挑（不再硬编码 S4/S11，换数据也能跑）；
    * 求解后断言 `current is not None and found`，失败就抛错让 `main.py` 返回非 0。
    """
    app = App(graph)
    try:
        pair = solver.pick_connected_pair(graph)
        if pair is None:
            raise RuntimeError("图里没有连通的站点对，无法冒烟")
        start, goal = pair
        app.start_box.set(f"{graph.stations[start].name}（{start}）")
        app.end_box.set(f"{graph.stations[goal].name}（{goal}）")
        app.algo_box.current(0)
        app.search(animate=False, silent=True)
        if app.current is None or not app.current.found:
            raise RuntimeError(f"界面冒烟失败：{app.status.cget('text')}")
        app.update()
        app.canvas.postscript(file=out_ps, colormode="color")
    finally:
        app.destroy()
    return 0