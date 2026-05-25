"""Lifecycle of a single Agartha bot in live mode.

The runner is a small, synchronous (but asyncio-friendly) machine that
drives **one** bot through its state graph. The high-level loop:

    1. place_entry_limit
    2. await_entry_fill   (with timeout / cancel policy)
    3. monitor position   (update peak, check trailing, check time stop)
    4. on trailing trigger:
         place_exit_limit  (plan_exit decides price)
         await_exit_fill   (60s -> reorder; 5min -> border; 10min -> stale)
    5. on stale_exit:
         emit supervisor alert (event), stop the runner (supervisor closes)

All decisions persist to ``cluster_bots`` + ``orders`` + ``event_log``.
The runner is **idempotent on restart**: resuming reads bot state from DB
and continues from the right slot.

Engine-pure: depends on LiveClient interface (so StubLiveClient works in
tests and dry-run).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from backtest.agartha_cluster.api_throttle import ApiThrottle
from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.event_logger import EventLogger
from backtest.agartha_cluster.live_client import (
    LiveClient,
    PlaceOrderResult,
    new_client_order_id,
)
from backtest.agartha_cluster.models import (
    BotRecord,
    BotState,
    EventKind,
    EventLevel,
    EventSource,
    OrderRecord,
    OrderSide,
    OrderState,
    OrderType,
    SymbolFilters,
    SymbolParams,
)
from backtest.agartha_cluster.state_machine import transition
from backtest.agartha_exit_planner import (
    ExitAction,
    SymbolFilters as PlannerFilters,
    plan_exit,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _round_qty_down(qty: float, step: float) -> float:
    if step <= 0:
        return float(qty)
    return (int(qty / step + 1e-12)) * step


def _round_price_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return float(price)
    return (int(price / tick + 1e-12)) * tick


@dataclass
class RunnerConfig:
    entry_weight_cost: int = 2
    entry_orders_cost: int = 1
    exit_weight_cost: int = 2
    exit_orders_cost: int = 1
    entry_fill_grace_seconds: int = 60 * 60     # cancel entry after 1h unfilled
    exit_reorder_after_seconds: int = 60        # try re-quote @ band lower
    exit_border_after_seconds: int = 5 * 60
    exit_stale_after_seconds: int = 10 * 60


class BotRunner:
    """Drives one bot from entry placement to terminal state."""

    def __init__(
        self,
        *,
        db: ClusterDB,
        client: LiveClient,
        throttle: ApiThrottle,
        events: EventLogger,
        config: Optional[RunnerConfig] = None,
    ):
        self.db = db
        self.client = client
        self.throttle = throttle
        self.events = events
        self.config = config or RunnerConfig()

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------
    def place_entry(self, bot: BotRecord, filters: SymbolFilters) -> BotRecord:
        params = SymbolParams(**{
            "symbol": bot.symbol,
            **json.loads(bot.params_snapshot_json),
        }) if False else None  # noqa: F841 - placeholder for clarity

        runtime = json.loads(bot.params_snapshot_json)
        entry_offset_pct = float(runtime.get("entry_limit_offset_pct", 0.0) or 0.0)

        self.throttle.wait_for_budget(
            weight=self.config.entry_weight_cost,
            orders=self.config.entry_orders_cost,
        )

        last_price = float(self.client.get_price(bot.symbol))
        if last_price <= 0:
            self._fail_deploy(bot, reason="no_price_for_entry")
            return self.db.get_bot(bot.bot_id)  # type: ignore[return-value]

        target_price = last_price * (1.0 - entry_offset_pct / 100.0) if entry_offset_pct > 0 else last_price
        target_price = _round_price_to_tick(target_price, filters.tick_size)
        qty = bot.capital_usdt / max(target_price, 1e-12)
        qty = _round_qty_down(qty, filters.step_size)
        if target_price * qty < filters.min_notional:
            self._fail_deploy(bot, reason=f"notional<{filters.min_notional}")
            return self.db.get_bot(bot.bot_id)  # type: ignore[return-value]

        client_order_id = new_client_order_id("agc-buy")
        order = OrderRecord(
            order_id=None,
            client_order_id=client_order_id,
            bot_id=bot.bot_id,
            symbol=bot.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            state=OrderState.PENDING,
            price=target_price,
            qty=qty,
            submitted_ts=_now_ms(),
            correlation_id=bot.correlation_id,
        )
        self.db.insert_order(order)

        self._transition_bot(bot, BotState.PLACING_ENTRY, reason="placing_buy_limit")

        result = self.client.place_limit(
            symbol=bot.symbol,
            side=OrderSide.BUY,
            price=target_price,
            qty=qty,
            client_order_id=client_order_id,
        )
        self.throttle.record(weight=result.weight_used or self.config.entry_weight_cost, orders=1)
        self.db.log_api_call(
            endpoint="/api/v3/order",
            method="POST",
            weight=result.weight_used or self.config.entry_weight_cost,
            status_code=200 if result.accepted else 4,
            latency_ms=result.latency_ms,
            bot_id=bot.bot_id,
            correlation_id=bot.correlation_id,
            request_summary=f"BUY {qty}@{target_price}",
            error_text=result.error,
        )

        if not result.accepted:
            self.db.update_order_state(
                client_order_id=client_order_id,
                state=OrderState.REJECTED,
                raw_response=result.raw_response,
            )
            self.events.error(
                kind=EventKind.ORDER_REJECTED,
                source=EventSource.BINANCE_REST,
                bot_id=bot.bot_id,
                symbol=bot.symbol,
                correlation_id=bot.correlation_id,
                payload={"error": result.error, "client_order_id": client_order_id},
            )
            self._fail_deploy(bot, reason="entry_rejected")
            return self.db.get_bot(bot.bot_id)  # type: ignore[return-value]

        self.db.update_order_state(
            client_order_id=client_order_id,
            state=OrderState.SUBMITTED,
            order_id=result.order_id,
            raw_response=result.raw_response,
        )
        self.events.info(
            kind=EventKind.ORDER_PLACED,
            source=EventSource.BINANCE_REST,
            bot_id=bot.bot_id,
            symbol=bot.symbol,
            correlation_id=bot.correlation_id,
            payload={
                "side": "BUY",
                "price": target_price,
                "qty": qty,
                "client_order_id": client_order_id,
                "order_id": result.order_id,
            },
        )
        self.db.update_bot(
            bot.bot_id,
            entry_order_id=result.order_id,
            entry_client_order_id=client_order_id,
            entry_price=target_price,
            entry_qty=qty,
            deployed_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._transition_bot(
            self.db.get_bot(bot.bot_id),  # type: ignore[arg-type]
            BotState.AWAITING_ENTRY_FILL,
            reason="buy_limit_submitted",
        )
        return self.db.get_bot(bot.bot_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------
    def place_exit(self, bot: BotRecord, filters: SymbolFilters) -> BotRecord:
        runtime = json.loads(bot.params_snapshot_json)
        trailing = float(runtime["trailing_stop_pct"])
        breakeven = float(runtime.get("breakeven_lock_pct", 0.0) or 0.0)

        current = float(self.client.get_price(bot.symbol))
        peak = max(float(bot.peak_price or 0.0), current)
        plan = plan_exit(
            current_price=current,
            entry_price=float(bot.entry_price or 0.0),
            peak_price=peak,
            trailing_stop_pct=trailing,
            breakeven_lock_pct=breakeven,
            filters=PlannerFilters(
                tick_size=filters.tick_size,
                bid_multiplier_down=filters.bid_multiplier_down,
                bid_multiplier_up=filters.bid_multiplier_up,
                ask_multiplier_down=filters.ask_multiplier_down,
                ask_multiplier_up=filters.ask_multiplier_up,
                min_notional=filters.min_notional,
            ),
        )

        if plan.action == ExitAction.HOLD:
            self.db.update_bot(bot.bot_id, peak_price=peak, trail_floor=plan.trail_floor)
            return self.db.get_bot(bot.bot_id)  # type: ignore[return-value]

        if plan.action == ExitAction.OUT_OF_BAND or plan.limit_price is None:
            self.events.warn(
                kind=EventKind.EXIT_OUT_OF_BAND,
                source=EventSource.SERVICE,
                bot_id=bot.bot_id,
                symbol=bot.symbol,
                correlation_id=bot.correlation_id,
                payload={"reason": plan.reason, "current": current, "peak": peak},
            )
            self.db.update_bot(bot.bot_id, peak_price=peak, trail_floor=plan.trail_floor)
            return self.db.get_bot(bot.bot_id)  # type: ignore[return-value]

        self.throttle.wait_for_budget(
            weight=self.config.exit_weight_cost,
            orders=self.config.exit_orders_cost,
        )

        self._transition_bot(bot, BotState.PLACING_EXIT, reason=plan.reason)

        exit_qty = _round_qty_down(float(bot.entry_qty or 0.0), filters.step_size)
        if exit_qty * plan.limit_price < filters.min_notional:
            self.events.warn(
                kind=EventKind.EXIT_OUT_OF_BAND,
                source=EventSource.SERVICE,
                bot_id=bot.bot_id,
                symbol=bot.symbol,
                correlation_id=bot.correlation_id,
                payload={"reason": "notional_below_min", "qty": exit_qty, "price": plan.limit_price},
            )
            self._transition_bot(
                self.db.get_bot(bot.bot_id),  # type: ignore[arg-type]
                BotState.IN_POSITION,
                reason="notional_below_min_back_to_position",
            )
            return self.db.get_bot(bot.bot_id)  # type: ignore[return-value]

        client_order_id = new_client_order_id("agc-sell")
        order = OrderRecord(
            order_id=None,
            client_order_id=client_order_id,
            bot_id=bot.bot_id,
            symbol=bot.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            state=OrderState.PENDING,
            price=float(plan.limit_price),
            qty=exit_qty,
            submitted_ts=_now_ms(),
            correlation_id=bot.correlation_id,
        )
        self.db.insert_order(order)

        result = self.client.place_limit(
            symbol=bot.symbol,
            side=OrderSide.SELL,
            price=float(plan.limit_price),
            qty=exit_qty,
            client_order_id=client_order_id,
        )
        self.throttle.record(weight=result.weight_used or self.config.exit_weight_cost, orders=1)
        self.db.log_api_call(
            endpoint="/api/v3/order",
            method="POST",
            weight=result.weight_used or self.config.exit_weight_cost,
            status_code=200 if result.accepted else 4,
            latency_ms=result.latency_ms,
            bot_id=bot.bot_id,
            correlation_id=bot.correlation_id,
            request_summary=f"SELL {exit_qty}@{plan.limit_price}",
            error_text=result.error,
        )

        if not result.accepted:
            self.db.update_order_state(
                client_order_id=client_order_id,
                state=OrderState.REJECTED,
                raw_response=result.raw_response,
            )
            self.events.error(
                kind=EventKind.ORDER_REJECTED,
                source=EventSource.BINANCE_REST,
                bot_id=bot.bot_id,
                symbol=bot.symbol,
                correlation_id=bot.correlation_id,
                payload={"error": result.error, "client_order_id": client_order_id},
            )
            self._transition_bot(
                self.db.get_bot(bot.bot_id),  # type: ignore[arg-type]
                BotState.IN_POSITION,
                reason="exit_rejected_back_to_position",
            )
            return self.db.get_bot(bot.bot_id)  # type: ignore[return-value]

        self.db.update_order_state(
            client_order_id=client_order_id,
            state=OrderState.SUBMITTED,
            order_id=result.order_id,
            raw_response=result.raw_response,
        )
        self.events.info(
            kind=EventKind.ORDER_PLACED,
            source=EventSource.BINANCE_REST,
            bot_id=bot.bot_id,
            symbol=bot.symbol,
            correlation_id=bot.correlation_id,
            payload={
                "side": "SELL",
                "price": plan.limit_price,
                "qty": exit_qty,
                "client_order_id": client_order_id,
                "order_id": result.order_id,
                "action": plan.action.value,
                "fallback_used": plan.fallback_used,
            },
        )
        self.db.update_bot(
            bot.bot_id,
            exit_order_id=result.order_id,
            exit_client_order_id=client_order_id,
            peak_price=peak,
            trail_floor=plan.trail_floor,
        )
        self._transition_bot(
            self.db.get_bot(bot.bot_id),  # type: ignore[arg-type]
            BotState.AWAITING_EXIT_FILL,
            reason="sell_limit_submitted",
        )
        return self.db.get_bot(bot.bot_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Manual close (supervisor)
    # ------------------------------------------------------------------
    def manual_close(self, bot: BotRecord, *, reason: str) -> BotRecord:
        """Force a SELL LIMIT @ current price (best-effort) and mark MANUAL_CLOSED.

        Triggered by ``supervisor close <bot_id>``.
        """
        filters = self.db.get_symbol_filters(bot.symbol) or SymbolFilters(symbol=bot.symbol)
        if bot.entry_qty is None or bot.entry_qty <= 0:
            self._transition_bot(bot, BotState.MANUAL_CLOSED, reason=f"manual_close_no_position:{reason}")
            self.db.update_bot(bot.bot_id, closed_at=time.strftime("%Y-%m-%d %H:%M:%S"), notes=reason)
            self.events.warn(
                kind=EventKind.MANUAL_CLOSE,
                source=EventSource.SUPERVISOR,
                bot_id=bot.bot_id,
                symbol=bot.symbol,
                correlation_id=bot.correlation_id,
                payload={"reason": reason, "no_position": True},
            )
            return self.db.get_bot(bot.bot_id)  # type: ignore[return-value]

        if bot.exit_client_order_id:
            self.client.cancel_order(symbol=bot.symbol, client_order_id=bot.exit_client_order_id)
            self.throttle.record(weight=1, orders=0)
            self.db.update_order_state(
                client_order_id=bot.exit_client_order_id, state=OrderState.CANCELLED
            )

        current = float(self.client.get_price(bot.symbol))
        target = _round_price_to_tick(current * (1.0 - 0.001), filters.tick_size)
        client_order_id = new_client_order_id("agc-mcls")
        self.db.insert_order(
            OrderRecord(
                order_id=None,
                client_order_id=client_order_id,
                bot_id=bot.bot_id,
                symbol=bot.symbol,
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                state=OrderState.PENDING,
                price=target,
                qty=float(bot.entry_qty),
                submitted_ts=_now_ms(),
                correlation_id=bot.correlation_id,
            )
        )
        self.throttle.wait_for_budget(weight=2, orders=1)
        result = self.client.place_limit(
            symbol=bot.symbol,
            side=OrderSide.SELL,
            price=target,
            qty=float(bot.entry_qty),
            client_order_id=client_order_id,
        )
        self.throttle.record(weight=result.weight_used or 1, orders=1)
        self.db.log_api_call(
            endpoint="/api/v3/order",
            method="POST",
            weight=result.weight_used or 1,
            status_code=200 if result.accepted else 4,
            latency_ms=result.latency_ms,
            bot_id=bot.bot_id,
            correlation_id=bot.correlation_id,
            request_summary=f"MANUAL_CLOSE SELL {bot.entry_qty}@{target}",
            error_text=result.error,
        )
        self.db.update_order_state(
            client_order_id=client_order_id,
            state=OrderState.SUBMITTED if result.accepted else OrderState.REJECTED,
            order_id=result.order_id,
            raw_response=result.raw_response,
        )
        self.events.warn(
            kind=EventKind.MANUAL_CLOSE,
            source=EventSource.SUPERVISOR,
            bot_id=bot.bot_id,
            symbol=bot.symbol,
            correlation_id=bot.correlation_id,
            payload={
                "reason": reason,
                "client_order_id": client_order_id,
                "target_price": target,
                "qty": bot.entry_qty,
                "accepted": result.accepted,
            },
        )
        self._transition_bot(bot, BotState.MANUAL_CLOSED, reason=f"manual_close:{reason}")
        self.db.update_bot(bot.bot_id, closed_at=time.strftime("%Y-%m-%d %H:%M:%S"), notes=reason)
        return self.db.get_bot(bot.bot_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Fill events from WS userDataStream
    # ------------------------------------------------------------------
    def on_fill(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: OrderSide,
        price: float,
        qty: float,
        fee: float = 0.0,
        fee_asset: Optional[str] = None,
        ts_ms: Optional[int] = None,
        exchange_fill_id: Optional[str] = None,
        raw_payload: Optional[str] = None,
    ) -> None:
        ts_ms = int(ts_ms if ts_ms is not None else _now_ms())
        row = self.db.get_order_by_client_id(client_order_id)
        if row is None:
            self.events.warn(
                kind=EventKind.RECONCILIATION_DRIFT,
                source=EventSource.BINANCE_WS,
                symbol=symbol,
                correlation_id=None,
                payload={"orphan_client_order_id": client_order_id},
            )
            return
        bot_id = int(row["bot_id"])
        bot = self.db.get_bot(bot_id)
        if bot is None:
            return
        self.db.insert_fill(
            bot_id=bot_id,
            order_pk=int(row["order_pk"]),
            exchange_fill_id=exchange_fill_id,
            symbol=symbol,
            side=side,
            price=float(price),
            qty=float(qty),
            fee=float(fee),
            fee_asset=fee_asset,
            ts_ms=ts_ms,
            is_maker=False,
            correlation_id=bot.correlation_id,
            raw_payload=raw_payload,
        )
        self.db.update_order_state(
            client_order_id=client_order_id,
            state=OrderState.FILLED,
            filled_qty=float(qty),
            avg_fill_price=float(price),
        )
        self.events.info(
            kind=EventKind.ORDER_FILLED,
            source=EventSource.BINANCE_WS,
            bot_id=bot_id,
            symbol=symbol,
            correlation_id=bot.correlation_id,
            payload={"side": side.value, "price": price, "qty": qty, "fee": fee},
        )

        if side == OrderSide.BUY:
            self.db.update_bot(
                bot_id,
                entry_filled_ts=ts_ms,
                entry_price=float(price),
                entry_qty=float(qty),
                peak_price=float(price),
            )
            self._transition_bot(bot, BotState.IN_POSITION, reason="entry_filled")
        else:
            entry_price = float(bot.entry_price or 0.0)
            pnl = (float(price) - entry_price) * float(qty)
            self.db.update_bot(
                bot_id,
                exit_filled_ts=ts_ms,
                exit_price=float(price),
                exit_qty=float(qty),
                realized_pnl_usdt=pnl,
                closed_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            target_state = BotState.CLOSED_WIN if pnl >= 0 else BotState.CLOSED_LOSS
            self._transition_bot(bot, target_state, reason="exit_filled")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _transition_bot(
        self,
        bot: BotRecord,
        target: BotState,
        *,
        reason: Optional[str] = None,
    ) -> None:
        new_state = transition(bot.state, target, reason=reason)
        if new_state == bot.state:
            return
        self.db.update_bot(bot.bot_id, state=new_state.value)
        self.db.append_state_log(
            bot_id=bot.bot_id,
            from_state=bot.state,
            to_state=new_state,
            reason=reason,
            correlation_id=bot.correlation_id,
        )
        self.events.info(
            kind=EventKind.BOT_STATE_CHANGED,
            source=EventSource.SERVICE,
            bot_id=bot.bot_id,
            symbol=bot.symbol,
            correlation_id=bot.correlation_id,
            payload={"from": bot.state.value, "to": new_state.value, "reason": reason},
        )

    def _fail_deploy(self, bot: BotRecord, *, reason: str) -> None:
        try:
            self._transition_bot(bot, BotState.FAILED_DEPLOY, reason=reason)
        except Exception:
            # Even if transition is invalid, mark notes for forensics.
            self.db.update_bot(bot.bot_id, notes=f"failed_deploy:{reason}")
        self.events.error(
            kind=EventKind.NEEDS_MANUAL_ACTION,
            source=EventSource.SERVICE,
            bot_id=bot.bot_id,
            symbol=bot.symbol,
            correlation_id=bot.correlation_id,
            payload={"failure": reason},
        )
