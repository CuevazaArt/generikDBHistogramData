"""Export healthy kline series from klines.db to partitioned Parquet.

Layout produced (matches the redesign plan):

    data/klines/symbol=<SYM>/interval=<INT>/year=<YYYY>/month=<MM>/part-000.parquet

A short JSON sidecar `_manifest.json` summarises rows/min/max per partition so
verification is trivial. Series with too few candles to be useful are skipped
(controlled via `--min-rows`).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Tuple

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover - hard requirement
    raise SystemExit(
        "pyarrow is required for the Parquet backup. Run: pip install pyarrow"
    ) from exc


COLUMNS = [
    ("open_time", pa.int64()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),
    ("close_time", pa.int64()),
    ("quote_asset_volume", pa.float64()),
    ("num_trades", pa.int64()),
    ("taker_buy_base", pa.float64()),
    ("taker_buy_quote", pa.float64()),
    ("ignore_field", pa.string()),
]
SCHEMA = pa.schema(COLUMNS)


def list_series(conn: sqlite3.Connection) -> List[Tuple[str, str, int, int, int]]:
    cur = conn.cursor()
    cur.execute(
        "SELECT symbol, interval, COUNT(*), MIN(open_time), MAX(open_time) "
        "FROM klines GROUP BY symbol, interval ORDER BY symbol, interval"
    )
    return list(cur.fetchall())


def partition_key(ts_ms: int) -> Tuple[int, int]:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    return dt.year, dt.month


def export_series(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str,
    out_root: str,
) -> Dict[str, object]:
    cur = conn.cursor()
    cur.execute(
        "SELECT open_time, open, high, low, close, volume, close_time, "
        "quote_asset_volume, num_trades, taker_buy_base, taker_buy_quote, ignore_field "
        "FROM klines WHERE symbol=? AND interval=? ORDER BY open_time ASC",
        (symbol, interval),
    )

    buckets: Dict[Tuple[int, int], List[List[object]]] = defaultdict(list)
    for row in cur:
        year, month = partition_key(int(row[0]))
        buckets[(year, month)].append(list(row))

    partitions: List[Dict[str, object]] = []
    for (year, month), rows in sorted(buckets.items()):
        out_dir = os.path.join(
            out_root,
            f"symbol={symbol}",
            f"interval={interval}",
            f"year={year:04d}",
            f"month={month:02d}",
        )
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "part-000.parquet")
        cols: Dict[str, List[object]] = {name: [] for name, _ in COLUMNS}
        for r in rows:
            for i, (name, _) in enumerate(COLUMNS):
                v = r[i]
                if name == "num_trades":
                    cols[name].append(int(v) if v is not None else None)
                elif name in ("open_time", "close_time"):
                    cols[name].append(int(v) if v is not None else None)
                elif name == "ignore_field":
                    cols[name].append(str(v) if v is not None else "")
                else:
                    cols[name].append(float(v) if v is not None else None)
        table = pa.Table.from_pydict(cols, schema=SCHEMA)
        pq.write_table(table, out_path, compression="zstd")
        partitions.append(
            {
                "year": year,
                "month": month,
                "rows": len(rows),
                "min_open_time": int(rows[0][0]),
                "max_open_time": int(rows[-1][0]),
                "path": os.path.relpath(out_path, out_root),
            }
        )

    return {
        "symbol": symbol,
        "interval": interval,
        "total_rows": int(sum(p["rows"] for p in partitions)),
        "partitions": partitions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="klines.db")
    parser.add_argument(
        "--out-root",
        default=os.path.join("data", "klines"),
        help="Root directory for the partitioned Parquet layout",
    )
    parser.add_argument(
        "--min-rows",
        type=int,
        default=100,
        help="Skip kline series with fewer than this many candles",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"db not found: {args.db}")

    conn = sqlite3.connect(args.db, timeout=120.0)
    conn.execute("PRAGMA busy_timeout=120000")
    try:
        series = list_series(conn)
    finally:
        pass

    healthy: List[Tuple[str, str, int]] = []
    skipped: List[Tuple[str, str, int]] = []
    for sym, interval, count, _mn, _mx in series:
        if count >= args.min_rows:
            healthy.append((sym, interval, count))
        else:
            skipped.append((sym, interval, count))

    print("Series detectadas:")
    for sym, interval, count, mn, mx in series:
        tag = "OK" if count >= args.min_rows else "SKIP"
        print(f"  [{tag}] {sym:10s} {interval:4s} rows={count:>8d} range=({mn}..{mx})")

    print()
    print(f"Exportando {len(healthy)} series a {args.out_root}")
    os.makedirs(args.out_root, exist_ok=True)

    manifest: Dict[str, object] = {
        "source_db": os.path.abspath(args.db),
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "min_rows_per_series": args.min_rows,
        "skipped_series": [
            {"symbol": s, "interval": i, "rows": c} for s, i, c in skipped
        ],
        "series": [],
    }
    for sym, interval, _ in healthy:
        print(f"  -> {sym} {interval} ...", flush=True)
        result = export_series(conn, sym, interval, args.out_root)
        manifest["series"].append(result)
        print(
            f"     filas exportadas: {result['total_rows']} en {len(result['partitions'])} particiones"
        )

    manifest_path = os.path.join(args.out_root, "_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    print(f"\nManifest: {manifest_path}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
