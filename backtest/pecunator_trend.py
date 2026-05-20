"""Pecunator trend helpers for backtesting.

Adapted from Pecunator's runtime trend_signal module so strategies can
replay the same dual-gate logic offline:
- Gate 1 (trend): Heikin-Ashi MA(1) vs MA(2) on HA opens.
- Gate 2 (entry): current price vs current candle open.
"""

from __future__ import annotations

from typing import Any, Dict, List


def compute_heikin_ashi(klines: List[Dict[str, Any]]) -> List[Dict[str, float]]:
    """Convert candle dicts to Heikin-Ashi candles."""
    result: List[Dict[str, float]] = []
    prev_ha_open = None
    prev_ha_close = None
    for c in klines:
        o = float(c.get("open", 0.0))
        h = float(c.get("high", 0.0))
        l = float(c.get("low", 0.0))
        close = float(c.get("close", 0.0))
        ha_close = (o + h + l + close) / 4.0
        if prev_ha_open is None:
            ha_open = (o + close) / 2.0
        else:
            ha_open = (prev_ha_open + prev_ha_close) / 2.0
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        result.append(
            {
                "ha_open": ha_open,
                "ha_close": ha_close,
                "ha_high": ha_high,
                "ha_low": ha_low,
            }
        )
        prev_ha_open = ha_open
        prev_ha_close = ha_close
    return result


def compute_trend(ha_candles: List[Dict[str, float]], index: int) -> str:
    """Compute trend at index using HA MA(1) vs MA(2)."""
    if index < 1 or index >= len(ha_candles):
        return "UNKNOWN"
    ma1 = float(ha_candles[index]["ha_open"])
    ma2 = (float(ha_candles[index]["ha_open"]) + float(ha_candles[index - 1]["ha_open"])) / 2.0
    return "BULLISH" if ma1 > ma2 else "BEARISH"


def compute_entry_gate(current_price: float, candle_open: float) -> str:
    """Compute entry gate from price vs regular candle open."""
    return "CLEAR" if current_price > candle_open else "BLOCKED"


def annotate_pecunator_gates(candles: List[Dict[str, Any]], price_key: str = "price_source") -> None:
    """Annotate each candle with Pecunator-compatible trend gates.

    Adds:
    - pec_trend: BULLISH/BEARISH/UNKNOWN
    - pec_entry_gate: CLEAR/BLOCKED
    """
    if not candles:
        return
    ha = compute_heikin_ashi(candles)
    for i, c in enumerate(candles):
        price = float(c.get(price_key, c.get("close", 0.0)))
        candle_open = float(c.get("open", 0.0))
        c["pec_trend"] = compute_trend(ha, i)
        c["pec_entry_gate"] = compute_entry_gate(price, candle_open)

