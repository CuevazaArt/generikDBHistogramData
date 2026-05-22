"""Numerical-parity harness for the Rust core vs the pure-Python engine.

Contract guaranteed:

* For the same inputs, ``genericbt_core.run_backtest(...)`` (Rust path or
  Python fallback) returns a :class:`backtest.engine.BacktestResult` whose
  ``metrics`` dict matches the reference :func:`backtest.engine.run_backtest`
  to at least 12 significant digits for all canonical metric keys.
* The pure-Python self-consistency check (running the reference engine
  twice on the same inputs) always runs. It guarantees that the harness
  itself is deterministic, so any future Rust-vs-Python diff is squarely
  the Rust side's fault.
* The Rust-vs-Python comparison is gated on
  ``genericbt_core.is_rust_available()`` — skipped cleanly when the wheel
  was not built (e.g. dev laptops without a Rust toolchain).

The synthetic candle series is a sine wave around a slow uptrend (~5000
bars). The ``SmaCrossStrategy(fast=10, slow=30)`` produces a healthy
number of crossings on this shape so every branch of the engine runs.
"""
from __future__ import annotations

import math
import os
import random
import sys
from typing import Any, Dict, List

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


# The metric keys we audit for parity. These are exactly the keys returned
# by :func:`backtest.metrics.summarize_metrics`.
_CANONICAL_METRICS = (
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

# 12 significant digits ~ relative tolerance of 1e-12. We allow a slightly
# looser absolute floor for tiny values (e.g. profit_factor when num_trades
# is small).
_REL_TOL = 1e-12
_ABS_TOL = 1e-9


def _try_numpy_candles(n: int) -> List[Dict[str, Any]]:
    """Sine-wave-over-trend candles via numpy when available."""

    import numpy as np  # type: ignore[import-not-found]

    rng = np.random.default_rng(seed=42)
    base_ts = 1_700_000_000_000
    step_ms = 1_000
    i = np.arange(n, dtype=np.float64)
    trend = 100.0 + 0.01 * i
    wave = 5.0 * np.sin(2 * np.pi * i / 250.0)
    noise = rng.normal(loc=0.0, scale=0.05, size=n)
    close = trend + wave + noise
    high = close + np.abs(rng.normal(loc=0.0, scale=0.05, size=n))
    low = close - np.abs(rng.normal(loc=0.0, scale=0.05, size=n))
    open_ = np.concatenate(([close[0]], close[:-1]))
    out: List[Dict[str, Any]] = []
    for k in range(n):
        out.append(
            {
                "symbol": "TESTUSDT",
                "interval": "1s",
                "open_time": int(base_ts + k * step_ms),
                "open": float(open_[k]),
                "high": float(high[k]),
                "low": float(low[k]),
                "close": float(close[k]),
                "volume": 1.0,
                "close_time": int(base_ts + k * step_ms + step_ms - 1),
                "quote_asset_volume": float(close[k]),
                "num_trades": 1,
                "taker_buy_base": 0.0,
                "taker_buy_quote": 0.0,
                "ignore_field": None,
            }
        )
    return out


def _pure_python_candles(n: int) -> List[Dict[str, Any]]:
    """Fallback used when numpy is unavailable. Deterministic via Random(42)."""

    rng = random.Random(42)
    base_ts = 1_700_000_000_000
    step_ms = 1_000
    out: List[Dict[str, Any]] = []
    prev_close = 100.0
    for k in range(n):
        trend = 100.0 + 0.01 * k
        wave = 5.0 * math.sin(2.0 * math.pi * k / 250.0)
        noise = rng.gauss(0.0, 0.05)
        close = trend + wave + noise
        high = close + abs(rng.gauss(0.0, 0.05))
        low = close - abs(rng.gauss(0.0, 0.05))
        out.append(
            {
                "symbol": "TESTUSDT",
                "interval": "1s",
                "open_time": int(base_ts + k * step_ms),
                "open": float(prev_close),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": 1.0,
                "close_time": int(base_ts + k * step_ms + step_ms - 1),
                "quote_asset_volume": float(close),
                "num_trades": 1,
                "taker_buy_base": 0.0,
                "taker_buy_quote": 0.0,
                "ignore_field": None,
            }
        )
        prev_close = close
    return out


def _build_candles(n: int = 5000) -> List[Dict[str, Any]]:
    """Try numpy first; otherwise fall back to a deterministic ``random`` series."""

    try:
        return _try_numpy_candles(n)
    except Exception:
        return _pure_python_candles(n)


def _build_engine_config():
    from genericbt_core import EngineConfig

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


def _approx_equal(actual: float, expected: float) -> bool:
    if math.isnan(actual) and math.isnan(expected):
        return True
    if math.isinf(actual) or math.isinf(expected):
        return actual == expected
    return math.isclose(actual, expected, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)


def _assert_metrics_equal(left: Dict[str, float], right: Dict[str, float]) -> None:
    """Assert every canonical metric matches within tolerance."""

    diffs: List[str] = []
    for key in _CANONICAL_METRICS:
        if key not in left or key not in right:
            diffs.append(f"{key}: missing (left={key in left}, right={key in right})")
            continue
        lv = float(left[key])
        rv = float(right[key])
        if not _approx_equal(lv, rv):
            diffs.append(f"{key}: left={lv!r} right={rv!r} diff={lv - rv!r}")
    assert not diffs, "metric drift:\n  " + "\n  ".join(diffs)


def test_python_fallback_self_consistency() -> None:
    """The reference engine must be deterministic given identical inputs.

    Runs ``backtest.engine.run_backtest`` twice on a fresh copy of the same
    synthetic series and asserts the canonical metrics match exactly. This
    is always-on (no Rust dependency) and catches regressions in either
    the reference engine or the harness itself.
    """

    import copy

    from backtest.engine import run_backtest as py_run_backtest
    from backtest.strategies import SmaCrossStrategy

    candles_a = _build_candles(5000)
    candles_b = copy.deepcopy(candles_a)

    result_a = py_run_backtest(
        config=_build_engine_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=candles_a,
    )
    result_b = py_run_backtest(
        config=_build_engine_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=candles_b,
    )
    _assert_metrics_equal(result_a.metrics, result_b.metrics)

    # Also sanity-check that the strategy actually traded on this series;
    # otherwise the parity assertion is vacuous.
    assert result_a.metrics["num_trades"] >= 1.0


def test_python_fallback_via_shim_matches_reference() -> None:
    """``genericbt_core.run_backtest`` in fallback mode == reference engine."""

    import copy

    import genericbt_core
    from backtest.engine import run_backtest as py_run_backtest
    from backtest.strategies import SmaCrossStrategy

    candles_ref = _build_candles(5000)
    candles_shim = copy.deepcopy(candles_ref)

    ref = py_run_backtest(
        config=_build_engine_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=candles_ref,
    )
    # Force the Python branch even if the wheel happens to be built — this
    # test pins the shim's fallback to the reference engine exactly.
    os.environ["BACKTEST_ENGINE_KIND"] = "python"
    via_shim = genericbt_core.run_backtest(
        config=_build_engine_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=candles_shim,
    )

    _assert_metrics_equal(ref.metrics, via_shim.metrics)


@pytest.fixture
def _rust_available() -> bool:
    import genericbt_core

    return genericbt_core.is_rust_available()


def _is_rust_available() -> bool:
    import genericbt_core

    return genericbt_core.is_rust_available()


@pytest.mark.skipif(not _is_rust_available(), reason="Rust core not built")
def test_rust_vs_python_metric_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rust engine output must equal the Python engine on the same inputs."""

    import copy

    import genericbt_core
    from backtest.engine import run_backtest as py_run_backtest
    from backtest.strategies import SmaCrossStrategy

    candles_ref = _build_candles(5000)
    candles_rs = copy.deepcopy(candles_ref)

    ref = py_run_backtest(
        config=_build_engine_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=candles_ref,
    )
    monkeypatch.setenv("BACKTEST_ENGINE_KIND", "rust")
    rs = genericbt_core.run_backtest(
        config=_build_engine_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=candles_rs,
    )
    _assert_metrics_equal(ref.metrics, rs.metrics)
