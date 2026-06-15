"""Extreme-price detector for Binance symbols.

Evaluates five independent signals on daily / weekly klines and produces a
composite *extreme score* (0–1) plus a boolean *actionable* flag when the
required confluence threshold is met.

Signals
-------
1. **Drawdown from ATH** – ratio ``(ATH - price) / ATH``.
2. **Weekly RSI** – Wilder RSI(14) computed on weekly closes.
3. **Distance to MA200** – how many standard-deviations below the 200-day
   simple moving average the current price sits.
4. **Relative volume** – current daily volume vs. its 20-day SMA; a spike
   suggests panic / capitulation selling.
5. **Historical percentile** – where the current price sits within the
   all-time min–max range.
"""

from __future__ import annotations

import sys
import os
from dataclasses import dataclass, field
from math import sqrt
from typing import Dict, List, Optional, Tuple

# Allow importing sibling packages when running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from extremo_monitor.config import ExtremeThresholds, SurvivalFilter


# ---------------------------------------------------------------------------
# Data helpers (pure functions, no I/O)
# ---------------------------------------------------------------------------

def _sma(values: List[float], period: int) -> List[Optional[float]]:
    """Simple moving average; returns ``None`` for warm-up bars."""
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def _stdev(values: List[float], period: int) -> List[Optional[float]]:
    """Rolling population standard deviation."""
    sma_vals = _sma(values, period)
    out: List[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        mean = sma_vals[i]
        if mean is None:
            continue
        window = values[i - period + 1 : i + 1]
        var = sum((v - mean) ** 2 for v in window) / period
        out[i] = sqrt(var)
    return out


def _rsi(values: List[float], period: int) -> List[Optional[float]]:
    """Wilder RSI."""
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return out
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        out[period] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, len(values)):
        g = gains[i - 1]
        lo = losses[i - 1]
        avg_gain = ((avg_gain * (period - 1)) + g) / period
        avg_loss = ((avg_loss * (period - 1)) + lo) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def _resample_daily_to_weekly(
    daily_closes: List[float], daily_timestamps: List[int]
) -> Tuple[List[float], List[int]]:
    """Aggregate daily closes into weekly (7-day) closes."""
    if not daily_closes:
        return [], []
    weekly_closes: List[float] = []
    weekly_ts: List[int] = []
    DAY_MS = 86_400_000
    week_start = daily_timestamps[0]
    for close, ts in zip(daily_closes, daily_timestamps):
        if ts - week_start >= 7 * DAY_MS:
            weekly_closes.append(close)
            weekly_ts.append(ts)
            week_start = ts
    # Include last partial week
    if not weekly_closes or weekly_ts[-1] != daily_timestamps[-1]:
        weekly_closes.append(daily_closes[-1])
        weekly_ts.append(daily_timestamps[-1])
    return weekly_closes, weekly_ts


# ---------------------------------------------------------------------------
# Signal evaluators
# ---------------------------------------------------------------------------

@dataclass
class SignalResult:
    """Result of evaluating one signal."""
    name: str
    active: bool  # True if the threshold is met
    value: float  # raw metric value
    threshold: float  # the threshold used
    description: str = ""


@dataclass
class ExtremeResult:
    """Composite result for one symbol."""
    symbol: str
    score: float  # 0.0–1.0
    confluence: int  # how many signals fired
    actionable: bool  # confluence >= min_confluence
    signals: List[SignalResult] = field(default_factory=list)
    current_price: float = 0.0
    ath: float = 0.0


def _signal_drawdown(
    closes: List[float], threshold: float
) -> SignalResult:
    """Signal 1: Drawdown from all-time high."""
    if not closes:
        return SignalResult("drawdown_ath", False, 0.0, threshold)
    ath = max(closes)
    current = closes[-1]
    drawdown = (ath - current) / ath if ath > 0 else 0.0
    return SignalResult(
        name="drawdown_ath",
        active=drawdown >= threshold,
        value=round(drawdown, 4),
        threshold=threshold,
        description=f"Caída {drawdown:.1%} desde ATH ${ath:,.2f}",
    )


def _signal_rsi_weekly(
    daily_closes: List[float],
    daily_timestamps: List[int],
    rsi_period: int,
    rsi_max: float,
) -> SignalResult:
    """Signal 2: Weekly RSI."""
    weekly_closes, _ = _resample_daily_to_weekly(daily_closes, daily_timestamps)
    if len(weekly_closes) <= rsi_period:
        return SignalResult("rsi_weekly", False, 50.0, rsi_max,
                            description="Datos insuficientes para RSI semanal")
    rsi_vals = _rsi(weekly_closes, rsi_period)
    current_rsi = next(
        (v for v in reversed(rsi_vals) if v is not None), 50.0
    )
    return SignalResult(
        name="rsi_weekly",
        active=current_rsi <= rsi_max,
        value=round(current_rsi, 2),
        threshold=rsi_max,
        description=f"RSI semanal = {current_rsi:.1f}",
    )


