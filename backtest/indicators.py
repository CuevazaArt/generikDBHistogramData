"""Technical indicators for backtest features."""
from math import sqrt
from typing import Dict, List, Optional


def _rolling_sma(values: List[float], period: int) -> List[Optional[float]]:
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


def _rolling_ema(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or not values:
        return out
    alpha = 2.0 / (period + 1.0)
    ema: Optional[float] = None
    for i, v in enumerate(values):
        if ema is None:
            ema = v
        else:
            ema = alpha * v + (1.0 - alpha) * ema
        if i >= period - 1:
            out[i] = ema
    return out


def _rsi(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return out
    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period + 1, len(values)):
        gain = gains[i - 1]
        loss = losses[i - 1]
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def _atr(highs: List[float], lows: List[float], closes: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(highs)
    if period <= 0 or len(highs) < 2:
        return out
    trs = [0.0]
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr_vals = _rolling_ema(trs, period)
    return atr_vals


def apply_indicators(
    candles: List[Dict],
    sma_period: int = 20,
    ema_period: int = 20,
    rsi_period: int = 14,
    atr_period: int = 14,
    price_key: str = "price_source",
) -> List[Dict]:
    if not candles:
        return candles
    prices = [float(c.get(price_key, c["close"])) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    sma_vals = _rolling_sma(prices, sma_period)
    ema_vals = _rolling_ema(prices, ema_period)
    rsi_vals = _rsi(prices, rsi_period)
    atr_vals = _atr(highs, lows, closes, atr_period)
    for i, c in enumerate(candles):
        c["sma"] = sma_vals[i]
        c["ema"] = ema_vals[i]
        c["rsi"] = rsi_vals[i]
        c["atr"] = atr_vals[i]
    return candles


def equity_sharpe_ratio(equity_curve: List[float]) -> float:
    if len(equity_curve) < 3:
        return 0.0
    returns = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        cur = equity_curve[i]
        if prev <= 0:
            continue
        returns.append((cur - prev) / prev)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * sqrt(252.0)

