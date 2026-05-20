"""Tests for backtest metric calculations."""
import math

from backtest.metrics import (
    calmar_ratio,
    max_drawdown,
    profit_factor,
    sortino_ratio,
    summarize_metrics,
    ulcer_index,
    win_rate,
)


def test_max_drawdown_basic():
    equity = [100.0, 110.0, 90.0, 95.0, 120.0]
    # Peak 110 -> trough 90 -> dd = (110-90)/110
    assert math.isclose(max_drawdown(equity), (110.0 - 90.0) / 110.0)


def test_max_drawdown_empty():
    assert max_drawdown([]) == 0.0


def test_win_rate_and_profit_factor():
    pnls = [10.0, -5.0, 4.0, -2.0]
    assert math.isclose(win_rate(pnls), 0.5)
    # gross_profit = 14, gross_loss = 7
    assert math.isclose(profit_factor(pnls), 14.0 / 7.0)


def test_profit_factor_no_losses_returns_high_constant():
    assert profit_factor([1.0, 2.0, 3.0]) == 999.0
    assert profit_factor([]) == 0.0


def test_calmar_uses_total_return_and_mdd():
    # 20% return with 10% drawdown -> calmar 2.0
    assert math.isclose(calmar_ratio(0.20, 0.10), 2.0)


def test_calmar_with_zero_drawdown():
    assert calmar_ratio(0.0, 0.0) == 0.0
    assert calmar_ratio(0.10, 0.0) == 999.0


def test_sortino_is_zero_on_constant_curve():
    assert sortino_ratio([100.0] * 10) == 0.0


def test_sortino_positive_when_only_upside():
    equity = [100.0, 102.0, 105.0, 108.0]
    # No downside -> mean > 0 -> returns 999.0 constant
    assert sortino_ratio(equity) == 999.0


def test_ulcer_index_nonnegative_and_zero_for_monotone_up():
    monotone_up = [100.0, 105.0, 110.0, 120.0]
    assert math.isclose(ulcer_index(monotone_up), 0.0)
    drawdown_curve = [100.0, 110.0, 95.0, 100.0]
    val = ulcer_index(drawdown_curve)
    assert val > 0.0


def test_summarize_metrics_contains_new_keys():
    equity = [100.0, 110.0, 90.0, 95.0, 120.0]
    pnls = [10.0, -5.0]
    out = summarize_metrics(initial_cash=100.0, final_equity=120.0, equity_curve=equity, trade_pnls=pnls)
    expected_keys = {
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
    }
    assert expected_keys.issubset(out.keys())
    assert math.isclose(out["total_return"], 0.20)
    assert out["num_trades"] == 2.0
