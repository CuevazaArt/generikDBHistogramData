"""DuckDB-backed read helpers for backtest report artefacts.

This module wraps DuckDB queries over the Parquet data lake produced by
phases 0 and 2 of the redesign:

* ``data/events/run_<id>/part-*.parquet`` (append-only event stream)
* ``data/equity/run_<id>/equity.parquet`` (compact equity curve)

The public surface is intentionally small and mirrors the row shapes that
``backtest.storage`` returns when reading the legacy SQLite database, so
``backtest.plots`` and ``backtest.sweet_spot_report`` can swap backends
without diverging the output format.

``duckdb`` is imported lazily inside every helper to keep this module
free of side effects. Callers should call :func:`is_available` (or
:func:`has_equity_parquet`) before assuming the queries can run.
"""
from __future__ import annotations

import glob
import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple


# --- Capability probing -------------------------------------------------

def is_available() -> bool:
    """Return ``True`` iff ``import duckdb`` works in this interpreter."""
    try:
        import duckdb
    except ImportError:
        return False
    return True


@contextmanager
def open_connection() -> Iterator[Any]:
    """Yield an in-memory DuckDB connection. No file persistence."""
    import duckdb

    conn = duckdb.connect(database=":memory:")
    try:
        yield conn
    finally:
        conn.close()


# --- Path math ----------------------------------------------------------

def _events_dir(run_id: int, data_root: str) -> str:
    return os.path.join(data_root, "events", f"run_{int(run_id)}")


def _events_glob(run_id: int, data_root: str) -> str:
    return os.path.join(_events_dir(run_id, data_root), "part-*.parquet")


def _events_files(run_id: int, data_root: str) -> List[str]:
    return sorted(glob.glob(_events_glob(run_id, data_root)))


def _equity_file(run_id: int, data_root: str) -> str:
    return os.path.join(data_root, "equity", f"run_{int(run_id)}", "equity.parquet")


def _trials_file(study_name: str, data_root: str) -> str:
    return os.path.join(data_root, "studies", str(study_name), "trials.parquet")


# --- Presence helpers ---------------------------------------------------

def has_events_parquet(run_id: int, data_root: str = "data") -> bool:
    """``True`` iff at least one ``part-*.parquet`` exists for the run."""
    return len(_events_files(run_id, data_root)) > 0


def has_equity_parquet(run_id: int, data_root: str = "data") -> bool:
    """``True`` iff the equity curve can be served from Parquet.

    Includes the dedicated ``equity.parquet`` file and the fallback case
    where only the events Parquet exists (DuckDB will derive the curve).
    """
    if os.path.exists(_equity_file(run_id, data_root)):
        return True
    return has_events_parquet(run_id, data_root)


# --- Per-run readers ----------------------------------------------------

def equity_curve_from_parquet(
    run_id: int, data_root: str = "data"
) -> List[Tuple[int, int | None, float]]:
    """Equity curve as ``[(seq, event_time, equity), ...]`` ordered by seq.

    Matches the shape of ``backtest.storage.run_equity_curve``. Prefers
    ``data/equity/run_<id>/equity.parquet`` and falls back to the events
    Parquet when the dedicated file is missing.
    """
    if not is_available():
        return []

    equity_path = _equity_file(run_id, data_root)
    if os.path.exists(equity_path):
        source = equity_path
    else:
        if not has_events_parquet(run_id, data_root):
            return []
        source = _events_glob(run_id, data_root)

    query = """
        SELECT seq, event_time, equity
        FROM read_parquet(?)
        WHERE equity IS NOT NULL
        ORDER BY seq ASC
    """
    with open_connection() as conn:
        rows = conn.execute(query, [source]).fetchall()

    return [
        (
            int(seq),
            int(event_time) if event_time is not None else None,
            float(equity),
        )
        for seq, event_time, equity in rows
    ]


def signal_events_from_parquet(
    run_id: int, data_root: str = "data"
) -> List[Tuple[int, int | None, str | None, str | None, float | None, float | None, str | None]]:
    """Signal events shaped like ``backtest.storage.run_signal_events``.

    Returns ``[(seq, event_time, event_type, side, price, qty, payload_json)]``
    filtered to rows where ``side`` is ``buy`` or ``sell``.
    """
    if not is_available() or not has_events_parquet(run_id, data_root):
        return []

    query = """
        SELECT seq, event_time, event_type, side, price, qty, payload_json
        FROM read_parquet(?)
        WHERE side IN ('buy', 'sell')
        ORDER BY seq ASC
    """
    with open_connection() as conn:
        rows = conn.execute(query, [_events_glob(run_id, data_root)]).fetchall()

    out: List[Tuple] = []
    for seq, ts, etype, side, price, qty, payload in rows:
        out.append(
            (
                int(seq),
                int(ts) if ts is not None else None,
                str(etype) if etype is not None else None,
                str(side) if side is not None else None,
                float(price) if price is not None else None,
                float(qty) if qty is not None else None,
                payload,
            )
        )
    return out


