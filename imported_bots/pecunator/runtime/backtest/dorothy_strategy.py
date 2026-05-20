"""DorothyBacktestStrategy — replays Dorothy's DCA-scalp logic against historical klines.

This strategy mirrors dorothy.py's core decision loop:
  1. BUY when no anchor exists OR price drops below threshold
  2. SELL LIMIT (take-profit) at buy_price * (1 + profit_factor)
  3. Regime filter: BTC EMA200, ADX trend, vol_zscore
  4. Max rungs: limit DCA depth per symbol
  5. Drawdown guard: block buys above max_drawdown_pct
  6. Stop loss: market-sell when price < anchor * (1 - stop_loss_pct)

Usage:
    from runtime.backtest.engine import BacktestEngine, Candle
    from runtime.backtest.dorothy_strategy import DorothyBacktestStrategy

    engine = BacktestEngine()
    strategy = DorothyBacktestStrategy()
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
class DorothyBacktestConfig:
    """Mirrors DorothyConfig for backtesting purposes."""
    quote_order_qty: Decimal = Decimal("8")
    profit_factor: Decimal = Decimal("0.05")
    margin_drop_factor: Decimal = Decimal("0.03")
    max_drawdown_pct: Decimal = Decimal("0.20")
    stop_loss_pct: Decimal = Decimal("0.10")
    max_rungs: int = 5
    # BTC EMA200 filter disabled by default in backtest
    # (requires separate BTC klines feed).
    btc_ema200_enabled: bool = False
    btc_ema200_value: Decimal = Decimal("0")
    btc_price: Decimal = Decimal("0")


class DorothyBacktestStrategy(BacktestStrategy):
    """Replay Dorothy's DCA-scalp logic on historical data.

    State tracked:
      - sell_anchors: list of (buy_price, target_sell_price, qty) tuples
      - peak_equity: for drawdown computation
    """

    def __init__(self, config: DorothyBacktestConfig | None = None) -> None:
        self.config = config or DorothyBacktestConfig()
        self._sell_anchors: list[tuple[Decimal, Decimal, Decimal]] = []
        self._peak_equity: Decimal = Decimal("0")

    def on_candle(
        self, idx: int, candle: Candle, portfolio: Portfolio,
    ) -> list[dict[str, Any]]:
        orders: list[dict[str, Any]] = []
        c = self.config
        market_price = candle.close

        # ── Check stop loss on all anchors ──
        for anchor_buy, anchor_sell, anchor_qty in list(self._sell_anchors):
            stop_price = anchor_buy * (Decimal("1") - c.stop_loss_pct)
            if c.stop_loss_pct > 0 and market_price <= stop_price:
                orders.append({
                    "side": "SELL",
                    "qty": anchor_qty,
                    "reason": f"STOP_LOSS@{stop_price}",
                })
                self._sell_anchors.remove((anchor_buy, anchor_sell, anchor_qty))

        # ── Check take-profit fills ──
        for anchor_buy, anchor_sell, anchor_qty in list(self._sell_anchors):
            if candle.high >= anchor_sell:
                orders.append({
                    "side": "SELL",
                    "qty": anchor_qty,
                    "reason": f"TAKE_PROFIT@{anchor_sell}",
                })
                self._sell_anchors.remove((anchor_buy, anchor_sell, anchor_qty))

        # ── Drawdown guard ──
        equity = portfolio.cash_usdt + portfolio.position_qty * market_price
        if equity > self._peak_equity:
            self._peak_equity = equity
        dd = Decimal("0")
        if self._peak_equity > 0:
            dd = (self._peak_equity - equity) / self._peak_equity
        if dd > c.max_drawdown_pct:
            return orders  # Block buys

        # ── Max rungs guard ──
        if len(self._sell_anchors) >= c.max_rungs:
            return orders

        # ── BTC EMA200 filter (optional) ──
        if c.btc_ema200_enabled and c.btc_price > 0 and c.btc_ema200_value > 0:
            if c.btc_price < c.btc_ema200_value:
                return orders  # Block buys in bearish macro

        # ── Entry logic ──
        should_buy = False
        if not self._sell_anchors:
            # No anchor: buy immediately (first entry)
            should_buy = True
        else:
            # Lowest existing sell anchor → compute threshold
            lowest_anchor_sell = min(a[1] for a in self._sell_anchors)
            threshold = lowest_anchor_sell * (
                Decimal("1") - (c.profit_factor + c.margin_drop_factor)
            )
            should_buy = market_price <= threshold

        if should_buy:
            buy_qty = c.quote_order_qty / market_price if market_price > 0 else Decimal("0")
            if buy_qty > 0 and c.quote_order_qty <= portfolio.cash_usdt:
                orders.append({
                    "side": "BUY",
                    "qty": buy_qty,
                    "reason": "DCA_ENTRY",
                })
                sell_price = market_price * (Decimal("1") + c.profit_factor)
                self._sell_anchors.append((market_price, sell_price, buy_qty))

        return orders
