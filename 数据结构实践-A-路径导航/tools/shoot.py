"""生成 docs/截图/ 下的界面截图（P3-08）。

脚本会真的打开窗口、模拟点击与键盘操作、再截屏，保证图里的界面是"跑出来的"
而不是拼的。用法::

    python tools/shoot.py            # 生成全部 9 张
    python tools/shoot.py --only 03  # 只重拍某一张

截图清单：01 主界面 / 02 点选交互 / 03 搜索结果 / 04 三算法对比 /
05 动画帧 / 06 不连通 / 07 未选点 / 08 数据错误 / 09 空图
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import ImageGrab  # noqa: E402

from core.graph import Graph  # noqa: E402
from ui.window import MainWindow, SearchState, build_window  # noqa: E402

SHOT_DIR = ROOT / "docs" / "截图"
WINDOW_POS = (40, 40)


def pump(root: tk.Tk, ms: int) -> None:
    """跑事件循环 ms 毫秒（不阻塞 Tk 的 after 回调）。"""
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        root.update()
        time.sleep(0.01)


def wait_until(win: MainWindow, predicate, timeout_ms: int = 6000) -> bool:
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        win.root.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def grab(root: tk.Tk, name: str) -> Path:
    root.update_idletasks()
    root.update()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / name
    ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
    print(f"  saved {path.name}  ({w}×{h})")
    return path


def new_window(data_dir: Path | None = None, empty: bool = False):
    root = tk.Tk()
    root.geometry(f"1180x720+{WINDOW_POS[0]}+{WINDOW_POS[1]}")
    win = build_window(root, data_dir=data_dir, force_empty=empty)
    root.update_idletasks()
    root.geometry(f"1180x720+{WINDOW_POS[0]}+{WINDOW_POS[1]}")
    root.lift()
    root.update()
    return root, win


def pick_start(win: MainWindow, sid: str) -> None:
    win._set_pick_mode("start")
    x, y = win.view.station_screen_pos(sid)
    win.view.canvas.event_generate("<Button-1>", x=int(x), y=int(y))


def pick_end(win: MainWindow, sid: str) -> None:
    win._set_pick_mode("end")
    x, y = win.view.station_screen_pos(sid)
    win.view.canvas.event_generate("<Button-1>", x=int(x), y=int(y))


def wait_animation(win: MainWindow, timeout_ms: int = 6000) -> None:
    """等异步求解落地，再等动画播完。"""
    wait_until(
        win,
        lambda: win.controller.state is not SearchState.COMPUTING,
        timeout_ms,
    )
    wait_until(win, lambda: not win._animating, timeout_ms)
    win.root.update()


def close_dialog(win: MainWindow) -> None:
    if win._dialogs:
        win._dialogs[-1]._close()
        pump(win.root, 120)


# ---------------------------------------------------------------------------
# 单张截图
# ---------------------------------------------------------------------------
def shot_01_main() -> None:
    root, win = new_window()
    pick_start(win, "S01")
    win.start_combo.set(win.controller.label_for("S01"))
    pick_end(win, "S07")
    win.end_combo.set(win.controller.label_for("S07"))
    pump(root, 400)
    grab(root, "01-主界面.png")
    win.close()


def shot_02_pick() -> None:
    root, win = new_window()
    pick_start(win, "S01")
    win._set_pick_mode("end")
    pump(root, 350)
    grab(root, "02-点选交互.png")
    win.close()


def shot_03_result() -> None:
    root, win = new_window()
    start_search(win, "S01", "S07", "dijkstra", "time")
    pump(root, 300)
    grab(root, "03-搜索结果.png")
    win.close()


def shot_04_compare() -> None:
    root, win = new_window()
    # 用"西山→南站"这组 OD：Dijkstra/A* 走 4.8 km、11 min，BFS 走 5.2 km、14 min，
    # 三行数值有差异，能同时说明"同一份 bench.csv"和"BFS 只保证区间数最少"。
    start_search(win, "S13", "S12", "dijkstra", "time")
    win._on_compare()
    pump(root, 400)
    grab(root, "04-三算法对比.png")
    win.close()


def shot_05_animation() -> None:
    root, win = new_window()
    start_search(win, "S08", "S17", "dijkstra", "time")
    pump(root, 350)
    win.speed_var.set("1×")
    win._on_replay()
    pump(root, 550)
    grab(root, "05-动画帧.png")
    win.close()


def shot_06_disconnected() -> None:
    root, win = new_window()
    start_search(win, "S01", "S18", "dijkstra", "time", expect_dialog=True)
    pump(root, 400)
    grab(root, "06-不连通.png")
    close_dialog(win)
    win.close()


def shot_07_noselection() -> None:
    root, win = new_window()
    win._on_search()
    pump(root, 400)
    grab(root, "07-未选点.png")
    close_dialog(win)
    win.close()


def shot_08_dataerror() -> None:
    root, win = new_window()

    def boom(*_args, **_kwargs):
        raise RuntimeError("edges.csv 第 12 行：站点 S99 不存在")

    win.controller._solve = boom
    start_search(win, "S01", "S07", "dijkstra", "time", expect_dialog=True)
    pump(root, 400)
    grab(root, "08-数据错误.png")
    close_dialog(win)
    win.close()


def shot_09_empty() -> None:
    root, win = new_window(empty=True)
    pump(root, 900)
    grab(root, "09-空图.png")
    close_dialog(win)
    win.close()


def start_search(win: MainWindow, start: str, end: str, algorithm: str, weight: str,
                 expect_dialog: bool = False) -> None:
    win.controller.set_start(start)
    win.controller.set_end(end)
    win.controller.algorithm = algorithm
    win.controller.weight = weight
    win.start_combo.set(win.controller.label_for(start))
    win.end_combo.set(win.controller.label_for(end))
    win.weight_var.set(weight)
    win._after_selection_change()
    win._on_search()
    if not expect_dialog:
        wait_animation(win)


SHOTS = {
    "01": shot_01_main,
    "02": shot_02_pick,
    "03": shot_03_result,
    "04": shot_04_compare,
    "05": shot_05_animation,
    "06": shot_06_disconnected,
    "07": shot_07_noselection,
    "08": shot_08_dataerror,
    "09": shot_09_empty,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None, help="只重拍某一张，如 03")
    args = parser.parse_args()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    targets = [args.only] if args.only else list(SHOTS)
    print(f"输出目录：{SHOT_DIR}")
    for key in targets:
        if key not in SHOTS:
            print(f"未知截图编号：{key}")
            return 2
        print(f"[{key}]")
        SHOTS[key]()
    print("全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
