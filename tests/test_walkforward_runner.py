"""Fase 4 walk-forward runner tests.

The serial fold test seeds a tiny synthetic SQLite kline store under
``tmp_path`` so the assertions never depend on real Binance dumps being
present on disk.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import Tuple

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from backtest.aggregator import aggregate_walk_forward_metrics
from backtest.engine import EngineConfig
from backtest.walkforward_runner import (
    WalkForwardConfig,
    WalkForwardWindow,
    build_windows,
    run_walk_forward,
)


_MS_PER_DAY = 86_400_000
_BASE_TS = 1_704_067_200_000  # 2024-01-01T00:00:00Z


def _seed_synthetic_klines(
    db_path: str, symbol: str, *, rows: int = 720, seed_offset: int = 0
) -> Tuple[int, int]:
    """Seed a synthetic 1h kline series; return (first_open_time, last_open_time)."""
    import db as legacy_db  # repo-root SQLite helper

    legacy_db.init_db(db_path)
    insert_rows = []
    for i in range(rows):
        open_t = _BASE_TS + i * 3_600_000
        # Two interfering sinusoids so SmaCrossStrategy gets golden/death crosses.
        base = 50_000.0 + 200.0 * math.sin((i + seed_offset) / 17.0)
        op = base
        cl = base + 25.0 * math.cos((i + seed_offset) / 11.0)
        hi = max(op, cl) + 15.0
        lo = min(op, cl) - 15.0
        vol = 10.0
        ct = open_t + 3_600_000 - 1
        qv = vol * (op + cl) / 2.0
        nt = 100
        tb = vol * 0.55
        tq = qv * 0.55
        insert_rows.append((open_t, op, hi, lo, cl, vol, ct, qv, nt, tb, tq, "0"))
    legacy_db.insert_klines(db_path, symbol, "1h", insert_rows)
    return _BASE_TS, _BASE_TS + (rows - 1) * 3_600_000


def test_build_windows_basic() -> None:
    """365-day full range, 90/30/30 windows -> 9 folds, first train at full_start."""
    cfg = WalkForwardConfig(
        full_start_ts=0,
        full_end_ts=365 * _MS_PER_DAY,
        train_window_ms=90 * _MS_PER_DAY,
        test_window_ms=30 * _MS_PER_DAY,
        step_ms=30 * _MS_PER_DAY,
        anchored=False,
    )
    windows = build_windows(cfg)
    assert len(windows) == 9
    first = windows[0]
    assert first.train_start_ts == 0
    assert first.train_end_ts == 90 * _MS_PER_DAY
    assert first.test_start_ts == first.train_end_ts
    assert first.test_end_ts == 120 * _MS_PER_DAY
    last = windows[-1]
    assert last.test_end_ts <= 365 * _MS_PER_DAY
    # Each fold has exactly the same span in rolling mode.
    for w in windows:
        assert w.train_end_ts - w.train_start_ts == 90 * _MS_PER_DAY
        assert w.test_end_ts - w.test_start_ts == 30 * _MS_PER_DAY


def test_build_windows_anchored() -> None:
    """Anchored mode pins every fold's train_start_ts to full_start_ts (expanding window)."""
    cfg = WalkForwardConfig(
        full_start_ts=10_000,
        full_end_ts=10_000 + 365 * _MS_PER_DAY,
        train_window_ms=90 * _MS_PER_DAY,
        test_window_ms=30 * _MS_PER_DAY,
        step_ms=30 * _MS_PER_DAY,
        anchored=True,
    )
    windows = build_windows(cfg)
    assert windows, "expected at least one fold"
    for w in windows:
        assert w.train_start_ts == 10_000
    spans = [w.train_end_ts - w.train_start_ts for w in windows]
    # Expanding: each subsequent fold has a strictly larger train window.
    assert spans == sorted(spans)
    assert spans[0] == 90 * _MS_PER_DAY
    assert spans[-1] > spans[0]


