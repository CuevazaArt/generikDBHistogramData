"""Unit tests for AgarthaStrategy (moonshot + trailing)."""
import pytest

from backtest.registry import get_strategy, list_strategy_names, params_from_cli, suggest_params
from backtest.strategies import AgarthaStrategy
from backtest.strategy_base import StrategyContext


def _ctx(price: float, cash: float, position_qty: float, avg_entry: float, candles=None, *, low=None):
    bar_low = float(low) if low is not None else price
    candle = {"open": price, "high": price, "low": bar_low, "close": price, "price_source": price}
    candles = candles or [candle]
    return StrategyContext(
        index=len(candles) - 1,
        candle=candle,
        candles=candles,
        cash=cash,
        position_qty=position_qty,
        avg_entry=avg_entry,
        equity=cash + position_qty * price,
    )


def test_agartha_registered_in_registry():
    assert "agartha" in list_strategy_names()
    assert get_strategy("agartha") is AgarthaStrategy


def test_agartha_initial_buy_with_fixed_notional():
    strat = AgarthaStrategy(quote_order_qty_usdt=10.0, trailing_stop_pct=30.0)
    sig = strat.on_bar(_ctx(price=1.0, cash=100.0, position_qty=0.0, avg_entry=0.0))
    assert sig.action == "buy"
    assert sig.reason == "agartha_initial_entry"
    assert sig.metadata["target_notional"] == pytest.approx(10.0)


def test_agartha_holds_when_insufficient_cash():
    strat = AgarthaStrategy(quote_order_qty_usdt=10.0)
    sig = strat.on_bar(_ctx(price=1.0, cash=5.0, position_qty=0.0, avg_entry=0.0))
    assert sig.action == "hold"
    assert sig.reason == "insufficient_cash"


def _full_sell_signal():
    """Mimic a 100% sell signal as the engine would send it."""
    from backtest.strategy_base import Signal
    return Signal(action="sell", size_pct=1.0, reason="agartha_trailing_stop")


def test_agartha_continuous_reentry_by_default():
    """Por default Agartha es ciclo continuo: tras cerrar, vuelve a comprar."""
    strat = AgarthaStrategy(quote_order_qty_usdt=10.0, trailing_stop_pct=30.0)
    strat.on_fill({"side": "buy", "price": 1.0}, None, _ctx(1.0, 90.0, 10.0, 1.0))
    strat.on_fill({"side": "sell", "price": 2.0}, _full_sell_signal(),
                  _ctx(2.0, 90.0, 10.0, 1.0))
    assert strat.cycles_closed == 1
    sig = strat.on_bar(_ctx(price=2.0, cash=110.0, position_qty=0.0, avg_entry=0.0))
    assert sig.action == "buy"
    assert sig.reason == "agartha_reentry"
    assert sig.metadata["cycle_index"] == 1


def test_agartha_single_shot_when_max_cycles_one():
    strat = AgarthaStrategy(quote_order_qty_usdt=10.0, trailing_stop_pct=30.0, max_cycles=1)
    strat.on_fill({"side": "buy", "price": 1.0}, None, _ctx(1.0, 90.0, 10.0, 1.0))
    strat.on_fill({"side": "sell", "price": 2.0}, _full_sell_signal(),
                  _ctx(2.0, 90.0, 10.0, 1.0))
    sig = strat.on_bar(_ctx(price=2.0, cash=110.0, position_qty=0.0, avg_entry=0.0))
    assert sig.action == "hold"
    assert sig.reason == "agartha_max_cycles_reached"


def test_agartha_reentry_cooldown_blocks_for_n_bars():
    strat = AgarthaStrategy(
        quote_order_qty_usdt=10.0, trailing_stop_pct=30.0, reentry_cooldown_bars=3,
    )
    strat.on_fill({"side": "buy", "price": 1.0}, None, _ctx(1.0, 90.0, 10.0, 1.0))
    strat.on_fill({"side": "sell", "price": 2.0}, _full_sell_signal(),
                  _ctx(2.0, 90.0, 10.0, 1.0))
    # 3 barras de cooldown
    for i in range(3):
        sig = strat.on_bar(_ctx(price=2.0, cash=110.0, position_qty=0.0, avg_entry=0.0))
        assert sig.action == "hold", f"bar {i} should be cooldown"
        assert sig.reason == "agartha_reentry_cooldown"
    sig = strat.on_bar(_ctx(price=2.0, cash=110.0, position_qty=0.0, avg_entry=0.0))
    assert sig.action == "buy"
    assert sig.reason == "agartha_reentry"


