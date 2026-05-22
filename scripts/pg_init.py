"""Apply pending PostgreSQL migrations for the backtesting framework.

Usage:
    python scripts/pg_init.py --dsn postgresql://genericbt:genericbt@127.0.0.1:5433/genericbt
    python scripts/pg_init.py --dry-run

Exit codes:
    0  success (migrations applied or already up to date)
    1  connection failure / migration error
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_on_path() -> None:
    root = str(_project_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.getenv("PG_DSN"),
        help="PostgreSQL DSN. Defaults to env PG_DSN.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only list migrations that would run, without touching the database.",
    )
    args = parser.parse_args()

    if not args.dsn:
        print("ERROR: --dsn not provided and PG_DSN env is unset", file=sys.stderr)
        return 1

    _ensure_on_path()
    try:
        from backtest.migrations import (
            apply_migrations,
            current_version,
            list_migration_files,
        )
    except ImportError as exc:
        print(f"ERROR: cannot import backtest.migrations: {exc}", file=sys.stderr)
        return 1

    files = list_migration_files()
    if args.dry_run:
        print(f"DSN: {args.dsn}")
        print(f"Discovered {len(files)} migration files:")
        for path in files:
            print(f"  - {path.name}")
        print("(dry-run: no statements executed)")
        return 0

    try:
        applied = apply_migrations(args.dsn)
    except Exception as exc:
        print(f"ERROR: migration failed: {exc}", file=sys.stderr)
        return 1

    if applied:
        print(f"Applied {len(applied)} migrations: {', '.join(applied)}")
    else:
        print("Database already up to date. No migrations applied.")

    final = current_version(args.dsn)
    print(f"Current schema version: {final if final is not None else '<none>'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
