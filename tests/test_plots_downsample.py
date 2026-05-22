"""Tests for the LTTB equity downsampler in `backtest.plots`."""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

os.environ.setdefault("MPLBACKEND", "Agg")
try:
    import matplotlib  # noqa: E402

    matplotlib.use("Agg", force=True)
except ImportError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.plots import _downsample_equity_rows  # noqa: E402


def _linear_rows(n: int) -> List[Tuple[int, int, float]]:
    """A monotonically increasing series; LTTB picks endpoints + interior shape."""
    return [(i, 1_700_000_000_000 + i * 1_000, float(i)) for i in range(n)]


def _rows_with_spike(n: int, spike_idx: int, spike_value: float) -> List[Tuple[int, int, float]]:
    rows: List[Tuple[int, int, float]] = []
    for i in range(n):
        eq = math.sin(i / 100.0) + 1.0
        if i == spike_idx:
            eq = spike_value
        rows.append((i, 1_700_000_000_000 + i * 1_000, float(eq)))
    return rows


def test_downsample_pass_through_when_small() -> None:
    rows = _linear_rows(50)
    assert _downsample_equity_rows(rows, max_points=100) == rows


def test_downsample_keeps_endpoints() -> None:
    rows = _linear_rows(10_000)
    result = _downsample_equity_rows(rows, max_points=500)
    assert len(result) == 500
    assert result[0] == rows[0]
    assert result[-1] == rows[-1]


def test_downsample_preserves_extrema() -> None:
    rows = _rows_with_spike(10_000, spike_idx=5_000, spike_value=99.0)
    result = _downsample_equity_rows(rows, max_points=100)
    equities = [r[2] for r in result]
    assert any(math.isclose(eq, 99.0) for eq in equities), (
        "LTTB should retain the spike sample because its triangle area dominates "
        "every other candidate in its bucket"
    )


def test_downsample_deterministic() -> None:
    rows = _rows_with_spike(2_000, spike_idx=937, spike_value=42.0)
    a = _downsample_equity_rows(rows, max_points=200)
    b = _downsample_equity_rows(rows, max_points=200)
    assert a == b


def test_downsample_extreme_two_points() -> None:
    rows = _linear_rows(1_000)
    result = _downsample_equity_rows(rows, max_points=2)
    assert result == [rows[0], rows[-1]]


def test_downsample_equal_size_returns_original() -> None:
    rows = _linear_rows(123)
    assert _downsample_equity_rows(rows, max_points=123) == rows


def test_plot_equity_accepts_max_plot_points_kwarg(tmp_path: Path) -> None:
    """Smoke test the integration: large series + max_plot_points produces files."""
    pytest.importorskip("matplotlib")
    from backtest.plots import plot_equity_and_drawdown

    rows = _rows_with_spike(15_000, spike_idx=7_500, spike_value=12.5)
    out = plot_equity_and_drawdown(
        rows,
        output_dir=str(tmp_path),
        run_id=999,
        max_plot_points=500,
    )
    assert "equity" in out and os.path.exists(out["equity"])
    assert "drawdown" in out and os.path.exists(out["drawdown"])