def test_run_walk_forward_serial(tmp_path: Path) -> None:
    db_path = str(tmp_path / "walkforward.db")
    first_ts, last_ts = _seed_synthetic_klines(db_path, "XRPUSDT", rows=720)

    train_window_ms = 10 * _MS_PER_DAY
    test_window_ms = 5 * _MS_PER_DAY
    step_ms = 5 * _MS_PER_DAY
    cfg = WalkForwardConfig(
        full_start_ts=first_ts,
        full_end_ts=last_ts + 3_600_000,
        train_window_ms=train_window_ms,
        test_window_ms=test_window_ms,
        step_ms=step_ms,
        anchored=False,
    )
    engine_cfg = EngineConfig(
        db_path=db_path,
        symbol="XRPUSDT",
        interval="1h",
        initial_cash=10_000.0,
        fee_rate=0.001,
        slippage_bps=2.0,
        events_mode="minimal",
        sma_fast=10,
        sma_slow=30,
    )

    result = run_walk_forward(
        cfg=cfg,
        strategy_name="sma_cross",
        strategy_params={"fast": 10, "slow": 30},
        engine_config=engine_cfg,
        db_path=db_path,
    )

    assert len(result.windows) >= 2
    assert len(result.fold_results) == len(result.windows)
    expected_keys = {
        "n_folds",
        "train_mean_total_return",
        "test_mean_total_return",
        "train_test_correlation_total_return",
        "test_mean_sharpe",
        "test_median_sharpe",
        "test_worst_total_return",
        "test_best_total_return",
        "decay_test_vs_train_pct",
        "per_fold_summary",
    }
    assert expected_keys.issubset(result.aggregated.keys())
    assert result.aggregated["n_folds"] == len(result.fold_results)
    assert len(result.aggregated["per_fold_summary"]) == len(result.fold_results)

    for fold in result.fold_results:
        assert isinstance(fold["train_run_id"], int) and fold["train_run_id"] > 0
        assert isinstance(fold["test_run_id"], int) and fold["test_run_id"] > 0
        # Metrics dict is non-empty and carries the canonical keys our aggregator reads.
        train_metrics = fold["train_metrics"]
        test_metrics = fold["test_metrics"]
        for required in ("total_return", "sharpe", "final_equity", "initial_cash"):
            assert required in train_metrics
            assert required in test_metrics


def test_decay_metric_signs() -> None:
    """decay_test_vs_train_pct is positive when test underperforms train."""
    fold_results_overfit = [
        {
            "fold_index": 0,
            "train_metrics": {"total_return": 0.20, "sharpe": 1.5},
            "test_metrics": {"total_return": 0.05, "sharpe": 0.8},
        },
        {
            "fold_index": 1,
            "train_metrics": {"total_return": 0.30, "sharpe": 1.8},
            "test_metrics": {"total_return": 0.10, "sharpe": 0.9},
        },
    ]
    aggregated = aggregate_walk_forward_metrics(fold_results_overfit)
    assert aggregated["decay_test_vs_train_pct"] > 0.0

    fold_results_inverse = [
        {
            "fold_index": 0,
            "train_metrics": {"total_return": 0.05, "sharpe": 0.5},
            "test_metrics": {"total_return": 0.20, "sharpe": 1.6},
        },
        {
            "fold_index": 1,
            "train_metrics": {"total_return": 0.10, "sharpe": 0.7},
            "test_metrics": {"total_return": 0.30, "sharpe": 1.9},
        },
    ]
    aggregated_inv = aggregate_walk_forward_metrics(fold_results_inverse)
    assert aggregated_inv["decay_test_vs_train_pct"] < 0.0
    assert aggregated_inv["test_best_total_return"] >= aggregated_inv["test_worst_total_return"]


def test_walk_forward_window_dataclass_is_frozen() -> None:
    w = WalkForwardWindow(
        train_start_ts=1, train_end_ts=2, test_start_ts=2, test_end_ts=3, fold_index=0
    )
    with pytest.raises(Exception):
        w.fold_index = 99  # type: ignore[misc]
