"""Trend Signal Service — Dual-gate trend detection for Pecunator bots.

Gate 1 (Trend): Heikin-Ashi smoothed MA crossover.
    - Computes HA candles from regular OHLC klines.
    - SMA(1) vs SMA(2) on HA opens → BULLISH / BEARISH.
    - Dorothy requires BULLISH; Elphaba requires BEARISH.

Gate 2 (Entry): Regular candle momentum filter.
    - Compares current price vs the current 1h candle's open.
    - price > open → CLEAR  (Dorothy can enter, Elphaba should not)
    - price < open → BLOCKED (Dorothy waits, Elphaba can short)

Caching: Each symbol's gates are refreshed at most once per
`trend_ttl_sec` / `entry_ttl_sec` to conserve API weight.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_LOG = logging.getLogger("pecunator.modules.trend_signal")


# ── Pure functions (no state) ───────────────────────────────────────

def compute_heikin_ashi(klines: List[list]) -> List[Dict[str, float]]:
    """Convert standard Binance klines to Heikin-Ashi candles.

    Each kline is [open_time, open, high, low, close, volume, ...].
    Returns list of dicts with ha_open, ha_close, ha_high, ha_low.
    """
    result: List[Dict[str, float]] = []
    prev_ha_open = None
    prev_ha_close = None

    for k in klines:
        o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        ha_close = (o + h + l + c) / 4.0

        if prev_ha_open is None:
            ha_open = (o + c) / 2.0
        else:
            ha_open = (prev_ha_open + prev_ha_close) / 2.0

        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)

        result.append({
            "ha_open": ha_open,
            "ha_close": ha_close,
            "ha_high": ha_high,
            "ha_low": ha_low,
        })
        prev_ha_open = ha_open
        prev_ha_close = ha_close

    return result


def compute_trend(ha_candles: List[Dict[str, float]]) -> Dict[str, Any]:
    """Gate 1: SMA crossover on HA opens.

    MA1 = last HA open (SMA 1-period = instantaneous).
    MA2 = average of last 2 HA opens (SMA 2-period).
    MA1 > MA2 → BULLISH (uptrend)
    MA1 < MA2 → BEARISH (downtrend)
    """
    if len(ha_candles) < 2:
        return {"signal": "UNKNOWN", "ma1": 0.0, "ma2": 0.0}

    ma1 = ha_candles[-1]["ha_open"]
    ma2 = (ha_candles[-1]["ha_open"] + ha_candles[-2]["ha_open"]) / 2.0

    signal = "BULLISH" if ma1 > ma2 else "BEARISH"
    return {"signal": signal, "ma1": ma1, "ma2": ma2}


def compute_entry_gate(
    current_price: float,
    candle_open_1h: float,
) -> Dict[str, Any]:
    """Gate 2: Price vs current 1h candle open.

    price > candle_open → CLEAR  (momentum up; Dorothy enters, Elphaba waits)
    price < candle_open → BLOCKED (momentum down; Dorothy waits, Elphaba shorts)
    """
    diff = current_price - candle_open_1h
    diff_pct = (diff / candle_open_1h * 100) if candle_open_1h > 0 else 0.0
    gate = "CLEAR" if diff > 0 else "BLOCKED"
    return {
        "gate": gate,
        "current_price": current_price,
        "candle_open": candle_open_1h,
        "diff": diff,
        "diff_pct": diff_pct,
    }


# ── Stateful service (per-symbol cache) ─────────────────────────────

@dataclass
class _SymbolState:
    """Cached trend/entry gate state for a single symbol."""
    trend_signal: str = "UNKNOWN"   # BULLISH / BEARISH / UNKNOWN
    ma1: float = 0.0
    ma2: float = 0.0
    entry_gate: str = "UNKNOWN"     # CLEAR / BLOCKED / UNKNOWN
    entry_price: float = 0.0
    candle_open: float = 0.0
    trend_updated_at: float = 0.0
    entry_updated_at: float = 0.0


class TrendSignalService:
    """Thread-safe trend signal service with per-symbol caching.

    Designed to be used as a singleton — one per runtime.
    Bots call needs_*_refresh() to check TTL, then update_*() with
    fresh klines data, and finally get_full_state() to read the result.
    """

    def __init__(
        self,
        trend_ttl_sec: float = 300.0,   # Refresh trend every 5 min
        entry_ttl_sec: float = 60.0,    # Refresh entry gate every 1 min
    ) -> None:
        self._trend_ttl = trend_ttl_sec
        self._entry_ttl = entry_ttl_sec
        self._states: Dict[str, _SymbolState] = {}
        self._lock = threading.Lock()

    def _get_or_create(self, symbol: str) -> _SymbolState:
        """Get or create state for a symbol (must hold _lock)."""
        if symbol not in self._states:
            self._states[symbol] = _SymbolState()
        return self._states[symbol]

    # ── TTL checks ──────────────────────────────────────────────

    def needs_trend_refresh(self, symbol: str) -> bool:
        """True if the trend gate for this symbol is stale."""
        with self._lock:
            state = self._get_or_create(symbol)
            return (time.monotonic() - state.trend_updated_at) > self._trend_ttl

    def needs_entry_refresh(self, symbol: str) -> bool:
        """True if the entry gate for this symbol is stale."""
        with self._lock:
            state = self._get_or_create(symbol)
            return (time.monotonic() - state.entry_updated_at) > self._entry_ttl

    # ── Updates ─────────────────────────────────────────────────

    def update_trend(self, symbol: str, klines_1h: List[list]) -> None:
        """Recompute trend gate from fresh 1h klines."""
        try:
            ha = compute_heikin_ashi(klines_1h)
            trend = compute_trend(ha)
            with self._lock:
                state = self._get_or_create(symbol)
                state.trend_signal = trend["signal"]
                state.ma1 = trend["ma1"]
                state.ma2 = trend["ma2"]
                state.trend_updated_at = time.monotonic()
            _LOG.debug("trend_signal: %s trend=%s ma1=%.4f ma2=%.4f",
                       symbol, trend["signal"], trend["ma1"], trend["ma2"])
        except Exception as e:
            _LOG.warning("trend_signal: update_trend failed for %s: %s", symbol, e)

    def update_entry_gate(
        self,
        symbol: str,
        current_price: float,
        candle_open_1h: float,
    ) -> None:
        """Recompute entry gate from current price and candle open."""
        try:
            entry = compute_entry_gate(current_price, candle_open_1h)
            with self._lock:
                state = self._get_or_create(symbol)
                state.entry_gate = entry["gate"]
                state.entry_price = current_price
                state.candle_open = candle_open_1h
                state.entry_updated_at = time.monotonic()
            _LOG.debug("trend_signal: %s entry_gate=%s price=%.4f open=%.4f",
                       symbol, entry["gate"], current_price, candle_open_1h)
        except Exception as e:
            _LOG.warning("trend_signal: update_entry_gate failed for %s: %s", symbol, e)

    # ── Read ────────────────────────────────────────────────────

    def get_full_state(self, symbol: str) -> Dict[str, Any]:
        """Return the full cached state for a symbol."""
        with self._lock:
            state = self._get_or_create(symbol)
            return {
                "trend": state.trend_signal,
                "ma1": state.ma1,
                "ma2": state.ma2,
                "entry_gate": state.entry_gate,
                "entry_price": state.entry_price,
                "candle_open": state.candle_open,
                "trend_age_sec": round(time.monotonic() - state.trend_updated_at, 1)
                    if state.trend_updated_at > 0 else None,
                "entry_age_sec": round(time.monotonic() - state.entry_updated_at, 1)
                    if state.entry_updated_at > 0 else None,
            }

    def get_all_symbols(self) -> Dict[str, Dict[str, Any]]:
        """Return state for all tracked symbols (for diagnostics)."""
        with self._lock:
            return {sym: self.get_full_state(sym) for sym in self._states}


# ── Singleton ───────────────────────────────────────────────────────

_service: Optional[TrendSignalService] = None


def get_trend_signal_service(
    trend_ttl_sec: float = 300.0,
    entry_ttl_sec: float = 60.0,
) -> TrendSignalService:
    """Get or create the global TrendSignalService singleton."""
    global _service
    if _service is None:
        _service = TrendSignalService(
            trend_ttl_sec=trend_ttl_sec,
            entry_ttl_sec=entry_ttl_sec,
        )
        _LOG.info("TrendSignalService initialized (trend_ttl=%ds, entry_ttl=%ds)",
                  trend_ttl_sec, entry_ttl_sec)
    return _service
