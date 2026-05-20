"""Tests for the SpotBroker simulator."""
import math

from backtest.broker import SpotBroker


def test_initial_state():
    broker = SpotBroker(initial_cash=1000.0, fee_rate=0.0, slippage_bps=0.0)
    assert broker.state.cash == 1000.0
    assert broker.state.position_qty == 0.0
    assert broker.state.avg_entry == 0.0
    assert broker.mark_equity(50.0) == 1000.0


def test_market_buy_with_zero_fee_and_zero_slip():
    broker = SpotBroker(initial_cash=1000.0, fee_rate=0.0, slippage_bps=0.0)
    fill = broker.execute_market("buy", price=100.0, size_pct=1.0)
    assert fill is not None
    assert math.isclose(fill["price"], 100.0)
    assert math.isclose(fill["qty"], 10.0)
    assert math.isclose(broker.state.cash, 0.0)
    assert math.isclose(broker.state.position_qty, 10.0)
    assert math.isclose(broker.state.avg_entry, 100.0)


def test_market_buy_then_sell_round_trip():
    broker = SpotBroker(initial_cash=1000.0, fee_rate=0.001, slippage_bps=0.0)
    broker.execute_market("buy", price=100.0, size_pct=1.0)
    pos_before = broker.state.position_qty
    fill = broker.execute_market("sell", price=110.0, size_pct=1.0)
    assert fill is not None
    assert math.isclose(fill["qty"], pos_before)
    assert broker.state.position_qty == 0.0
    assert broker.state.avg_entry == 0.0
    # Equity should grow because price moved up despite fees
    assert broker.state.cash > 1000.0


def test_sell_without_position_returns_none():
    broker = SpotBroker(initial_cash=1000.0)
    assert broker.execute_market("sell", price=100.0, size_pct=1.0) is None


def test_partial_sell_keeps_remaining_position():
    broker = SpotBroker(initial_cash=1000.0, fee_rate=0.0, slippage_bps=0.0)
    broker.execute_market("buy", price=100.0, size_pct=1.0)
    broker.execute_market("sell", price=120.0, size_pct=0.5)
    assert broker.state.position_qty > 0.0
    assert broker.state.avg_entry == 100.0
