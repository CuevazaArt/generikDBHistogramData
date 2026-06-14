"""Tests for the Fase 2 Arrow-batch streaming entry points.

These cover both the low-level iterator (`iter_candles_arrow_batches`) and
the engine wrapper (`run_backtest_streaming`) that consumes it.

When pyarrow is unavailable on the host the parquet test is skipped, but
the in-memory wrapper test stays active because it constructs the iterator
manually.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Iterator, List

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from backtest.data_feed import iter_candles_arrow_batches
from backtest.engine import EngineConfig, run_backtest, run_backtest_streaming
from backtest.strategies import SmaCrossStrategy
from tests.test_engine_rs_parity import _build_candles


def _pyarrow_available() -> bool:
    try:
        import pyarrow.parquet
    except ImportError:
        return False
    return True


def _base_config() -> EngineConfig:
    return EngineConfig(
        db_path=":memory:",
        symbol="TESTUSDT",
        interval="1s",
        initial_cash=10_000.0,
        fee_rate=0.001,
        slippage_bps=2.0,
        sma_fast=10,
        sma_slow=30,
        ema_period=20,
        rsi_period=14,
        atr_period=14,
        events_mode="minimal",
    )


@pytest.mark.skipif(not _pyarrow_available(), reason="pyarrow not installed")
def test_iter_candles_arrow_batches_from_lake() -> None:
    """The iterator must yield non-empty batches from the BTCUSDT 1h lake.

    The Phase 0 migration drop already produced the partition layout at
    ``data/klines/symbol=BTCUSDT/interval=1h/year=2024/month=01/part-000.parquet``;
    we read it back and verify the basic shape contract.
    """
    parquet_root = os.path.join(ROOT_DIR, "data", "klines")
    if not os.path.isdir(parquet_root):
        pytest.skip("kline lake not materialised on this host")

    batches: List[List[Dict[str, Any]]] = list(
        iter_candles_arrow_batches(
            parquet_root=parquet_root,
            symbol="BTCUSDT",
            interval="1h",
            batch_size=200,
        )
    )
    assert batches, "expected at least one batch"
    total_rows = sum(len(b) for b in batches)
    # We don't pin an exact count (the data file may evolve) but require a
    # plausible lower bound consistent with the manifest's 8784 / year rows.
    assert total_rows >= 200
    sample = batches[0][0]
    for required in ("open_time", "open", "high", "low", "close"):
        assert required in sample, f"missing field {required!r} in candle dict"


@pytest.mark.skipif(not _pyarrow_available(), reason="pyarrow not installed")
def test_iter_candles_arrow_batches_filter_window() -> None:
    """Start/end filters must be honoured at batch granularity."""
    parquet_root = os.path.join(ROOT_DIR, "data", "klines")
    if not os.path.isdir(parquet_root):
        pytest.skip("kline lake not materialised on this host")

    # Pull a known-good first row to derive a tight window.
    first = next(
        iter_candles_arrow_batches(
            parquet_root=parquet_root,
            symbol="BTCUSDT",
            interval="1h",
            batch_size=8,
        )
    )
    assert first
    first_ts = int(first[0]["open_time"])
    window_end = first_ts + 5 * 60 * 60 * 1000  # 5 hours later

    filtered = list(
        iter_candles_arrow_batches(
            parquet_root=parquet_root,
            symbol="BTCUSDT",
            interval="1h",
            start_ts=first_ts,
            end_ts=window_end,
            batch_size=2,
        )
    )
    rows = [row for batch in filtered for row in batch]
    assert rows, "filtered window returned no rows"
    for row in rows:
        ts = int(row["open_time"])
        assert first_ts <= ts <= window_end


def test_streaming_matches_in_memory() -> None:
    """`run_backtest_streaming` must produce byte-identical metrics."""
    candles_a = _build_candles(2000)
    candles_b = [dict(c) for c in candles_a]

    reference = run_backtest(
        config=_base_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=candles_a,
    )

    def _batch_iter(rows: List[Dict[str, Any]], size: int = 500) -> Iterator[List[Dict[str, Any]]]:
        for i in range(0, len(rows), size):
            yield rows[i : i + size]

    streamed = run_backtest_streaming(
        config=_base_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candle_batch_iter=_batch_iter(candles_b, size=500),
    )

    canonical = (
        "initial_cash",
        "final_equity",
        "total_return",
        "max_drawdown",
        "sharpe",
        "sortino",
        "calmar",
        "ulcer_index",
        "win_rate",
        "profit_factor",
        "num_trades",
    )
    for key in canonical:
        assert reference.metrics[key] == streamed.metrics[key], (
            f"metric drift on {key}: ref={reference.metrics[key]!r} "
            f"streamed={streamed.metrics[key]!r}"
        )
    assert reference.events == streamed.events
    assert reference.equity_curve == streamed.equity_curve


def test_streaming_handles_empty_iterator() -> None:
    """Empty iterator must not error and must produce trivially valid metrics."""
    result = run_backtest_streaming(
        config=_base_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candle_batch_iter=iter(()),
    )
    assert result.metrics["num_trades"] == 0.0
    assert result.equity_curve == []
