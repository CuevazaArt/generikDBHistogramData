"""Tests for backtest.duckdb_reads.

The suite is hermetic: every fixture writes Parquet artefacts into the
pytest ``tmp_path`` directory, never under the repo's ``data/`` tree.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, List, Mapping

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import duckdb_reads
from backtest.storage_paths import StoragePaths, tmp_then_rename


def _write_equity_parquet(target: str, rows: Iterable[Mapping[str, object]]) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    schema = pa.schema(
        [
            ("seq", pa.int64()),
            ("event_time", pa.int64()),
            ("equity", pa.float64()),
        ]
    )
    os.makedirs(os.path.dirname(target), exist_ok=True)
    table = pa.Table.from_pylist(list(rows), schema=schema)
    with tmp_then_rename(target) as tmp:
        pq.write_table(table, tmp, compression="zstd")


def _write_events_parquet(target: str, rows: Iterable[Mapping[str, object]]) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    schema = pa.schema(
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
    os.makedirs(os.path.dirname(target), exist_ok=True)
    table = pa.Table.from_pylist(list(rows), schema=schema)
    with tmp_then_rename(target) as tmp:
        pq.write_table(table, tmp, compression="zstd")


def test_is_available() -> None:
    assert duckdb_reads.is_available() is True


def test_no_parquet_files_returns_empty(tmp_path: Path) -> None:
    root = str(tmp_path)
    assert duckdb_reads.has_equity_parquet(99999, data_root=root) is False
    assert duckdb_reads.has_events_parquet(99999, data_root=root) is False
    assert duckdb_reads.equity_curve_from_parquet(99999, data_root=root) == []
    assert duckdb_reads.signal_events_from_parquet(99999, data_root=root) == []
    assert duckdb_reads.run_events_from_parquet(99999, data_root=root) == []
    assert duckdb_reads.monthly_returns_aggregate(99999, data_root=root) == []


def test_open_connection_yields_usable_handle() -> None:
    with duckdb_reads.open_connection() as conn:
        rows = conn.execute("SELECT 1").fetchall()
        assert rows == [(1,)]


def test_synthetic_equity_roundtrip(tmp_path: Path) -> None:
    paths = StoragePaths(data_root=str(tmp_path))
    target = paths.equity_file(42)
    rows: List[Mapping[str, object]] = [
        {"seq": 1, "event_time": 1_700_000_000_000, "equity": 1000.0},
        {"seq": 2, "event_time": 1_700_000_001_000, "equity": 1010.0},
        {"seq": 3, "event_time": 1_700_000_002_000, "equity": 1005.0},
        {"seq": 4, "event_time": 1_700_000_003_000, "equity": 1050.0},
        {"seq": 5, "event_time": 1_700_000_004_000, "equity": 1100.0},
    ]
    _write_equity_parquet(target, rows)

    assert duckdb_reads.has_equity_parquet(42, data_root=str(tmp_path)) is True

    out = duckdb_reads.equity_curve_from_parquet(42, data_root=str(tmp_path))
    assert len(out) == 5
    assert out[0] == (1, 1_700_000_000_000, 1000.0)
    assert out[-1] == (5, 1_700_000_004_000, 1100.0)
    # Ordering is strictly by seq ASC.
    assert [r[0] for r in out] == sorted(r[0] for r in out)


def test_monthly_returns_aggregate(tmp_path: Path) -> None:
    paths = StoragePaths(data_root=str(tmp_path))
    target = paths.equity_file(42)
    base = 1_704_067_200_000  # 2024-01-01T00:00:00 UTC
    rows: List[Mapping[str, object]] = [
        {"seq": 1, "event_time": base + 0, "equity": 1000.0},
        {"seq": 2, "event_time": base + 1_000, "equity": 1010.0},
        {"seq": 3, "event_time": base + 2_000, "equity": 1100.0},
    ]
    _write_equity_parquet(target, rows)

    out = duckdb_reads.monthly_returns_aggregate(42, data_root=str(tmp_path))
    assert len(out) == 1
    entry = out[0]
    assert str(entry["month"]).startswith("2024-01")
    assert entry["pnl"] == pytest.approx(100.0)
    assert entry["return_pct"] == pytest.approx(0.1)


def test_equity_curve_falls_back_to_events(tmp_path: Path) -> None:
    paths = StoragePaths(data_root=str(tmp_path))
    paths.ensure_run_layout(7)
    events_target = paths.events_part(7, 0)
    rows: List[Mapping[str, object]] = [
        {
            "trial_id": None,
            "seq": 1,
            "event_time": 1_700_000_000_000,
            "event_type": "bar",
            "side": None,
            "price": 1.0,
            "qty": 0.0,
            "cash": 1000.0,
            "equity": 1000.0,
            "position_qty": 0.0,
            "payload_json": None,
        },
        {
            "trial_id": None,
            "seq": 2,
            "event_time": 1_700_000_001_000,
            "event_type": "fill",
            "side": "buy",
            "price": 1.0,
            "qty": 10.0,
            "cash": 990.0,
            "equity": 1000.0,
            "position_qty": 10.0,
            "payload_json": None,
        },
        {
            "trial_id": None,
            "seq": 3,
            "event_time": 1_700_000_002_000,
            "event_type": "fill",
            "side": "sell",
            "price": 1.1,
            "qty": 10.0,
            "cash": 1001.0,
            "equity": 1011.0,
            "position_qty": 0.0,
            "payload_json": None,
        },
    ]
    _write_events_parquet(events_target, rows)

    # No equity.parquet was written; only events. Backend should still
    # service equity_curve_from_parquet from the events file.
    assert not os.path.exists(paths.equity_file(7))
    assert duckdb_reads.has_equity_parquet(7, data_root=str(tmp_path)) is True

    eq = duckdb_reads.equity_curve_from_parquet(7, data_root=str(tmp_path))
    assert [r[0] for r in eq] == [1, 2, 3]
    assert [r[2] for r in eq] == [1000.0, 1000.0, 1011.0]

    signals = duckdb_reads.signal_events_from_parquet(7, data_root=str(tmp_path))
    assert [r[3] for r in signals] == ["buy", "sell"]
    assert [r[2] for r in signals] == ["fill", "fill"]

    full = duckdb_reads.run_events_from_parquet(7, data_root=str(tmp_path))
    assert len(full) == 3
    # (seq, event_time, event_type, side, cash, equity, payload_json)
    assert full[0][0] == 1 and full[0][2] == "bar"
    assert full[1][4] == 990.0
    assert full[2][5] == 1011.0


def test_trial_objectives_returns_none_without_parquet(tmp_path: Path) -> None:
    # No studies/<name>/trials.parquet exists; Optuna still lives in the DB.
    out = duckdb_reads.trial_objectives_from_parquet("missing_study", data_root=str(tmp_path))
    assert out is None
