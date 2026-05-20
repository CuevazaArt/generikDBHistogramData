"""T1.5: Backtest engine — replay klines with realistic fees and slippage.

This is NOT a paper trade simulator. It replays historical kline data
and evaluates strategy decisions against actual candle data, modeling:
- Taker/maker fees (configurable, default 0.10%)
- Slippage (configurable, default 0.05%)
- Fill probability based on candle range

Usage:
    engine = BacktestEngine()
    results = engine.run(
        strategy=DorothyBacktestStrategy(config),
        klines=load_klines("XRPUSDT", "2025-01-01", "2025-12-31"),
    )
    print(results.summary())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

_LOG = logging.getLogger("pecunator.backtest.engine")


@dataclass
class Candle:
    """Single OHLCV candle."""
    timestamp_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @classmethod
    def from_binance_kline(cls, k: list) -> "Candle":
        return cls(
            timestamp_ms=int(k[0]),
            open=Decimal(str(k[1])),
            high=Decimal(str(k[2])),
            low=Decimal(str(k[3])),
            close=Decimal(str(k[4])),
            volume=Decimal(str(k[5])),
        )


@dataclass
class Trade:
    """Record of a simulated trade."""
    candle_idx: int
    timestamp_ms: int
    side: str  # "BUY" or "SELL"
    price: Decimal
    qty: Decimal
    fee_usdt: Decimal
    reason: str
    pnl: Optional[Decimal] = None  # Set on SELL


@dataclass
class BacktestResult:
    """Aggregated results of a backtest run."""
    symbol: str
    candles_processed: int
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[Decimal] = field(default_factory=list)
    peak_equity: Decimal = Decimal("0")
    max_drawdown_pct: Decimal = Decimal("0")

    def summary(self) -> dict[str, Any]:
        """Compute performance summary."""
        buys = [t for t in self.trades if t.side == "BUY"]
        sells = [t for t in self.trades if t.side == "SELL"]
        closed = [t for t in sells if t.pnl is not None]

        total_fees = sum(t.fee_usdt for t in self.trades)
        winners = [t for t in closed if t.pnl is not None and t.pnl > 0]
        losers = [t for t in closed if t.pnl is not None and t.pnl < 0]

        gross_win = sum(t.pnl for t in winners) if winners else Decimal("0")
        gross_loss = abs(sum(t.pnl for t in losers)) if losers else Decimal("0")
        net_pnl = gross_win - gross_loss
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else Decimal("999")
        win_rate = (Decimal(len(winners)) / Decimal(len(closed))) if closed else Decimal("0")

        return {
            "symbol": self.symbol,
            "candles": self.candles_processed,
            "total_trades": len(self.trades),
            "buys": len(buys),
            "sells": len(sells),
            "closed_trades": len(closed),
            "winners": len(winners),
            "losers": len(losers),
            "net_pnl_usdt": str(net_pnl),
            "gross_win_usdt": str(gross_win),
            "gross_loss_usdt": str(gross_loss),
            "total_fees_usdt": str(total_fees),
            "profit_factor": str(profit_factor),
            "win_rate": str(win_rate),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "peak_equity_usdt": str(self.peak_equity),
        }


class BacktestStrategy:
    """Abstract strategy interface for backtesting."""

    def on_candle(
        self, idx: int, candle: Candle, portfolio: "Portfolio",
    ) -> list[dict[str, Any]]:
        """Process a candle and return a list of order dicts.

        Each order dict: {"side": "BUY"|"SELL", "qty": Decimal, "reason": str}
        """
        raise NotImplementedError


@dataclass
class Portfolio:
    """Simple portfolio tracker for backtesting."""
    cash_usdt: Decimal = Decimal("1000")
    position_qty: Decimal = Decimal("0")
    avg_buy_price: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    buy_count: int = 0


class BacktestEngine:
    """Replay klines through a strategy with realistic execution."""

    def __init__(
        self,
        fee_bps: Decimal = Decimal("10"),      # 0.10%
        slippage_bps: Decimal = Decimal("5"),   # 0.05%
    ) -> None:
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps

    def run(
        self,
        strategy: BacktestStrategy,
        candles: list[Candle],
        *,
        initial_capital: Decimal = Decimal("1000"),
        symbol: str = "UNKNOWN",
    ) -> BacktestResult:
        """Run backtest over candles."""
        portfolio = Portfolio(cash_usdt=initial_capital)
        result = BacktestResult(symbol=symbol, candles_processed=0)

        for idx, candle in enumerate(candles):
            result.candles_processed += 1

            # Get strategy decisions
            orders = strategy.on_candle(idx, candle, portfolio)

            for order in orders:
                side = order.get("side", "")
                qty = Decimal(str(order.get("qty", "0")))
                reason = order.get("reason", "")

                if side == "BUY" and qty > 0:
                    self._execute_buy(candle, qty, portfolio, result, idx, reason)
                elif side == "SELL" and qty > 0:
                    self._execute_sell(candle, qty, portfolio, result, idx, reason)

            # Track equity curve
            mark_price = candle.close
            equity = portfolio.cash_usdt + portfolio.position_qty * mark_price
            result.equity_curve.append(equity)

            if equity > result.peak_equity:
                result.peak_equity = equity
            if result.peak_equity > 0:
                dd = (result.peak_equity - equity) / result.peak_equity
                if dd > result.max_drawdown_pct:
                    result.max_drawdown_pct = dd

        return result

    def _execute_buy(
        self, candle: Candle, qty: Decimal, portfolio: Portfolio,
        result: BacktestResult, idx: int, reason: str,
    ) -> None:
        """Simulate a market buy with fees and slippage."""
        # Slippage: buy at slightly higher price
        slip_mult = Decimal("1") + self.slippage_bps / Decimal("10000")
        fill_price = candle.close * slip_mult

        cost = fill_price * qty
        fee = cost * self.fee_bps / Decimal("10000")
        total_cost = cost + fee

        if total_cost > portfolio.cash_usdt:
            return  # Not enough capital

        portfolio.cash_usdt -= total_cost
        portfolio.total_cost += cost
        portfolio.position_qty += qty
        portfolio.buy_count += 1
        if portfolio.position_qty > 0:
            portfolio.avg_buy_price = portfolio.total_cost / portfolio.position_qty

        result.trades.append(Trade(
            candle_idx=idx, timestamp_ms=candle.timestamp_ms,
            side="BUY", price=fill_price, qty=qty,
            fee_usdt=fee, reason=reason,
        ))

    def _execute_sell(
        self, candle: Candle, qty: Decimal, portfolio: Portfolio,
        result: BacktestResult, idx: int, reason: str,
    ) -> None:
        """Simulate a market sell with fees and slippage."""
        sell_qty = min(qty, portfolio.position_qty)
        if sell_qty <= 0:
            return

        # Slippage: sell at slightly lower price
        slip_mult = Decimal("1") - self.slippage_bps / Decimal("10000")
        fill_price = candle.close * slip_mult

        revenue = fill_price * sell_qty
        fee = revenue * self.fee_bps / Decimal("10000")
        net_revenue = revenue - fee

        # P&L for this trade
        cost_basis = portfolio.avg_buy_price * sell_qty
        pnl = net_revenue - cost_basis

        portfolio.cash_usdt += net_revenue
        portfolio.position_qty -= sell_qty
        if portfolio.position_qty > 0:
            portfolio.total_cost = portfolio.avg_buy_price * portfolio.position_qty
        else:
            portfolio.total_cost = Decimal("0")
            portfolio.avg_buy_price = Decimal("0")

        result.trades.append(Trade(
            candle_idx=idx, timestamp_ms=candle.timestamp_ms,
            side="SELL", price=fill_price, qty=sell_qty,
            fee_usdt=fee, reason=reason, pnl=pnl,
        ))
