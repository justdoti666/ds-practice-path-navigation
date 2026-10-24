#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 data/metro_raw.csv 派生 164 站压力数据集 data/stress_164.json（T2 交付）。

规则
----
删除 5 个「非换乘的线路端点站」（某条线的首站/末站，且该站名只出现在这一条线上），
使唯一站名数 169 → 164。删除对象是**确定性的**：按 CSV 中线路首次出现顺序轮转，
每条线最多贡献 1 个候选（优先首站，其次末站），轮转 5 轮凑满 5 个，保证删除的站
分散在 5 条不同线路上。被删站是叶子节点，删除后图仍连通、各线仍 ≥ 2 站。

生成方式
--------
完全复用 ``tools/import_metro.py`` 的 ``build_graph``（线性坐标：x=站序×spacing、
y=线序×line_gap；换乘站合并取首次坐标；meta.layout 记录布局参数），与
``data/real_metro.json`` 同一坐标系、同一加载口径；生成后用 ``core.graph.load_graph``
校验：站数=164、加载通过、图连通、线路数不变。

用法
----
    python tools/gen_stress_data.py                 # 生成 + 校验
    python tools/gen_stress_data.py --check         # 只校验已有文件
    python tools/gen_stress_data.py --target 164 --out data/stress_164.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # path-nav/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import import_metro                                    # noqa: E402  (tools/import_metro.py)
from core.graph import load_graph                      # noqa: E402

CSV_DEFAULT = ROOT / "data" / "metro_raw.csv"
OUT_DEFAULT = ROOT / "data" / "stress_164.json"
TARGET_DEFAULT = 164
BASE_EXPECTED = 169


# --------------------------------------------------------------------- 拓扑
def line_sequences(rows) -> "OrderedDict[str, list[str]]":
    """按 CSV 行序得到每条线的站名序列（保持行车顺序）。"""
    seqs: "OrderedDict[str, list[str]]" = OrderedDict()
    for r in rows:
        seqs.setdefault(r["line_id"], []).append(r["station"])
    return seqs


def find_non_transfer_endpoints(rows) -> list[dict]:
    """所有「非换乘 + 线路端点」的站点，附线路与首/末位置。"""
    seqs = line_sequences(rows)
    count: dict[str, int] = {}
    for ss in seqs.values():
        for name in ss:
            count[name] = count.get(name, 0) + 1
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for lid, ss in seqs.items():
        for pos, name in (("first", ss[0]), ("last", ss[-1])):
            if count[name] == 1 and (lid, name) not in seen:
                seen.add((lid, name))
                out.append({"line": lid, "station": name, "position": pos})
    return out


def select_deletions(rows, k: int) -> list[dict]:
    """确定性选出 k 个待删站：按线路轮转，每线最多 1 个，优先首站。"""
    seqs = line_sequences(rows)
    count: dict[str, int] = {}
    for ss in seqs.values():
        for name in ss:
            count[name] = count.get(name, 0) + 1
    per_line: dict[str, list[dict]] = {lid: [] for lid in seqs}
    for lid, ss in seqs.items():
        for pos, name in (("first", ss[0]), ("last", ss[-1])):
            if count[name] == 1:
                per_line[lid].append({"line": lid, "station": name, "position": pos})
    chosen: list[dict] = []
    taken: set[str] = set()
    round_no = 0
    while len(chosen) < k and round_no < 8:
        for lid, cands in per_line.items():
            if round_no >= len(cands):
                continue
            cand = cands[round_no]
            if cand["station"] in taken:
                continue
            chosen.append(cand)
            taken.add(cand["station"])
            if len(chosen) >= k:
                break
        round_no += 1
    if len(chosen) != k:
        raise SystemExit(f"只能找到 {len(chosen)} 个非换乘端点站，目标需要 {k} 个")
    return chosen


def filter_rows(rows, deleted: set[str]) -> list[dict]:
    return [r for r in rows if r["station"] not in deleted]


def connectivity(graph) -> list[int]:
    """返回各连通分量的站点数（降序）。"""
    seen: set[str] = set()
    comps: list[int] = []
    for sid in graph.stations:
        if sid in seen:
            continue
        stack = [sid]
        seen.add(sid)
        size = 0
        while stack:
            u = stack.pop()
            size += 1
            for e in graph.neighbors(u):
                v = e.other(u)
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        comps.append(size)
    return sorted(comps, reverse=True)


