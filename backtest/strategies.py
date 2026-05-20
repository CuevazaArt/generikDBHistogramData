"""Built-in example strategies."""
from backtest.pecunator_trend import annotate_pecunator_gates
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

    name = "dorothy_legacy"

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


class DorothyHubStrategy(StrategyBase):
    """Backtest adapter for Pecunator Dorothy behavior."""

    name = "dorothy"

    def __init__(
        self,
        profit_factor: float = 0.05,
        margin_drop_factor: float = 0.03,
        quote_order_qty_usdt: float = 8.0,
        max_rungs: int = 5,
        **params,
    ):
        super().__init__(
            profit_factor=profit_factor,
            margin_drop_factor=margin_drop_factor,
            quote_order_qty_usdt=quote_order_qty_usdt,
            max_rungs=max_rungs,
            **params,
        )
        self.profit_factor = max(0.0, float(profit_factor))
        self.margin_drop_factor = max(0.0, float(margin_drop_factor))
        self.quote_order_qty_usdt = max(1.0, float(quote_order_qty_usdt))
        self.max_rungs = max(1, int(max_rungs))
        self.active_sell_limits: list[float] = []

    def on_start(self, candles):
        annotate_pecunator_gates(candles, price_key="price_source")

    def on_bar(self, ctx: StrategyContext) -> Signal:
        price = float(ctx.candle.get("price_source", ctx.candle["close"]))
        trend = str(ctx.candle.get("pec_trend", "UNKNOWN"))
        entry_gate = str(ctx.candle.get("pec_entry_gate", "UNKNOWN"))
        if trend != "BULLISH":
            return Signal(action="hold", reason="wait_trend_bullish", metadata={"trend": trend})
        if entry_gate != "BLOCKED":
            return Signal(action="hold", reason="wait_entry_blocked", metadata={"entry_gate": entry_gate})
        if price <= 0:
            return Signal(action="hold", reason="invalid_price")

        self.active_sell_limits = [p for p in self.active_sell_limits if p > 0]
        if self.active_sell_limits and ctx.position_qty > 0:
            hit = [p for p in self.active_sell_limits if price >= p]
            if hit:
                ratio = len(hit) / max(1, len(self.active_sell_limits))
                self.active_sell_limits = [p for p in self.active_sell_limits if p not in hit]
                return Signal(
                    action="sell",
                    size_pct=max(0.01, min(1.0, ratio)),
                    reason="take_profit_limit_hit",
                    metadata={"hit_limits": len(hit), "remaining_limits": len(self.active_sell_limits)},
                )

        if len(self.active_sell_limits) >= self.max_rungs:
            return Signal(action="hold", reason="max_rungs_reached")
        if ctx.cash < self.quote_order_qty_usdt:
            return Signal(action="hold", reason="insufficient_cash")

        if not self.active_sell_limits:
            size_pct = max(0.01, min(1.0, self.quote_order_qty_usdt / max(ctx.cash, 1e-9)))
            return Signal(action="buy", size_pct=size_pct, reason="initial_reference_buy")

        anchor = min(self.active_sell_limits)
        drop_trigger = anchor * (1.0 - (self.profit_factor + self.margin_drop_factor))
        if price <= drop_trigger:
            size_pct = max(0.01, min(1.0, self.quote_order_qty_usdt / max(ctx.cash, 1e-9)))
            return Signal(
                action="buy",
                size_pct=size_pct,
                reason="dca_drop_below_anchor",
                metadata={"anchor_limit": anchor, "drop_trigger": drop_trigger},
            )
        return Signal(action="hold")

    def on_fill(self, fill, signal: Signal, ctx: StrategyContext) -> None:
        _ = signal, ctx
        if fill.get("side") == "buy":
            fill_price = float(fill.get("price", 0.0) or 0.0)
            if fill_price > 0:
                self.active_sell_limits.append(fill_price * (1.0 + self.profit_factor))


class ElphabaHubStrategy(StrategyBase):
    """Backtest adapter for Pecunator Elphaba behavior."""

    name = "elphaba"

    def __init__(
        self,
        profit_factor: float = 0.05,
        margin_rise_factor: float = 0.03,
        quote_order_qty_usdt: float = 8.0,
        max_rungs: int = 5,
        **params,
    ):
        super().__init__(
            profit_factor=profit_factor,
            margin_rise_factor=margin_rise_factor,
            quote_order_qty_usdt=quote_order_qty_usdt,
            max_rungs=max_rungs,
            **params,
        )
        self.profit_factor = max(0.0, float(profit_factor))
        self.margin_rise_factor = max(0.0, float(margin_rise_factor))
        self.quote_order_qty_usdt = max(1.0, float(quote_order_qty_usdt))
        self.max_rungs = max(1, int(max_rungs))
        # Reuses long-only broker by treating "short anchors" as sell triggers.
        self.short_entry_anchors: list[float] = []

    def on_start(self, candles):
        annotate_pecunator_gates(candles, price_key="price_source")

    def on_bar(self, ctx: StrategyContext) -> Signal:
        price = float(ctx.candle.get("price_source", ctx.candle["close"]))
        trend = str(ctx.candle.get("pec_trend", "UNKNOWN"))
        entry_gate = str(ctx.candle.get("pec_entry_gate", "UNKNOWN"))
        if trend != "BEARISH":
            return Signal(action="hold", reason="wait_trend_bearish", metadata={"trend": trend})
        if entry_gate != "BLOCKED":
            return Signal(action="hold", reason="wait_entry_blocked", metadata={"entry_gate": entry_gate})
        if price <= 0:
            return Signal(action="hold", reason="invalid_price")

        # Cover logic on downward move (mapped to spot sell of held position).
        if self.short_entry_anchors and ctx.position_qty > 0:
            hit = []
            for entry in self.short_entry_anchors:
                cover_target = entry * (1.0 - self.profit_factor)
                if price <= cover_target:
                    hit.append(entry)
            if hit:
                ratio = len(hit) / max(1, len(self.short_entry_anchors))
                self.short_entry_anchors = [p for p in self.short_entry_anchors if p not in hit]
                return Signal(
                    action="sell",
                    size_pct=max(0.01, min(1.0, ratio)),
                    reason="cover_target_hit",
                    metadata={"hit_anchors": len(hit), "remaining_anchors": len(self.short_entry_anchors)},
                )

        if len(self.short_entry_anchors) >= self.max_rungs:
            return Signal(action="hold", reason="max_rungs_reached")
        if ctx.cash < self.quote_order_qty_usdt:
            return Signal(action="hold", reason="insufficient_cash")

        should_buy = False
        threshold = None
        if not self.short_entry_anchors:
            should_buy = True
        else:
            highest_short = max(self.short_entry_anchors)
            threshold = highest_short * (1.0 + self.profit_factor + self.margin_rise_factor)
            should_buy = price >= threshold
        if should_buy:
            size_pct = max(0.01, min(1.0, self.quote_order_qty_usdt / max(ctx.cash, 1e-9)))
            return Signal(
                action="buy",
                size_pct=size_pct,
                reason="inverse_dca_entry",
                metadata={"rise_trigger": threshold},
            )
        return Signal(action="hold")

    def on_fill(self, fill, signal: Signal, ctx: StrategyContext) -> None:
        _ = signal, ctx
        if fill.get("side") == "buy":
            fill_price = float(fill.get("price", 0.0) or 0.0)
            if fill_price > 0:
                self.short_entry_anchors.append(fill_price)