def run_events_from_parquet(
    run_id: int, data_root: str = "data"
) -> List[Tuple[int, int | None, str | None, str | None, float | None, float | None, str | None]]:
    """Full event stream shaped like ``backtest.storage.run_events``.

    Returns ``[(seq, event_time, event_type, side, cash, equity, payload_json)]``
    ordered by seq ASC.
    """
    if not is_available() or not has_events_parquet(run_id, data_root):
        return []

    query = """
        SELECT seq, event_time, event_type, side, cash, equity, payload_json
        FROM read_parquet(?)
        ORDER BY seq ASC
    """
    with open_connection() as conn:
        rows = conn.execute(query, [_events_glob(run_id, data_root)]).fetchall()

    out: List[Tuple] = []
    for seq, ts, etype, side, cash, equity, payload in rows:
        out.append(
            (
                int(seq),
                int(ts) if ts is not None else None,
                str(etype) if etype is not None else None,
                str(side) if side is not None else None,
                float(cash) if cash is not None else None,
                float(equity) if equity is not None else None,
                payload,
            )
        )
    return out


def trial_objectives_from_parquet(
    study_name: str, data_root: str = "data"
) -> List[Tuple[int, float]] | None:
    """Return ``[(trial_number, objective), ...]`` if a Parquet store exists.

    Optuna trial state lives in PostgreSQL or SQLite, not Parquet. The hook
    returns ``None`` until a future ``data/studies/<name>/trials.parquet``
    artefact is produced; callers should fall back to the metadata DB.
    """
    target = _trials_file(study_name, data_root)
    if not os.path.exists(target) or not is_available():
        return None

    query = """
        SELECT trial_number, objective
        FROM read_parquet(?)
        WHERE objective IS NOT NULL
        ORDER BY trial_number ASC
    """
    with open_connection() as conn:
        rows = conn.execute(query, [target]).fetchall()
    return [(int(n), float(obj)) for n, obj in rows]


# --- Aggregations -------------------------------------------------------

def monthly_returns_aggregate(
    run_id: int, data_root: str = "data"
) -> List[Dict[str, Any]]:
    """Push the monthly-return reduction down to DuckDB.

    Equivalent (but much faster on large runs) to the Python loop in
    :func:`backtest.plots.plot_monthly_return_heatmap`. Each entry has
    ``month`` (``YYYY-MM``), ``pnl`` (``last - first``) and ``return_pct``
    (``(last - first) / first``).
    """
    if not is_available():
        return []

    equity_path = _equity_file(run_id, data_root)
    if os.path.exists(equity_path):
        source = equity_path
    elif has_events_parquet(run_id, data_root):
        source = _events_glob(run_id, data_root)
    else:
        return []

    # DuckDB's first()/last() respect the order of the input. The CTE forces
    # a deterministic ordering by seq before the aggregation runs.
    # ``make_timestamp(microseconds)`` returns a naive TIMESTAMP, sidestepping
    # the session-timezone drift that ``to_timestamp`` (TIMESTAMPTZ) suffers.
    query = """
        WITH samples AS (
            SELECT seq, event_time, equity
            FROM read_parquet(?)
            WHERE equity IS NOT NULL AND event_time IS NOT NULL
            ORDER BY seq ASC
        )
        SELECT
            strftime(date_trunc('month', make_timestamp(event_time * 1000)), '%Y-%m') AS month,
            last(equity) - first(equity) AS pnl,
            CASE
                WHEN first(equity) = 0 THEN 0.0
                ELSE (last(equity) - first(equity)) / first(equity)
            END AS return_pct
        FROM samples
        GROUP BY 1
        ORDER BY 1
    """
    with open_connection() as conn:
        rows = conn.execute(query, [source]).fetchall()

    return [
        {
            "month": str(month),
            "pnl": float(pnl) if pnl is not None else 0.0,
            "return_pct": float(ret) if ret is not None else 0.0,
        }
        for month, pnl, ret in rows
    ]


__all__ = [
    "equity_curve_from_parquet",
    "has_equity_parquet",
    "has_events_parquet",
    "is_available",
    "monthly_returns_aggregate",
    "open_connection",
    "run_events_from_parquet",
    "signal_events_from_parquet",
    "trial_objectives_from_parquet",
]
