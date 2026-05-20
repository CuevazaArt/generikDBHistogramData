"""ElphabaBacktestStrategy — replays Elphaba's SHORT DCA-scalp logic against historical klines.

This strategy mirrors elphaba.py's core decision loop (inverse of Dorothy):
  1. SHORT (sell) when no anchor exists OR price rises above threshold
  2. BUY BACK (cover) at short_price * (1 - profit_factor)
  3. Max rungs: limit DCA depth per symbol
  4. Drawdown guard: block new shorts above max_drawdown_pct
  5. Stop loss: market-buy (cover) when price > anchor * (1 + stop_loss_pct)

Usage:
    from runtime.backtest.engine import BacktestEngine, Candle
    from runtime.backtest.elphaba_strategy import ElphabaBacktestStrategy

    engine = BacktestEngine()
    strategy = ElphabaBacktestStrategy()
    candles = [Candle.from_binance_kline(k) for k in klines_data]
    result = engine.run(strategy, candles, symbol="XRPUSDT")
    print(result.summary())
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from runtime.backtest.engine import BacktestStrategy, Candle, Portfolio


@dataclass
class ElphabaBacktestConfig:
    """Mirrors ElphabaConfig for backtesting purposes."""
    quote_order_qty: Decimal = Decimal("8")
    profit_factor: Decimal = Decimal("0.05")
    margin_rise_factor: Decimal = Decimal("0.03")
    max_drawdown_pct: Decimal = Decimal("0.20")
    stop_loss_pct: Decimal = Decimal("0.10")
    max_rungs: int = 5


class ElphabaBacktestStrategy(BacktestStrategy):
    """Replay Elphaba's SHORT DCA-scalp logic on historical data.

    State tracked:
      - cover_anchors: list of (short_price, target_cover_price, qty) tuples
      - peak_equity: for drawdown computation

    Elphaba profits from downward movement. She:
      - Opens SHORT positions (sells borrowed asset)
      - Closes them with BUY BACK (covers) at a lower price
      - DCA upward: adds more shorts when price rises above a margin threshold
    """

    def __init__(self, config: ElphabaBacktestConfig | None = None) -> None:
        self.config = config or ElphabaBacktestConfig()
        # (short_entry_price, target_cover_price, qty)
        self._cover_anchors: list[tuple[Decimal, Decimal, Decimal]] = []
        self._peak_equity: Decimal = Decimal("0")

    def on_candle(
        self, idx: int, candle: Candle, portfolio: Portfolio,
    ) -> list[dict[str, Any]]:
        orders: list[dict[str, Any]] = []
        c = self.config
        market_price = candle.close

        # ── Check stop loss on all anchors (price rose too much) ──
        for short_price, cover_target, qty in list(self._cover_anchors):
            stop_price = short_price * (Decimal("1") + c.stop_loss_pct)
            if c.stop_loss_pct > 0 and market_price >= stop_price:
                orders.append({
                    "side": "BUY",  # Cover — close the short
                    "qty": qty,
                    "reason": f"STOP_LOSS_COVER@{stop_price}",
                })
                self._cover_anchors.remove((short_price, cover_target, qty))

        # ── Check take-profit fills (price dropped to target) ──
        for short_price, cover_target, qty in list(self._cover_anchors):
            if candle.low <= cover_target:
                orders.append({
                    "side": "BUY",  # Cover — close the short
                    "qty": qty,
                    "reason": f"TAKE_PROFIT_COVER@{cover_target}",
                })
                self._cover_anchors.remove((short_price, cover_target, qty))

        # ── Drawdown guard ──
        # For shorts: equity = cash + unrealized P&L from short positions
        short_pnl = sum(
            (sp - market_price) * q for sp, _, q in self._cover_anchors
        )
        equity = portfolio.cash_usdt + short_pnl
        if equity > self._peak_equity:
            self._peak_equity = equity
        dd = Decimal("0")
        if self._peak_equity > 0:
            dd = (self._peak_equity - equity) / self._peak_equity
        if dd > c.max_drawdown_pct:
            return orders  # Block new shorts

        # ── Max rungs guard ──
        if len(self._cover_anchors) >= c.max_rungs:
            return orders

        # ── Entry logic (open short) ──
        should_short = False
        if not self._cover_anchors:
            # No anchor: short immediately (first entry)
            should_short = True
        else:
            # Highest existing short entry → compute threshold for adding
            highest_short = max(a[0] for a in self._cover_anchors)
            threshold = highest_short * (
                Decimal("1") + (c.profit_factor + c.margin_rise_factor)
            )
            should_short = market_price >= threshold

        if should_short:
            short_qty = c.quote_order_qty / market_price if market_price > 0 else Decimal("0")
            if short_qty > 0 and c.quote_order_qty <= portfolio.cash_usdt:
                orders.append({
                    "side": "SELL",  # Open short
                    "qty": short_qty,
                    "reason": "DCA_SHORT_ENTRY",
                })
                cover_price = market_price * (Decimal("1") - c.profit_factor)
                self._cover_anchors.append((market_price, cover_price, short_qty))

        return orders
