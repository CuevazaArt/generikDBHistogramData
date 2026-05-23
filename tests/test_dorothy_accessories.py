"""Tests for Dorothy sizing accessories."""
from decimal import Decimal

import pytest

from backtest.dorothy_accessories import volumen_compuesto_factor, volumen_compuesto_notional
from backtest.strategies import DorothyHubStrategy
from backtest.strategy_base import StrategyContext


def test_volumen_compuesto_factor_examples():
    assert volumen_compuesto_factor(Decimal("1000"), Decimal("1000")) == Decimal("1")
    assert volumen_compuesto_factor(Decimal("1100"), Decimal("1000")) == Decimal("1.1")
    assert volumen_compuesto_factor(Decimal("900"), Decimal("1000")) == Decimal("0.9")


def test_volumen_compuesto_notional_scales_and_floors_at_six():
    notional, factor = volumen_compuesto_notional(
        base_quote_usdt=Decimal("8"),
        equity=Decimal("1100"),
        initial_equity=Decimal("1000"),
        min_quote_usdt=Decimal("6"),
    )
    assert factor == Decimal("1.1")
    assert notional == Decimal("8.8")

    notional, factor = volumen_compuesto_notional(
        base_quote_usdt=Decimal("8"),
        equity=Decimal("900"),
        initial_equity=Decimal("1000"),
        min_quote_usdt=Decimal("6"),
    )
    assert factor == Decimal("0.9")
    assert notional == Decimal("7.2")

    notional, factor = volumen_compuesto_notional(
        base_quote_usdt=Decimal("8"),
        equity=Decimal("500"),
        initial_equity=Decimal("1000"),
        min_quote_usdt=Decimal("6"),
    )
    assert factor == Decimal("0.5")
    assert notional == Decimal("6")


def test_volumen_compuesto_notional_greed_boost():
    notional, factor = volumen_compuesto_notional(
        base_quote_usdt=Decimal("8"),
        equity=Decimal("1100"),
        initial_equity=Decimal("1000"),
        min_quote_usdt=Decimal("6"),
        greed_factor=Decimal("0.01"),
    )
    assert factor == Decimal("1.111")
    assert notional == Decimal("8.888")


def test_dorothy_volumen_compuesto_greed_metadata():
    strategy = DorothyHubStrategy(
        profit_factor=0.02,
        margin_drop_factor=0.0005,
        quote_order_qty_usdt=8.0,
        max_rungs=10,
        volumen_compuesto=True,
        volumen_compuesto_greed_factor=Decimal("0.01"),
        initial_run_cash=1000.0,
        symbol="XRPUSDT",
        require_trend_gate=False,
    )
    candle = {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "price_source": 1.0}
    ctx = StrategyContext(
        index=0,
        candle=candle,
        candles=[candle],
        cash=5000.0,
        position_qty=0.0,
        avg_entry=0.0,
        equity=1100.0,
    )
    signal = strategy.on_bar(ctx)
    assert signal.action == "buy"
    assert signal.metadata.get("volumen_compuesto_greed_factor") == "0.01"
    assert signal.metadata.get("volumen_compuesto_factor") == "1.111"
    assert signal.metadata.get("volumen_compuesto_notional_usdt") == "8.888"


def test_dorothy_volumen_compuesto_buy_metadata():
    strategy = DorothyHubStrategy(
        profit_factor=0.02,
        margin_drop_factor=0.0005,
        quote_order_qty_usdt=8.0,
        max_rungs=10,
        volumen_compuesto=True,
        initial_run_cash=1000.0,
        symbol="XRPUSDT",
        require_trend_gate=False,
    )
    candle = {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "price_source": 1.0}
    ctx = StrategyContext(
        index=0,
        candle=candle,
        candles=[candle],
        cash=5000.0,
        position_qty=0.0,
        avg_entry=0.0,
        equity=1100.0,
    )
    signal = strategy.on_bar(ctx)
    assert signal.action == "buy"
    assert signal.metadata.get("volumen_compuesto") is True
    assert signal.metadata.get("volumen_compuesto_factor") == "1.1"
    assert signal.metadata.get("volumen_compuesto_notional_usdt") == "8.8"


def test_dorothy_rejects_both_volume_accessories():
    with pytest.raises(ValueError, match="mutually exclusive"):
        DorothyHubStrategy(
            volumen_incremental=True,
            volumen_compuesto=True,
            initial_run_cash=1000.0,
        )