def test_agartha_on_bar_fallback_detects_broker_close():
    """Si on_fill no detecto el cierre (ctx pre-fill stale), on_bar lo recupera."""
    strat = AgarthaStrategy(quote_order_qty_usdt=10.0, trailing_stop_pct=30.0, max_cycles=1)
    strat.entry_price = 1.0
    strat.peak_price = 1.5
    strat.bars_in_position = 10
    sig = strat.on_bar(_ctx(price=1.2, cash=110.0, position_qty=0.0, avg_entry=0.0))
    assert strat.cycles_closed == 1
    assert sig.action == "hold"
    assert sig.reason == "agartha_max_cycles_reached"


def test_agartha_trailing_triggers_after_drawdown_from_peak():
    strat = AgarthaStrategy(
        quote_order_qty_usdt=10.0,
        trailing_stop_pct=20.0,
        activation_profit_pct=0.0,
    )
    # Compra
    strat.on_fill({"side": "buy", "price": 1.0}, None, _ctx(1.0, 90.0, 10.0, 1.0))
    # Subida fuerte
    sig = strat.on_bar(_ctx(price=2.0, cash=90.0, position_qty=10.0, avg_entry=1.0))
    assert sig.action == "hold"
    assert strat.peak_price == pytest.approx(2.0)
    # Caida 25% desde peak (2.0 -> 1.5) > 20% trailing → debe vender
    sig = strat.on_bar(_ctx(price=1.5, cash=90.0, position_qty=10.0, avg_entry=1.0))
    assert sig.action == "sell"
    assert sig.reason == "agartha_trailing_stop"
    assert sig.metadata["peak_price"] == pytest.approx(2.0)


def test_agartha_trailing_not_active_until_activation_threshold():
    strat = AgarthaStrategy(
        quote_order_qty_usdt=10.0,
        trailing_stop_pct=20.0,
        activation_profit_pct=50.0,
    )
    strat.on_fill({"side": "buy", "price": 1.0}, None, _ctx(1.0, 90.0, 10.0, 1.0))
    # Subio +30%, no llega a +50% activacion
    sig = strat.on_bar(_ctx(price=1.3, cash=90.0, position_qty=10.0, avg_entry=1.0))
    assert sig.action == "hold"
    # Cae fuerte (no deberia vender, trailing inactivo)
    sig = strat.on_bar(_ctx(price=0.5, cash=90.0, position_qty=10.0, avg_entry=1.0))
    assert sig.action == "hold"
    assert not strat.trailing_active


def test_agartha_breakeven_lock_protects_entry():
    strat = AgarthaStrategy(
        quote_order_qty_usdt=10.0,
        trailing_stop_pct=50.0,
        activation_profit_pct=0.0,
        breakeven_lock_pct=100.0,  # bloquea breakeven cuando peak >= 2x
    )
    strat.on_fill({"side": "buy", "price": 1.0}, None, _ctx(1.0, 90.0, 10.0, 1.0))
    # Subida a 3x
    strat.on_bar(_ctx(price=3.0, cash=90.0, position_qty=10.0, avg_entry=1.0))
    # Trailing seria 3 * (1-0.5) = 1.5, pero breakeven lock empuja floor a 1.0
    # Caida a 1.4 (debajo de trailing 1.5 normal pero arriba de breakeven 1.0)
    sig = strat.on_bar(_ctx(price=1.4, cash=90.0, position_qty=10.0, avg_entry=1.0))
    # En este caso la caida cruza el trailing puro (1.5) → vende
    assert sig.action == "sell"
    # Pero el floor reportado es el max(trailing, entry) = 1.5
    assert sig.metadata["trail_floor"] >= 1.0


