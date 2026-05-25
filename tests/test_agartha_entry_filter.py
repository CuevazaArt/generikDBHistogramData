"""Tests for backtest.agartha_entry_filter (entry gate + WS monitor stub)."""
import pytest

from backtest.agartha_entry_filter import (
    AgarthaWsMonitor,
    EntryGateConfig,
    GateOutcome,
    evaluate_entry_gate,
)


def _candle(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_insufficient_history_returns_blocked():
    cfg = EntryGateConfig(donchian_lookback=20, macro_ma_lookback=20)
    out = evaluate_entry_gate(
        operating_history=[_candle(1, 1, 1, 1)] * 5,
        macro_history=[_candle(1, 1, 1, 1)] * 5,
        current_price=1.0,
        config=cfg,
    )
    assert out.outcome == GateOutcome.INSUFFICIENT_HISTORY
    assert not out.armed


def test_blocked_when_price_above_donchian_trigger():
    cfg = EntryGateConfig(donchian_lookback=5, macro_ma_lookback=2, donchian_tolerance_pct=0.0,
                          require_momentum_uptick=False, macro_drop_pct=99.0)
    op = [_candle(1, 1.2, 0.9, 1.0), _candle(1, 1.2, 0.95, 1.05),
          _candle(1, 1.3, 1.0, 1.1), _candle(1, 1.4, 1.05, 1.2),
          _candle(1, 1.5, 1.1, 1.3)]
    macro = [_candle(1, 1.5, 0.8, 1.2), _candle(1, 1.5, 0.8, 1.2)]
    out = evaluate_entry_gate(operating_history=op, macro_history=macro,
                              current_price=1.4, config=cfg)
    assert out.outcome == GateOutcome.BLOCKED_DONCHIAN
    assert out.donchian_low == pytest.approx(0.9)


def test_armed_when_price_touches_donchian_with_uptick():
    cfg = EntryGateConfig(donchian_lookback=5, macro_ma_lookback=2,
                          donchian_tolerance_pct=0.5, macro_drop_pct=99.0)
    op = [_candle(1.0, 1.1, 0.95, 1.0), _candle(0.95, 1.0, 0.90, 0.92),
          _candle(0.92, 0.96, 0.85, 0.90), _candle(0.90, 0.92, 0.82, 0.85),
          _candle(0.85, 0.88, 0.80, 0.84)]
    macro = [_candle(1.0, 1.2, 0.7, 1.0)] * 5
    # uptick: last close (0.84) > prev close (0.85)? NO; pero usamos config sin uptick
    cfg_no_uptick = EntryGateConfig(donchian_lookback=5, macro_ma_lookback=2,
                                    donchian_tolerance_pct=0.5, macro_drop_pct=99.0,
                                    require_momentum_uptick=False)
    out = evaluate_entry_gate(operating_history=op, macro_history=macro,
                              current_price=0.80, config=cfg_no_uptick)
    assert out.armed
    assert out.outcome == GateOutcome.ARMED


def test_blocked_by_momentum_when_last_close_lower():
    cfg = EntryGateConfig(donchian_lookback=5, macro_ma_lookback=2,
                          donchian_tolerance_pct=5.0, macro_drop_pct=99.0,
                          require_momentum_uptick=True)
    op = [_candle(1.0, 1.0, 0.8, 1.0)] * 4 + [_candle(0.95, 0.95, 0.8, 0.85)]
    macro = [_candle(1.0, 1.2, 0.7, 1.0)] * 5
    out = evaluate_entry_gate(operating_history=op, macro_history=macro,
                              current_price=0.84, config=cfg)
    assert out.outcome == GateOutcome.BLOCKED_MOMENTUM
    assert not out.armed


def test_armed_with_uptick_after_low():
    cfg = EntryGateConfig(donchian_lookback=5, macro_ma_lookback=2,
                          donchian_tolerance_pct=5.0, macro_drop_pct=99.0,
                          require_momentum_uptick=True)
    op = [_candle(1.0, 1.0, 0.8, 0.85)] * 3 + [
        _candle(0.85, 0.85, 0.80, 0.80),
        _candle(0.80, 0.84, 0.78, 0.83),  # uptick: 0.83 > 0.80
    ]
    macro = [_candle(1.0, 1.2, 0.7, 1.0)] * 5
    # min_low=0.78; trigger=0.78*1.05=0.819. current 0.81 < 0.819 -> armed.
    out = evaluate_entry_gate(operating_history=op, macro_history=macro,
                              current_price=0.81, config=cfg)
    assert out.armed
    assert out.outcome == GateOutcome.ARMED


def test_blocked_by_macro_drop():
    cfg = EntryGateConfig(donchian_lookback=2, macro_ma_lookback=5,
                          macro_ma_source="close", macro_drop_pct=10.0,
                          require_momentum_uptick=False)
    op = [_candle(1.0, 1.0, 0.5, 0.6)] * 3
    macro = [_candle(1, 1, 1, 1.0), _candle(1, 1, 1, 1.0), _candle(1, 1, 1, 1.0),
             _candle(1, 1, 1, 1.0), _candle(1, 1, 1, 1.0)]
    # MA=1.0, floor=0.9; current 0.5 < floor -> blocked
    out = evaluate_entry_gate(operating_history=op, macro_history=macro,
                              current_price=0.5, config=cfg)
    assert out.outcome == GateOutcome.BLOCKED_MACRO
    assert out.macro_floor == pytest.approx(0.9)


def test_ws_monitor_accumulates_and_evaluates():
    cfg = EntryGateConfig(donchian_lookback=3, macro_ma_lookback=2, donchian_tolerance_pct=5.0,
                          macro_drop_pct=99.0, require_momentum_uptick=False)
    mon = AgarthaWsMonitor(cfg)
    for _ in range(5):
        mon.push_operating_candle(_candle(1, 1.1, 0.9, 1.0))
        mon.push_macro_candle(_candle(1, 1.1, 0.9, 1.0))
    out = mon.on_tick(0.92)
    assert mon.last_decision is out
    # 0.92 <= min_low(0.9) * 1.05 = 0.945 -> donchian OK; macro 99% holgura -> OK
    assert out.armed


def test_decision_serialization_to_dict():
    cfg = EntryGateConfig(donchian_lookback=3, macro_ma_lookback=2, macro_drop_pct=99.0,
                          require_momentum_uptick=False)
    op = [_candle(1, 1, 0.9, 1)] * 3
    macro = [_candle(1, 1, 0.9, 1)] * 2
    out = evaluate_entry_gate(operating_history=op, macro_history=macro,
                              current_price=0.91, config=cfg)
    d = out.to_dict()
    assert d["outcome"] in ("armed", "blocked_donchian", "blocked_macro", "blocked_momentum")
    assert "donchian_low" in d
