"""One-shot migration from the legacy SQLite store to PostgreSQL + Parquet.

Steps performed:
    1. Apply PostgreSQL migrations (creates the meta/ops schemas + tables).
    2. Verify the Parquet kline backup is healthy (reads data/klines/_manifest.json
       and checks every listed partition file exists).
    3. Read any leftover bt_* rows still present in klines.db. After the recent
       cleanup the user did, none are expected, but the tool handles whatever
       is found.
    4. For each legacy `bt_run`: compute its idempotency key, INSERT into
       meta.runs, port its bt_events rows into data/events/run_<new_id>/part-000.parquet
       via storage_pg.persist_run_events, and INSERT its bt_metrics into
       meta.run_metrics.
    5. For each legacy `bt_trial`: INSERT into meta.trial_runs (study row is
       autocreated if missing).
    6. With --dry-run, print everything that WOULD be inserted/written and
       exit without touching PG or the filesystem.

Usage:
    python scripts/migrate_to_pg.py --dsn postgresql://genericbt:genericbt@127.0.0.1:5433/genericbt
    python scripts/migrate_to_pg.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_on_path() -> None:
    root = str(_project_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cur.fetchone() is not None


def _read_legacy_runs(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    if not _has_table(conn, "bt_runs"):
        return []
    cur = conn.execute(
        """
        SELECT run_id, strategy_name, symbol, interval, start_ts, end_ts,
               initial_cash, fee_rate, slippage_bps, config_json, status,
               created_at, ended_at
        FROM bt_runs
        ORDER BY run_id ASC
        """
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _read_legacy_metrics(conn: sqlite3.Connection, run_id: int) -> Dict[str, float]:
    if not _has_table(conn, "bt_metrics"):
        return {}
    cur = conn.execute(
        "SELECT metric_name, metric_value FROM bt_metrics WHERE run_id = ?",
        (int(run_id),),
    )
    out: Dict[str, float] = {}
    for name, value in cur.fetchall():
        if value is None:
            continue
        out[str(name)] = float(value)
    return out


def _iter_legacy_events(conn: sqlite3.Connection, run_id: int):
    if not _has_table(conn, "bt_events"):
        return
    cur = conn.execute(
        """
        SELECT trial_id, seq, event_time, event_type, side, price, qty,
               cash, equity, position_qty, payload_json
        FROM bt_events
        WHERE run_id = ?
        ORDER BY seq ASC
        """,
        (int(run_id),),
    )
    cols = [c[0] for c in cur.description]
    for row in cur:
        yield dict(zip(cols, row))


def _read_legacy_trials(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    if not _has_table(conn, "bt_trials"):
        return []
    cur = conn.execute(
        """
        SELECT trial_id, study_name, trial_number, state, objective,
               params_json, started_at, finished_at, duration_sec
        FROM bt_trials
        ORDER BY trial_id ASC
        """
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _verify_manifest(data_root: str) -> Tuple[int, int, List[str]]:
    """Return (partitions_ok, partitions_missing, list_of_missing)."""
    manifest_path = os.path.join(data_root, "klines", "_manifest.json")
    if not os.path.exists(manifest_path):
        return (0, 0, [f"manifest not found: {manifest_path}"])
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    ok = 0
    missing: List[str] = []
    for series in manifest.get("series", []):
        for part in series.get("partitions", []):
            rel = part.get("path")
            if not rel:
                continue
            full = os.path.join(data_root, "klines", rel)
            if os.path.exists(full):
                ok += 1
            else:
                missing.append(full)
    return (ok, len(missing), missing)


def _utc_to_pg(value: str | None) -> str | None:
    if not value:
        return None
    # Legacy stored ISO8601 strings; PG accepts those directly.
    return value


def _migrate(
    *,
    dsn: str,
    sqlite_path: str,
    data_root: str,
    dry_run: bool,
) -> int:
    from backtest.migrations import apply_migrations
    from backtest.idempotency import compute_run_key
    from backtest.storage_paths import StoragePaths
    from backtest import storage_pg

    paths = StoragePaths(data_root=data_root)

    print("=" * 72)
    print("PostgreSQL migration tool")
    print(f"  DSN          : {dsn}")
    print(f"  Legacy DB    : {sqlite_path}")
    print(f"  Data root    : {data_root}")
    print(f"  Dry run      : {dry_run}")
    print("=" * 72)

    if not os.path.exists(sqlite_path):
        print(f"WARNING: SQLite source {sqlite_path} does not exist. Continuing (nothing to port).")
        sqlite_runs: List[Dict[str, Any]] = []
        sqlite_trials: List[Dict[str, Any]] = []
    else:
        conn = sqlite3.connect(sqlite_path, timeout=60.0)
        conn.execute("PRAGMA busy_timeout=60000")
        try:
            sqlite_runs = _read_legacy_runs(conn)
            sqlite_trials = _read_legacy_trials(conn)
            print(f"Step 1/5: SQLite probe -> {len(sqlite_runs)} runs, {len(sqlite_trials)} trials")
        finally:
            conn.close()

    ok, miss, missing = _verify_manifest(data_root)
    print(f"Step 2/5: Parquet kline backup -> {ok} partitions OK, {miss} missing")
    if missing:
        for path in missing[:10]:
            print(f"   missing: {path}")
        if miss:
            print("ERROR: Parquet backup is incomplete. Aborting before any write.")
            return 1

    if dry_run:
        print()
        print("Step 3/5: would apply migrations (dry-run)")
        print("Step 4/5: would port the following runs:")
        for r in sqlite_runs:
            print(
                f"  - legacy run_id={r['run_id']} strategy={r['strategy_name']} "
                f"symbol={r['symbol']} interval={r['interval']} status={r['status']}"
            )
        print("Step 5/5: would port the following trials:")
        for t in sqlite_trials:
            print(
                f"  - legacy trial_id={t['trial_id']} study={t['study_name']} "
                f"trial_number={t['trial_number']} state={t['state']}"
            )
        print()
        print("Dry-run complete. No PG or filesystem mutation performed.")
        return 0

    print("Step 3/5: applying migrations...")
    applied = apply_migrations(dsn)
    if applied:
        print(f"   applied: {', '.join(applied)}")
    else:
        print("   already up to date")

    total_events = 0
    total_bytes = 0
    new_runs_ids: Dict[int, int] = {}
    print("Step 4/5: porting runs and events...")
    for r in sqlite_runs:
        legacy_id = int(r["run_id"])
        try:
            config = json.loads(r.get("config_json") or "{}")
        except json.JSONDecodeError:
            config = {}
        strategy_params = config.get("strategy", {}) if isinstance(config, dict) else {}
        engine_kind = "python"
        engine_version = "legacy"

        idem = compute_run_key(
            strategy=r["strategy_name"],
            symbol=r["symbol"],
            interval=r["interval"],
            start_ts=r.get("start_ts"),
            end_ts=r.get("end_ts"),
            initial_cash=r["initial_cash"],
            fee_rate=r["fee_rate"],
            slippage_bps=r["slippage_bps"],
            strategy_params=strategy_params if isinstance(strategy_params, dict) else {},
            engine_kind=engine_kind,
            engine_version=engine_version,
        )

        new_id = storage_pg.create_run(
            dsn,
            strategy=r["strategy_name"],
            symbol=r["symbol"],
            interval=r["interval"],
            start_ts=r.get("start_ts"),
            end_ts=r.get("end_ts"),
            initial_cash=r["initial_cash"],
            fee_rate=r["fee_rate"],
            slippage_bps=r["slippage_bps"],
            config=config,
            idempotency_key=idem,
            engine_kind=engine_kind,
            engine_version=engine_version,
            strategy_params=strategy_params if isinstance(strategy_params, dict) else {},
            host_info={"migrated_from": sqlite_path},
            storage_paths=paths,
        )
        new_runs_ids[legacy_id] = new_id
        print(f"   legacy run_id={legacy_id} -> new run_id={new_id}")

        conn = sqlite3.connect(sqlite_path, timeout=60.0)
        try:
            events_iter = _iter_legacy_events(conn, legacy_id)
            target = storage_pg.persist_run_events(
                dsn,
                run_id=new_id,
                events=events_iter,
                seq=0,
                storage_paths=paths,
            )
        finally:
            conn.close()
        if target and os.path.exists(target):
            total_bytes += os.path.getsize(target)
            print(f"      events -> {target}")

        conn = sqlite3.connect(sqlite_path, timeout=60.0)
        try:
            metrics = _read_legacy_metrics(conn, legacy_id)
        finally:
            conn.close()
        if metrics:
            storage_pg.persist_run_metrics(dsn, run_id=new_id, metrics=metrics)
            print(f"      metrics: {len(metrics)}")

        storage_pg.finish_run(
            dsn,
            run_id=new_id,
            status=str(r.get("status") or "completed"),
        )

    print("Step 5/5: porting trials...")
    for t in sqlite_trials:
        study_name = str(t["study_name"])
        storage_pg.ensure_study(
            dsn,
            study_name=study_name,
            strategy="legacy",
            base_config={"migrated_from": sqlite_path},
        )
        try:
            params = json.loads(t.get("params_json") or "{}")
        except json.JSONDecodeError:
            params = {}
        storage_pg.save_trial(
            dsn,
            study_name=study_name,
            optuna_trial_num=int(t["trial_number"]),
            state=str(t["state"]),
            objective=t.get("objective"),
            params=params if isinstance(params, dict) else {},
            started_at=_utc_to_pg(t.get("started_at")),
            finished_at=_utc_to_pg(t.get("finished_at")),
        )

    print()
    print("=" * 72)
    print("Migration complete")
    print(f"  Runs migrated   : {len(new_runs_ids)}")
    print(f"  Trials migrated : {len(sqlite_trials)}")
    print(f"  Events bytes    : {total_bytes}")
    print("=" * 72)
    return 0


def run_migration(
    dsn: str,
    from_sqlite: str = "klines.db",
    data_root: str = "data",
    dry_run: bool = False,
) -> int:
    """Importable entry point used by `backtest_cli.py migrate`.

    Wraps the private `_migrate(...)` so callers do not depend on the
    underscore-prefixed name. The standalone CLI in `main()` continues to
    work unchanged.
    """
    _ensure_on_path()
    return _migrate(
        dsn=dsn or "",
        sqlite_path=from_sqlite,
        data_root=data_root,
        dry_run=bool(dry_run),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.getenv("PG_DSN"))
    parser.add_argument("--from-sqlite", dest="from_sqlite", default="klines.db")
    parser.add_argument("--data-root", dest="data_root", default="data")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    args = parser.parse_args()

    if not args.dsn and not args.dry_run:
        print("ERROR: --dsn not provided and PG_DSN env is unset", file=sys.stderr)
        return 1

    try:
        return run_migration(
            dsn=args.dsn or "",
            from_sqlite=args.from_sqlite,
            data_root=args.data_root,
            dry_run=bool(args.dry_run),
        )
    except Exception as exc:
        print(f"ERROR: migration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
