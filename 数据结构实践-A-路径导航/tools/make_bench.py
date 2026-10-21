"""生成 data/bench.csv（P4 的对比实验数据由 P3 提供，保证"同一份数据喂两处"）。

用法::

    python tools/make_bench.py            # 重新生成
    python tools/make_bench.py --verify    # 只校验，不写文件

界面里的"三算法对比"表与报告里的对比表都读这一个文件；数值由当前
``core.solver`` 现场算出来再写入，禁止手抄，因此二者永远一致。
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import solver  # noqa: E402
from core.graph import load_graph  # noqa: E402

DATA_DIR = ROOT / "data"
BENCH_PATH = DATA_DIR / "bench.csv"

OD_PAIRS = [
    ("A", "S01", "S07", "中央公园→大学城（同线直达）"),
    ("B", "S08", "S17", "北苑→临港（跨线换乘）"),
    ("C", "S13", "S12", "西山→南站（西南→东南）"),
    ("D", "S12", "S16", "南站→机场（含机场快线段）"),
]

FIELDS = [
    "pair",
    "start_id",
    "end_id",
    "algorithm",
    "weight",
    "distance_km",
    "time_min",
    "transfers",
    "expanded",
    "elapsed_ms",
]


def build_rows() -> list[dict]:
    graph = load_graph(DATA_DIR)
    rows: list[dict] = []
    for pair, start, end, _desc in OD_PAIRS:
        for weight in solver.WEIGHTS:
            for algorithm in solver.ALGORITHMS:
                result = solver.solve(graph, start, end, algorithm, weight)
                rows.append(
                    {
                        "pair": pair,
                        "start_id": start,
                        "end_id": end,
                        "algorithm": algorithm,
                        "weight": weight,
                        "distance_km": result.distance_km,
                        "time_min": result.time_min,
                        "transfers": result.transfers,
                        "expanded": result.expanded,
                        "elapsed_ms": result.elapsed_ms,
                    }
                )
    return rows


def write_bench(rows: list[dict]) -> None:
    with BENCH_PATH.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def verify(rows: list[dict]) -> int:
    if not BENCH_PATH.exists():
        print("bench.csv 不存在")
        return 1
    with BENCH_PATH.open("r", encoding="utf-8-sig", newline="") as fh:
        stored = {tuple(row[key] for key in ("start_id", "end_id", "algorithm", "weight")): row
                  for row in csv.DictReader(fh)}
    bad = 0
    for row in rows:
        key = (row["start_id"], row["end_id"], row["algorithm"], row["weight"])
        old = stored.get(key)
        if old is None:
            print(f"缺少行：{key}")
            bad += 1
            continue
        for field in ("distance_km", "time_min", "transfers", "expanded"):
            if abs(float(old[field]) - float(row[field])) > 1e-6:
                print(f"不一致：{key} 字段 {field}: {old[field]} != {row[field]}")
                bad += 1
    print(f"校验完成：{len(rows)} 行，{bad} 处不一致")
    return 0 if bad == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    rows = build_rows()
    if args.verify:
        return verify(rows)
    write_bench(rows)
    print(f"已写入 {BENCH_PATH}（{len(rows)} 行 / {len(OD_PAIRS)} 组 OD）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
