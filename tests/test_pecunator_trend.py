"""Tests for Pecunator Heikin-Ashi trend helpers."""
from backtest.pecunator_trend import (
    annotate_pecunator_gates,
    compute_entry_gate,
    compute_heikin_ashi,
    compute_trend,
)


def _candle(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_heikin_ashi_first_candle_matches_initial_formula():
    candles = [_candle(100.0, 110.0, 95.0, 105.0)]
    ha = compute_heikin_ashi(candles)
    assert len(ha) == 1
    assert ha[0]["ha_close"] == (100.0 + 110.0 + 95.0 + 105.0) / 4.0
    assert ha[0]["ha_open"] == (100.0 + 105.0) / 2.0


def test_entry_gate_blocked_when_price_below_open():
    assert compute_entry_gate(99.0, 100.0) == "BLOCKED"
    assert compute_entry_gate(100.0, 100.0) == "BLOCKED"
    assert compute_entry_gate(101.0, 100.0) == "CLEAR"


def test_trend_unknown_for_first_index():
    ha = [
        {"ha_open": 100.0, "ha_close": 110.0, "ha_high": 110.0, "ha_low": 100.0},
        {"ha_open": 105.0, "ha_close": 115.0, "ha_high": 115.0, "ha_low": 105.0},
    ]
    assert compute_trend(ha, 0) == "UNKNOWN"
    assert compute_trend(ha, 1) in ("BULLISH", "BEARISH")


def test_annotate_adds_expected_keys():
    candles = [
        _candle(100.0, 110.0, 95.0, 105.0),
        _candle(106.0, 112.0, 100.0, 108.0),
        _candle(108.0, 115.0, 104.0, 112.0),
    ]
    # `price_source` is normally added by engine transforms; emulate it.
    for c in candles:
        c["price_source"] = c["close"]
    annotate_pecunator_gates(candles)
    for c in candles:
        for key in ("pec_trend", "pec_entry_gate", "ha_open", "ha_close", "ha_high", "ha_low"):
            assert key in c
