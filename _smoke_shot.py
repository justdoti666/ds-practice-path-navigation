#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""开发辅助：把界面真实截图存成 PNG（用于"画面能不能看"的验收，不参与交付逻辑）。

用法：python _smoke_shot.py out.png
依赖：Pillow（运行环境已带）；失败时会打印原因并返回非 0。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.graph import load_graph  # noqa: E402
from ui.window import App  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "_smoke/ui.png")
DATA = Path(__file__).resolve().parent / "data" / "demo_city.json"


def main() -> int:
    try:
        from PIL import ImageGrab
    except Exception as exc:
        print(f"[skip] Pillow 不可用: {exc}")
        return 2
    g = load_graph(DATA)
    app = App(g)
    app.attributes("-topmost", True)
    app.geometry("1180x720+80+60")
    app.start_box.set("大学城（S4）")
    app.end_box.set("机场（S11）")
    app.search(animate=False)
    app.update()
    time.sleep(0.6)
    app.update()
    x, y = app.winfo_rootx(), app.winfo_rooty()
    w, h = app.winfo_width(), app.winfo_height()
    try:
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    except Exception as exc:
        print(f"[fail] 截屏失败: {exc}")
        app.destroy()
        return 3
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT)
    app.destroy()
    print(f"saved: {OUT} ({img.width}x{img.height})")
    return 0


if __name__ == "__main__":
    sys.exit(main())