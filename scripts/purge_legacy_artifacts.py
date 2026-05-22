"""Borron y cuenta nueva.

Drops every backtest/optuna table from `klines.db`, removes the orphan single
candle on `BTCUSDT/1m`, vacuums the database so disk space is reclaimed, and
wipes filesystem artifacts (reports/, logs/, caches, test fixtures).

The healthy klines table is preserved. Run only after a verified Parquet
backup exists under `data/klines/`.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from typing import List

BT_TABLES = (
    "bt_events",
    "bt_metrics",
    "bt_trial_metrics",
    "bt_trials",
    "bt_runs",
)

OPTUNA_TABLES = (
    "alembic_version",
    "studies",
    "study_directions",
    "study_system_attributes",
    "study_user_attributes",
    "trial_heartbeats",
    "trial_intermediate_values",
    "trial_params",
    "trial_system_attributes",
    "trial_user_attributes",
    "trial_values",
    "trials",
    "version_info",
)

FS_TARGETS_DIRS = (
    "reports",
    "logs",
    "__pycache__",
    ".mypy_cache",
)

FS_TARGETS_FILES = (
    "test_klines.db",
)


def drop_tables(conn: sqlite3.Connection, names: List[str]) -> None:
    cur = conn.cursor()
    existing = {
        r[0]
        for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for name in names:
        if name in existing:
            print(f"  drop table {name}")
            cur.execute(f"DROP TABLE IF EXISTS {name}")
    conn.commit()


def remove_orphan_klines(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        "SELECT symbol, interval, COUNT(*) FROM klines GROUP BY symbol, interval HAVING COUNT(*) < 100"
    )
    rows = cur.fetchall()
    for sym, interval, count in rows:
        print(f"  removing orphan klines: {sym} {interval} ({count} rows)")
        cur.execute("DELETE FROM klines WHERE symbol=? AND interval=?", (sym, interval))
    conn.commit()


def reset_sqlite_sequence(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM sqlite_sequence WHERE name NOT IN ('klines')")
    conn.commit()


def rmtree_safe(path: str) -> None:
    if os.path.isdir(path):
        print(f"  rm -r {path}")
        shutil.rmtree(path, ignore_errors=True)


def remove_recursive_caches(root: str, target_dirname: str) -> None:
    for cur_root, dirs, _files in os.walk(root):
        if target_dirname in dirs:
            full = os.path.join(cur_root, target_dirname)
            print(f"  rm -r {full}")
            shutil.rmtree(full, ignore_errors=True)
            dirs.remove(target_dirname)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="klines.db")
    parser.add_argument(
        "--require-backup",
        default=os.path.join("data", "klines", "_manifest.json"),
        help="Path that must exist before purging proceeds",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation",
    )
    args = parser.parse_args()

    if not os.path.exists(args.require_backup):
        raise SystemExit(
            f"Backup manifest not found: {args.require_backup}. Run backup_klines_to_parquet.py first."
        )
    if not os.path.exists(args.db):
        raise SystemExit(f"db not found: {args.db}")

    if not args.yes:
        prompt = (
            f"This will DROP all bt_* / optuna tables in {args.db}, vacuum it, "
            "remove orphan kline series (< 100 rows), and delete reports/, logs/, "
            "__pycache__, .mypy_cache, test_klines.db. Continue? [y/N]: "
        )
        answer = input(prompt).strip().lower()
        if answer not in {"y", "yes"}:
            print("aborted")
            return 1

    print("== sqlite cleanup ==")
    conn = sqlite3.connect(args.db, timeout=600.0)
    conn.execute("PRAGMA busy_timeout=600000")
    drop_tables(conn, list(BT_TABLES))
    drop_tables(conn, list(OPTUNA_TABLES))
    remove_orphan_klines(conn)
    reset_sqlite_sequence(conn)
    conn.commit()
    print("  VACUUM (this can take a few minutes on 4 GB)...")
    conn.isolation_level = None
    conn.execute("VACUUM")
    conn.close()

    print("== filesystem cleanup ==")
    for d in FS_TARGETS_DIRS:
        rmtree_safe(d)
    for f in FS_TARGETS_FILES:
        if os.path.exists(f):
            print(f"  rm {f}")
            os.remove(f)
    remove_recursive_caches(".", "__pycache__")
    remove_recursive_caches(".", ".mypy_cache")

    print()
    new_size = os.path.getsize(args.db) / (1024 * 1024)
    print(f"klines.db size after vacuum: {new_size:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
