"""Backtest metric calculations."""
from math import sqrt
from typing import Dict, List

from backtest.indicators import equity_sharpe_ratio


_PERIODS_PER_YEAR = 252.0


def _returns_from_equity(equity_curve: List[float]) -> List[float]:
    returns: List[float] = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        cur = equity_curve[i]
        if prev <= 0:
            continue
        returns.append((cur - prev) / prev)
    return returns


def max_drawdown(equity_curve: List[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    mdd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd
    return mdd


def win_rate(trade_pnls: List[float]) -> float:
    if not trade_pnls:
        return 0.0
    wins = sum(1 for p in trade_pnls if p > 0)
    return wins / len(trade_pnls)


def profit_factor(trade_pnls: List[float]) -> float:
    gross_profit = sum(p for p in trade_pnls if p > 0)
    gross_loss = abs(sum(p for p in trade_pnls if p < 0))
    if gross_loss == 0:
        return float(gross_profit > 0) * 999.0
    return gross_profit / gross_loss


def sortino_ratio(equity_curve: List[float]) -> float:
    """Sharpe-like ratio that only penalizes downside deviation."""
    returns = _returns_from_equity(equity_curve)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    negatives = [r for r in returns if r < 0]
    if not negatives:
        return 0.0 if mean <= 0 else 999.0
    downside_var = sum(r * r for r in negatives) / len(negatives)
    downside_std = sqrt(downside_var)
    if downside_std == 0:
        return 0.0
    return (mean / downside_std) * sqrt(_PERIODS_PER_YEAR)


def calmar_ratio(total_return: float, mdd: float) -> float:
    """Return divided by max drawdown magnitude."""
    if mdd <= 0:
        return 0.0 if total_return <= 0 else 999.0
    return total_return / mdd


def ulcer_index(equity_curve: List[float]) -> float:
    """RMS of percentage drawdowns over the equity curve."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    squared = 0.0
    count = 0
    for v in equity_curve:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            squared += dd * dd
            count += 1
    if count == 0:
        return 0.0
    return sqrt(squared / count)


def summarize_metrics(
    initial_cash: float,
    final_equity: float,
    equity_curve: List[float],
    trade_pnls: List[float],
) -> Dict[str, float]:
    total_return = 0.0
    if initial_cash > 0:
        total_return = (final_equity - initial_cash) / initial_cash
    mdd = float(max_drawdown(equity_curve))
    return {
        "initial_cash": float(initial_cash),
        "final_equity": float(final_equity),
        "total_return": float(total_return),
        "max_drawdown": mdd,
        "sharpe": float(equity_sharpe_ratio(equity_curve)),
        "sortino": float(sortino_ratio(equity_curve)),
        "calmar": float(calmar_ratio(total_return, mdd)),
        "ulcer_index": float(ulcer_index(equity_curve)),
        "win_rate": float(win_rate(trade_pnls)),
        "profit_factor": float(profit_factor(trade_pnls)),
        "num_trades": float(len(trade_pnls)),
    }

