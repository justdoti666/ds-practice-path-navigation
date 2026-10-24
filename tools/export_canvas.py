#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P4-06 缺失的画布导出脚本：无头建窗 → 选起终点 → 搜索 → 导出画布。

交付里的 ``tools/export_canvas.py`` 只存在于任务书，没有实现；本脚本补上：

* PostScript：直接调用 ``CanvasView.postscript``（纯标准库 + Tkinter，运行期零依赖）；
* PNG：开发期用 Pillow 的 ``ImageGrab`` 截真实窗口（可作为报告插图/验收证据；
  运行期代码不 import Pillow，仅本脚本在 ``--png`` 时才尝试）；
* JSON：把界面上的数值（里程/时间/换乘/扩展/耗时）、画布 item 统计一起落盘，
  供修复前后自动对拍用。

用法（在项目根目录执行；默认导出当前工程）：

    python tools/export_canvas.py --start S4 --end S11 \
        --ps _qa/out/demo.ps --png _qa/out/demo.png --no-animation

    python tools/export_canvas.py --root ..\\_orig\\path-nav \
        --data data/real_metro.json --start S001 --end S169 \
        --png _qa/baseline/169.png --no-animation

    python tools/export_canvas.py _smoke/ui.png      # 兼容 P4 任务书里的旧写法

    # 并行走廊细节证据：3× 缩放到某站附近（如 169 站数据集的广州南站 S017）
    python tools/export_canvas.py --data data/real_metro.json --start S001 --end S169 \
        --zoom 3 --focus S017 --png _qa/corridor.png --no-animation

参数：
    --root        工程根目录（默认按本脚本位置推断为所在工程的根）
    --data        数据文件，相对 root 解析；默认 data/demo_city.json
    --start/--end 起终点（站点 id 或站名）；不传则按 smoke() 的方式自动挑一对连通点
    --algo        dijkstra / astar / bfs（默认 dijkstra）
    --weight      time / distance（默认 time）
    --ps          导出画布 PostScript 的路径
    --png         截整窗 PNG 的路径（需要 Pillow）
    --png-canvas  只截画布区域的 PNG 路径（需要 Pillow）
    --json        导出观测数值 JSON 的路径
    --no-animation  跳过路径动画，直接截到完整静态路径（对比截图建议打开）
    --zoom        截图前缩放到指定倍率（如 3）
    --focus       截图前把某站平移到画布中心（站点 id 或站名，配合 --zoom）
    --timeout     等待搜索结果的秒数（默认 30）
    --settle-ms   结果出来后等界面重绘稳定的毫秒数（默认 800）
    --size/--pos  窗口尺寸/位置，默认 1180x720 / +80+60（与设计尺寸一致）

退出码：0 成功；2 参数/数据/建窗失败；3 搜索失败或截图失败。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

DEF_SIZE = "1180x720"
DEF_POS = "+80+60"


# ---------------------------------------------------------------- 工具
def _parse_size(text: str) -> tuple[int, int]:
    try:
        w, h = text.lower().split("x", 1)
        return max(480, int(w)), max(360, int(h))
    except (ValueError, AttributeError):
        return 1180, 720


def _parse_pos(text: str) -> tuple[int, int]:
    try:
        x, y = text.replace(" ", "").split("+", 1)
        return int(x), int(y)
    except (ValueError, AttributeError):
        return 80, 60


def _as_path(value: str | None, cwd: Path) -> Path | None:
    if not value:
        return None
    p = Path(value)
    return p if p.is_absolute() else (cwd / p)