def test_agartha_time_stop_closes_position():
    strat = AgarthaStrategy(quote_order_qty_usdt=10.0, trailing_stop_pct=30.0, max_holding_bars=3)
    strat.on_fill({"side": "buy", "price": 1.0}, None, _ctx(1.0, 90.0, 10.0, 1.0))
    for _ in range(2):
        sig = strat.on_bar(_ctx(price=1.0, cash=90.0, position_qty=10.0, avg_entry=1.0))
        assert sig.action == "hold"
    sig = strat.on_bar(_ctx(price=1.0, cash=90.0, position_qty=10.0, avg_entry=1.0))
    assert sig.action == "sell"
    assert sig.reason == "agartha_time_stop"


def test_agartha_partial_tp_then_continues():
    strat = AgarthaStrategy(
        quote_order_qty_usdt=10.0,
        trailing_stop_pct=50.0,
        partial_tp_pct=100.0,
        partial_tp_size_pct=0.5,
    )
    strat.on_fill({"side": "buy", "price": 1.0}, None, _ctx(1.0, 90.0, 10.0, 1.0))
    sig = strat.on_bar(_ctx(price=2.0, cash=90.0, position_qty=10.0, avg_entry=1.0))
    assert sig.action == "sell"
    assert sig.reason == "agartha_partial_tp"
    assert sig.size_pct == pytest.approx(0.5)
    assert strat.partial_tp_done
    # Siguiente vela: no debe disparar partial otra vez
    sig = strat.on_bar(_ctx(price=2.5, cash=100.0, position_qty=5.0, avg_entry=1.0))
    assert sig.reason != "agartha_partial_tp"


def test_agartha_export_import_state_roundtrip():
    strat = AgarthaStrategy(quote_order_qty_usdt=10.0, trailing_stop_pct=30.0)
    strat.entry_price = 1.5
    strat.peak_price = 3.0
    strat.bars_in_position = 42
    strat.trailing_active = True
    strat.partial_tp_done = True
    strat.cycles_closed = 1
    state = strat.export_state()

    other = AgarthaStrategy(quote_order_qty_usdt=10.0, trailing_stop_pct=30.0)
    other.import_state(state)
    assert other.entry_price == pytest.approx(1.5)
    assert other.peak_price == pytest.approx(3.0)
    assert other.bars_in_position == 42
    assert other.trailing_active is True
    assert other.partial_tp_done is True
    assert other.cycles_closed == 1


def test_params_from_cli_agartha_contains_required_keys():
    import argparse
    ns = argparse.Namespace(
        quote_order_qty_usdt=10.0,
        trailing_stop_pct=30.0,
        activation_profit_pct=50.0,
        max_holding_bars=0,
        breakeven_lock_pct=100.0,
        partial_tp_pct=0.0,
        partial_tp_size_pct=0.0,
        max_cycles=0,
        reentry_cooldown_bars=4,
    )
    out = params_from_cli(ns, "agartha")
    assert out["quote_order_qty_usdt"] == 10.0
    assert out["trailing_stop_pct"] == 30.0
    assert out["activation_profit_pct"] == 50.0
    assert out["breakeven_lock_pct"] == 100.0
    assert out["max_cycles"] == 0
    assert out["reentry_cooldown_bars"] == 4


def test_agartha_entry_limit_places_pending_order_on_first_bar():
    strat = AgarthaStrategy(quote_order_qty_usdt=10.0, trailing_stop_pct=30.0,
                            entry_limit_offset_pct=10.0)
    sig = strat.on_bar(_ctx(price=1.0, cash=100.0, position_qty=0.0, avg_entry=0.0))
    assert sig.action == "hold"
    assert sig.reason == "agartha_limit_placed"
    assert sig.metadata["limit_price"] == pytest.approx(0.9)
    assert strat.pending_limit_price == pytest.approx(0.9)


def test_agartha_entry_limit_fills_when_low_touches_price():
    strat = AgarthaStrategy(quote_order_qty_usdt=10.0, trailing_stop_pct=30.0,
                            entry_limit_offset_pct=10.0)
    strat.on_bar(_ctx(price=1.0, cash=100.0, position_qty=0.0, avg_entry=0.0))
    # En el siguiente bar, el low baja a 0.85 (cruza la limit a 0.9) -> fill
    sig = strat.on_bar(_ctx(price=0.95, cash=100.0, position_qty=0.0, avg_entry=0.0, low=0.85))
    assert sig.action == "buy"
    assert sig.reason == "agartha_limit_fill_initial"
    assert sig.metadata["limit_fill_price"] == pytest.approx(0.9)
    assert strat.pending_limit_price == 0.0


