"""Tests for the ``genericbt_core`` Python shim.

These tests are the contract the sibling CLI relies on:

* ``import genericbt_core`` must always succeed, even when the Rust wheel
  is absent (e.g. dev laptops without a Rust toolchain).
* ``genericbt_core.is_rust_available()`` must always return a ``bool``.
* ``genericbt_core.run_backtest`` must transparently fall through to the
  pure-Python engine when either the wheel is missing or
  ``BACKTEST_ENGINE_KIND`` is set to ``"python"``.
* When the wheel IS built (``is_rust_available() is True``), setting
  ``BACKTEST_ENGINE_KIND=rust`` must produce a ``BacktestResult`` with the
  same dict shape as the Python engine. (Numerical equality is asserted in
  ``test_engine_rs_parity.py``.)
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


def test_import_does_not_fail() -> None:
    """``import genericbt_core`` must work with or without the Rust wheel."""

    import genericbt_core  # noqa: F401

    assert hasattr(genericbt_core, "run_backtest")
    assert hasattr(genericbt_core, "is_rust_available")
    assert hasattr(genericbt_core, "EngineConfig")
    assert hasattr(genericbt_core, "BacktestResult")


def test_is_rust_available_returns_bool() -> None:
    import genericbt_core

    value = genericbt_core.is_rust_available()
    assert isinstance(value, bool)


def _synthetic_candles(n: int = 400) -> List[Dict[str, Any]]:
    """Deterministic monotonically-increasing candle series.

    Kept tiny and explicit (no numpy dependency) so the shim test runs in
    well under a second even on cold CI workers. The SmaCrossStrategy will
    fire a handful of crosses on this shape, exercising every branch in
    the engine (fills, rejects, holds, on_finish).
    """

    out: List[Dict[str, Any]] = []
    base_ts = 1_700_000_000_000  # ms
    step_ms = 60_000
    for i in range(n):
        # Triangular wave around a slow uptrend — guaranteed crossovers.
        trend = 100.0 + 0.05 * i
        wave = 5.0 * (1.0 if (i // 20) % 2 == 0 else -1.0)
        close = trend + wave
        out.append(
            {
                "symbol": "TESTUSDT",
                "interval": "1m",
                "open_time": base_ts + i * step_ms,
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 1.0,
                "close_time": base_ts + i * step_ms + (step_ms - 1),
                "quote_asset_volume": close,
                "num_trades": 1,
                "taker_buy_base": 0.0,
                "taker_buy_quote": 0.0,
                "ignore_field": None,
            }
        )
    return out


def _engine_config():
    """Build a minimal ``EngineConfig`` that bypasses DB loading."""

    from genericbt_core import EngineConfig

    return EngineConfig(
        db_path=":memory:",
        symbol="TESTUSDT",
        interval="1m",
        initial_cash=10_000.0,
        fee_rate=0.001,
        slippage_bps=2.0,
        sma_fast=5,
        sma_slow=15,
        ema_period=10,
        rsi_period=14,
        atr_period=14,
        events_mode="minimal",
    )


def test_run_backtest_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pure-Python path must work even when the wheel is absent."""

    monkeypatch.setenv("BACKTEST_ENGINE_KIND", "python")

    import genericbt_core
    from backtest.strategies import SmaCrossStrategy

    result = genericbt_core.run_backtest(
        config=_engine_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 5, "slow": 15},
        candles=_synthetic_candles(),
    )

    assert isinstance(result, genericbt_core.BacktestResult)
    metrics = result.metrics
    # Sanity: these are the metrics the rest of the system reads.
    for key in (
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
    ):
        assert key in metrics, f"missing metric key: {key}"
        assert isinstance(metrics[key], float)
    assert metrics["initial_cash"] == pytest.approx(10_000.0)
    # Equity curve length must be <= candle count (loop_seconds may skip).
    assert len(result.equity_curve) == len(_synthetic_candles())


def test_run_backtest_defaults_to_python_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``BACKTEST_ENGINE_KIND`` is not set, the shim must NOT crash.

    The selection rule is "rust only if wheel present AND env=='rust'", so
    leaving the env var unset must dispatch to the pure-Python engine.
    """

    monkeypatch.delenv("BACKTEST_ENGINE_KIND", raising=False)

    import genericbt_core
    from backtest.strategies import SmaCrossStrategy

    result = genericbt_core.run_backtest(
        config=_engine_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 5, "slow": 15},
        candles=_synthetic_candles(200),
    )
    assert isinstance(result, genericbt_core.BacktestResult)
    assert "final_equity" in result.metrics


def test_run_backtest_python_path_when_wheel_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when ``BACKTEST_ENGINE_KIND=rust`` but the wheel is missing,
    the shim must NOT raise — it silently falls back to Python so the
    sibling CLI doesn't have to special-case dev installs.
    """

    monkeypatch.setenv("BACKTEST_ENGINE_KIND", "rust")

    import genericbt_core
    from backtest.strategies import SmaCrossStrategy

    if genericbt_core.is_rust_available():
        pytest.skip("Rust wheel is built; this test only exercises the no-wheel branch")

    result = genericbt_core.run_backtest(
        config=_engine_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 5, "slow": 15},
        candles=_synthetic_candles(200),
    )
    assert isinstance(result, genericbt_core.BacktestResult)


@pytest.mark.skipif(
    True,
    reason="Activated dynamically below via skipif on is_rust_available()",
)
def _placeholder() -> None:  # pragma: no cover - guard so the symbol exists
    pass


def _rust_available() -> bool:
    import genericbt_core

    return genericbt_core.is_rust_available()


@pytest.mark.skipif(not _rust_available(), reason="Rust core not built")
def test_run_backtest_rust_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the wheel IS built, the rust path must run and return the same
    dict shape. Numerical parity lives in ``test_engine_rs_parity.py``.
    """

    monkeypatch.setenv("BACKTEST_ENGINE_KIND", "rust")

    import genericbt_core
    from backtest.strategies import SmaCrossStrategy

    result = genericbt_core.run_backtest(
        config=_engine_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 5, "slow": 15},
        candles=_synthetic_candles(),
    )
    assert isinstance(result, genericbt_core.BacktestResult)
    for key in ("initial_cash", "final_equity", "total_return", "num_trades"):
        assert key in result.metrics