def _try_dpi_aware() -> None:
    """Windows 高 DPI 下让 Tk 与 ImageGrab 用同一套物理像素坐标。"""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _canvas_metrics(view) -> dict:
    """画布 item 统计 + 布局坐标观测（纯只读，用 Tk 的 tags 分类）。"""
    try:
        items = list(view.find_all())
    except Exception:
        return {"items_total": 0, "by_tag": {}}
    by_tag: dict[str, int] = {}
    for item in items:
        try:
            for tag in view.gettags(item):
                by_tag[tag] = by_tag.get(tag, 0) + 1
        except Exception:
            continue
    counts = {
        "#all": len(items),
        "background": by_tag.get("background", 0),
        "network": by_tag.get("network", 0),
        "station": by_tag.get("station", 0),
        "label": by_tag.get("label", 0),
        "path": by_tag.get("path", 0),
        "overlay": by_tag.get("overlay", 0),
        "p3sel": by_tag.get("p3sel", 0),
        "anim": by_tag.get("anim", 0),
    }
    # 每条线 1 根描边 + 1 根彩线（接驳线也是），按 tag line:<id> 统计线路数
    line_ids = sorted(t[5:] for t in by_tag if t.startswith("line:"))
    line_item_counts: dict[str, int] = {}
    line_bboxes: dict[str, list[list[float]]] = {}
    try:
        for lid in line_ids:
            line_items = list(view.find_withtag(f"line:{lid}"))
            line_item_counts[lid] = len(line_items)
            boxes: list[list[float]] = []
            seen = set()
            for item in line_items:
                bb = view.bbox(item)
                if not bb:
                    continue
                key = tuple(round(float(v), 2) for v in bb)
                if key in seen:
                    continue
                seen.add(key)
                boxes.append(list(key))
            line_bboxes[lid] = boxes
    except Exception:  # noqa: BLE001
        pass
    # 并行走廊核对用：只导出“被多条线路共用相邻站对”的那几条线的分段折线坐标
    corridor_line_coords: dict[str, list[list[list[float]]]] = {}
    try:
        owners: dict[tuple[str, str], set[str]] = {}
        for line in getattr(view.draw_graph, "lines", []):
            seq = list(getattr(line, "station_ids", []) or [])
            if getattr(line, "loop", False) and len(seq) > 2:
                seq = seq + [seq[0]]
            for a, b in zip(seq, seq[1:]):
                key = (a, b) if a <= b else (b, a)
                owners.setdefault(key, set()).add(line.id)
        shared_ids = sorted({lid for ids in owners.values() if len(ids) > 1 for lid in ids})
        for lid in shared_ids:
            parts: list[list[list[float]]] = []
            for item in view.find_withtag(f"line:{lid}"):
                coords = list(view.coords(item))
                pts = [[round(float(coords[i]), 1), round(float(coords[i + 1]), 1)]
                       for i in range(0, len(coords) - 1, 2)]
                if pts not in parts:   # 白色描边与彩线坐标相同，去重
                    parts.append(pts)
            corridor_line_coords[lid] = parts
    except Exception:  # noqa: BLE001
        pass
    screen = getattr(view, "_screen", None) or {}
    xs = [pt[0] for pt in screen.values()]
    ys = [pt[1] for pt in screen.values()]
    pts = [(round(x, 1), round(y, 1)) for x, y in screen.values()]
    station_screen = {}
    for sid in screen:
        try:
            pos = view.station_screen(sid)
        except Exception:  # noqa: BLE001
            pos = screen.get(sid)
        if pos:
            station_screen[sid] = [round(float(pos[0]), 2), round(float(pos[1]), 2)]
    draw_lines = []
    try:
        for line in getattr(view.draw_graph, "lines", []):
            draw_lines.append({
                "id": line.id,
                "name": getattr(line, "name", line.id),
                "stations": list(getattr(line, "station_ids", []) or []),
                "loop": bool(getattr(line, "loop", False)),
            })
    except Exception:  # noqa: BLE001
        pass
    station_label_rects = {}
    try:
        for sid, box in (getattr(view, "station_label_boxes", {}) or {}).items():
            station_label_rects[sid] = [round(float(v), 2) for v in box.rect]
    except Exception:  # noqa: BLE001
        pass
    segment_label_rects = []
    try:
        for box in (getattr(view, "segment_label_boxes", []) or []):
            segment_label_rects.append([round(float(v), 2) for v in box.rect])
    except Exception:  # noqa: BLE001
        pass
    reserved_rects = []
    try:
        for rect in (getattr(view, "_reserved", []) or []):
            if rect:
                reserved_rects.append([round(float(v), 2) for v in rect])
    except Exception:  # noqa: BLE001
        pass
    return {
        "items_total": len(items),
        "by_tag": counts,
        "line_items": sum(by_tag.get(f"line:{lid}", 0) for lid in line_ids),
        "lines_drawn": len(line_ids),
        "line_item_counts": {lid: line_item_counts.get(lid, 0) for lid in line_ids},
        "line_bboxes": line_bboxes,
        "corridor_line_coords": corridor_line_coords,
        "stations_drawn": len(screen),
        "screen_bbox": [min(xs), min(ys), max(xs), max(ys)] if xs else None,
        "station_screen_unique_points": len(set(pts)),
        "station_labels_shown": len(getattr(view, "station_label_boxes", {}) or {}),
        "segment_labels_shown": len(getattr(view, "segment_label_boxes", []) or []),
        "hidden_station_labels": len(getattr(view, "hidden_station_labels", []) or []),
        "hidden_segment_labels": int(getattr(view, "hidden_segment_labels", 0) or 0),
        "zoom": round(float(getattr(getattr(view, "_transform", None), "zoom", 0.0)), 4),
        # 供 _qa/canvas_geometry.py 做「显示坐标下的交叉/重叠/站名避让」独立复核
        "station_screen": station_screen,
        "draw_lines": draw_lines,
        "station_label_rects": station_label_rects,
        "segment_label_rects": segment_label_rects,
        "reserved_rects": reserved_rects,
        "hidden_station_label_ids": list(getattr(view, "hidden_station_labels", []) or []),
    }


