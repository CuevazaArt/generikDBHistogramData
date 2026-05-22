"""PostgreSQL-backed implementation of the storage surface.

This module mirrors the public API of `backtest.storage` (the legacy SQLite
layer) but persists metadata into PostgreSQL via psycopg 3 and the bulk
event/equity payloads into Parquet under `data/<bucket>/run_<id>/` (via
`storage_paths`). Connections come from a lazily-instantiated
`psycopg_pool.ConnectionPool`.

Notes:
    - psycopg 3 accepts plain `postgresql://...` DSNs but not the SQLAlchemy
      `postgresql+psycopg://...` form. `_normalise_dsn` strips that prefix.
    - All write paths use the `tmp_then_rename` context manager so partial
      writes are never visible to readers.
    - Reads use `psycopg.rows.dict_row` to match the dict-shaped responses
      consumed by the legacy code.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import psycopg  # type: ignore[import-not-found]
from psycopg.rows import dict_row  # type: ignore[import-not-found]
from psycopg_pool import ConnectionPool  # type: ignore[import-not-found]

from backtest.idempotency import compute_run_key
from backtest.storage_paths import StoragePaths, tmp_then_rename


_DSN_PREFIX = "postgresql+psycopg://"


def _normalise_dsn(dsn: str) -> str:
    if dsn.startswith(_DSN_PREFIX):
        return "postgresql://" + dsn[len(_DSN_PREFIX):]
    return dsn


# --- Connection management ------------------------------------------------

_POOLS: Dict[str, ConnectionPool] = {}
_POOL_LOCK = Lock()


def _get_pool(dsn: str) -> ConnectionPool:
    raw = _normalise_dsn(dsn)
    with _POOL_LOCK:
        pool = _POOLS.get(raw)
        if pool is None:
            pool = ConnectionPool(
                conninfo=raw,
                min_size=0,
                max_size=8,
                kwargs={"row_factory": dict_row},
                open=True,
            )
            _POOLS[raw] = pool
    return pool


def close_all_pools() -> None:
    """Drain every cached pool. Mostly useful from tests/teardown."""
    with _POOL_LOCK:
        for pool in list(_POOLS.values()):
            try:
                pool.close()
            except Exception:
                pass
        _POOLS.clear()


def connect(dsn: str, autocommit: bool = False) -> "psycopg.Connection":
    """Return a standalone psycopg connection (not pooled).

    Useful for the migration runner and for ad-hoc maintenance scripts where
    a transient connection is preferable to drawing from the pool.
    """
    return psycopg.connect(_normalise_dsn(dsn), autocommit=autocommit, row_factory=dict_row)


@contextmanager
def transaction(dsn: str) -> Iterator["psycopg.Connection"]:
    """Yield a pooled connection inside an explicit transaction."""
    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.transaction():
            yield conn


# --- Event Parquet schema -------------------------------------------------

try:
    import pyarrow as pa  # type: ignore[import-not-found]
    import pyarrow.parquet as pq  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - hard requirement at runtime
    pa = None  # type: ignore[assignment]
    pq = None  # type: ignore[assignment]


def _events_schema():
    if pa is None:
        raise RuntimeError("pyarrow is required for storage_pg.persist_run_events")
    return pa.schema(
        [
            ("trial_id", pa.int64()),
            ("seq", pa.int64()),
            ("event_time", pa.int64()),
            ("event_type", pa.string()),
            ("side", pa.string()),
            ("price", pa.float64()),
            ("qty", pa.float64()),
            ("cash", pa.float64()),
            ("equity", pa.float64()),
            ("position_qty", pa.float64()),
            ("payload_json", pa.string()),
        ]
    )


def _event_to_dict(e: Dict[str, Any]) -> Dict[str, Any]:
    payload = e.get("payload")
    payload_json: Optional[str]
    if payload is not None and not isinstance(payload, str):
        payload_json = json.dumps(payload, ensure_ascii=False)
    else:
        raw_str = e.get("payload_json")
        if isinstance(raw_str, str):
            payload_json = raw_str
        elif isinstance(payload, str):
            payload_json = payload
        else:
            payload_json = None
    return {
        "trial_id": int(e["trial_id"]) if e.get("trial_id") is not None else None,
        "seq": int(e["seq"]),
        "event_time": int(e["event_time"]) if e.get("event_time") is not None else None,
        "event_type": str(e["event_type"]),
        "side": e.get("side"),
        "price": float(e["price"]) if e.get("price") is not None else None,
        "qty": float(e["qty"]) if e.get("qty") is not None else None,
        "cash": float(e["cash"]) if e.get("cash") is not None else None,
        "equity": float(e["equity"]) if e.get("equity") is not None else None,
        "position_qty": float(e["position_qty"]) if e.get("position_qty") is not None else None,
        "payload_json": payload_json,
    }


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --- Runs -----------------------------------------------------------------

def create_run(
    dsn: str,
    *,
    strategy: str,
    symbol: str,
    interval: str,
    start_ts: Optional[int],
    end_ts: Optional[int],
    initial_cash: float,
    fee_rate: float,
    slippage_bps: float,
    config: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    engine_kind: str = "python",
    engine_version: str = "0.0.0",
    strategy_params: Optional[Dict[str, Any]] = None,
    host_info: Optional[Dict[str, Any]] = None,
    storage_paths: Optional[StoragePaths] = None,
) -> int:
    """Idempotently create (or recover) a row in `meta.runs`.

    If `idempotency_key` is None it is derived from the canonical run inputs.
    When a row with the same key already exists, its `run_id` is returned and
    no INSERT is issued.
    """
    if idempotency_key is None:
        idempotency_key = compute_run_key(
            strategy=strategy,
            symbol=symbol,
            interval=interval,
            start_ts=start_ts,
            end_ts=end_ts,
            initial_cash=initial_cash,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            strategy_params=strategy_params or {},
            engine_kind=engine_kind,
            engine_version=engine_version,
        )

    config_json = json.dumps(config or {}, ensure_ascii=False)
    host_json = json.dumps(host_info or {}, ensure_ascii=False)

    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT run_id FROM meta.runs WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                row = cur.fetchone()
                if row is not None:
                    return int(row["run_id"])

                cur.execute(
                    """
                    INSERT INTO meta.runs (
                        idempotency_key, strategy, symbol, interval,
                        start_ts, end_ts, initial_cash, fee_rate, slippage_bps,
                        config, host_info, status, engine_kind, started_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s, %s, %s
                    )
                    RETURNING run_id
                    """,
                    (
                        idempotency_key,
                        strategy,
                        symbol,
                        interval,
                        int(start_ts) if start_ts is not None else None,
                        int(end_ts) if end_ts is not None else None,
                        float(initial_cash),
                        float(fee_rate),
                        float(slippage_bps),
                        config_json,
                        host_json,
                        "running",
                        engine_kind,
                        _now_utc(),
                    ),
                )
                created = cur.fetchone()
                run_id = int(created["run_id"])

    if storage_paths is not None:
        storage_paths.ensure_run_layout(run_id)
    return run_id


def finish_run(
    dsn: str,
    run_id: int,
    status: str = "completed",
    *,
    events_parquet: Optional[str] = None,
    equity_parquet: Optional[str] = None,
    checkpoints_dir: Optional[str] = None,
) -> None:
    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE meta.runs
                    SET status = %s,
                        finished_at = %s,
                        events_parquet = COALESCE(%s, events_parquet),
                        equity_parquet = COALESCE(%s, equity_parquet),
                        checkpoints_dir = COALESCE(%s, checkpoints_dir)
                    WHERE run_id = %s
                    """,
                    (status, _now_utc(), events_parquet, equity_parquet, checkpoints_dir, int(run_id)),
                )


