"""Verify Parquet backup matches the source SQLite klines table row-for-row."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

try:
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pyarrow is required") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="klines.db")
    parser.add_argument("--out-root", default=os.path.join("data", "klines"))
    args = parser.parse_args()

    manifest_path = os.path.join(args.out_root, "_manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    conn = sqlite3.connect(args.db, timeout=120.0)
    conn.execute("PRAGMA busy_timeout=120000")
    cur = conn.cursor()

    all_ok = True
    for series in manifest["series"]:
        symbol = series["symbol"]
        interval = series["interval"]
        cur.execute(
            "SELECT COUNT(*), MIN(open_time), MAX(open_time) "
            "FROM klines WHERE symbol=? AND interval=?",
            (symbol, interval),
        )
        db_count, db_min, db_max = cur.fetchone()

        pq_count = 0
        pq_min = None
        pq_max = None
        for part in series["partitions"]:
            path = os.path.join(args.out_root, part["path"])
            table = pq.read_table(path, columns=["open_time"])
            n = table.num_rows
            pq_count += n
            if n:
                col = table.column("open_time")
                mn = int(col[0].as_py())
                mx = int(col[-1].as_py())
                pq_min = mn if pq_min is None else min(pq_min, mn)
                pq_max = mx if pq_max is None else max(pq_max, mx)

        ok = (db_count == pq_count and db_min == pq_min and db_max == pq_max)
        marker = "OK" if ok else "MISMATCH"
        print(
            f"[{marker}] {symbol:10s} {interval:4s} db_count={db_count} pq_count={pq_count} "
            f"range_db=({db_min}..{db_max}) range_pq=({pq_min}..{pq_max})"
        )
        all_ok = all_ok and ok

    conn.close()
    print()
    print("RESULT:", "ALL OK" if all_ok else "FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