def _signal_ma200_distance(
    closes: List[float], sigma_min: float
) -> SignalResult:
    """Signal 3: Price distance from 200-day SMA in standard deviations."""
    period = 200
    if len(closes) < period:
        return SignalResult("ma200_distance", False, 0.0, sigma_min,
                            description="Datos insuficientes para MA200")
    sma_vals = _sma(closes, period)
    std_vals = _stdev(closes, period)
    ma = sma_vals[-1]
    std = std_vals[-1]
    current = closes[-1]
    if ma is None or std is None or std == 0:
        return SignalResult("ma200_distance", False, 0.0, sigma_min)
    distance_sigma = (current - ma) / std
    return SignalResult(
        name="ma200_distance",
        active=distance_sigma <= -sigma_min,
        value=round(distance_sigma, 2),
        threshold=-sigma_min,
        description=f"{distance_sigma:.1f}σ de MA200 (${ma:,.4f})",
    )


def _signal_volume_spike(
    volumes: List[float], ma_period: int, multiplier: float
) -> SignalResult:
    """Signal 4: Volume spike relative to its moving average."""
    if len(volumes) < ma_period:
        return SignalResult("volume_spike", False, 0.0, multiplier,
                            description="Datos insuficientes para vol MA")
    vol_sma = _sma(volumes, ma_period)
    avg_vol = vol_sma[-1]
    current_vol = volumes[-1]
    if avg_vol is None or avg_vol == 0:
        return SignalResult("volume_spike", False, 0.0, multiplier)
    ratio = current_vol / avg_vol
    return SignalResult(
        name="volume_spike",
        active=ratio >= multiplier,
        value=round(ratio, 2),
        threshold=multiplier,
        description=f"Volumen {ratio:.1f}× vs promedio",
    )


def _signal_percentile(
    closes: List[float], pct_max: float
) -> SignalResult:
    """Signal 5: Historical percentile of the current price."""
    if not closes:
        return SignalResult("percentile", False, 50.0, pct_max)
    lo = min(closes)
    hi = max(closes)
    current = closes[-1]
    if hi == lo:
        pct = 50.0
    else:
        pct = ((current - lo) / (hi - lo)) * 100.0
    return SignalResult(
        name="percentile",
        active=pct <= pct_max,
        value=round(pct, 2),
        threshold=pct_max,
        description=f"Percentil {pct:.1f}% (min ${lo:,.4f} – max ${hi:,.4f})",
    )


# ---------------------------------------------------------------------------
# Main detector
# ---------------------------------------------------------------------------

def evaluate_extreme(
    symbol: str,
    daily_closes: List[float],
    daily_timestamps: List[int],
    daily_volumes: List[float],
    thresholds: ExtremeThresholds | None = None,
) -> ExtremeResult:
    """Compute the composite extreme score for *symbol*.

    Parameters
    ----------
    symbol : str
        Trading pair, e.g. ``"BTCUSDT"``.
    daily_closes : list[float]
        Daily close prices ordered chronologically.
    daily_timestamps : list[int]
        Corresponding open_time in epoch-ms.
    daily_volumes : list[float]
        Daily quote-asset volume.
    thresholds : ExtremeThresholds, optional
        Custom thresholds; defaults are used if ``None``.

    Returns
    -------
    ExtremeResult
        Contains ``score``, ``confluence``, ``actionable``, and per-signal
        details.
    """
    if thresholds is None:
        thresholds = ExtremeThresholds()

    signals: List[SignalResult] = []

    # Signal 1 – Drawdown from ATH
    signals.append(_signal_drawdown(daily_closes, thresholds.drawdown_min))

    # Signal 2 – Weekly RSI
    signals.append(
        _signal_rsi_weekly(
            daily_closes, daily_timestamps,
            thresholds.rsi_period, thresholds.rsi_max,
        )
    )

    # Signal 3 – Distance to MA200
    signals.append(_signal_ma200_distance(daily_closes, thresholds.ma200_sigma_min))

    # Signal 4 – Volume spike
    signals.append(
        _signal_volume_spike(
            daily_volumes, thresholds.volume_ma_period, thresholds.volume_multiplier,
        )
    )

    # Signal 5 – Percentile
    signals.append(_signal_percentile(daily_closes, thresholds.percentile_max))

    confluence = sum(1 for s in signals if s.active)
    score = confluence / len(signals)
    actionable = confluence >= thresholds.min_confluence

    return ExtremeResult(
        symbol=symbol,
        score=round(score, 2),
        confluence=confluence,
        actionable=actionable,
        signals=signals,
        current_price=daily_closes[-1] if daily_closes else 0.0,
        ath=max(daily_closes) if daily_closes else 0.0,
    )


def passes_survival_filter(
    symbol: str,
    volume_24h: float,
    listing_days: int,
    survival: SurvivalFilter | None = None,
) -> Tuple[bool, str]:
    """Check whether *symbol* passes the survival filter.

    Returns ``(True, "")`` if it passes, or ``(False, reason)`` otherwise.
    """
    if survival is None:
        survival = SurvivalFilter()

    base = symbol.replace("USDT", "").replace("USDC", "")
    if base in survival.excluded_symbols:
        return False, f"{base} está en la lista de exclusión (stablecoin/wrapped)"

    if volume_24h < survival.min_volume_24h_usd:
        return False, f"Volumen 24h ${volume_24h:,.0f} < ${survival.min_volume_24h_usd:,.0f}"

    if listing_days < survival.min_listing_days:
        return False, f"Listado hace {listing_days} días < {survival.min_listing_days} días"

    return True, ""
