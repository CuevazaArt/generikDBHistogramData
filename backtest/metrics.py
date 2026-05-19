"""Backtest metric calculations."""
from typing import Dict, List

from backtest.indicators import equity_sharpe_ratio


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


def summarize_metrics(
    initial_cash: float,
    final_equity: float,
    equity_curve: List[float],
    trade_pnls: List[float],
) -> Dict[str, float]:
    total_return = 0.0
    if initial_cash > 0:
        total_return = (final_equity - initial_cash) / initial_cash
    return {
        "initial_cash": float(initial_cash),
        "final_equity": float(final_equity),
        "total_return": float(total_return),
        "max_drawdown": float(max_drawdown(equity_curve)),
        "sharpe": float(equity_sharpe_ratio(equity_curve)),
        "win_rate": float(win_rate(trade_pnls)),
        "profit_factor": float(profit_factor(trade_pnls)),
        "num_trades": float(len(trade_pnls)),
    }