def test_agartha_entry_limit_does_not_fill_when_low_above_limit():
    strat = AgarthaStrategy(quote_order_qty_usdt=10.0, trailing_stop_pct=30.0,
                            entry_limit_offset_pct=10.0)
    strat.on_bar(_ctx(price=1.0, cash=100.0, position_qty=0.0, avg_entry=0.0))
    sig = strat.on_bar(_ctx(price=1.05, cash=100.0, position_qty=0.0, avg_entry=0.0, low=0.95))
    assert sig.action == "hold"
    assert sig.reason == "agartha_limit_pending"
    assert strat.pending_limit_price == pytest.approx(0.9)
    assert strat.bars_since_limit_placed == 1


def test_agartha_entry_limit_expires_and_cancels_by_default():
    strat = AgarthaStrategy(quote_order_qty_usdt=10.0, trailing_stop_pct=30.0,
                            entry_limit_offset_pct=10.0, entry_limit_expiry_bars=3)
    strat.on_bar(_ctx(price=1.0, cash=100.0, position_qty=0.0, avg_entry=0.0))
    # 3 barras sin fill -> expira
    for _ in range(2):
        strat.on_bar(_ctx(price=1.05, cash=100.0, position_qty=0.0, avg_entry=0.0, low=0.95))
    sig = strat.on_bar(_ctx(price=1.05, cash=100.0, position_qty=0.0, avg_entry=0.0, low=0.95))
    assert sig.reason == "agartha_limit_expired_no_fill"
    assert strat.pending_limit_price == 0.0


def test_agartha_entry_limit_reprices_on_expiry_when_flag_set():
    strat = AgarthaStrategy(quote_order_qty_usdt=10.0, trailing_stop_pct=30.0,
                            entry_limit_offset_pct=10.0, entry_limit_expiry_bars=2,
                            entry_limit_reprice_on_expiry=True)
    strat.on_bar(_ctx(price=1.0, cash=100.0, position_qty=0.0, avg_entry=0.0))
    strat.on_bar(_ctx(price=1.10, cash=100.0, position_qty=0.0, avg_entry=0.0, low=1.0))
    sig = strat.on_bar(_ctx(price=1.20, cash=100.0, position_qty=0.0, avg_entry=0.0, low=1.10))
    assert sig.reason == "agartha_limit_repriced"
    assert strat.pending_limit_price == pytest.approx(1.08)  # 1.20 * 0.90


def test_agartha_zero_offset_uses_immediate_buy_legacy():
    """entry_limit_offset_pct=0 = comportamiento original (compra inmediata)."""
    strat = AgarthaStrategy(quote_order_qty_usdt=10.0, trailing_stop_pct=30.0,
                            entry_limit_offset_pct=0.0)
    sig = strat.on_bar(_ctx(price=1.0, cash=100.0, position_qty=0.0, avg_entry=0.0))
    assert sig.action == "buy"
    assert sig.reason == "agartha_initial_entry"


def test_suggest_params_agartha_respects_overrides():
    class TrialStub:
        def __init__(self):
            self.last = {}

        def suggest_float(self, name, low, high):
            self.last[name] = high
            return high

        def suggest_int(self, name, low, high):
            self.last[name] = high
            return high

    params = suggest_params(
        TrialStub(),
        "agartha",
        search_overrides={
            "trailing_stop_pct_min": 20.0,
            "trailing_stop_pct_max": 40.0,
            "activation_profit_pct_min": 10.0,
            "activation_profit_pct_max": 80.0,
            "breakeven_lock_pct_min": 50.0,
            "breakeven_lock_pct_max": 200.0,
        },
    )
    assert params["trailing_stop_pct"] == pytest.approx(40.0)
    assert params["activation_profit_pct"] == pytest.approx(80.0)
    assert params["breakeven_lock_pct"] == pytest.approx(200.0)
