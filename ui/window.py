"""P3 界面与交互层。

职责边界
--------
* 本文件只做「界面 + 交互 + 状态机」，**不含任何 Canvas 绘图代码**
  （绘图全部委托给 ``ui.canvas.CanvasView``，见 P3-06 的解耦要求）。
* 本文件也不实现最短路算法，只调用 ``core.solver.solve``（P2）。

对外可测试的部分
----------------
:class:`Controller` 是纯 Python 的状态机，不依赖 Tk，可在无窗口环境下用假
solver 驱动（见 ``tests/test_ui.py``）；:class:`MainWindow` 只负责把状态机
映射到控件。
"""

from __future__ import annotations

import csv
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tkinter import ttk

from core import solver as solver_mod
from ui.p3_compat import Graph, GraphDataError, load_graph
from ui.canvas import CanvasView, pick_ui_font
from ui.widgets import NoticeDialog, flat_button

__all__ = [
    "MainWindow",
    "Controller",
    "CompareRow",
    "SearchState",
    "UiError",
    "NoSelectionError",
    "SameStationError",
    "EmptyGraphError",
    "DataError",
    "DisconnectedError",
    "parse_station_label",
    "station_label",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
APP_TITLE = "城市交通路径导航 · 数据结构实践（选题 A）"
WINDOW_W, WINDOW_H = 1180, 720
MIN_W, MIN_H = 1180, 720  # 设计尺寸即最小尺寸，保证任何尺寸下控件都不被裁切

BG_APP = "#eef2f7"
CARD = "#ffffff"
BORDER = "#e2e8f0"
TEXT = "#0f172a"
MUTED = "#64748b"
ACCENT = "#1d4ed8"
ACCENT_SOFT = "#e8efff"

STATE_STYLE = {
    "idle": ("空闲", "#64748b", "#eef2f7"),
    "ready": ("就绪", "#1d4ed8", "#e8efff"),
    "computing": ("计算中", "#b45309", "#fef3c7"),
    "result": ("已展示", "#15803d", "#e8f7ee"),
    "error": ("错误", "#b91c1c", "#fee2e2"),
}

ALGORITHM_CHOICES = [
    ("dijkstra", "Dijkstra（堆优化）"),
    ("astar", "A*（启发式）"),
    ("bfs", "BFS（最少换乘段）"),
]
WEIGHT_CHOICES = [("time", "时间最短"), ("distance", "距离最短")]
ALGORITHM_ORDER = ("dijkstra", "astar", "bfs")
ALGORITHM_SHORT = {"dijkstra": "Dijkstra", "astar": "A*", "bfs": "BFS"}


# ---------------------------------------------------------------------------
# 异常：界面层的四类错误 + 文案（P3-05）
# ---------------------------------------------------------------------------
class UiError(Exception):
    """界面层可展示的错误基类：title / message / hint 直接进弹窗。"""

    title = "提示"
    severity = "warning"
    retryable = False

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        if hint:
            self.hint = hint


class NoSelectionError(UiError):
    title = "还没选好起终点"
    hint = "在左侧两个下拉框里选择起点和终点，或点「点选起点／点选终点」后到右侧画布上点站点。"
    retryable = False


class SameStationError(UiError):
    title = "起点和终点是同一样东西"
    hint = "选两个不同的站点，才能算出有意义的路径。"


class EmptyGraphError(UiError):
    title = "路网是空的"
    hint = "检查 data/stations.csv 与 data/edges.csv 是否存在、是否有数据，然后点「重新加载」。"
    severity = "error"
    retryable = True


class DisconnectedError(UiError):
    title = "这两个站点之间不连通"
    severity = "error"
    retryable = True

    def __init__(self, start_name: str = "", end_name: str = "") -> None:
        if start_name and end_name:
            message = f"从「{start_name}」到「{end_name}」没有任何可通行的线路。"
        else:
            message = "起点与终点之间没有任何可通行的线路。"
        hint = (
            "这类站点目前还没有接入主城路网（例如在建中的 S1 线）。"
            "换一个起点或终点再试，或检查 data/edges.csv 里是否缺了接驳区间。"
        )
        super().__init__(message, hint)


class DataError(UiError):
    title = "数据出错了"
    severity = "error"
    retryable = True

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(
            message,
            hint or "常见原因：CSV 列名不对、里程/时间不是数字、站点 id 拼错。改好数据后点「重新加载」。",
        )


# ---------------------------------------------------------------------------
# 下拉框文案：统一 ``站名（ID）``
# ---------------------------------------------------------------------------
def station_label(name: str, sid: str) -> str:
    return f"{name}（{sid}）"


def parse_station_label(text: str) -> str | None:
    """从 ``站名（ID）`` 里取回 id；空串返回 None。"""
    if not text:
        return None
    text = text.strip()
    for opener in ("（", "("):
        if opener in text:
            tail = text.rsplit(opener, 1)[1].rstrip("）)")
            return tail.strip() or None
    return None


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------
class SearchState(Enum):
    IDLE = "idle"
    READY = "ready"
    COMPUTING = "computing"
    RESULT = "result"
    ERROR = "error"

    @property
    def label(self) -> str:
        return STATE_STYLE[self.value][0]


@dataclass
class CompareRow:
    """三算法对比表的一行。"""

    algorithm: str
    result: object | None = None
    bench: dict | None = None
    matched: bool | None = None


def bench_key(start: str, end: str, weight: str, algorithm: str) -> tuple[str, str, str, str]:
    return (start, end, weight, algorithm)


def load_bench_rows(path: str | Path | None) -> tuple[dict[tuple[str, str, str, str], dict], str]:
    """读 bench.csv。返回 ``(键->行, 状态文案)``；文件缺失不致命。"""
    if path is None:
        return {}, "未配置 bench.csv"
    bench_path = Path(path)
    if not bench_path.exists():
        return {}, f"未找到 {bench_path.name}"
    rows: dict[tuple[str, str, str, str], dict] = {}
    try:
        with bench_path.open("r", encoding="utf-8-sig", newline="") as fh:
            for raw in csv.DictReader(fh):
                try:
                    key = bench_key(
                        (raw.get("start_id") or raw.get("start") or "").strip(),
                        (raw.get("end_id") or raw.get("goal") or "").strip(),
                        (raw.get("weight") or "").strip(),
                        (raw.get("algorithm") or "").strip(),
                    )
                    rows[key] = {
                        "distance_km": float(raw.get("distance_km") or 0),
                        "time_min": float(raw.get("time_min") or 0),
                        "transfers": int(float(raw.get("transfers") or 0)),
                        "expanded": int(float(raw.get("expanded") or 0)),
                        "elapsed_ms": float(raw.get("elapsed_ms") or 0),
                    }
                except (TypeError, ValueError):
                    continue
    except OSError as exc:
        return {}, f"读取失败：{exc}"
    if not rows:
        return {}, f"{bench_path.name} 没有可用数据"
    return rows, f"已加载 {len(rows)} 行基准"


class Controller:
    """搜索状态机（无 Tk 依赖，便于单测）。

    状态流转::

        IDLE --选好起终点--> READY --begin_search--> COMPUTING
        COMPUTING --apply(找到)--> RESULT
        COMPUTING --apply(不连通)/fail--> ERROR
        RESULT/ERROR --改选/清除--> READY / IDLE
    """

    def __init__(
        self,
        graph: Graph,
        solve_fn=None,
        bench_rows: dict | None = None,
        bench_status: str = "",
    ) -> None:
        self.graph = graph
        self._solve = solve_fn or solver_mod.solve
        self.bench_rows = bench_rows or {}
        self.bench_status = bench_status
        self.start_id: str | None = None
        self.end_id: str | None = None
        self.algorithm = "dijkstra"
        self.weight = "time"
        self.state = SearchState.IDLE
        self.result = None
        self.last_error: UiError | None = None
        self.token = 0
        self.solve_calls = 0
        self.last_compare: list[CompareRow] = []
        self._cancelled: set[int] = set()

    # ---- 站点 ------------------------------------------------------
    def station_choices(self) -> list[tuple[str, str]]:
        """[(id, "站名（ID）")]，按 id 排序，保证下拉框顺序稳定。"""
        out = []
        for sid in sorted(self.graph.stations):
            out.append((sid, station_label(self.graph.stations[sid].name, sid)))
        return out

    def label_for(self, sid: str | None) -> str:
        if not sid or sid not in self.graph.stations:
            return ""
        return station_label(self.graph.stations[sid].name, sid)

    def name_for(self, sid: str | None) -> str:
        if not sid or sid not in self.graph.stations:
            return sid or ""
        return self.graph.stations[sid].name

    def set_start(self, sid: str | None) -> None:
        self.start_id = sid or None
        self._selection_changed()

    def set_end(self, sid: str | None) -> None:
        self.end_id = sid or None
        self._selection_changed()

    def swap(self) -> None:
        self.start_id, self.end_id = self.end_id, self.start_id
        self._selection_changed()

    def _selection_changed(self) -> None:
        if self.state is SearchState.COMPUTING:
            self.cancel()
        self.result = None
        self.last_error = None
        self.state = (
            SearchState.READY if self.start_id and self.end_id else SearchState.IDLE
        )

    def clear(self) -> None:
        """清空结果与画布状态，保留起终点选择。"""
        self.result = None
        self.last_error = None
        self._cancelled.clear()
        self.state = (
            SearchState.READY if self.start_id and self.end_id else SearchState.IDLE
        )

    # ---- 校验 ------------------------------------------------------
    def validate(self) -> None:
        if self.graph.is_empty or not self.graph.stations:
            raise EmptyGraphError("路网里没有任何站点，无法搜索。")
        if not self.start_id or not self.end_id:
            raise NoSelectionError("搜索前需要先确定起点和终点。")
        if self.start_id == self.end_id:
            raise SameStationError(
                f"起点和终点都选成了「{self.name_for(self.start_id)}」，换一个试试。"
            )
        for sid in (self.start_id, self.end_id):
            if sid not in self.graph.stations:
                raise DataError(f"站点 {sid} 不在当前路网中，可能是数据被替换过。")

    # ---- 搜索流程 --------------------------------------------------
    def begin_search(self) -> int:
        """进入「计算中」并返回 token；重复点击返回同一个 token（不重复计算）。"""
        if self.state is SearchState.COMPUTING:
            return self.token
        self.validate()
        self.token += 1
        self.state = SearchState.COMPUTING
        self.last_error = None
        return self.token

    def is_current(self, token: int) -> bool:
        return token == self.token and token not in self._cancelled

    def perform(self, token: int):
        """真正调用 P2 的 solver（在窗口里放到后台线程执行）。"""
        if not self.is_current(token):
            return None
        self.solve_calls += 1
        try:
            result = self._solve(
                self.graph, self.start_id, self.end_id, self.algorithm, self.weight
            )
        except UiError:
            raise
        except GraphDataError as exc:
            raise DataError(f"读取路网时出错：{exc}") from exc
        except Exception as exc:  # noqa: BLE001 - 任何算法/数据异常都转成可展示的错误
            raise DataError(f"算法执行失败：{type(exc).__name__}: {exc}") from exc
        if result is None or not hasattr(result, "found"):
            raise DataError("算法返回的结果格式不对（缺少 found 字段）。")
        return result

    def apply(self, token: int, result) -> UiError | None:
        """把 solver 的结果落到状态机；不连通时转成 :class:`DisconnectedError`。"""
        if not self.is_current(token):
            return None
        if result is None:
            self.state = SearchState.ERROR
            self.last_error = DataError("没有拿到计算结果。")
            return self.last_error
        if not getattr(result, "found", False):
            self.state = SearchState.ERROR
            self.last_error = DisconnectedError(
                self.name_for(self.start_id), self.name_for(self.end_id)
            )
            return self.last_error
        self.result = result
        self.last_error = None
        self.state = SearchState.RESULT
        return None

    def fail(self, token: int, error: UiError) -> None:
        if not self.is_current(token):
            return
        self.state = SearchState.ERROR
        self.last_error = error

    def cancel(self) -> None:
        if self.state is SearchState.COMPUTING:
            self._cancelled.add(self.token)
            self.result = None
            self.state = (
                SearchState.READY if self.start_id and self.end_id else SearchState.IDLE
            )

    def run_search(self):
        """同步跑完整条流程（单测与 ``--check`` 用）。"""
        token = self.begin_search()
        result = self.perform(token)
        error = self.apply(token, result)
        if error is not None:
            raise error
        return result

    # ---- 三算法对比 ------------------------------------------------
    def compare(self) -> list[CompareRow]:
        self.validate()
        rows: list[CompareRow] = []
        any_found = False
        for algorithm in ALGORITHM_ORDER:
            self.solve_calls += 1
            try:
                result = self._solve(
                    self.graph, self.start_id, self.end_id, algorithm, self.weight
                )
            except Exception as exc:  # noqa: BLE001
                raise DataError(f"{ALGORITHM_SHORT[algorithm]} 执行失败：{exc}") from exc
            found = bool(getattr(result, "found", False))
            any_found = any_found or found
            bench = self.bench_rows.get(
                bench_key(self.start_id, self.end_id, self.weight, algorithm)
            ) if self.start_id and self.end_id else None
            rows.append(
                CompareRow(
                    algorithm=algorithm,
                    result=result if found else None,
                    bench=bench,
                    matched=compare_with_bench(result, bench) if bench else None,
                )
            )
        if not any_found:
            raise DisconnectedError(self.name_for(self.start_id), self.name_for(self.end_id))
        self.last_compare = rows
        return rows


def compare_with_bench(result, bench: dict) -> bool:
    """与 bench.csv 对数值（容忍浮点误差，elapsed_ms 本来就每次不同，不参与比较）。"""
    if result is None or bench is None:
        return False
    return (
        abs(float(getattr(result, "distance_km", 0.0)) - bench["distance_km"]) <= 0.011
        and abs(float(getattr(result, "time_min", 0.0)) - bench["time_min"]) <= 0.051
        and int(getattr(result, "transfers", -1)) == bench["transfers"]
        and int(getattr(result, "expanded", -1)) == bench["expanded"]
    )


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow:
    """把 :class:`Controller` 的状态渲染到 Tk 控件上。"""

    def __init__(
        self,
        root: tk.Tk,
        graph: Graph,
        bench_rows: dict | None = None,
        bench_status: str = "",
        data_dir: str | Path | None = None,
        startup_error: UiError | None = None,
    ) -> None:
        self.root = root
        self.graph = graph
        self.data_dir = Path(data_dir) if data_dir else None
        self.family = pick_ui_font(root)
        self.controller = Controller(graph, bench_rows=bench_rows, bench_status=bench_status)
        self._animating = False
        self._last_result = None
        self.pick_mode: str | None = None
        self._bench_status = bench_status or "未配置基准"
        self._events: queue.Queue = queue.Queue()
        self._dialogs: list[NoticeDialog] = []
        self._drain_after = None

        root.title(APP_TITLE)
        root.configure(bg=BG_APP)
        root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        root.minsize(MIN_W, MIN_H)
        self._setup_style()
        self._build_layout()
        self._populate_graph_meta()
        self._refresh_station_choices()
        self._sync_state()
        self.root.bind("<Return>", self._on_enter)
        self.root.bind("<KP_Enter>", self._on_enter)
        self.root.bind("<Escape>", self._on_escape)
        self.root.bind("<Control-c>", self._on_copy)
        self.root.bind("<F5>", lambda _e: self._retry())
        self.root.after(60, self._drain_events)
        if startup_error is not None:
            self.root.after(350, lambda: self._show_error(startup_error, on_retry=self._reload_data))

    # ------------------------------------------------------------------
    # 样式
    # ------------------------------------------------------------------
    def _setup_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "TCombobox",
            fieldbackground=CARD,
            background=CARD,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            arrowcolor=MUTED,
            padding=5,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", CARD), ("disabled", "#f1f5f9")],
            foreground=[("disabled", "#94a3b8")],
            selectbackground=[("readonly", CARD)],
            selectforeground=[("readonly", TEXT)],
            bordercolor=[("focus", ACCENT)],
        )
        self.root.option_add("*TCombobox*Listbox.font", (self.family, 10))
        style.configure(
            "Treeview",
            background=CARD,
            fieldbackground=CARD,
            foreground=TEXT,
            rowheight=26,
            borderwidth=0,
            font=(self.family, 9),
        )
        style.configure(
            "Treeview.Heading",
            background="#f8fafc",
            foreground=MUTED,
            relief="flat",
            font=(self.family, 9, "bold"),
            padding=(6, 5),
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT_SOFT)],
            foreground=[("selected", TEXT)],
        )
        style.configure("Vertical.TScrollbar", background="#cbd5e1", troughcolor=CARD,
                        bordercolor=CARD, arrowcolor=MUTED)

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    def _build_layout(self) -> None:
        app = tk.Frame(self.root, bg=BG_APP, padx=14, pady=12)
        app.pack(fill="both", expand=True)
        app.grid_rowconfigure(0, weight=1)
        app.grid_columnconfigure(1, weight=1)

        self.left = tk.Frame(app, bg=CARD, width=336)
        self.left.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        self.left.pack_propagate(False)

        right = tk.Frame(app, bg=BG_APP)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        self._build_left_panel()
        self._build_right_panel(right)

    # ---- 左侧 --------------------------------------------------------
    def _build_left_panel(self) -> None:
        pad = 18
        header = tk.Frame(self.left, bg=CARD, padx=pad, pady=0)
        header.pack(fill="x", pady=(16, 10))
        tk.Label(header, text="城市交通路径导航", bg=CARD, fg=TEXT,
                 font=(self.family, 16, "bold"), anchor="w").pack(fill="x")
        tk.Label(header, text="数据结构实践 · 选题 A ｜ 最短路径演示", bg=CARD, fg=MUTED,
                 font=(self.family, 9), anchor="w").pack(fill="x", pady=(3, 0))

        tk.Frame(self.left, bg=BORDER, height=1).pack(fill="x", padx=pad)

        # 起终点
        trip = tk.Frame(self.left, bg=CARD, padx=pad, pady=0)
        trip.pack(fill="x", pady=(10, 0))
        tk.Label(trip, text="起点", bg=CARD, fg=MUTED, font=(self.family, 9)).pack(anchor="w")
        self.start_var = tk.StringVar()
        self.start_combo = ttk.Combobox(trip, textvariable=self.start_var, state="readonly",
                                        height=12)
        self.start_combo.pack(fill="x", pady=(3, 6))
        self.start_combo.bind("<<ComboboxSelected>>", self._on_start_combo)

        tk.Label(trip, text="终点", bg=CARD, fg=MUTED, font=(self.family, 9)).pack(anchor="w")
        self.end_var = tk.StringVar()
        self.end_combo = ttk.Combobox(trip, textvariable=self.end_var, state="readonly",
                                      height=12)
        self.end_combo.pack(fill="x", pady=(3, 6))
        self.end_combo.bind("<<ComboboxSelected>>", self._on_end_combo)

        pick_row = tk.Frame(trip, bg=CARD)
        pick_row.pack(fill="x", pady=(0, 2))
        self.swap_btn = flat_button(pick_row, "⇄ 交换", self._on_swap, kind="ghost")
        self.swap_btn.configure(font=(self.family, 9), padx=8, pady=4)
        self.swap_btn.pack(side="left")
        self.pick_start_btn = flat_button(pick_row, "点选起点", lambda: self._toggle_pick("start"),
                                           kind="ghost")
        self.pick_start_btn.configure(font=(self.family, 9), padx=8, pady=4)
        self.pick_start_btn.pack(side="left", padx=(6, 0))
        self.pick_end_btn = flat_button(pick_row, "点选终点", lambda: self._toggle_pick("end"),
                                         kind="ghost")
        self.pick_end_btn.configure(font=(self.family, 9), padx=8, pady=4)
        self.pick_end_btn.pack(side="left", padx=(6, 0))

        self.pick_hint = tk.Label(
            trip, text="在画布上点站点，也能直接选起终点", bg=CARD, fg=MUTED,
            font=(self.family, 8), anchor="w", justify="left", wraplength=286,
        )
        self.pick_hint.pack(fill="x", pady=(3, 0))

        # 搜索设置
        settings = tk.Frame(self.left, bg=CARD, padx=pad, pady=0)
        settings.pack(fill="x", pady=(10, 0))
        tk.Label(settings, text="算法", bg=CARD, fg=MUTED, font=(self.family, 9)).pack(anchor="w")
        self.algorithm_var = tk.StringVar(value=ALGORITHM_CHOICES[0][1])
        self.algorithm_combo = ttk.Combobox(
            settings, textvariable=self.algorithm_var, state="readonly",
            values=[label for _, label in ALGORITHM_CHOICES],
        )
        self.algorithm_combo.pack(fill="x", pady=(3, 6))
        self.algorithm_combo.bind("<<ComboboxSelected>>", self._on_algorithm_combo)

        weight_row = tk.Frame(settings, bg=CARD)
        weight_row.pack(fill="x")
        tk.Label(weight_row, text="权重", bg=CARD, fg=MUTED,
                 font=(self.family, 9)).pack(side="left")
        self.weight_var = tk.StringVar(value="time")
        for key, label in WEIGHT_CHOICES:
            tk.Radiobutton(
                weight_row, text=label, value=key, variable=self.weight_var,
                command=self._on_weight_change, bg=CARD, fg=TEXT,
                activebackground=CARD, selectcolor="#ffffff",
                font=(self.family, 9), bd=0, highlightthickness=0, cursor="hand2",
            ).pack(side="left", padx=(12, 0))

        # 操作按钮
        actions = tk.Frame(self.left, bg=CARD, padx=pad, pady=0)
        actions.pack(fill="x", pady=(12, 0))
        self.search_btn = flat_button(actions, "搜索路径  Enter", self._on_search, kind="primary")
        self.search_btn.configure(font=(self.family, 11, "bold"), pady=10)
        self.search_btn.pack(fill="x")

        row = tk.Frame(actions, bg=CARD)
        row.pack(fill="x", pady=(6, 0))
        self.compare_btn = flat_button(row, "三算法对比", self._on_compare, kind="soft")
        self.compare_btn.configure(font=(self.family, 9), pady=6)
        self.compare_btn.pack(side="left", fill="x", expand=True)
        self.cancel_btn = flat_button(row, "取消", self._on_cancel, kind="ghost")
        self.cancel_btn.configure(font=(self.family, 9), pady=6)
        self.cancel_btn.pack(side="left", padx=(6, 0))
        self.clear_btn = flat_button(row, "清除  Esc", self._on_clear, kind="ghost")
        self.clear_btn.configure(font=(self.family, 9), pady=6)
        self.clear_btn.pack(side="left", padx=(6, 0))

        # 状态
        status = tk.Frame(self.left, bg=CARD, padx=pad, pady=0)
        status.pack(fill="x", pady=(12, 0))
        self.state_chip = tk.Label(status, text="空闲", bg="#eef2f7", fg="#64748b",
                                   font=(self.family, 9, "bold"), padx=10, pady=3)
        self.state_chip.pack(side="left", anchor="n")
        self.state_text = tk.Label(
            status, text="", bg=CARD, fg=MUTED, font=(self.family, 9),
            anchor="w", justify="left", wraplength=214,
        )
        self.state_text.pack(side="left", padx=(10, 0), fill="x", expand=True)

        # 日志
        log_box = tk.Frame(self.left, bg=CARD, padx=pad, pady=0)
        log_box.pack(fill="both", expand=True, pady=(10, 14))
        tk.Label(log_box, text="运行日志", bg=CARD, fg=MUTED,
                 font=(self.family, 8, "bold"), anchor="w").pack(fill="x")
        log_wrap = tk.Frame(log_box, bg=CARD, highlightbackground=BORDER,
                            highlightthickness=1)
        log_wrap.pack(fill="both", expand=True, pady=(4, 0))
        self.log_text = tk.Text(
            log_wrap, height=5, bg="#fbfdff", fg="#475569", bd=0,
            font=(self.family, 8), padx=8, pady=6, wrap="word",
            state="disabled", cursor="arrow",
        )
        log_scroll = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        self.log_text.tag_configure("time", foreground="#94a3b8")
        self.log_text.tag_configure("warn", foreground="#b45309")
        self.log_text.tag_configure("error", foreground="#b91c1c")

    # ---- 右侧 --------------------------------------------------------
    def _build_right_panel(self, right: tk.Frame) -> None:
        toolbar = tk.Frame(right, bg=BG_APP)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.graph_meta = tk.Label(toolbar, text="", bg=BG_APP, fg=MUTED,
                                   font=(self.family, 9), anchor="w")
        self.graph_meta.pack(side="left")
        self.speed_var = tk.StringVar(value="1×")
        speed_box = ttk.Combobox(toolbar, textvariable=self.speed_var, state="readonly",
                                 values=["1×", "2×", "3×"], width=4)
        speed_box.bind("<<ComboboxSelected>>", lambda _e: None)
        self.replay_btn = flat_button(toolbar, "重播动画", self._on_replay, kind="ghost")
        self.replay_btn.configure(font=(self.family, 9), padx=8, pady=3)
        self.replay_btn.pack(side="right", padx=(6, 0))
        self.reload_btn = flat_button(toolbar, "重新加载", self._on_reload_click, kind="ghost")
        self.reload_btn.configure(font=(self.family, 9), padx=8, pady=3)
        self.reload_btn.pack(side="right", padx=(6, 0))
        speed_box.pack(side="right", padx=(0, 6))
        tk.Label(toolbar, text="动画速度", bg=BG_APP, fg=MUTED,
                 font=(self.family, 9)).pack(side="right")

        # 画布卡片
        canvas_card = tk.Frame(right, bg=BORDER, padx=1, pady=1)
        canvas_card.grid(row=1, column=0, sticky="nsew")
        canvas_card.grid_rowconfigure(0, weight=1)
        canvas_card.grid_columnconfigure(0, weight=1)
        self.view = CanvasView(canvas_card, self.graph)
        self.view.grid(row=0, column=0, sticky="nsew")
        self.view.bind_click(self._on_canvas_click)

        # 指标卡
        metrics = tk.Frame(right, bg=BG_APP)
        metrics.grid(row=2, column=0, sticky="ew", pady=(8, 8))
        self.metric_values: dict[str, tk.Label] = {}
        specs = [
            ("km", "里程", "km"),
            ("min", "时间", "min"),
            ("transfers", "换乘", "次"),
            ("expanded", "扩展节点", "个"),
            ("elapsed", "计算耗时", "ms"),
        ]
        for i, (key, title, unit) in enumerate(specs):
            metrics.grid_columnconfigure(i, weight=1, uniform="metric")
            card = tk.Frame(metrics, bg=CARD, padx=12, pady=8,
                            highlightbackground=BORDER, highlightthickness=1)
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            tk.Label(card, text=title, bg=CARD, fg=MUTED,
                     font=(self.family, 8), anchor="w").pack(fill="x")
            value_row = tk.Frame(card, bg=CARD)
            value_row.pack(fill="x", pady=(2, 0))
            value = tk.Label(value_row, text="—", bg=CARD, fg=TEXT,
                             font=(self.family, 15, "bold"), anchor="w")
            value.pack(side="left")
            tk.Label(value_row, text=unit, bg=CARD, fg=MUTED,
                     font=(self.family, 8), anchor="s").pack(side="left", padx=(4, 0), pady=(0, 2))
            self.metric_values[key] = value

        # 底部：路径 + 对比
        bottom = tk.Frame(right, bg=BG_APP, height=200)
        bottom.grid(row=3, column=0, sticky="nsew")
        bottom.grid_propagate(False)
        bottom.grid_columnconfigure(0, weight=2, uniform="bottom")
        bottom.grid_columnconfigure(1, weight=3, uniform="bottom")
        bottom.grid_rowconfigure(0, weight=1)

        path_card = tk.Frame(bottom, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        path_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        path_head = tk.Frame(path_card, bg=CARD, padx=12, pady=8)
        path_head.pack(fill="x")
        tk.Label(path_head, text="途经站点", bg=CARD, fg=TEXT,
                 font=(self.family, 9, "bold")).pack(side="left")
        self.path_meta = tk.Label(path_head, text="尚未搜索", bg=CARD, fg=MUTED,
                                  font=(self.family, 8))
        self.path_meta.pack(side="left", padx=(8, 0))
        self.copy_btn = flat_button(path_head, "复制", self._on_copy_button, kind="ghost")
        self.copy_btn.configure(font=(self.family, 8), padx=6, pady=1)
        self.copy_btn.pack(side="right")

        path_wrap = tk.Frame(path_card, bg=CARD)
        path_wrap.pack(fill="both", expand=True, padx=(10, 4), pady=(0, 8))
        self.path_text = tk.Text(
            path_wrap, height=7, bg=CARD, fg=TEXT, bd=0, font=(self.family, 9),
            padx=4, pady=2, wrap="none", state="disabled", cursor="arrow",
        )
        path_scroll = ttk.Scrollbar(path_wrap, orient="vertical", command=self.path_text.yview)
        self.path_text.configure(yscrollcommand=path_scroll.set)
        self.path_text.pack(side="left", fill="both", expand=True)
        path_scroll.pack(side="right", fill="y")
        self.path_text.tag_configure("idx", foreground="#94a3b8")
        self.path_text.tag_configure("name", foreground=TEXT, font=(self.family, 9, "bold"))
        self.path_text.tag_configure("meta", foreground=MUTED, font=(self.family, 8))
        self.path_text.tag_configure("hint", foreground="#9aa4b2", font=(self.family, 9))
        self.path_text.tag_configure("transfer", foreground="#b45309",
                                     font=(self.family, 8, "bold"))

        compare_card = tk.Frame(bottom, bg=CARD, highlightbackground=BORDER,
                                highlightthickness=1)
        compare_card.grid(row=0, column=1, sticky="nsew")
        compare_head = tk.Frame(compare_card, bg=CARD, padx=12, pady=8)
        compare_head.pack(fill="x")
        tk.Label(compare_head, text="三算法对比", bg=CARD, fg=TEXT,
                 font=(self.family, 9, "bold")).pack(side="left")
        self.bench_label = tk.Label(compare_head, text=self._bench_status, bg=CARD, fg=MUTED,
                                    font=(self.family, 8))
        self.bench_label.pack(side="right", padx=(0, 2))

        table_wrap = tk.Frame(compare_card, bg=CARD)
        table_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        columns = ("algorithm", "km", "min", "transfers", "expanded", "elapsed", "check")
        self.compare_table = ttk.Treeview(
            table_wrap, columns=columns, show="headings", height=3, selectmode="browse"
        )
        headings = {
            "algorithm": ("算法", 78, "w"),
            "km": ("距离 km", 66, "e"),
            "min": ("时间 min", 66, "e"),
            "transfers": ("换乘", 44, "center"),
            "expanded": ("扩展", 52, "e"),
            "elapsed": ("耗时 ms", 66, "e"),
            "check": ("校验", 58, "center"),
        }
        for key, (text, width, anchor) in headings.items():
            self.compare_table.heading(key, text=text, anchor=anchor)
            self.compare_table.column(key, width=width, anchor=anchor, stretch=False)
        self.compare_table.tag_configure("best", background="#ecfdf5", foreground="#065f46")
        self.compare_table.tag_configure("mismatch", foreground="#b91c1c")
        self.compare_table.pack(side="left", fill="both", expand=True)
        table_scroll = ttk.Scrollbar(table_wrap, orient="vertical",
                                     command=self.compare_table.yview)
        self.compare_table.configure(yscrollcommand=table_scroll.set)
        table_scroll.pack(side="right", fill="y")
        self.compare_hint = tk.Label(
            table_wrap, text="点击「三算法对比」生成对比表", bg=CARD, fg="#9aa4b2",
            font=(self.family, 9),
        )
        self.compare_hint.place(relx=0.5, rely=0.58, anchor="center")

        self._set_buttons_busy(False)
        self._refresh_pick_buttons()

    # ------------------------------------------------------------------
    # 数据 / 选项
    # ------------------------------------------------------------------
    def _populate_graph_meta(self) -> None:
        station_count = len(self.graph.stations)
        edge_count = len(self.graph.iter_edges())
        self.graph_meta.configure(
            text=f"路网：{station_count} 个站点 · {edge_count} 条区间"
        )

    def _refresh_station_choices(self) -> None:
        choices = self.controller.station_choices()
        self._choice_labels = [label for _, label in choices]
        self._choices_by_sid = dict(choices)
        self._choices_by_label = {label: sid for sid, label in choices}
        self.start_combo.configure(values=self._choice_labels)
        self.end_combo.configure(values=self._choice_labels)
        self.start_combo.set(self.controller.label_for(self.controller.start_id))
        self.end_combo.set(self.controller.label_for(self.controller.end_id))

    # ------------------------------------------------------------------
    # 交互：起终点
    # ------------------------------------------------------------------
    def _on_start_combo(self, _event=None) -> None:
        sid = parse_station_label(self.start_var.get())
        self.controller.set_start(sid)
        self._after_selection_change()

    def _on_end_combo(self, _event=None) -> None:
        sid = parse_station_label(self.end_var.get())
        self.controller.set_end(sid)
        self._after_selection_change()

    def _current_algorithm_key(self) -> str:
        label = self.algorithm_var.get()
        for key, text in ALGORITHM_CHOICES:
            if text == label:
                return key
        return "dijkstra"

    def _on_algorithm_combo(self, _event=None) -> None:
        self.controller.algorithm = self._current_algorithm_key()
        self._after_selection_change(
            log=f"算法切换为 {ALGORITHM_SHORT[self.controller.algorithm]}"
        )

    def _on_weight_change(self) -> None:
        self.controller.weight = self.weight_var.get()
        self._after_selection_change(
            log=f"权重切换为 {dict(WEIGHT_CHOICES)[self.controller.weight]}"
        )

    def _on_swap(self) -> None:
        self.controller.swap()
        self.start_combo.set(self.controller.label_for(self.controller.start_id))
        self.end_combo.set(self.controller.label_for(self.controller.end_id))
        self._after_selection_change(log="已交换起点与终点")

    def _on_canvas_click(self, sid: str) -> None:
        if self.pick_mode == "start":
            self._apply_pick("start", sid)
            self._set_pick_mode(None)
        elif self.pick_mode == "end":
            self._apply_pick("end", sid)
            self._set_pick_mode(None)
        elif not self.controller.start_id:
            self._apply_pick("start", sid)
            self._set_pick_mode(None)
        elif not self.controller.end_id:
            self._apply_pick("end", sid)
            self._set_pick_mode(None)
        else:
            self.pick_hint.configure(
                text="起终点都已选好；要改选，先点「点选起点／点选终点」", fg="#b45309"
            )

    def _apply_pick(self, which: str, sid: str) -> None:
        name = self.controller.name_for(sid)
        if which == "start":
            self.controller.set_start(sid)
            self.start_combo.set(self.controller.label_for(sid))
            self._log(f"点选起点：{name}（{sid}）")
        else:
            self.controller.set_end(sid)
            self.end_combo.set(self.controller.label_for(sid))
            self._log(f"点选终点：{name}（{sid}）")
        self.pick_hint.configure(text="已选中，可继续点选或直接搜索", fg=MUTED)
        self._after_selection_change()

    def _toggle_pick(self, mode: str) -> None:
        self._set_pick_mode(None if self.pick_mode == mode else mode)

    def _set_pick_mode(self, mode: str | None) -> None:
        self.pick_mode = mode
        self._refresh_pick_buttons()
        if mode == "start":
            self.pick_hint.configure(text="点选模式：请在右侧画布点击起点站点", fg=ACCENT)
        elif mode == "end":
            self.pick_hint.configure(text="点选模式：请在右侧画布点击终点站点", fg=ACCENT)
        else:
            self.pick_hint.configure(text="在画布上点站点，也能直接选起终点", fg=MUTED)

    def _refresh_pick_buttons(self) -> None:
        mode = getattr(self, "pick_mode", None)
        for btn, key in ((self.pick_start_btn, "start"), (self.pick_end_btn, "end")):
            if mode == key:
                btn.configure(bg=ACCENT, fg="#ffffff", activebackground="#2563eb",
                              activeforeground="#ffffff")
            else:
                btn.configure(bg="#f1f5f9", fg=TEXT, activebackground="#e2e8f0",
                              activeforeground=TEXT)

    def _after_selection_change(self, log: str | None = None) -> None:
        self.controller.clear()
        self._last_result = None
        self.view.reset()
        self.view.show_selection(self.controller.start_id, self.controller.end_id)
        self._clear_result_panels()
        self._sync_state()
        if log:
            self._log(log)

    # ------------------------------------------------------------------
    # 交互：搜索
    # ------------------------------------------------------------------
    def _on_search(self) -> None:
        if self._animating or self.controller.state is SearchState.COMPUTING:
            return
        try:
            token = self.controller.begin_search()
        except UiError as err:
            self._fail_with(err)
            return
        self._sync_state()
        self._log(
            f"开始搜索：{self.controller.name_for(self.controller.start_id)} → "
            f"{self.controller.name_for(self.controller.end_id)}"
            f"（{ALGORITHM_SHORT[self.controller.algorithm]} · "
            f"{dict(WEIGHT_CHOICES)[self.controller.weight]}）"
        )
        self._run_async(
            lambda: self.controller.perform(token),
            on_ok=lambda result: self._on_search_done(token, result),
            on_err=lambda err: self._on_search_failed(token, err),
        )

    def _on_search_done(self, token: int, result) -> None:
        if not self.controller.is_current(token):
            return
        error = self.controller.apply(token, result)
        if error is not None:
            self._fail_with(error)
            return
        if result is None:
            return
        self._render_result(result)
        self._sync_state()
        self._log(
            f"完成：{len(result.path)} 站 · {result.distance_km:.1f} km · "
            f"{result.time_min:.0f} min · 换乘 {result.transfers} 次 · "
            f"扩展 {result.expanded} · {result.elapsed_ms:.1f} ms"
        )
        self._play_animation()

    def _on_search_failed(self, token: int, error: UiError) -> None:
        self.controller.fail(token, error)
        self._fail_with(error)

    def _on_cancel(self) -> None:
        if self.controller.state is not SearchState.COMPUTING:
            return
        self.controller.cancel()
        self._animating = False
        self._sync_state("已取消本次计算")
        self._log("已取消计算", "warn")

    def _on_clear(self) -> None:
        self._animating = False
        self.controller.clear()
        self._last_result = None
        self.view.reset()
        self.view.show_selection(self.controller.start_id, self.controller.end_id)
        self._clear_result_panels()
        self._sync_state("已清除结果，起终点保留")
        self._log("已清除结果")

    def _on_compare(self) -> None:
        if self._animating or self.controller.state is SearchState.COMPUTING:
            return
        try:
            rows = self.controller.compare()
        except UiError as err:
            self._fail_with(err)
            return
        self._render_compare(rows)
        self._sync_state()
        best_names = self._best_names(rows)
        summary = f"对比完成：{len(rows)} 种算法"
        if best_names:
            summary += f"，最优 {' / '.join(best_names)}"
        self._log(summary)
        self._log(
            "基准校验：" + "；".join(
                f"{ALGORITHM_SHORT[row.algorithm]}="
                + ("一致" if row.matched else "不一致" if row.matched is False else "无基准")
                for row in rows
            )
        )

    def _best_value(self, rows: list[CompareRow]) -> float | None:
        """当前权重下的最优值（时间或距离）；没有结果返回 None。"""
        values = [
            row.result.time_min if self.controller.weight == "time" else row.result.distance_km
            for row in rows
            if row.result is not None
        ]
        return min(values) if values else None

    def _best_names(self, rows: list[CompareRow]) -> list[str]:
        best = self._best_value(rows)
        if best is None:
            return []
        names = []
        for row in rows:
            if row.result is None:
                continue
            value = (
                row.result.time_min
                if self.controller.weight == "time"
                else row.result.distance_km
            )
            if abs(value - best) <= 1e-6:
                names.append(ALGORITHM_SHORT[row.algorithm])
        return names

    def _retry(self) -> None:
        if self.controller.state is SearchState.ERROR:
            self._on_search()

    # ------------------------------------------------------------------
    # 异步执行（solver 在线程里跑，结果回主线程）
    # ------------------------------------------------------------------
    def _run_async(self, work, on_ok, on_err) -> None:
        def runner() -> None:
            try:
                outcome = work()
            except UiError as err:
                self._events.put(lambda e=err: on_err(e))
            except Exception as exc:  # noqa: BLE001
                wrapped = DataError(f"界面线程捕获到异常：{exc}")
                self._events.put(lambda e=wrapped: on_err(e))
            else:
                self._events.put(lambda o=outcome: on_ok(o))

        threading.Thread(target=runner, daemon=True).start()

    def _drain_events(self) -> None:
        while True:
            try:
                callback = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as exc:  # noqa: BLE001
                self._log(f"回调异常：{exc}", "error")
        try:
            if self.root.winfo_exists():
                self._drain_after = self.root.after(40, self._drain_events)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # 渲染：状态 / 结果 / 对比
    # ------------------------------------------------------------------
    def _sync_state(self, message: str | None = None) -> None:
        state = self.controller.state
        label, fg, bg = STATE_STYLE[state.value]
        self.state_chip.configure(text=label, fg=fg, bg=bg)
        if message is None:
            message = self._default_message(state)
        self.state_text.configure(text=message)
        busy = state is SearchState.COMPUTING or self._animating
        self._set_buttons_busy(busy)
        self._refresh_pick_buttons()

    def _default_message(self, state: SearchState) -> str:
        if state is SearchState.IDLE:
            return "选择起终点，或点画布上的站点"
        if state is SearchState.READY:
            return "按 Enter 开始搜索"
        if state is SearchState.COMPUTING:
            return "正在计算最短路径…"
        if state is SearchState.RESULT:
            return "结果已显示；Esc 清除，Ctrl+C 复制"
        return "出错了，按 F5 或点「重试」"

    def _set_buttons_busy(self, busy: bool) -> None:
        normal = "disabled" if busy else "normal"
        self.search_btn.configure(state=normal)
        self.compare_btn.configure(state=normal)
        self.clear_btn.configure(state=normal)
        self.swap_btn.configure(state=normal)
        self.pick_start_btn.configure(state=normal)
        self.pick_end_btn.configure(state=normal)
        for combo in (self.start_combo, self.end_combo, self.algorithm_combo):
            combo.configure(state="disabled" if busy else "readonly")
        computing = self.controller.state is SearchState.COMPUTING
        self.cancel_btn.configure(state="normal" if computing else "disabled")
        has_result = self._last_result is not None
        self.replay_btn.configure(state="normal" if (has_result and not busy) else "disabled")

    def _render_result(self, result) -> None:
        self._last_result = result
        self.view.show_selection(self.controller.start_id, self.controller.end_id)
        self.view.show_path(result)
        self._render_metrics(result)
        self._render_path(result)

    def _render_metrics(self, result) -> None:
        values = {
            "km": f"{result.distance_km:.1f}",
            "min": f"{result.time_min:.0f}",
            "transfers": f"{result.transfers}",
            "expanded": f"{result.expanded}",
            "elapsed": f"{result.elapsed_ms:.1f}",
        }
        for key, text in values.items():
            self.metric_values[key].configure(text=text)

    def _render_path(self, result) -> None:
        text = self.path_text
        text.configure(state="normal")
        text.delete("1.0", "end")
        path = list(getattr(result, "path", []) or [])
        for index, sid in enumerate(path):
            station = self.graph.stations.get(sid)
            name = station.name if station else sid
            text.insert("end", f"{index + 1:>2}  ", "idx")
            text.insert("end", name, "name")
            if station is not None:
                text.insert("end", f"  {sid}", "meta")
            text.insert("end", "\n")
            if index + 1 < len(path):
                edge = self.graph.edge_between(sid, path[index + 1])
                # 真实 P1 返回“平行边列表”，这里取最快的一条做展示；兼容单条与列表
                if isinstance(edge, (list, tuple)):
                    edge = min(edge, key=lambda e: getattr(e, "time_min", getattr(e, "minutes", 0.0))) if edge else None
                if edge is not None:
                    line = getattr(edge, "line", "?")
                    km = getattr(edge, "km", getattr(edge, "distance_km", 0.0))
                    minutes = getattr(edge, "minutes", getattr(edge, "time_min", 0.0))
                    text.insert(
                        "end",
                        f"     {line} · {km:.1f} km · {minutes:.0f} min\n",
                        "meta",
                    )
        if getattr(result, "transfers", 0):
            text.insert("end", f"\n共换乘 {result.transfers} 次\n", "transfer")
        text.configure(state="disabled")
        self.path_meta.configure(
            text=f"共 {len(path)} 站 · {ALGORITHM_SHORT.get(self.controller.algorithm, '')}"
        )

    def _clear_result_panels(self) -> None:
        for label in self.metric_values.values():
            label.configure(text="—")
        self.path_text.configure(state="normal")
        self.path_text.delete("1.0", "end")
        self.path_text.insert(
            "end", "选好起点和终点后按 Enter，\n这里会列出途经站点与线路。", "hint"
        )
        self.path_text.configure(state="disabled")
        self.path_meta.configure(text="尚未搜索")
        for item in self.compare_table.get_children():
            self.compare_table.delete(item)
        self.bench_label.configure(text=self._bench_status)
        if hasattr(self, "compare_hint"):
            self.compare_hint.place(relx=0.5, rely=0.58, anchor="center")

    def _render_compare(self, rows: list[CompareRow]) -> None:
        if hasattr(self, "compare_hint"):
            self.compare_hint.place_forget()
        for item in self.compare_table.get_children():
            self.compare_table.delete(item)
        best_value = self._best_value(rows)
        for row in rows:
            result = row.result
            if result is None:
                values = (ALGORITHM_SHORT[row.algorithm], "—", "—", "—", "—", "—", "不连通")
                tags = ("mismatch",)
            else:
                check = "✓ 一致" if row.matched else "× 不一致" if row.matched is False else "— 无基准"
                values = (
                    ALGORITHM_SHORT[row.algorithm],
                    f"{result.distance_km:.1f}",
                    f"{result.time_min:.0f}",
                    f"{result.transfers}",
                    f"{result.expanded}",
                    f"{result.elapsed_ms:.1f}",
                    check,
                )
                value = (
                    result.time_min
                    if self.controller.weight == "time"
                    else result.distance_km
                )
                is_best = best_value is not None and abs(value - best_value) <= 1e-6
                tags = ("best",) if is_best else ()
                if row.matched is False:
                    tags = tags + ("mismatch",)
            self.compare_table.insert("", "end", iid=row.algorithm, values=values, tags=tags)
        self.compare_table.selection_remove(self.compare_table.selection())
        self.bench_label.configure(text=self._bench_status)

    # ------------------------------------------------------------------
    # 动画
    # ------------------------------------------------------------------
    def _play_animation(self) -> None:
        result = self._last_result
        if result is None:
            return
        self._animating = True
        self._sync_state("结果已生成，正在播放路径动画…")
        self._set_buttons_busy(True)
        speed = {"1×": 1, "2×": 2, "3×": 3}.get(self.speed_var.get(), 1)

        def done() -> None:
            self._animating = False
            self._sync_state()
            self._set_buttons_busy(False)

        try:
            self.view.animate(result, speed_level=speed, on_done=done)
        except TypeError:
            self.view.animate(result, speed_level=speed)
            duration = 1200
            if hasattr(self.view, "animation_duration_ms"):
                duration = self.view.animation_duration_ms(result, speed)
            self.root.after(duration, done)

    def _on_replay(self) -> None:
        if self._last_result is None or self._animating:
            return
        self._play_animation()

    # ------------------------------------------------------------------
    # 错误提示
    # ------------------------------------------------------------------
    def _fail_with(self, error: UiError) -> None:
        self.controller.last_error = error
        self._log(f"{error.title}：{error.message}", "error")
        self._sync_state(error.message)
        self._show_error(error)

    def _show_error(self, error: UiError, on_retry=None) -> NoticeDialog:
        parent = self.root
        retry = on_retry
        if error.retryable and retry is None:
            if isinstance(error, EmptyGraphError):
                retry = self._reload_data
            else:
                retry = self._retry
        dialog = NoticeDialog(parent, error, on_retry=retry, on_close=self._on_dialog_close)
        self._dialogs.append(dialog)
        dialog.bind("<Destroy>", lambda _e: self._forget_dialog(dialog), add="+")
        return dialog

    def _forget_dialog(self, dialog: NoticeDialog) -> None:
        if dialog in self._dialogs:
            self._dialogs.remove(dialog)

    def _on_dialog_close(self) -> None:
        if self.controller.state is SearchState.ERROR:
            self._sync_state()
        if not self.controller.start_id:
            try:
                self.start_combo.focus_set()
            except tk.TclError:
                pass

    def _reload_data(self) -> None:
        self._log("重新加载数据…")
        try:
            graph = load_graph(self.data_dir)
        except GraphDataError as exc:
            self._fail_with(DataError(f"重新加载失败：{exc}"))
            return
        self.graph = graph
        self.controller = Controller(graph, bench_rows=self.controller.bench_rows,
                                     bench_status=self.controller.bench_status)
        self.controller.weight = self.weight_var.get()
        self.controller.algorithm = self._current_algorithm_key()
        canvas_parent = self.view.master
        self.view.destroy()
        self.view = CanvasView(canvas_parent, graph)
        self.view.grid(row=0, column=0, sticky="nsew")
        self.view.bind_click(self._on_canvas_click)
        self._populate_graph_meta()
        self._refresh_station_choices()
        self._last_result = None
        self.pick_mode = None
        self._refresh_pick_buttons()
        self._clear_result_panels()
        self._sync_state("数据已重新加载")
        self._log(f"数据已加载：{len(graph.stations)} 站 / {len(graph.iter_edges())} 区间")

    def _on_reload_click(self) -> None:
        self._reload_data()

    def close(self) -> None:
        """关窗时停掉轮询定时器，避免 Tk 报 "invalid command name"。"""
        try:
            if self._drain_after is not None:
                self.root.after_cancel(self._drain_after)
                self._drain_after = None
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # 键盘 & 复制
    # ------------------------------------------------------------------
    def _on_enter(self, _event=None) -> str | None:
        focus = self.root.focus_get()
        # 文本框与表格里 Enter 有自己的语义，不拦截；其余位置 Enter 一律=搜索
        if isinstance(focus, (tk.Text, ttk.Treeview)):
            return None
        self._on_search()
        return "break"

    def _on_escape(self, _event=None) -> None:
        if self._dialogs:
            return
        self._on_clear()

    def _on_copy(self, _event=None):
        focus = self.root.focus_get()
        if isinstance(focus, tk.Text):
            return None
        return self._copy_result()

    def _on_copy_button(self) -> None:
        self._copy_result()

    def _copy_result(self) -> str | None:
        result = self._last_result
        if result is None:
            self._log("还没有可复制的结果", "warn")
            return "break"
        lines = [
            f"路径：{self.controller.name_for(self.controller.start_id)} → "
            f"{self.controller.name_for(self.controller.end_id)}",
            f"算法：{ALGORITHM_SHORT[self.controller.algorithm]} ｜ "
            f"权重：{dict(WEIGHT_CHOICES)[self.controller.weight]}",
        ]
        for index, sid in enumerate(result.path):
            lines.append(f"{index + 1}. {self.controller.name_for(sid)}（{sid}）")
        lines.append(
            f"里程 {result.distance_km:.1f} km ｜ 时间 {result.time_min:.0f} min ｜ "
            f"换乘 {result.transfers} 次 ｜ 扩展 {result.expanded} ｜ "
            f"耗时 {result.elapsed_ms:.1f} ms"
        )
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self._log("已复制路径到剪贴板")
        return "break"

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _log(self, message: str, level: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  ", "time")
        self.log_text.insert("end", message + "\n", level if level != "info" else ())
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 400:
            self.log_text.delete("1.0", "80.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------
def build_window(
    root: tk.Tk,
    data_dir: str | Path | None = None,
    force_empty: bool = False,
) -> MainWindow:
    """装配主窗口：加载数据 + bench，并把加载错误变成界面提示而不是崩溃。"""
    # 固定按 96 DPI 排版：界面尺寸按 1180×720 像素设计，避免 125%/150%
    # 显示缩放时控件互相挤压、被裁切（DoD：控件不重叠、不被裁切）。
    root.tk.call("tk", "scaling", 96 / 72)
    base = Path(data_dir) if data_dir else None
    startup_error: UiError | None = None
    graph = Graph()
    if force_empty:
        startup_error = EmptyGraphError("演示模式：当前路网被刻意清空，用来展示空图提示。")
    else:
        try:
            graph = load_graph(base)
        except GraphDataError as exc:
            startup_error = DataError(f"启动时读不到可用路网：{exc}")
    bench_path = (base / "bench.csv") if (base and base.is_dir()) else (
        Path(__file__).resolve().parent.parent / "tools" / "bench.csv"
    )
    bench_rows, bench_status = load_bench_rows(bench_path)
    window = MainWindow(
        root,
        graph,
        bench_rows=bench_rows,
        bench_status=bench_status,
        data_dir=base,
        startup_error=startup_error,
    )
    root.protocol("WM_DELETE_WINDOW", window.close)
    return window


# ---------------------------------------------------------------- 入口适配
def run_gui(graph=None) -> int:
    """main.py 的 GUI 入口（真实数据默认 data/demo_city.json）。"""
    root = tk.Tk()
    build_window(root, data_dir=None)
    root.mainloop()
    return 0


def smoke(graph, out_ps: str) -> int:
    """无人工干预冒烟：建窗 → 自动选连通起终点 → 搜索 → 导出画布。"""
    from core import solver
    from ui.p3_compat import _as_compat

    graph = _as_compat(graph)          # MainWindow 需要 P3 的 is_empty/iter_edges
    root = tk.Tk()
    window = MainWindow(root, graph, bench_rows={}, bench_status="冒烟")
    root.update()
    pair = solver.pick_connected_pair(graph)
    if pair:
        window.controller.set_start(pair[0])
        window.controller.set_end(pair[1])
        window._on_search()
        deadline = time.time() + 10.0
        while time.time() < deadline:
            root.update()
            time.sleep(0.02)
            if window._last_result is not None:
                break
    root.update()
    window.view.postscript(file=out_ps, colormode="color")
    try:
        window.close()          # 取消动画/队列 after 任务（可能连 root 一起销毁）
    except Exception:
        pass
    try:
        root.update_idletasks()
        root.destroy()
    except tk.TclError:
        pass                    # close() 已经销毁过 root
    return 0