# --------------------------------------------------------------------- 生成
def generate(csv_path: Path, out_path: Path, target: int,
             spacing: float, line_gap: float, min_gap: float) -> dict:
    if not csv_path.exists():
        raise SystemExit(f"CSV 不存在: {csv_path}")
    rows = list(import_metro.read_rows(csv_path))
    base_unique = {r["station"] for r in rows}
    base_lines = len(line_sequences(rows))
    n_delete = len(base_unique) - target
    if n_delete <= 0:
        raise SystemExit(f"CSV 唯一站数 {len(base_unique)} ≤ 目标 {target}，无需删除")

    deleted = select_deletions(rows, n_delete)
    deleted_names = [d["station"] for d in deleted]
    kept = filter_rows(rows, set(deleted_names))

    raw = import_metro.build_graph(
        kept, spacing=spacing, line_gap=line_gap, min_gap=min_gap,
        name="广州地铁（简化拓扑·164 站压力数据集）")

    # --- 附加 meta（不改变坐标与 load_graph 语义）
    meta = raw["meta"]
    meta["note"] = (
        "approximated：原始线路表无经纬度，按线路线性布局生成坐标；"
        "换乘站合并为同一站点，坐标取首次出现位置。"
        f"本文件由 gen_stress_data.py 从 data/metro_raw.csv 删除 {len(deleted)} 个"
        f"非换乘端点站派生，用于验证 164 站下显示层布局不重叠。")
    meta["stress"] = {
        "target_stations": target,
        "base_stations": len(base_unique),
        "deleted_stations": deleted,
        "generator": "tools/gen_stress_data.py（复用 tools/import_metro.build_graph）",
        "layout_kind": "linear-band（与 real_metro.json 同参数）",
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")
    return raw


# --------------------------------------------------------------------- 校验
def verify(path: Path, target: int) -> dict:
    """加载校验 + 站数/连通/线路数/被删站不残留。返回摘要 dict。"""
    graph = load_graph(path)
    problems: list[str] = []
    n = len(graph.stations)
    if n != target:
        problems.append(f"站数 {n} != 目标 {target}")
    comps = connectivity(graph)
    if len(comps) != 1:
        problems.append(f"图不连通：{len(comps)} 个分量 {comps}")
    if not graph.lines:
        problems.append("线路为空")
    for lid in graph.lines:
        if len(graph.stations_of_line(lid)) < 2:
            problems.append(f"线路 {lid} 站点不足 2 个")
    if not (len(graph.edges) > 0):
        problems.append("边为空")
    # 孤立站（度 0）应为 0
    isolated = [sid for sid in graph.stations if not graph.neighbors(sid)]
    if isolated:
        problems.append(f"存在孤立站 {isolated[:5]}")
    # meta.layout 必须保留（display_layout.needs_schematic_layout 依赖它）
    if not isinstance(graph.meta.get("layout"), dict):
        problems.append("meta.layout 缺失（display_layout 退化检测依赖它）")
    summary = {
        "path": str(path),
        "stations": n,
        "edges": len(graph.edges),
        "lines": len(graph.lines),
        "components": comps,
        "transfers": sum(1 for v in graph.lines_of_station().values() if len(v) > 1),
        "meta_layout": graph.meta.get("layout"),
        "meta_stress": graph.meta.get("stress"),
        "problems": problems,
    }
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从 metro_raw.csv 派生 164 站压力数据集")
    ap.add_argument("--csv", default=str(CSV_DEFAULT))
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--target", type=int, default=TARGET_DEFAULT)
    ap.add_argument("--spacing", type=float, default=1.5, help="同线相邻站间距 km（同 import_metro）")
    ap.add_argument("--line-gap", type=float, default=8.0, help="相邻线路纵向间距 km（同 import_metro）")
    ap.add_argument("--min-gap", type=float, default=0.1, help="合并后重合站点的最小间距 km（同 import_metro）")
    ap.add_argument("--check", action="store_true", help="只校验已有输出文件，不重新生成")
    args = ap.parse_args(argv)

    csv_path = Path(args.csv)
    out_path = Path(args.out)

    raw = None
    if not args.check:
        raw = generate(csv_path, out_path, args.target,
                       args.spacing, args.line_gap, args.min_gap)
        st = raw["meta"]["stress"]
        print(f"[生成] {out_path}")
        print(f"  删除 {len(st['deleted_stations'])} 个非换乘端点站：")
        for d in st["deleted_stations"]:
            print(f"    - 线路 {d['line']} {d['position']}端：{d['station']}")
        print(f"  站 {len(raw['stations'])} / 边 {len(raw['edges'])} / 线 {len(raw['lines'])}")

    summary = verify(out_path, args.target)
    print(f"[校验] 站 {summary['stations']} / 边 {summary['edges']} / "
          f"线 {summary['lines']} / 分量 {summary['components']} / "
          f"换乘站 {summary['transfers']}")
    if summary["meta_stress"]:
        print(f"[校验] 被删站：{[d['station'] for d in summary['meta_stress']['deleted_stations']]}")
    if summary["problems"]:
        for p in summary["problems"]:
            print(f"  [失败] {p}", file=sys.stderr)
        return 1
    print("[校验通过] load_graph OK、站数正确、图连通、meta.layout 保留")
    return 0


if __name__ == "__main__":
    sys.exit(main())