def persist_run_events(
    dsn: str,
    run_id: int,
    events: Iterable[Dict[str, Any]],
    *,
    storage_paths: Optional[StoragePaths] = None,
    seq: int = 0,
    batch_size: int = 50_000,
) -> Optional[str]:
    """Append events to Parquet at events_part(run_id, seq) and record the path."""
    if pa is None or pq is None:
        raise RuntimeError("pyarrow is required for storage_pg.persist_run_events")

    paths = storage_paths or StoragePaths()
    paths.ensure_run_layout(run_id)
    target = paths.events_part(run_id, seq)

    schema = _events_schema()
    buffer: List[Dict[str, Any]] = []
    wrote_any = False
    with tmp_then_rename(target) as tmp_path:
        writer = pq.ParquetWriter(tmp_path, schema, compression="zstd")
        try:
            for e in events:
                buffer.append(_event_to_dict(e))
                if len(buffer) >= batch_size:
                    writer.write_table(pa.Table.from_pylist(buffer, schema=schema))
                    buffer.clear()
                    wrote_any = True
            if buffer:
                writer.write_table(pa.Table.from_pylist(buffer, schema=schema))
                wrote_any = True
        finally:
            writer.close()

    relative = os.path.relpath(target, paths.data_root).replace("\\", "/")
    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE meta.runs SET events_parquet = %s WHERE run_id = %s",
                    (relative, int(run_id)),
                )
    return target if wrote_any else None