def _resolve_station(graph, token: str) -> str | None:
    """把站点 id 或站名解析成 id（同名多义时明确报错，与 main.py 口径一致）。"""
    if token in graph.stations:
        return token
    hits = [sid for sid, st in graph.stations.items() if st.name == token]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        print(f"[export] ambiguous station name {token!r}: {', '.join(sorted(hits))}")
    return None


def _pump(root, seconds: float) -> None:
    """泵事件循环指定时长，等画布重绘/布局稳定。"""
    end = time.time() + max(0.0, seconds)
    while True:
        try:
            root.update()
        except Exception:  # noqa: BLE001
            return
        if time.time() >= end:
            break
        time.sleep(0.01)


def _grab(root, box: tuple[int, int, int, int], out_png: Path) -> tuple[int, int]:
    from PIL import ImageGrab  # 仅开发期截图用；项目运行期不依赖

    img = ImageGrab.grab(bbox=box)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)
    return img.width, img.height


# ---------------------------------------------------------------- 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="无头建窗 → 搜索 → 导出画布 PostScript / PNG / 观测 JSON"
    )
    ap.add_argument("out", nargs="?", default=None,
                    help="兼容旧写法：等价于 --png（.ps 后缀时等价于 --ps）")
    ap.add_argument("--root", default=None, help="工程根目录（含 core/、ui/）")
    ap.add_argument("--data", default="data/demo_city.json",
                    help="数据文件（相对 --root；默认 data/demo_city.json）")
    ap.add_argument("--start", default=None, help="起点站点 id 或站名")
    ap.add_argument("--end", default=None, help="终点站点 id 或站名")
    ap.add_argument("--algo", default="dijkstra",
                    choices=["dijkstra", "astar", "bfs"])
    ap.add_argument("--weight", default="time", choices=["time", "distance"])
    ap.add_argument("--ps", default=None, help="画布 PostScript 输出路径")
    ap.add_argument("--png", default=None, help="整窗 PNG 输出路径（需要 Pillow）")
    ap.add_argument("--png-canvas", default=None, help="只截画布区域的 PNG 路径")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="观测数值 JSON 输出路径")
    ap.add_argument("--no-animation", action="store_true",
                    help="不播放路径动画，直接截完整静态结果")
    ap.add_argument("--zoom", type=float, default=None,
                    help="截图前把画布缩放到指定倍率（如 3；演示并行走廊细节用）")
    ap.add_argument("--focus", default=None,
                    help="截图前把该站点（id 或站名）平移到画布中心（配合 --zoom 用）")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="等待搜索结果的秒数（默认 30）")
    ap.add_argument("--settle-ms", type=int, default=800,
                    help="结果出来后等界面重绘稳定的毫秒数（默认 800）")
    ap.add_argument("--size", default=DEF_SIZE, help=f"窗口尺寸（默认 {DEF_SIZE}）")
    ap.add_argument("--pos", default=DEF_POS, help=f"窗口位置（默认 {DEF_POS}）")
    args = ap.parse_args(argv)

    cwd = Path.cwd()
    root_dir = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    if not (root_dir / "core").is_dir() or not (root_dir / "ui").is_dir():
        print(f"[export] not a project root: {root_dir} (need core/ and ui/)")
        return 2

    out_ps = _as_path(args.ps, cwd)
    out_png = _as_path(args.png, cwd)
    out_png_canvas = _as_path(args.png_canvas, cwd)
    out_json = _as_path(args.json_out, cwd)
    if args.out:
        legacy = _as_path(args.out, cwd)
        if legacy.suffix.lower() == ".ps" and out_ps is None:
            out_ps = legacy
        elif out_png is None:
            out_png = legacy
    if not any((out_ps, out_png, out_png_canvas, out_json)):
        print("[export] nothing to export: give --ps/--png/--json (or a positional out.png)")
        return 2

    data_path = Path(args.data)
    if not data_path.is_absolute():
        data_path = root_dir / data_path
    if not data_path.exists():
        print(f"[export] data not found: {data_path}")
        return 2

    _try_dpi_aware()

    if str(root_dir) not in sys.path:
        sys.path.insert(0, str(root_dir))
    else:
        sys.path.remove(str(root_dir))
        sys.path.insert(0, str(root_dir))

    try:
        from core.graph import load_graph
    except Exception as exc:  # noqa: BLE001
        print(f"[export] import core.graph failed (root={root_dir}): {exc}")
        return 2

    try:
        graph = load_graph(str(data_path))
    except Exception as exc:  # noqa: BLE001
        print(f"[export] load_graph failed: {exc}")
        return 2

    print(f"[export] root   : {root_dir}")
    print(f"[export] data   : {data_path}")
    print(f"[export] graph  : {len(graph.stations)} stations / "
          f"{len(graph.edges)} edges / {len(graph.lines)} lines")

    import tkinter as tk

    try:
        from ui.window import create_window
    except Exception as exc:  # noqa: BLE001
        print(f"[export] import ui.window failed: {exc}")
        return 2

    try:
        win_w, win_h = _parse_size(args.size)
        win_x, win_y = _parse_pos(args.pos)
        root = tk.Tk()
        window = create_window(root, graph)
        root.attributes("-topmost", True)
        root.geometry(f"{win_w}x{win_h}+{win_x}+{win_y}")
        root.update()
    except Exception as exc:  # noqa: BLE001
        print(f"[export] build window failed (Tk display unavailable?): {exc}")
        return 2

    # ---- 选起终点（真实接口：window.controller.set_start/set_end）----
    start = args.start
    end = args.end
    if not start or not end:
        try:
            from core import solver

            pair = solver.pick_connected_pair(graph)
        except Exception:  # noqa: BLE001
            pair = None
        if pair:
            start = start or pair[0]
            end = end or pair[1]
    if not start or not end:
        print("[export] no --start/--end and no connected pair auto-pick")
        _teardown(window, root)
        return 3

    try:
        start = _resolve_station(window.graph, start)
        end = _resolve_station(window.graph, end)
        if start is None or end is None:
            print(f"[export] station not found: start={args.start!r} end={args.end!r}")
            _teardown(window, root)
            return 3
        window.controller.set_start(start)
        window.controller.set_end(end)
        # 让左侧下拉框显示选中项（截图证据需要看到实际选择的站点）
        if hasattr(window, "start_combo"):
            window.start_combo.set(window.controller.label_for(window.controller.start_id))
        if hasattr(window, "end_combo"):
            window.end_combo.set(window.controller.label_for(window.controller.end_id))
        if hasattr(window.view, "show_selection"):
            window.view.show_selection(window.controller.start_id, window.controller.end_id)
        # 算法/权重：直接同步到控制器与左侧控件（不触发会清空选择的回调）
        if args.algo != window.controller.algorithm:
            window.controller.algorithm = args.algo
            try:
                from ui.window import ALGORITHM_CHOICES

                for key, label in ALGORITHM_CHOICES:
                    if key == args.algo:
                        if hasattr(window, "algorithm_var"):
                            window.algorithm_var.set(label)
                        break
            except Exception:  # noqa: BLE001
                pass
        if args.weight != window.controller.weight:
            window.controller.weight = args.weight
            if hasattr(window, "weight_var"):
                window.weight_var.set(args.weight)
        root.update()
    except Exception as exc:  # noqa: BLE001
        print(f"[export] set start/end failed: {exc}")
        _teardown(window, root)
        return 3

    # ---- 关动画（对比截图必须看到完整静态路径）----
    if args.no_animation:
        try:
            window._play_animation = lambda: None  # 截图时跳过动画计时器
        except Exception:  # noqa: BLE001
            pass

    # ---- 搜索并等结果 ----
    t0 = time.time()
    try:
        window._on_search()
    except Exception as exc:  # noqa: BLE001
        print(f"[export] _on_search failed: {exc}")
        _teardown(window, root)
        return 3

    deadline = time.time() + max(5.0, args.timeout)
    while time.time() < deadline:
        root.update()
        time.sleep(0.02)
        result = getattr(window, "_last_result", None)
        animating = bool(getattr(window, "_animating", False))
        if result is not None and (args.no_animation or not animating):
            break
        state = getattr(getattr(window, "controller", None), "state", None)
        state_value = getattr(state, "value", state)
        if state_value == "error" and not animating:
            err = getattr(window.controller, "last_error", None)
            print(f"[export] search failed: {getattr(err, 'message', err)}")
            _teardown(window, root)
            return 3
    else:
        print(f"[export] timeout after {args.timeout:.1f}s: no result")
        _teardown(window, root)
        return 3

    search_ms = (time.time() - t0) * 1000.0

    # ---- 等界面稳定（画布重绘有 0ms/120ms 防抖）----
    _pump(root, max(args.settle_ms, 200) / 1000.0)

    # ---- 可选：缩放到指定倍率 / 把某站平移到画布中心（并行走廊证据用）----
    focus_sid = None
    if args.zoom is not None or args.focus:
        if args.zoom is not None:
            try:
                window.view.set_zoom(float(args.zoom))
            except Exception as exc:  # noqa: BLE001
                print(f"[export] set_zoom failed: {exc}")
                _teardown(window, root)
                return 3
        _pump(root, 0.35)
        if args.focus:
            focus_sid = _resolve_station(window.graph, args.focus)
            if focus_sid is None:
                print(f"[export] focus station not found: {args.focus!r}")
                _teardown(window, root)
                return 3
            pos = window.view.station_screen(focus_sid)
            if pos:
                cx = window.view.winfo_width() / 2.0
                cy = window.view.winfo_height() / 2.0
                try:
                    window.view.pan_by(cx - pos[0], cy - pos[1])
                except Exception as exc:  # noqa: BLE001
                    print(f"[export] pan to focus failed: {exc}")
            _pump(root, 0.4)

    root.update_idletasks()
    root.update()

    result = getattr(window, "_last_result", None)
    if result is None:
        print("[export] no result to export")
        _teardown(window, root)
        return 3

    # ---- 采集观测数值 ----
    def g(name, default=None):
        return getattr(result, name, default)

    events = getattr(result, "events", None) or []
    ui_cards: dict[str, str] = {}
    try:
        ui_cards = {k: v.cget("text") for k, v in window.metric_values.items()}
    except Exception:  # noqa: BLE001
        pass
    canvas = _canvas_metrics(window.view)
    payload = {
        "root": str(root_dir),
        "data": str(data_path),
        "data_name": graph.meta.get("name", ""),
        "graph": {
            "stations": len(graph.stations),
            "edges": len(graph.edges),
            "lines": len(graph.lines),
        },
        "query": {
            "start": start,
            "end": end,
            "algorithm": window.controller.algorithm,
            "weight": window.controller.weight,
            "animation": not args.no_animation,
            "zoom": args.zoom,
            "focus": focus_sid,
        },
        "result": {
            "found": bool(g("found", False)),
            "path_len": len(getattr(result, "path", None) or []),
            "distance_km": round(float(g("distance_km", 0.0) or 0.0), 6),
            "time_min": round(float(g("time_min", 0.0) or 0.0), 6),
            "transfers": g("transfers"),
            "expanded": g("expanded"),
            "elapsed_ms": round(float(g("elapsed_ms", 0.0) or 0.0), 4),
            "events": len(events),
            "path": list(getattr(result, "path", None) or []),
        },
        "ui_cards": ui_cards,
        "ui_path_meta": _safe_text(getattr(window, "path_meta", None)),
        "canvas": canvas,
        "timing": {"search_wall_ms": round(search_ms, 1)},
        "outputs": {
            "ps": str(out_ps) if out_ps else None,
            "png": str(out_png) if out_png else None,
            "png_canvas": str(out_png_canvas) if out_png_canvas else None,
        },
    }

    # ---- 导出 PostScript ----
    if out_ps is not None:
        out_ps.parent.mkdir(parents=True, exist_ok=True)
        try:
            window.view.postscript(file=str(out_ps), colormode="color")
        except Exception as exc:  # noqa: BLE001
            print(f"[export] postscript failed: {exc}")
            _teardown(window, root)
            return 3
        size = out_ps.stat().st_size if out_ps.exists() else 0
        payload["outputs"]["ps_bytes"] = size
        print(f"[export] ps     : {out_ps} ({size} bytes)")
        if size <= 0:
            print("[export] empty postscript")
            _teardown(window, root)
            return 3

    # ---- 导出 PNG（整窗 / 画布）----
    if out_png is not None or out_png_canvas is not None:
        try:
            root.update_idletasks()
            root.update()
            time.sleep(0.25)          # 等 DWM 合成，避免截到旧帧
            root.update()
            wx, wy = root.winfo_rootx(), root.winfo_rooty()
            ww, wh = root.winfo_width(), root.winfo_height()
            if out_png is not None:
                width, height = _grab(root, (wx, wy, wx + ww, wy + wh), out_png)
                payload["outputs"]["png_size"] = [width, height]
                print(f"[export] png    : {out_png} ({width}x{height})")
            if out_png_canvas is not None:
                vx, vy = window.view.winfo_rootx(), window.view.winfo_rooty()
                vw, vh = window.view.winfo_width(), window.view.winfo_height()
                width, height = _grab(root, (vx, vy, vx + vw, vy + vh), out_png_canvas)
                payload["outputs"]["png_canvas_size"] = [width, height]
                print(f"[export] png cv : {out_png_canvas} ({width}x{height})")
        except ImportError as exc:
            print(f"[export] Pillow unavailable, skip png: {exc}")
            _teardown(window, root)
            return 3
        except Exception as exc:  # noqa: BLE001
            print(f"[export] png grab failed: {exc}")
            _teardown(window, root)
            return 3

    # ---- 写观测 JSON ----
    if out_json is not None:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(f"[export] json   : {out_json}")

    # ---- 控制台摘要（ASCII，避免 Windows 控制台编码问题）----
    r = payload["result"]
    print(f"[export] query  : {start} -> {end} "
          f"({payload['query']['algorithm']}/{payload['query']['weight']}, "
          f"animation={'off' if args.no_animation else 'on'}"
          + (f", zoom={args.zoom}" if args.zoom is not None else "")
          + (f", focus={focus_sid}" if focus_sid else "") + ")")
    print(f"[export] result : found={r['found']} path={r['path_len']} "
          f"km={r['distance_km']:.2f} min={r['time_min']:.2f} "
          f"transfers={r['transfers']} expanded={r['expanded']} "
          f"search={r['elapsed_ms']:.2f}ms")
    cards = payload["ui_cards"]
    if cards:
        print(f"[export] ui     : km={cards.get('km')} min={cards.get('min')} "
              f"transfers={cards.get('transfers')} expanded={cards.get('expanded')}")
    c = payload["canvas"]
    print(f"[export] canvas : items={c['items_total']} "
          f"network={c['by_tag'].get('network')} station={c['by_tag'].get('station')} "
          f"label={c['by_tag'].get('label')} path={c['by_tag'].get('path')} "
          f"overlay={c['by_tag'].get('overlay')}")
    print(f"[export] labels : shown={c['station_labels_shown']} "
          f"hidden={c['hidden_station_labels']} seglabels={c['segment_labels_shown']} "
          f"stations_drawn={c['stations_drawn']} unique_pts={c['station_screen_unique_points']}")
    print(f"[export] timing : wall={search_ms:.0f}ms")

    _teardown(window, root)
    print("[export] ok")
    return 0


def _safe_text(widget) -> str:
    if widget is None:
        return ""
    try:
        return widget.cget("text")
    except Exception:  # noqa: BLE001
        return ""


def _teardown(window, root) -> None:
    try:
        window.close()          # 取消动画/队列 after 任务（可能连 root 一起销毁）
    except Exception:  # noqa: BLE001
        pass
    try:
        root.update_idletasks()
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())
