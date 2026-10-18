#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""真实线路表 → real_metro.json（P1-05）。

输入 CSV 的列固定为：``line_id,line_name,speed_kmh,color,station``，
同一线路的站点按**行车顺序**逐行排列；同一站名出现在多条线时自动合并为换乘站。

坐标策略（原始线路表没有经纬度，所以是**近似布局**）：
  * 每条线路占一条水平带：``y = 线路序号 × line_gap``
  * 线上站点按顺序排开：``x = 该站在本线的序号 × spacing``
  * 换乘站合并为同一个 sid，坐标取**首次出现**的位置
  * 生成物在 ``meta.note`` 里标注 ``approximated``，并在 ``meta.layout`` 记录布局参数

用法：
    python tools/import_metro.py                       # 用默认路径
    python tools/import_metro.py --csv data/metro_raw.csv --out data/real_metro.json
    python tools/import_metro.py --check               # 生成后立刻用图加载器验证
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REQUIRED_COLUMNS = ["line_id", "line_name", "speed_kmh", "color", "station"]


def read_rows(path: Path):
    """逐行读取 CSV，做最基本的字段校验（行号一并带出，便于定位）。"""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in fields]
        if missing:
            raise SystemExit(f"CSV 缺少列 {missing}；需要 {REQUIRED_COLUMNS}，实际 {fields}")
        for lineno, row in enumerate(reader, 2):
            station = (row.get("station") or "").strip()
            if not station:
                continue
            raw_speed = (row.get("speed_kmh") or "").strip()
            try:
                speed = float(raw_speed)
            except ValueError:
                raise SystemExit(f"第 {lineno} 行 speed_kmh 不是数字: {raw_speed!r}")
            if speed <= 0:
                raise SystemExit(f"第 {lineno} 行 speed_kmh 必须为正，实际 {speed}")
            yield {
                "line_id": (row.get("line_id") or "").strip(),
                "line_name": (row.get("line_name") or "").strip(),
                "speed_kmh": speed,
                "color": (row.get("color") or "").strip() or "#888888",
                "station": station,
                "lineno": lineno,
            }


def build_graph(rows, spacing: float, line_gap: float, min_gap: float, name: str) -> dict:
    lines: dict[str, dict] = {}
    for r in rows:
        if not r["line_id"] or not r["line_name"]:
            raise SystemExit(f"第 {r['lineno']} 行 line_id/line_name 不能为空")
        ln = lines.setdefault(r["line_id"], {
            "name": r["line_name"],
            "speed_kmh": r["speed_kmh"],
            "color": r["color"],
            "stations": [],
        })
        if ln["speed_kmh"] != r["speed_kmh"]:
            raise SystemExit(
                f"第 {r['lineno']} 行线路 {r['line_id']} 的速度与首次出现不一致："
                f"{r['speed_kmh']} vs {ln['speed_kmh']}")
        ln["stations"].append(r["station"])

    stations: dict[str, dict] = {}
    for li, ln in enumerate(lines.values()):
        for si, sname in enumerate(ln["stations"]):
            if sname in stations:
                continue                      # 换乘站：合并，保留首次坐标
            stations[sname] = {
                "id": f"S{len(stations) + 1:03d}",
                "name": sname,
                "x": si * spacing,
                "y": li * line_gap,
            }

    vmax = max((ln["speed_kmh"] for ln in lines.values()), default=0.0)
    edges = []
    seen: set = set()
    for lid, ln in lines.items():
        for a, b in zip(ln["stations"], ln["stations"][1:]):
            if a == b:
                continue
            sa, sb = stations[a], stations[b]
            dist = math.hypot(sa["x"] - sb["x"], sa["y"] - sb["y"])
            if dist < min_gap:
                dist = min_gap               # 线性布局合并后可能重合，给一个最小间距
            key = (min(sa["id"], sb["id"]), max(sa["id"], sb["id"]), lid)
            if key in seen:
                continue
            seen.add(key)
            t = dist / ln["speed_kmh"] * 60.0
            # 浮点安全：保证 time_min ≥ distance_km / vmax × 60（R2 的加载期校验）
            t = max(t, dist / vmax * 60.0)
            edges.append({
                "u": sa["id"], "v": sb["id"], "line": lid,
                "distance_km": dist, "time_min": t,
            })

    return {
        "meta": {
            "name": name,
            "note": "approximated：原始线路表无经纬度，按线路线性布局生成坐标；"
                    "换乘站合并为同一站点，坐标取首次出现位置。",
            "source": "data/metro_raw.csv",
            "layout": {"spacing_km": spacing, "line_gap_km": line_gap,
                       "min_gap_km": min_gap, "vmax_kmh": vmax},
        },
        "lines": [
            {"id": lid, "name": ln["name"], "speed_kmh": ln["speed_kmh"], "color": ln["color"]}
            for lid, ln in lines.items()
        ],
        "stations": list(stations.values()),
        "edges": edges,
    }


def summarize(raw: dict) -> str:
    lines_of: dict[str, set] = {s["id"]: set() for s in raw["stations"]}
    for e in raw["edges"]:
        lines_of[e["u"]].add(e["line"])
        lines_of[e["v"]].add(e["line"])
    transfers = sum(1 for v in lines_of.values() if len(v) > 1)
    isolated = sum(1 for v in lines_of.values() if not v)
    return (f"{len(raw['stations'])} 站 / {len(raw['edges'])} 边 / {len(raw['lines'])} 条线 ｜ "
            f"换乘站 {transfers} 个 ｜ 孤立站 {isolated} 个")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="真实线路表 → real_metro.json")
    ap.add_argument("--csv", default=str(ROOT / "data" / "metro_raw.csv"))
    ap.add_argument("--out", default=str(ROOT / "data" / "real_metro.json"))
    ap.add_argument("--name", default="广州地铁（简化拓扑）")
    ap.add_argument("--spacing", type=float, default=1.5, help="同线相邻站的间距 km")
    ap.add_argument("--line-gap", type=float, default=8.0, help="相邻线路之间的纵向间距 km")
    ap.add_argument("--min-gap", type=float, default=0.1, help="合并后重合站点的最小间距 km")
    ap.add_argument("--check", action="store_true", help="生成后用 core.graph 加载校验")
    args = ap.parse_args(argv)

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"CSV 不存在: {csv_path}")

    raw = build_graph(read_rows(csv_path), args.spacing, args.line_gap, args.min_gap, args.name)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：显式 LF，避免 Windows 上写出 CRLF 导致"同内容不同哈希"
    out.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    print(f"已生成 {out}")
    print(f"  {summarize(raw)}")

    if args.check:
        from core.graph import DataError, load_graph
        try:
            g = load_graph(out)
        except DataError as exc:
            print(f"  [加载校验失败] {exc}", file=sys.stderr)
            return 1
        print(f"  [加载校验通过] {len(g.stations)} 站 / {len(g.edges)} 边 / "
              f"{len(g.lines)} 线，bbox={tuple(round(v, 2) for v in g.bbox())}")

    if len(raw["stations"]) < 100:
        print(f"  [警告] 站点数 {len(raw['stations'])} < 100，未达到任务书要求", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
