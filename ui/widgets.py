"""自绘小控件（P3 界面层内部拆分）。

主窗 ``ui/window.py`` 只保留布局与交互逻辑；按钮与弹窗需要少量 Canvas 绘制
（圆形图标），集中放在这里，保证 ``ui/window.py`` 里 0 行绘图调用（P3-06）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ui.canvas import pick_ui_font

__all__ = ["flat_button", "StationSuggestPopup", "NoticeDialog", "CARD", "TEXT", "MUTED", "ACCENT"]

CARD = "#ffffff"
TEXT = "#0f172a"
MUTED = "#64748b"
ACCENT = "#1d4ed8"


def flat_button(parent, text, command, kind="ghost", width=None) -> tk.Button:
    """扁平风格按钮：primary / soft / ghost 三种。"""
    palette = {
        "primary": (ACCENT, "#ffffff", "#2563eb"),
        "ghost": ("#f1f5f9", TEXT, "#e2e8f0"),
        "soft": ("#e8efff", ACCENT, "#dbe6ff"),
    }
    bg, fg, active = palette.get(kind, palette["ghost"])
    family = pick_ui_font(parent)
    return tk.Button(
        parent, text=text, command=command, bg=bg, fg=fg,
        activebackground=active, activeforeground=fg,
        relief="flat", bd=0, cursor="hand2",
        font=(family, 10), width=width or 0,
        padx=12, pady=7, disabledforeground="#9aa4b2",
    )


class StationSuggestPopup(tk.Toplevel):
    """起终点输入框的候选弹层（不抢焦点，输入时可继续打字）。

    Tk 自带 ``ttk::combobox::Post`` 会把焦点移到 popdown，导致用户接着输入
    时字符进不了输入框；这里用 overrideredirect 的 Listbox，点击才回调。
    """

    ROW_HEIGHT = 22
    MAX_ROWS = 8

    def __init__(self, parent: tk.Misc, on_pick) -> None:
        super().__init__(parent)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self._on_pick = on_pick
        self._labels: list[str] = []
        self.family = pick_ui_font(self)

        outer = tk.Frame(self, bg="#cfd9e6", padx=1, pady=1)
        outer.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(
            outer, bd=0, highlightthickness=0, activestyle="none",
            bg=CARD, fg=TEXT, selectbackground=ACCENT, selectforeground="#ffffff",
            font=(self.family, 10), height=1, takefocus=0, exportselection=False,
        )
        self.listbox.pack(side="left", fill="both", expand=True)
        self.scroll = ttk.Scrollbar(outer, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="right", fill="y")
        self.listbox.bind("<ButtonRelease-1>", self._pick)
        self.listbox.bind("<MouseWheel>", self._wheel)
        self.bind("<Escape>", lambda _e: self.hide())

    def show(self, anchor: tk.Misc, labels: list[str]) -> None:
        if not labels:
            self.hide()
            return
        self._labels = list(labels)
        self.listbox.delete(0, "end")
        for label in self._labels:
            self.listbox.insert("end", label)
        rows = min(self.MAX_ROWS, len(self._labels))
        self.listbox.configure(height=rows)
        if len(self._labels) > self.MAX_ROWS:
            self.scroll.pack(side="right", fill="y")
        else:
            self.scroll.pack_forget()
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(0)
        self.listbox.activate(0)

        width = max(anchor.winfo_width(), 260)
        height = rows * self.ROW_HEIGHT + 6
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height() + 2
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        if x + width > screen_w:
            x = max(0, screen_w - width)
        if y + height > screen_h:
            y = max(0, anchor.winfo_rooty() - height - 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.deiconify()
        self.lift()

    def hide(self) -> None:
        if self.winfo_exists():
            self.withdraw()

    @property
    def visible(self) -> bool:
        return bool(self.winfo_exists() and self.winfo_ismapped())

    def selected_label(self) -> str | None:
        sel = self.listbox.curselection()
        if not sel:
            return None
        idx = int(sel[0])
        return self._labels[idx] if 0 <= idx < len(self._labels) else None

    def move(self, delta: int) -> None:
        if not self._labels:
            return
        sel = self.listbox.curselection()
        idx = int(sel[0]) if sel else 0
        idx = max(0, min(len(self._labels) - 1, idx + delta))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(idx)
        self.listbox.activate(idx)
        self.listbox.see(idx)

    def _pick(self, _event=None) -> None:
        label = self.selected_label()
        if label:
            self._on_pick(label)

    def _wheel(self, event) -> str:
        self.listbox.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"


class NoticeDialog(tk.Toplevel):
    """统一的模态提示框：图标 + 标题 + 说明 + 建议 + 按钮。

    四类异常（不连通 / 未选点 / 数据错误 / 空图）共用一套样式，
    文案由 :class:`ui.window.UiError` 子类提供。
    """

    ICONS = {
        "warning": "#f59e0b",
        "error": "#dc2626",
        "info": "#2563eb",
    }

    def __init__(self, parent: tk.Misc, error, on_retry=None, on_close=None) -> None:
        super().__init__(parent)
        self.withdraw()
        self.error = error
        self._on_retry = on_retry
        self._on_close = on_close
        self.family = pick_ui_font(self)
        self.title(error.title)
        self.configure(bg="#cfd9e6")
        self.resizable(False, False)
        # 去掉系统标题栏，弹窗样式与主窗保持一致（截图也更干净）
        self.overrideredirect(True)

        outer = tk.Frame(self, bg=CARD, padx=22, pady=18)
        outer.pack(fill="both", expand=True, padx=2, pady=2)

        close_x = tk.Label(
            outer, text="×", bg=CARD, fg="#94a3b8", font=(self.family, 15),
            cursor="hand2",
        )
        close_x.place(relx=1.0, y=0, x=-4, anchor="ne")
        close_x.bind("<Button-1>", lambda _e: self._close())

        head = tk.Frame(outer, bg=CARD)
        head.pack(fill="x", pady=(0, 0))
        color = self.ICONS.get(getattr(error, "severity", "warning"), self.ICONS["warning"])
        badge = tk.Canvas(head, width=42, height=42, bg=CARD, highlightthickness=0)
        badge.pack(side="left")
        badge.create_oval(2, 2, 40, 40, fill=color, outline="")
        badge.create_text(21, 22, text="!", fill="#ffffff", font=(self.family, 16, "bold"))

        title_box = tk.Frame(head, bg=CARD)
        title_box.pack(side="left", fill="x", expand=True, padx=(12, 0))
        tk.Label(
            title_box, text=error.title, bg=CARD, fg=TEXT,
            font=(self.family, 12, "bold"), anchor="w",
        ).pack(fill="x")
        tk.Label(
            title_box,
            text="需要你检查一下" if getattr(error, "severity", "") == "error"
            else "按提示处理一下就能继续",
            bg=CARD, fg=MUTED,
            font=(self.family, 9), anchor="w",
        ).pack(fill="x", pady=(2, 0))

        tk.Label(
            outer, text=error.message, bg=CARD, fg="#334155",
            font=(self.family, 10), wraplength=392, justify="left", anchor="w",
        ).pack(fill="x", pady=(14, 0))

        if getattr(error, "hint", ""):
            hint_box = tk.Frame(outer, bg="#f8fafc", padx=12, pady=10,
                                highlightbackground="#e6ebf3", highlightthickness=1)
            hint_box.pack(fill="x", pady=(12, 0))
            tk.Label(
                hint_box, text="建议", bg="#f8fafc", fg=ACCENT,
                font=(self.family, 9, "bold"), anchor="w",
            ).pack(fill="x")
            tk.Label(
                hint_box, text=error.hint, bg="#f8fafc", fg="#475569",
                font=(self.family, 9), wraplength=368, justify="left", anchor="w",
            ).pack(fill="x", pady=(4, 0))

        buttons = tk.Frame(outer, bg=CARD)
        buttons.pack(fill="x", pady=(16, 0))

        close_btn = flat_button(buttons, "知道了  Enter", self._close, kind="primary", width=12)
        close_btn.pack(side="right")

        if on_retry is not None and getattr(error, "retryable", False):
            retry_btn = flat_button(buttons, "重试  F5", self._retry, kind="ghost", width=9)
            retry_btn.pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda _e: self._close())
        self.bind("<Return>", lambda _e: self._close())

        self.update_idletasks()
        close_x.lift()
        width = 460
        height = self.winfo_reqheight()
        parent_w = max(parent.winfo_width(), 200)
        parent_h = max(parent.winfo_height(), 200)
        x = parent.winfo_rootx() + (parent_w - width) // 2
        y = parent.winfo_rooty() + int((parent_h - height) * 0.55)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.transient(parent)
        self.deiconify()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.lift()
        try:
            self.focus_force()
        except tk.TclError:
            pass
        close_btn.focus_set()

    def _close(self) -> None:
        if self._on_close is not None:
            self._on_close()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def _retry(self) -> None:
        callback = self._on_retry
        self._close()
        if callback is not None:
            callback()
