"""Tests for the strategy registry and CLI parameter helpers."""
import argparse

import pytest

from backtest.registry import get_strategy, list_strategy_names, params_from_cli, suggest_params
from backtest.strategies import (
    AntiLouiseLuckyStrategy,
    AntiLouiseStrategy,
    DorothyHubStrategy,
    LouiseLuckyStrategy,
    LouiseStrategy,
    MashaStrategy,
)
from backtest.strategy_base import StrategyContext


def test_listed_strategies_contain_core_bots():
    available = list_strategy_names(include_aliases=False)
    for name in (
        "dorothy",
        "elphaba",
        "ha_trend",
        "sma_cross",
        "masha",
        "louise",
        "louise_lucky",
        "anti_louise",
        "anti_louise_lucky",
        "thusnelda",
    ):
        assert name in available, f"missing strategy {name} in registry"


def test_get_strategy_returns_correct_classes():
    assert get_strategy("masha") is MashaStrategy
    assert get_strategy("louise") is LouiseStrategy
    assert get_strategy("louise_lucky") is LouiseLuckyStrategy
    assert get_strategy("anti_louise") is AntiLouiseStrategy
    assert get_strategy("anti_louise_lucky") is AntiLouiseLuckyStrategy


def test_get_strategy_rejects_unknown():
    with pytest.raises(ValueError):
        get_strategy("does_not_exist")


def test_params_from_cli_masha_contains_required_keys():
    ns = argparse.Namespace(
        fast=10, slow=30, quote_order_qty_usdt=8.0,
        take_profit_pct=1.5, stop_loss_pct=4.0, pullback_factor=0.006,
    )
    out = params_from_cli(ns, "masha")
    expected = {"fast", "slow", "quote_order_qty_usdt", "take_profit_pct", "stop_loss_pct", "pullback_factor"}
    assert expected.issubset(out.keys())


def test_params_from_cli_louise_lucky_includes_window():
    ns = argparse.Namespace(
        target_profit_pct=1.5, margin_drop_factor=0.004,
        quote_order_qty_usdt=8.0, lucky_window=24,
    )
    out = params_from_cli(ns, "louise_lucky")
    assert "lucky_window" in out
    assert out["lucky_window"] == 24


def test_params_from_cli_dorothy_excludes_notional_fields():
    ns = argparse.Namespace(
        profit_factor=0.05,
        margin_drop_factor=0.004,
        quote_order_qty_usdt=8.0,
        max_rungs=12,
        min_order_notional=6.0,
        max_order_notional=10.0,
        max_active_orders=200,
    )
    out = params_from_cli(ns, "dorothy")
    assert out["max_rungs"] == 12
    assert "min_order_notional" not in out
    assert "max_order_notional" not in out
    assert "max_active_orders" not in out


def test_suggest_params_dorothy_supports_discrete_profit_factor_step():
    class TrialStub:
        def suggest_categorical(self, _name, choices):
            return choices[-1]

        def suggest_int(self, _name, low, high):
            return high

    params = suggest_params(
        TrialStub(),
        "dorothy",
        search_overrides={
            "profit_factor_min": 0.01,
            "profit_factor_max": 0.06,
            "profit_factor_step": 0.02,
            "margin_drop_factor": 0.0005,
            "max_rungs_min": 7,
            "max_rungs_max": 7,
        },
    )
    assert params["profit_factor"] == pytest.approx(0.06)
    assert params["margin_drop_factor"] == pytest.approx(0.0005)
    assert params["max_rungs"] == 7


def test_dorothy_can_restore_internal_state():
    strategy = DorothyHubStrategy(profit_factor=0.03, margin_drop_factor=0.0005, quote_order_qty_usdt=8.0, max_rungs=10)
    strategy.import_state({"active_sell_limits": [1.2, 1.0, -3.0]})
    out = strategy.export_state()
    assert out["active_sell_limits"] == [1.0, 1.2]


def test_dorothy_buy_size_pct_can_be_below_one_percent():
    strategy = DorothyHubStrategy(
        profit_factor=0.02,
        margin_drop_factor=0.0005,
        quote_order_qty_usdt=8.0,
        max_rungs=10,
        symbol="XRPUSDT",
    )
    candle = {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "price_source": 1.0, "pec_trend": "BULLISH"}
    ctx = StrategyContext(
        index=0,
        candle=candle,
        candles=[candle],
        cash=5000.0,
        position_qty=0.0,
        avg_entry=0.0,
        equity=5000.0,
    )

    signal = strategy.on_bar(ctx)
    assert signal.action == "buy"
    assert 0.0 < signal.size_pct < 0.01
