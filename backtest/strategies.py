"""Built-in example strategies."""
from backtest.strategy_base import Signal, StrategyBase, StrategyContext


class SmaCrossStrategy(StrategyBase):
    name = "sma_cross"

    def __init__(self, fast: int = 10, slow: int = 30, **params):
        super().__init__(fast=fast, slow=slow, **params)
        self.fast = int(fast)
        self.slow = int(slow)

    def on_bar(self, ctx: StrategyContext) -> Signal:
        if ctx.index < max(self.fast, self.slow):
            return Signal(action="hold", reason="warmup")
        c = ctx.candles[ctx.index]
        prev = ctx.candles[ctx.index - 1]
        c_fast = c.get(f"sma_{self.fast}", c.get("sma"))
        c_slow = c.get(f"sma_{self.slow}", c.get("ema"))
        p_fast = prev.get(f"sma_{self.fast}", prev.get("sma"))
        p_slow = prev.get(f"sma_{self.slow}", prev.get("ema"))
        if None in (c_fast, c_slow, p_fast, p_slow):
            return Signal(action="hold", reason="indicator_none")
        if p_fast <= p_slow and c_fast > c_slow:
            return Signal(action="buy", reason="golden_cross")
        if p_fast >= p_slow and c_fast < c_slow and ctx.position_qty > 0:
            return Signal(action="sell", reason="death_cross")
        return Signal(action="hold")


class DorothyBacktestStrategy(StrategyBase):
    """Backtest adapter inspired by Dorothy live logic."""

    name = "dorothy"

    def __init__(
        self,
        profit_factor: float = 0.05,
        margin_drop_factor: float = 0.004,
        quote_order_qty_usdt: float = 8.0,
        min_order_notional: float = 6.0,
        max_order_notional: float = 10.0,
        max_active_orders: int = 200,
        **params,
    ):
        super().__init__(
            profit_factor=profit_factor,
            margin_drop_factor=margin_drop_factor,
            quote_order_qty_usdt=quote_order_qty_usdt,
            min_order_notional=min_order_notional,
            max_order_notional=max_order_notional,
            max_active_orders=max_active_orders,
            **params,
        )
        self.profit_factor = max(0.0, float(profit_factor))
        self.margin_drop_factor = max(0.0, float(margin_drop_factor))
        self.min_order_notional = max(0.0, float(min_order_notional))
        self.max_order_notional = max(self.min_order_notional, float(max_order_notional))
        self.quote_order_qty_usdt = min(
            self.max_order_notional,
            max(self.min_order_notional, float(quote_order_qty_usdt)),
        )
        self.max_active_orders = max(1, int(max_active_orders))
        self.active_sell_limits: list[float] = []

    def on_bar(self, ctx: StrategyContext) -> Signal:
        price = float(ctx.candle.get("price_source", ctx.candle["close"]))
        if price <= 0:
            return Signal(action="hold", reason="invalid_price")

        self.active_sell_limits = [p for p in self.active_sell_limits if p > 0]

        # Dorothy closes only through limit sell trigger.
        if self.active_sell_limits and ctx.position_qty > 0:
            hit = [p for p in self.active_sell_limits if price >= p]
            if hit:
                ratio = len(hit) / max(1, len(self.active_sell_limits))
                self.active_sell_limits = [p for p in self.active_sell_limits if p not in hit]
                return Signal(
                    action="sell",
                    size_pct=max(0.01, min(1.0, ratio)),
                    reason="sell_limit_hit",
                    metadata={"hit_limits": len(hit), "remaining_limits": len(self.active_sell_limits)},
                )

        if len(self.active_sell_limits) >= self.max_active_orders:
            return Signal(action="hold", reason="max_active_orders_reached")

        # Enforce notional bounds [6, 10] USDT by operation.
        desired_notional = min(self.max_order_notional, max(self.min_order_notional, self.quote_order_qty_usdt))
        if ctx.cash < self.min_order_notional or ctx.cash < desired_notional:
            return Signal(action="hold", reason="insufficient_notional")

        if not self.active_sell_limits:
            size_pct = max(0.01, min(1.0, desired_notional / max(ctx.cash, 1e-9)))
            return Signal(action="buy", size_pct=size_pct, reason="initial_reference_buy")

        anchor = min(self.active_sell_limits)
        drop_trigger = anchor * (1.0 - (self.profit_factor + self.margin_drop_factor))
        if price <= drop_trigger:
            size_pct = max(0.01, min(1.0, desired_notional / max(ctx.cash, 1e-9)))
            return Signal(
                action="buy",
                size_pct=size_pct,
                reason="buy_below_anchor_threshold",
                metadata={"anchor_limit": anchor, "drop_trigger": drop_trigger},
            )
        return Signal(action="hold")

    def on_fill(self, fill, signal: Signal, ctx: StrategyContext) -> None:
        _ = ctx
        if fill.get("side") == "buy":
            fill_price = float(fill.get("price", 0.0) or 0.0)
            if fill_price > 0:
                self.active_sell_limits.append(fill_price * (1.0 + self.profit_factor))