def persist_run_metrics(
    dsn: str,
    run_id: int,
    metrics: Dict[str, float],
    trial_id: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    rows: List[Tuple[int, str, Optional[float]]] = []
    for name, value in (metrics or {}).items():
        if value is None:
            rows.append((int(run_id), str(name), None))
        else:
            try:
                rows.append((int(run_id), str(name), float(value)))
            except (TypeError, ValueError):
                continue
    if not rows:
        return

    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO meta.run_metrics (run_id, name, value)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (run_id, name) DO UPDATE SET value = EXCLUDED.value
                    """,
                    rows,
                )


def get_bt_run_metrics(dsn: str, run_id: int) -> Dict[str, float]:
    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, value FROM meta.run_metrics WHERE run_id = %s",
                (int(run_id),),
            )
            return {row["name"]: float(row["value"]) for row in cur.fetchall() if row["value"] is not None}


def get_bt_recent_events(
    dsn: str,
    run_id: int,
    *,
    limit: int = 30,
    storage_paths: Optional[StoragePaths] = None,
) -> List[Dict[str, Any]]:
    """Tail the per-run Parquet event file. Returns up to `limit` newest events.

    Events are persisted to Parquet (see `persist_run_events`); reading the
    full file and slicing is the simplest correct option at this stage. Once
    runs are in the millions of events range, callers should switch to DuckDB
    (Fase 5).
    """
    if pa is None or pq is None:
        raise RuntimeError("pyarrow is required for storage_pg.get_bt_recent_events")
    paths = storage_paths or StoragePaths()
    # The events file path is stored in meta.runs; fall back to seq=0 if missing.
    pool = _get_pool(dsn)
    target: Optional[str] = None
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT events_parquet FROM meta.runs WHERE run_id = %s",
                (int(run_id),),
            )
            row = cur.fetchone()
            if row and row.get("events_parquet"):
                rel = row["events_parquet"]
                target = os.path.join(paths.data_root, rel)
    if target is None:
        target = paths.events_part(run_id, 0)
    if not os.path.exists(target):
        return []
    table = pq.read_table(target)
    py = table.to_pylist()
    return py[-int(limit):]


def list_runs(dsn: str, limit: int = 20) -> List[Dict[str, Any]]:
    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_id, strategy, symbol, interval, status,
                       started_at, finished_at, engine_kind
                FROM meta.runs
                ORDER BY run_id DESC
                LIMIT %s
                """,
                (int(limit),),
            )
            return list(cur.fetchall())


def run_descriptor(dsn: str, run_id: int) -> Optional[Dict[str, Any]]:
    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_id, strategy, symbol, interval, start_ts, end_ts,
                       initial_cash, fee_rate, slippage_bps, status, engine_kind,
                       started_at, finished_at, events_parquet, equity_parquet,
                       checkpoints_dir, last_checkpoint, config, host_info
                FROM meta.runs WHERE run_id = %s
                """,
                (int(run_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def run_equity_curve(
    dsn: str,
    run_id: int,
    *,
    storage_paths: Optional[StoragePaths] = None,
) -> List[Tuple[int, Optional[int], float]]:
    if pa is None or pq is None:
        raise RuntimeError("pyarrow is required for storage_pg.run_equity_curve")
    paths = storage_paths or StoragePaths()
    target = paths.events_part(run_id, 0)
    descriptor = run_descriptor(dsn, run_id)
    if descriptor and descriptor.get("events_parquet"):
        target = os.path.join(paths.data_root, descriptor["events_parquet"])
    if not os.path.exists(target):
        return []
    table = pq.read_table(target, columns=["seq", "event_time", "equity"])
    out: List[Tuple[int, Optional[int], float]] = []
    for seq, ts, eq in zip(
        table.column("seq").to_pylist(),
        table.column("event_time").to_pylist(),
        table.column("equity").to_pylist(),
    ):
        if eq is None:
            continue
        out.append((int(seq), int(ts) if ts is not None else None, float(eq)))
    return out


def run_events(
    dsn: str,
    run_id: int,
    *,
    storage_paths: Optional[StoragePaths] = None,
) -> List[Dict[str, Any]]:
    if pa is None or pq is None:
        raise RuntimeError("pyarrow is required for storage_pg.run_events")
    paths = storage_paths or StoragePaths()
    target = paths.events_part(run_id, 0)
    descriptor = run_descriptor(dsn, run_id)
    if descriptor and descriptor.get("events_parquet"):
        target = os.path.join(paths.data_root, descriptor["events_parquet"])
    if not os.path.exists(target):
        return []
    table = pq.read_table(target)
    return table.to_pylist()


def run_signal_events(
    dsn: str,
    run_id: int,
    *,
    storage_paths: Optional[StoragePaths] = None,
) -> List[Dict[str, Any]]:
    events = run_events(dsn, run_id, storage_paths=storage_paths)
    return [e for e in events if e.get("side") in ("buy", "sell")]


# --- Studies / trials -----------------------------------------------------

def ensure_study(
    dsn: str,
    *,
    study_name: str,
    strategy: str,
    base_config: Optional[Dict[str, Any]] = None,
    objective_metric: str = "objective",
    direction: str = "maximize",
    sampler: str = "tpe",
    seed: Optional[int] = None,
) -> None:
    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO meta.studies (
                        study_name, strategy, base_config, objective_metric,
                        direction, sampler, seed
                    )
                    VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT (study_name) DO NOTHING
                    """,
                    (
                        study_name,
                        strategy,
                        json.dumps(base_config or {}, ensure_ascii=False),
                        objective_metric,
                        direction,
                        sampler,
                        int(seed) if seed is not None else None,
                    ),
                )


