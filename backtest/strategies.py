"""Built-in example strategies."""
from backtest.exchange_filters import normalize_spot_buy_notional
from decimal import Decimal

from backtest.dorothy_accessories import _to_decimal, decimal_to_str, volumen_compuesto_notional
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

    def export_state(self) -> dict:
        return {
            "active_sell_limits": [float(v) for v in self.active_sell_limits if float(v) > 0.0],
        }

    def import_state(self, state: dict) -> None:
        raw_limits = state.get("active_sell_limits", []) if isinstance(state, dict) else []
        self.active_sell_limits = sorted([float(v) for v in raw_limits if float(v) > 0.0])


class DorothyHubStrategy(StrategyBase):
    """Backtest adapter for Pecunator Dorothy behavior."""

    name = "dorothy"

    def __init__(
        self,
        profit_factor: float = 0.05,
        margin_drop_factor: float = 0.03,
        quote_order_qty_usdt: float = 8.0,
        max_rungs: int = 5,
        volumen_incremental: bool = False,
        volumen_incremental_multiplier: float = 1.05,
        volumen_compuesto: bool = False,
        volumen_compuesto_min_usdt: Decimal | float | str = Decimal("6"),
        volumen_compuesto_greed_factor: Decimal | float | str = Decimal("0"),
        initial_run_cash: float | None = None,
        require_trend_gate: bool = False,
        require_entry_gate: bool = False,
        symbol: str = "XRPUSDT",
        **params,
    ):
        super().__init__(
            profit_factor=profit_factor,
            margin_drop_factor=margin_drop_factor,
            quote_order_qty_usdt=quote_order_qty_usdt,
            max_rungs=max_rungs,
            volumen_incremental=volumen_incremental,
            volumen_incremental_multiplier=volumen_incremental_multiplier,
            volumen_compuesto=volumen_compuesto,
            volumen_compuesto_min_usdt=volumen_compuesto_min_usdt,
            volumen_compuesto_greed_factor=volumen_compuesto_greed_factor,
            initial_run_cash=initial_run_cash,
            require_trend_gate=require_trend_gate,
            require_entry_gate=require_entry_gate,
            symbol=symbol,
            **params,
        )
        self.profit_factor = max(0.0, float(profit_factor))
        self.margin_drop_factor = max(0.0, float(margin_drop_factor))
        self.quote_order_qty_usdt = max(1.0, float(quote_order_qty_usdt))
        # max_rungs <= 0 disables the cap (unlimited DCA rungs for spot backtests).
        self.max_rungs = int(max_rungs)
        self.volumen_incremental = bool(volumen_incremental)
        self.volumen_incremental_multiplier = max(1.0, float(volumen_incremental_multiplier))
        self.volumen_compuesto = bool(volumen_compuesto)
        self.volumen_compuesto_min_usdt = max(Decimal("0"), _to_decimal(volumen_compuesto_min_usdt))
        greed = _to_decimal(volumen_compuesto_greed_factor)
        self.volumen_compuesto_greed_factor = max(Decimal("0"), greed)
        if self.volumen_incremental and self.volumen_compuesto:
            raise ValueError(
                "volumen_incremental and volumen_compuesto are mutually exclusive; enable one accessory per run"
            )
        self.initial_run_cash = (
            float(initial_run_cash) if initial_run_cash is not None and float(initial_run_cash) > 0 else None
        )
        self.require_trend_gate = bool(require_trend_gate)
        self.require_entry_gate = bool(require_entry_gate)
        self.symbol = str(symbol or "XRPUSDT").upper()
        self.active_sell_limits: list[float] = []

    def _quote_notional_for_buy(self, ctx: StrategyContext) -> tuple[float, dict]:
        """Return (notional USDT, accessory metadata) for the next buy."""
        base = float(self.quote_order_qty_usdt)
        meta: dict = {}
        notional = base

        if self.volumen_compuesto:
            if self.initial_run_cash is None:
                raise ValueError("volumen_compuesto requires initial_run_cash > 0")
            notional_dec, factor = volumen_compuesto_notional(
                base_quote_usdt=_to_decimal(self.quote_order_qty_usdt),
                equity=_to_decimal(ctx.equity),
                initial_equity=_to_decimal(self.initial_run_cash),
                min_quote_usdt=self.volumen_compuesto_min_usdt,
                greed_factor=self.volumen_compuesto_greed_factor,
            )
            notional = float(notional_dec)
            meta.update(
                {
                    "volumen_compuesto": True,
                    "volumen_compuesto_factor": decimal_to_str(factor),
                    "volumen_compuesto_greed_factor": decimal_to_str(self.volumen_compuesto_greed_factor),
                    "volumen_compuesto_equity": decimal_to_str(_to_decimal(ctx.equity)),
                    "volumen_compuesto_initial_equity": decimal_to_str(_to_decimal(self.initial_run_cash)),
                    "volumen_compuesto_min_usdt": decimal_to_str(self.volumen_compuesto_min_usdt),
                    "volumen_compuesto_notional_usdt": decimal_to_str(notional_dec),
                }
            )
        elif self.volumen_incremental and self.initial_run_cash is not None:
            available = float(ctx.cash)
            if available > float(self.initial_run_cash) + 1e-9:
                notional *= self.volumen_incremental_multiplier
            meta["volumen_incremental"] = True
            meta["volumen_incremental_multiplier"] = self.volumen_incremental_multiplier

        price = float(ctx.candle.get("price_source", ctx.candle.get("close", 0.0)))
        adj_notional, _qty = normalize_spot_buy_notional(self.symbol, notional, price)
        meta["quote_notional_usdt"] = max(0.0, adj_notional)
        if self.initial_run_cash is not None:
            meta["initial_run_cash"] = self.initial_run_cash
        return max(0.0, adj_notional), meta

    def _buy_signal(
        self,
        ctx: StrategyContext,
        reason: str,
        metadata: dict | None = None,
    ) -> Signal:
        if not self._entry_gate_allows_buy(ctx):
            return Signal(
                action="hold",
                reason="wait_entry_gate_blocked",
                metadata={"entry_gate": str(ctx.candle.get("pec_entry_gate", "UNKNOWN"))},
            )
        notional, sizing_meta = self._quote_notional_for_buy(ctx)
        if notional <= 0 or ctx.cash < notional:
            return Signal(action="hold", reason="insufficient_cash")
        # Preserve target buy notional (after spot-filter normalization) even when
        # available cash is high; avoid forcing Dorothy buys to a 1% floor.
        size_pct = max(0.0, min(1.0, notional / max(ctx.cash, 1e-9)))
        meta = dict(metadata or {})
        meta.update(sizing_meta)
        return Signal(action="buy", size_pct=size_pct, reason=reason, metadata=meta)

    def _entry_gate_allows_buy(self, ctx: StrategyContext) -> bool:
        if not self.require_entry_gate:
            return True
        return str(ctx.candle.get("pec_entry_gate", "CLEAR")) == "BLOCKED"

    def on_start(self, candles):
        annotate_pecunator_gates(candles, price_key="price_source")

    def on_bar(self, ctx: StrategyContext) -> Signal:
        price = float(ctx.candle.get("price_source", ctx.candle["close"]))
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

        if self.require_trend_gate:
            trend = str(ctx.candle.get("pec_trend", "UNKNOWN"))
            if trend != "BULLISH":
                return Signal(action="hold", reason="wait_trend_bullish", metadata={"trend": trend})

        if self.max_rungs > 0 and len(self.active_sell_limits) >= self.max_rungs:
            return Signal(action="hold", reason="max_rungs_reached")

        if not self.active_sell_limits:
            return self._buy_signal(ctx, reason="initial_reference_buy")

        anchor = min(self.active_sell_limits)
        drop_trigger = anchor * (1.0 - (self.profit_factor + self.margin_drop_factor))
        if price <= drop_trigger:
            return self._buy_signal(
                ctx,
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

    def export_state(self) -> dict:
        return {
            "active_sell_limits": [float(v) for v in self.active_sell_limits if float(v) > 0.0],
            "initial_run_cash": self.initial_run_cash,
        }

    def import_state(self, state: dict) -> None:
        raw_limits = state.get("active_sell_limits", []) if isinstance(state, dict) else []
        self.active_sell_limits = sorted([float(v) for v in raw_limits if float(v) > 0.0])
        if isinstance(state, dict) and state.get("initial_run_cash") is not None:
            self.initial_run_cash = float(state["initial_run_cash"])


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
        if trend != "BEARISH":
            return Signal(action="hold", reason="wait_trend_bearish", metadata={"trend": trend})
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


class MashaStrategy(StrategyBase):
    """Single-asset trend/pullback adapter inspired by Masha behavior."""

    name = "masha"

    def __init__(
        self,
        fast: int = 9,
        slow: int = 34,
        quote_order_qty_usdt: float = 8.0,
        take_profit_pct: float = 1.5,
        stop_loss_pct: float = 4.0,
        pullback_factor: float = 0.006,
        **params,
    ):
        super().__init__(
            fast=fast,
            slow=slow,
            quote_order_qty_usdt=quote_order_qty_usdt,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            pullback_factor=pullback_factor,
            **params,
        )
        self.fast = max(2, int(fast))
        self.slow = max(self.fast + 1, int(slow))
        self.quote_order_qty_usdt = max(1.0, float(quote_order_qty_usdt))
        self.take_profit_pct = max(0.0, float(take_profit_pct))
        self.stop_loss_pct = max(0.0, float(stop_loss_pct))
        self.pullback_factor = max(0.0, float(pullback_factor))

    def _sma(self, candles: list[dict], idx: int, period: int) -> float | None:
        if idx + 1 < period:
            return None
        start = idx + 1 - period
        closes = [float(candles[i].get("price_source", candles[i]["close"])) for i in range(start, idx + 1)]
        if not closes:
            return None
        return sum(closes) / float(len(closes))

    def on_bar(self, ctx: StrategyContext) -> Signal:
        price = float(ctx.candle.get("price_source", ctx.candle["close"]))
        if price <= 0:
            return Signal(action="hold", reason="invalid_price")
        if ctx.position_qty > 0 and ctx.avg_entry > 0:
            tp_trigger = float(ctx.avg_entry) * (1.0 + self.take_profit_pct / 100.0)
            sl_trigger = float(ctx.avg_entry) * (1.0 - self.stop_loss_pct / 100.0)
            if self.take_profit_pct > 0 and price >= tp_trigger:
                return Signal(action="sell", size_pct=1.0, reason="masha_take_profit", metadata={"tp_trigger": tp_trigger})
            if self.stop_loss_pct > 0 and price <= sl_trigger:
                return Signal(action="sell", size_pct=1.0, reason="masha_stop_loss", metadata={"sl_trigger": sl_trigger})

        if ctx.index < self.slow:
            return Signal(action="hold", reason="warmup")

        prev_idx = max(0, ctx.index - 1)
        fast_now = self._sma(ctx.candles, ctx.index, self.fast)
        slow_now = self._sma(ctx.candles, ctx.index, self.slow)
        fast_prev = self._sma(ctx.candles, prev_idx, self.fast)
        slow_prev = self._sma(ctx.candles, prev_idx, self.slow)
        if None in (fast_now, slow_now, fast_prev, slow_prev):
            return Signal(action="hold", reason="indicator_none")
        if ctx.cash < self.quote_order_qty_usdt and ctx.position_qty <= 0:
            return Signal(action="hold", reason="insufficient_cash")

        cross_up = fast_prev <= slow_prev and fast_now > slow_now
        cross_down = fast_prev >= slow_prev and fast_now < slow_now
        if cross_down and ctx.position_qty > 0:
            return Signal(action="sell", size_pct=1.0, reason="masha_trend_break")
        if cross_up and ctx.cash >= self.quote_order_qty_usdt:
            size_pct = max(0.01, min(1.0, self.quote_order_qty_usdt / max(ctx.cash, 1e-9)))
            return Signal(action="buy", size_pct=size_pct, reason="masha_trend_cross_up")
        if fast_now > slow_now:
            pullback_price = fast_now * (1.0 - self.pullback_factor)
            if price <= pullback_price and ctx.cash >= self.quote_order_qty_usdt:
                size_pct = max(0.01, min(1.0, self.quote_order_qty_usdt / max(ctx.cash, 1e-9)))
                return Signal(
                    action="buy",
                    size_pct=size_pct,
                    reason="masha_pullback_entry",
                    metadata={"pullback_trigger": pullback_price},
                )
        return Signal(action="hold")


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


class LouiseStrategy(StrategyBase):
    """DCA downside strategy with average-price take profit."""

    name = "louise"

    def __init__(
        self,
        target_profit_pct: float = 1.5,
        margin_drop_factor: float = 0.004,
        quote_order_qty_usdt: float = 8.0,
        **params,
    ):
        super().__init__(
            target_profit_pct=target_profit_pct,
            margin_drop_factor=margin_drop_factor,
            quote_order_qty_usdt=quote_order_qty_usdt,
            **params,
        )
        self.target_profit_pct = max(0.0, float(target_profit_pct))
        self.margin_drop_factor = max(0.0, float(margin_drop_factor))
        self.quote_order_qty_usdt = max(1.0, float(quote_order_qty_usdt))
        self.last_purchase_price = 0.0

    def on_start(self, candles):
        annotate_pecunator_gates(candles, price_key="price_source")

    def on_bar(self, ctx: StrategyContext) -> Signal:
        price = float(ctx.candle.get("price_source", ctx.candle["close"]))
        if price <= 0:
            return Signal(action="hold", reason="invalid_price")
        if self.target_profit_pct > 0 and ctx.position_qty > 0 and ctx.avg_entry > 0:
            tp_price = float(ctx.avg_entry) * (1.0 + self.target_profit_pct / 100.0)
            if price >= tp_price:
                return Signal(action="sell", size_pct=1.0, reason="louise_take_profit", metadata={"tp_price": tp_price})
        if ctx.cash < self.quote_order_qty_usdt:
            return Signal(action="hold", reason="insufficient_cash")
        size_pct = max(0.01, min(1.0, self.quote_order_qty_usdt / max(ctx.cash, 1e-9)))
        if ctx.position_qty <= 0 or self.last_purchase_price <= 0:
            return Signal(action="buy", size_pct=size_pct, reason="louise_initial_buy")
        drop_trigger = self.last_purchase_price * (1.0 - self.margin_drop_factor)
        if price < drop_trigger:
            return Signal(
                action="buy",
                size_pct=size_pct,
                reason="louise_dca_drop",
                metadata={"drop_trigger": drop_trigger},
            )
        return Signal(action="hold")

    def on_fill(self, fill, signal: Signal, ctx: StrategyContext) -> None:
        _ = signal, ctx
        if fill.get("side") == "buy":
            fill_price = float(fill.get("price", 0.0) or 0.0)
            if fill_price > 0:
                self.last_purchase_price = fill_price

    def export_state(self) -> dict:
        return {"last_purchase_price": float(self.last_purchase_price)}

    def import_state(self, state: dict) -> None:
        if state:
            self.last_purchase_price = float(state.get("last_purchase_price", 0.0) or 0.0)


class LouiseLuckyStrategy(LouiseStrategy):
    """Louise variant with local-low lucky entries."""

    name = "louise_lucky"

    def __init__(self, lucky_window: int = 24, **params):
        super().__init__(**params)
        self.lucky_window = max(3, int(lucky_window))

    def on_bar(self, ctx: StrategyContext) -> Signal:
        base = super().on_bar(ctx)
        if base.action != "hold":
            return base
        if ctx.cash < self.quote_order_qty_usdt:
            return Signal(action="hold", reason="insufficient_cash")
        price = float(ctx.candle.get("price_source", ctx.candle["close"]))
        lucky_floor = None
        if ctx.index > 0:
            prev = ctx.candles[ctx.index - 1]
            if "ha_low" in prev:
                lucky_floor = float(prev["ha_low"])
        if lucky_floor is None:
            start = max(0, ctx.index - self.lucky_window + 1)
            lows = [float(ctx.candles[i].get("price_source", ctx.candles[i]["close"])) for i in range(start, ctx.index + 1)]
            lucky_floor = min(lows) if lows else price
        if price <= lucky_floor:
            size_pct = max(0.01, min(1.0, self.quote_order_qty_usdt / max(ctx.cash, 1e-9)))
            return Signal(
                action="buy",
                size_pct=size_pct,
                reason="louise_lucky_strike_low",
                metadata={"lucky_floor": lucky_floor, "lucky_window": self.lucky_window},
            )
        return Signal(action="hold")

    def on_fill(self, fill, signal: Signal, ctx: StrategyContext) -> None:
        if signal.reason == "louise_lucky_strike_low":
            return
        super().on_fill(fill, signal, ctx)


class AntiLouiseStrategy(StrategyBase):
    """Inverse DCA (spot approximation of short ladder logic)."""

    name = "anti_louise"

    def __init__(
        self,
        target_profit_pct: float = 1.5,
        margin_rise_factor: float = 0.004,
        quote_order_qty_usdt: float = 8.0,
        **params,
    ):
        super().__init__(
            target_profit_pct=target_profit_pct,
            margin_rise_factor=margin_rise_factor,
            quote_order_qty_usdt=quote_order_qty_usdt,
            **params,
        )
        self.target_profit_pct = max(0.0, float(target_profit_pct))
        self.margin_rise_factor = max(0.0, float(margin_rise_factor))
        self.quote_order_qty_usdt = max(1.0, float(quote_order_qty_usdt))
        self.last_short_anchor = 0.0

    def on_start(self, candles):
        annotate_pecunator_gates(candles, price_key="price_source")

    def on_bar(self, ctx: StrategyContext) -> Signal:
        price = float(ctx.candle.get("price_source", ctx.candle["close"]))
        if price <= 0:
            return Signal(action="hold", reason="invalid_price")
        if ctx.position_qty > 0 and ctx.avg_entry > 0:
            cover_trigger = float(ctx.avg_entry) * (1.0 - self.target_profit_pct / 100.0)
            if price <= cover_trigger:
                return Signal(action="sell", size_pct=1.0, reason="anti_louise_cover_profit", metadata={"cover_trigger": cover_trigger})
        if ctx.cash < self.quote_order_qty_usdt:
            return Signal(action="hold", reason="insufficient_cash")
        size_pct = max(0.01, min(1.0, self.quote_order_qty_usdt / max(ctx.cash, 1e-9)))
        if ctx.position_qty <= 0 or self.last_short_anchor <= 0:
            return Signal(action="buy", size_pct=size_pct, reason="anti_louise_initial_inverse_entry")
        rise_trigger = self.last_short_anchor * (1.0 + self.margin_rise_factor)
        if price > rise_trigger:
            return Signal(
                action="buy",
                size_pct=size_pct,
                reason="anti_louise_inverse_dca_rise",
                metadata={"rise_trigger": rise_trigger},
            )
        return Signal(action="hold")

    def on_fill(self, fill, signal: Signal, ctx: StrategyContext) -> None:
        _ = signal, ctx
        if fill.get("side") == "buy":
            fill_price = float(fill.get("price", 0.0) or 0.0)
            if fill_price > 0:
                self.last_short_anchor = fill_price


class AntiLouiseLuckyStrategy(AntiLouiseStrategy):
    """Anti-Louise variant with local-high lucky entries."""

    name = "anti_louise_lucky"

    def __init__(self, lucky_window: int = 24, **params):
        super().__init__(**params)
        self.lucky_window = max(3, int(lucky_window))

    def on_bar(self, ctx: StrategyContext) -> Signal:
        base = super().on_bar(ctx)
        if base.action != "hold":
            return base
        if ctx.cash < self.quote_order_qty_usdt:
            return Signal(action="hold", reason="insufficient_cash")
        price = float(ctx.candle.get("price_source", ctx.candle["close"]))
        lucky_ceiling = None
        if ctx.index > 0:
            prev = ctx.candles[ctx.index - 1]
            if "ha_high" in prev:
                lucky_ceiling = float(prev["ha_high"])
        if lucky_ceiling is None:
            start = max(0, ctx.index - self.lucky_window + 1)
            highs = [float(ctx.candles[i].get("price_source", ctx.candles[i]["close"])) for i in range(start, ctx.index + 1)]
            lucky_ceiling = max(highs) if highs else price
        if price >= lucky_ceiling:
            size_pct = max(0.01, min(1.0, self.quote_order_qty_usdt / max(ctx.cash, 1e-9)))
            return Signal(
                action="buy",
                size_pct=size_pct,
                reason="anti_louise_lucky_strike_high",
                metadata={"lucky_ceiling": lucky_ceiling, "lucky_window": self.lucky_window},
            )
        return Signal(action="hold")

    def on_fill(self, fill, signal: Signal, ctx: StrategyContext) -> None:
        if signal.reason == "anti_louise_lucky_strike_high":
            return
        super().on_fill(fill, signal, ctx)


class AgarthaStrategy(StrategyBase):
    """Moonshot trailing strategy for Binance Alpha high-volatility tokens.

    Tesis: una sola compra fija (notional pequeño, capital de riesgo) sobre un
    simbolo elegido; sin stop-loss, con trailing stop dinamico que sube con el
    precio para proteger ganancia ya capturada. Se asume bag puede ir a cero;
    el upside esperado (x5/x10/x20) en otras instancias amortiza fracasos.

    Cada instancia opera un (1) simbolo. La diversificacion se logra desplegando
    N instancias en paralelo. Disenada para admitir accesorios futuros
    (partial TP, breakeven lock, time stop, re-entry) via params opcionales.

    Parametros:
        quote_order_qty_usdt: notional de la compra inicial (capital de riesgo).
        trailing_stop_pct: % de retroceso desde el pico permitido antes de vender.
        activation_profit_pct: % de ganancia minima sobre entry antes de activar
            el trailing (0 = activo desde la primera vela; default 0).
        max_holding_bars: tope de velas en posicion (0 = sin limite).
        breakeven_lock_pct: cuando price >= entry*(1+breakeven_lock_pct/100),
            el trailing nunca baja del entry (lock de breakeven). 0 = off.
        partial_tp_pct / partial_tp_size_pct: TP parcial opcional al alcanzar
            X% sobre entry, vendiendo Y% de la posicion. 0 = off.
        allow_reentry: si True, tras cerrar puede volver a comprar (default False
            -- una sola apuesta por instancia).
    """

    name = "agartha"

    def __init__(
        self,
        quote_order_qty_usdt: float = 10.0,
        trailing_stop_pct: float = 30.0,
        activation_profit_pct: float = 0.0,
        max_holding_bars: int = 0,
        breakeven_lock_pct: float = 0.0,
        partial_tp_pct: float = 0.0,
        partial_tp_size_pct: float = 0.0,
        allow_reentry: bool = False,
        **params,
    ):
        super().__init__(
            quote_order_qty_usdt=quote_order_qty_usdt,
            trailing_stop_pct=trailing_stop_pct,
            activation_profit_pct=activation_profit_pct,
            max_holding_bars=max_holding_bars,
            breakeven_lock_pct=breakeven_lock_pct,
            partial_tp_pct=partial_tp_pct,
            partial_tp_size_pct=partial_tp_size_pct,
            allow_reentry=allow_reentry,
            **params,
        )
        self.quote_order_qty_usdt = max(0.1, float(quote_order_qty_usdt))
        self.trailing_stop_pct = max(0.0, float(trailing_stop_pct))
        self.activation_profit_pct = max(0.0, float(activation_profit_pct))
        self.max_holding_bars = max(0, int(max_holding_bars))
        self.breakeven_lock_pct = max(0.0, float(breakeven_lock_pct))
        self.partial_tp_pct = max(0.0, float(partial_tp_pct))
        self.partial_tp_size_pct = max(0.0, min(1.0, float(partial_tp_size_pct)))
        self.allow_reentry = bool(allow_reentry)
        # Estado serializable
        self.entry_price: float = 0.0
        self.peak_price: float = 0.0
        self.bars_in_position: int = 0
        self.trailing_active: bool = False
        self.partial_tp_done: bool = False
        self.cycles_closed: int = 0

    def _reset_position_state(self) -> None:
        self.entry_price = 0.0
        self.peak_price = 0.0
        self.bars_in_position = 0
        self.trailing_active = False
        self.partial_tp_done = False

    def on_bar(self, ctx: StrategyContext) -> Signal:
        price = float(ctx.candle.get("price_source", ctx.candle["close"]))
        if price <= 0:
            return Signal(action="hold", reason="invalid_price")

        # Cierre observado desde el broker: si ya teniamos entry_price registrado
        # pero el broker quedo plano, contabilizamos el ciclo (defensa contra
        # detecciones perdidas en on_fill cuando el motor pasa ctx pre-fill).
        if self.entry_price > 0 and ctx.position_qty <= 0:
            self.cycles_closed += 1
            self._reset_position_state()

        # Sin posicion: comprar si nunca compramos (o si re-entry esta permitido).
        if ctx.position_qty <= 0 or self.entry_price <= 0:
            if self.cycles_closed > 0 and not self.allow_reentry:
                return Signal(action="hold", reason="agartha_single_shot_done")
            if ctx.cash < self.quote_order_qty_usdt:
                return Signal(action="hold", reason="insufficient_cash")
            size_pct = max(0.01, min(1.0, self.quote_order_qty_usdt / max(ctx.cash, 1e-9)))
            return Signal(
                action="buy",
                size_pct=size_pct,
                reason="agartha_initial_entry",
                metadata={"target_notional": self.quote_order_qty_usdt},
            )

        # En posicion: actualizar peak y contador de barras.
        self.bars_in_position += 1
        if price > self.peak_price:
            self.peak_price = price

        # Time stop (si configurado): cierra todo por exceso de tiempo.
        if self.max_holding_bars > 0 and self.bars_in_position >= self.max_holding_bars:
            return Signal(
                action="sell",
                size_pct=1.0,
                reason="agartha_time_stop",
                metadata={"bars_in_position": self.bars_in_position},
            )

        # Activacion del trailing: requiere superar activation_profit_pct.
        if not self.trailing_active and self.activation_profit_pct > 0:
            activate_at = self.entry_price * (1.0 + self.activation_profit_pct / 100.0)
            if price >= activate_at:
                self.trailing_active = True
        elif not self.trailing_active and self.activation_profit_pct == 0:
            self.trailing_active = True

        # Partial TP opcional (una sola vez).
        if (
            not self.partial_tp_done
            and self.partial_tp_pct > 0
            and self.partial_tp_size_pct > 0
        ):
            partial_trigger = self.entry_price * (1.0 + self.partial_tp_pct / 100.0)
            if price >= partial_trigger:
                self.partial_tp_done = True
                return Signal(
                    action="sell",
                    size_pct=self.partial_tp_size_pct,
                    reason="agartha_partial_tp",
                    metadata={"partial_trigger": partial_trigger, "size_pct": self.partial_tp_size_pct},
                )

        # Trailing stop: vender 100% si el precio cae mas de trailing_stop_pct
        # desde el peak. Con breakeven_lock_pct, el floor nunca baja del entry.
        if self.trailing_active and self.trailing_stop_pct > 0 and self.peak_price > 0:
            trail_floor = self.peak_price * (1.0 - self.trailing_stop_pct / 100.0)
            if self.breakeven_lock_pct > 0:
                breakeven_at = self.entry_price * (1.0 + self.breakeven_lock_pct / 100.0)
                if self.peak_price >= breakeven_at:
                    trail_floor = max(trail_floor, self.entry_price)
            if price <= trail_floor:
                return Signal(
                    action="sell",
                    size_pct=1.0,
                    reason="agartha_trailing_stop",
                    metadata={
                        "peak_price": self.peak_price,
                        "trail_floor": trail_floor,
                        "entry_price": self.entry_price,
                        "bars_in_position": self.bars_in_position,
                    },
                )

        return Signal(action="hold")

    def on_fill(self, fill, signal: Signal, ctx: StrategyContext) -> None:
        _ = ctx
        side = fill.get("side")
        fill_price = float(fill.get("price", 0.0) or 0.0)
        if side == "buy" and fill_price > 0:
            # Compra inicial: ancla precio, resetea peak/bars/trailing.
            self.entry_price = fill_price
            self.peak_price = fill_price
            self.bars_in_position = 0
            self.trailing_active = self.activation_profit_pct == 0
            self.partial_tp_done = False
        elif side == "sell":
            # El motor pasa ctx PRE-fill (ver engine.run_backtest), por lo que
            # ctx.position_qty no refleja la qty residual post-venta. Detectamos
            # cierre total via signal.size_pct (TP parcial vende <1.0).
            size_pct = float(getattr(signal, "size_pct", 1.0) or 1.0)
            if size_pct >= 0.999:
                self.cycles_closed += 1
                self._reset_position_state()

    def export_state(self) -> dict:
        return {
            "entry_price": float(self.entry_price),
            "peak_price": float(self.peak_price),
            "bars_in_position": int(self.bars_in_position),
            "trailing_active": bool(self.trailing_active),
            "partial_tp_done": bool(self.partial_tp_done),
            "cycles_closed": int(self.cycles_closed),
        }

    def import_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        self.entry_price = float(state.get("entry_price", 0.0) or 0.0)
        self.peak_price = float(state.get("peak_price", 0.0) or 0.0)
        self.bars_in_position = int(state.get("bars_in_position", 0) or 0)
        self.trailing_active = bool(state.get("trailing_active", False))
        self.partial_tp_done = bool(state.get("partial_tp_done", False))
        self.cycles_closed = int(state.get("cycles_closed", 0) or 0)
