"""Binance spot filter helpers for backtest order sizing.

Approximates LOT_SIZE / MIN_NOTIONAL constraints when a raw USDT notional
would be rejected. Quantities round **up** to the next valid step (conservative
for minimum-size compliance).
"""
from __future__ import annotations

import math
from typing import Dict, Tuple

# Typical spot filters for major USDT pairs (override per symbol if needed).
_DEFAULT_FILTERS: Dict[str, Dict[str, float]] = {
    "XRPUSDT": {"min_notional": 5.0, "step_size": 0.1, "min_qty": 0.1},
    "BTCUSDT": {"min_notional": 5.0, "step_size": 0.00001, "min_qty": 0.00001},
    "ETHUSDT": {"min_notional": 5.0, "step_size": 0.0001, "min_qty": 0.0001},
}


def _filters_for_symbol(symbol: str) -> Dict[str, float]:
    key = (symbol or "").upper()
    return dict(_DEFAULT_FILTERS.get(key, {"min_notional": 5.0, "step_size": 0.0001, "min_qty": 0.0001}))


def ceil_to_step(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return math.ceil(float(value) / step - 1e-12) * step


def normalize_spot_buy_notional(
    symbol: str,
    notional_usdt: float,
    price: float,
) -> Tuple[float, float]:
    """Return (adjusted_notional_usdt, qty) after LOT_SIZE / MIN_NOTIONAL rounding.

    Rounds quantity up to ``step_size`` so the order meets exchange minimums.
    """
    if price <= 0 or notional_usdt <= 0:
        return 0.0, 0.0
    f = _filters_for_symbol(symbol)
    min_notional = float(f["min_notional"])
    step = float(f["step_size"])
    min_qty = float(f["min_qty"])

    target = max(float(notional_usdt), min_notional)
    qty = target / price
    qty = max(ceil_to_step(qty, step), min_qty)
    adj = qty * price
    if adj < min_notional:
        qty = max(ceil_to_step(min_notional / price, step), min_qty)
        adj = qty * price
    return float(adj), float(qty)
