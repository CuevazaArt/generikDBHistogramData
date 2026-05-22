"""One-shot inspector for klines.db before cleanup.

Lists tables, kline series with counts and time ranges, and backtest table
sizes so we know exactly what is being preserved vs deleted.
"""
from __future__ import annotations

import os
import sqlite3
import sys


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "klines.db"
    if not os.path.exists(db_path):
        print(f"missing: {db_path}")
        return 1
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    cur = conn.cursor()

    print("=== TABLES ===")
    for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        print(" -", r[0])

    print("\n=== KLINE SERIES ===")
    print("symbol | interval | rows | min_open_time | max_open_time")
    for r in cur.execute(
        "SELECT symbol, interval, COUNT(*), MIN(open_time), MAX(open_time) "
        "FROM klines GROUP BY symbol, interval ORDER BY symbol, interval"
    ):
        print(r)

    for tbl in ("bt_runs", "bt_events", "bt_metrics", "bt_trials", "bt_trial_metrics"):
        try:
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            print(f"{tbl}: {cur.fetchone()[0]}")
        except Exception as exc:
            print(f"{tbl}: missing ({exc})")

    try:
        for r in cur.execute("SELECT status, COUNT(*) FROM bt_runs GROUP BY status"):
            print("bt_runs status:", r)
    except Exception:
        pass

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