def save_trial(
    dsn: str,
    *,
    study_name: str,
    optuna_trial_num: int,
    state: str,
    objective: Optional[float],
    params: Dict[str, Any],
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    run_id: Optional[int] = None,
) -> int:
    params_json = json.dumps(params or {}, ensure_ascii=False)
    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO meta.trial_runs (
                        study_name, optuna_trial_num, run_id, params,
                        objective_value, state, started_at, finished_at
                    )
                    VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT (study_name, optuna_trial_num) DO UPDATE SET
                        run_id = COALESCE(EXCLUDED.run_id, meta.trial_runs.run_id),
                        params = EXCLUDED.params,
                        objective_value = EXCLUDED.objective_value,
                        state = EXCLUDED.state,
                        started_at = COALESCE(EXCLUDED.started_at, meta.trial_runs.started_at),
                        finished_at = COALESCE(EXCLUDED.finished_at, meta.trial_runs.finished_at)
                    RETURNING trial_id
                    """,
                    (
                        study_name,
                        int(optuna_trial_num),
                        int(run_id) if run_id is not None else None,
                        params_json,
                        float(objective) if objective is not None else None,
                        state,
                        started_at,
                        finished_at,
                    ),
                )
                row = cur.fetchone()
                return int(row["trial_id"])


def save_trial_metrics(dsn: str, trial_id: int, metrics: Dict[str, float]) -> None:
    rows: List[Tuple[int, str, Optional[float]]] = []
    for name, value in (metrics or {}).items():
        if value is None:
            rows.append((int(trial_id), str(name), None))
        else:
            try:
                rows.append((int(trial_id), str(name), float(value)))
            except (TypeError, ValueError):
                continue
    if not rows:
        return
    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO meta.trial_metrics (trial_id, name, value)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (trial_id, name) DO UPDATE SET value = EXCLUDED.value
                    """,
                    rows,
                )


def list_top_bt_trials(dsn: str, study_name: str, limit: int = 10) -> List[Dict[str, Any]]:
    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trial_id, optuna_trial_num, state, objective_value, params,
                       started_at, finished_at, run_id
                FROM meta.trial_runs
                WHERE study_name = %s AND objective_value IS NOT NULL
                ORDER BY objective_value DESC NULLS LAST
                LIMIT %s
                """,
                (study_name, int(limit)),
            )
            return list(cur.fetchall())


def trial_objectives(dsn: str, study_name: str, limit: int = 500) -> List[Tuple[int, float]]:
    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT optuna_trial_num, objective_value
                FROM meta.trial_runs
                WHERE study_name = %s AND objective_value IS NOT NULL
                ORDER BY optuna_trial_num ASC
                LIMIT %s
                """,
                (study_name, int(limit)),
            )
            return [(int(r["optuna_trial_num"]), float(r["objective_value"])) for r in cur.fetchall()]


def study_trials(dsn: str, study_name: str, limit: int = 10_000) -> List[Dict[str, Any]]:
    pool = _get_pool(dsn)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trial_id, optuna_trial_num, state, objective_value, params,
                       started_at, finished_at, run_id
                FROM meta.trial_runs
                WHERE study_name = %s
                ORDER BY optuna_trial_num ASC
                LIMIT %s
                """,
                (study_name, int(limit)),
            )
            return list(cur.fetchall())


__all__ = [
    "connect",
    "transaction",
    "close_all_pools",
    "create_run",
    "finish_run",
    "persist_run_events",
    "persist_run_metrics",
    "get_bt_run_metrics",
    "get_bt_recent_events",
    "list_runs",
    "run_descriptor",
    "run_equity_curve",
    "run_events",
    "run_signal_events",
    "ensure_study",
    "save_trial",
    "save_trial_metrics",
    "list_top_bt_trials",
    "trial_objectives",
    "study_trials",
]