class HeikinAshiTrendStrategy(StrategyBase):
    """Trade Heikin-Ashi MA crossover with configurable direction mode.

    Modes:
    - both: bullish cross buys, bearish cross sells
    - long: same as both, explicitly one-directional long cycle
    - short: inverse cycle for spot backtests (bearish cross buys, bullish cross sells)
    """

    name = "ha_trend"

    def __init__(
        self,
        trend_mode: str = "both",
        quote_order_qty_usdt: float = 8.0,
        **params,
    ):
        super().__init__(trend_mode=trend_mode, quote_order_qty_usdt=quote_order_qty_usdt, **params)
        mode = (trend_mode or "both").strip().lower()
        if mode not in ("both", "long", "short"):
            mode = "both"
        self.trend_mode = mode
        self.quote_order_qty_usdt = max(1.0, float(quote_order_qty_usdt))
        self.prev_trend: str | None = None

    def on_start(self, candles):
        annotate_pecunator_gates(candles, price_key="price_source")
        self.prev_trend = None

    def on_bar(self, ctx: StrategyContext) -> Signal:
        price = float(ctx.candle.get("price_source", ctx.candle["close"]))
        trend = str(ctx.candle.get("pec_trend", "UNKNOWN"))
        if price <= 0 or trend not in ("BULLISH", "BEARISH"):
            return Signal(action="hold", reason="invalid_state")
        if self.prev_trend is None:
            self.prev_trend = trend
            return Signal(action="hold", reason="warmup")

        cross_up = self.prev_trend == "BEARISH" and trend == "BULLISH"
        cross_down = self.prev_trend == "BULLISH" and trend == "BEARISH"
        self.prev_trend = trend

        if self.trend_mode in ("both", "long"):
            if cross_up and ctx.cash >= self.quote_order_qty_usdt:
                size_pct = max(0.01, min(1.0, self.quote_order_qty_usdt / max(ctx.cash, 1e-9)))
                return Signal(action="buy", size_pct=size_pct, reason="ha_ma_cross_up", metadata={"mode": self.trend_mode})
            if cross_down and ctx.position_qty > 0:
                return Signal(action="sell", size_pct=1.0, reason="ha_ma_cross_down", metadata={"mode": self.trend_mode})
            return Signal(action="hold")

        # short mode (inverse in spot backtest terms)
        if cross_down and ctx.cash >= self.quote_order_qty_usdt:
            size_pct = max(0.01, min(1.0, self.quote_order_qty_usdt / max(ctx.cash, 1e-9)))
            return Signal(action="buy", size_pct=size_pct, reason="ha_ma_cross_down_inverse", metadata={"mode": self.trend_mode})
        if cross_up and ctx.position_qty > 0:
            return Signal(action="sell", size_pct=1.0, reason="ha_ma_cross_up_inverse", metadata={"mode": self.trend_mode})
        return Signal(action="hold")


class MashaPlaceholderStrategy(StrategyBase):
    """Temporary adapter for future Masha strategy integration."""

    name = "masha"

    def __init__(self, placeholder_level: int = 1, **params):
        super().__init__(placeholder_level=placeholder_level, **params)
        self.placeholder_level = max(1, int(placeholder_level))

    def on_bar(self, ctx: StrategyContext) -> Signal:
        _ = ctx
        return Signal(
            action="hold",
            reason="placeholder_masha_pending_adapter",
            metadata={"placeholder_level": self.placeholder_level},
        )


class ThusneldaPlaceholderStrategy(StrategyBase):
    """Temporary adapter for future Thusnelda strategy integration."""

    name = "thusnelda"

    def __init__(self, placeholder_level: int = 1, **params):
        super().__init__(placeholder_level=placeholder_level, **params)
        self.placeholder_level = max(1, int(placeholder_level))

    def on_bar(self, ctx: StrategyContext) -> Signal:
        _ = ctx
        return Signal(
            action="hold",
            reason="placeholder_thusnelda_pending_adapter",
            metadata={"placeholder_level": self.placeholder_level},
        )
