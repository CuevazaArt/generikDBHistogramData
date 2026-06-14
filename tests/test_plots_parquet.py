"""Tests for the DuckDB-backed resolver and dashboard glue in backtest.plots."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, List, Mapping

import pytest

# Use the non-interactive Agg backend before plots.py imports pyplot. Tk is
# unavailable on this CI/dev host and any plt.figure() call would otherwise
# crash.
os.environ.setdefault("MPLBACKEND", "Agg")
try:
    import matplotlib

    matplotlib.use("Agg", force=True)
except ImportError:  # pragma: no cover - matplotlib is optional for ci skip
    pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import plots
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


def test_resolve_equity_rows_pass_through() -> None:
    rows: List[tuple] = [
        (1, 1_700_000_000_000, 100.0),
        (2, 1_700_000_001_000, 110.0),
    ]
    # An obviously bogus data_root proves the resolver short-circuits when
    # the caller has already populated rows.
    out = plots._resolve_equity_rows(rows, run_id=42, data_root="path/does/not/exist")
    assert out == rows


def test_resolve_equity_rows_duckdb(tmp_path: Path) -> None:
    paths = StoragePaths(data_root=str(tmp_path))
    target = paths.equity_file(7)
    rows: List[Mapping[str, object]] = [
        {"seq": 1, "event_time": 1_700_000_000_000, "equity": 1000.0},
        {"seq": 2, "event_time": 1_700_000_001_000, "equity": 1100.0},
    ]
    _write_equity_parquet(target, rows)

    out = plots._resolve_equity_rows([], run_id=7, data_root=str(tmp_path))
    assert len(out) == 2
    assert out[0] == (1, 1_700_000_000_000, 1000.0)
    assert out[1] == (2, 1_700_000_001_000, 1100.0)


def test_resolve_equity_rows_empty(tmp_path: Path) -> None:
    # No artefacts under tmp_path -> empty result.
    out = plots._resolve_equity_rows([], run_id=99999, data_root=str(tmp_path))
    assert out == []
    # No run_id -> also empty, no exception.
    out = plots._resolve_equity_rows([], run_id=None, data_root=str(tmp_path))
    assert out == []
    # None for rows is treated as empty.
    out = plots._resolve_equity_rows(None, run_id=99999, data_root=str(tmp_path))
    assert out == []


def test_select_backend_picks_duckdb_when_parquet_exists(tmp_path: Path) -> None:
    paths = StoragePaths(data_root=str(tmp_path))
    target = paths.equity_file(11)
    _write_equity_parquet(
        target,
        [{"seq": 1, "event_time": 1_700_000_000_000, "equity": 1000.0}],
    )
    chosen = plots._select_backend(11, str(tmp_path), db_path=None)
    assert chosen == "duckdb"


def test_select_backend_falls_back_to_sqlite_without_parquet(tmp_path: Path) -> None:
    chosen = plots._select_backend(99999, str(tmp_path), db_path="ignored.db")
    assert chosen == "sqlite"


def _build_run_artefacts(tmp_path: Path, run_id: int) -> None:
    """Lay down equity + events Parquet so render_run_dashboard can run."""
    paths = StoragePaths(data_root=str(tmp_path))
    paths.ensure_run_layout(run_id)

    base = 1_704_067_200_000  # 2024-01-01T00:00:00 UTC
    eq_rows = [
        {"seq": i, "event_time": base + i * 86_400_000, "equity": 1000.0 + i * 5}
        for i in range(1, 8)
    ]
    _write_equity_parquet(paths.equity_file(run_id), eq_rows)

    event_rows = [
        {
            "trial_id": None,
            "seq": 1,
            "event_time": base + 86_400_000,
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
            "seq": 2,
            "event_time": base + 2 * 86_400_000,
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
    _write_events_parquet(paths.events_part(run_id, 0), event_rows)


def test_render_run_dashboard_duckdb_auto(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    run_id = 21
    _build_run_artefacts(tmp_path, run_id)

    out_dir = tmp_path / "reports"
    artefacts = plots.render_run_dashboard(
        output_dir=str(out_dir),
        run_id=run_id,
        data_root=str(tmp_path),
        backend="auto",
    )
    assert "equity" in artefacts
    assert "drawdown" in artefacts
    assert "integrated_report" in artefacts
    # Filename conventions are preserved across backends.
    assert os.path.basename(artefacts["equity"]) == f"run_{run_id}_equity.png"
    assert os.path.basename(artefacts["drawdown"]) == f"run_{run_id}_drawdown.png"
    assert os.path.basename(artefacts["integrated_report"]).endswith(
        f"run_{run_id}_integrated_report.md"
    )
    for path in artefacts.values():
        assert os.path.exists(path)


def test_render_run_dashboard_sqlite_requires_db_path(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        plots.render_run_dashboard(
            output_dir=str(tmp_path / "out"),
            run_id=42,
            backend="sqlite",
            db_path=None,
        )


def test_plot_equity_via_resolver_matches_inline_rows(tmp_path: Path) -> None:
    """The legacy in-memory path and the DuckDB resolver path produce the same PNG names."""
    pytest.importorskip("matplotlib")
    run_id = 33
    _build_run_artefacts(tmp_path, run_id)

    out_legacy = tmp_path / "legacy"
    out_duck = tmp_path / "duck"
    rows = [(i, 1_704_067_200_000 + i * 86_400_000, 1000.0 + i * 5) for i in range(1, 8)]
    legacy = plots.plot_equity_and_drawdown(rows, output_dir=str(out_legacy), run_id=run_id)
    # Empty rows -> the resolver kicks in and reads Parquet from data_root="data"
    # by default. Force the same effect by writing artefacts at the default
    # location? No: we instead exercise the resolver directly to confirm both
    # paths yield identical artefact key sets and file basenames.
    resolved = plots._resolve_equity_rows([], run_id=run_id, data_root=str(tmp_path))
    duck = plots.plot_equity_and_drawdown(resolved, output_dir=str(out_duck), run_id=run_id)
    assert sorted(legacy.keys()) == sorted(duck.keys())
    for key in legacy:
        assert os.path.basename(legacy[key]) == os.path.basename(duck[key])
