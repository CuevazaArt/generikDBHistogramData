import logging
import asyncio
import time
from decimal import Decimal
import os
from typing import Optional, Dict, Any

from runtime.core.louise_db import LouiseDB
from runtime.core.event_bus import EventBus
from runtime.core.api_governor import get_api_governor, P_TRADING
from runtime.core.api_fuse import get_api_fuse
from runtime.core.exchange_filters import get_exchange_filters
from runtime.core.alert_dispatcher import get_alert_dispatcher
from runtime.core.budget_guard import get_budget_guard
from runtime.core.settings import (
    louise_price_staleness_sec,
    louise_min_usdt_balance,
    louise_cooldown_buy_fail_sec,
    louise_cooldown_gateway_fail_sec,
)
from runtime.bot._ws_emit import publish_pnl_snapshot as _publish_pnl_snapshot_ws
from runtime.core.telemetry_vault import get_telemetry_vault

logger = logging.getLogger("louise_bot")

class LouiseBotRunner:
    """
    Main runner for a Louise bot instance.
    Implements pure DCA downside-only strategy.
    Immortality & Crash-recovery built-in via SQLite DB.
    """

    def __init__(self, bot_id: str, db: LouiseDB, bus: EventBus, gateway: Any):
        self.bot_id = bot_id
        self.db = db
        self.bus = bus
        self.gateway = gateway
        self.config: Optional[Dict[str, Any]] = None
        self.active_epoch: Optional[Dict[str, Any]] = None

        self.current_price: Decimal = Decimal("0")
        self.last_price_timestamp: int = 0
        self.usdt_free_balance: Decimal = Decimal("0")
        self.pending_orders: Dict[str, Dict[str, Any]] = {}
        self.cooldown_until: int = 0
        self.last_purchase_price: Decimal = Decimal("0")
        # Lucky Strike: True when the last fill was classified as a lucky extreme entry
        self._last_fill_was_lucky: bool = False

        self._running = False
        self._task: Optional[asyncio.Task] = None

    def initialize(self, subscribe: bool = True) -> bool:
        """Loads bot configuration and active epoch from database.

        Args:
            subscribe: If True, register event bus callbacks for live data. If False,
                      only validate config/DB state without side effects. Use False
                      when initializing a temporary runner for validation purposes.
        """
        self.config = self.db.get_bot(self.bot_id)
        if not self.config:
            logger.error(f"Bot {self.bot_id} not found in database.")
            return False

        self.active_epoch = self.db.get_active_epoch(self.bot_id)

        # Restore last_purchase_price from DB for crash recovery
        if self.active_epoch:
            purchases = self.db.get_purchases_by_epoch(self.active_epoch['epoch_id'])
            if purchases:
                self.last_purchase_price = Decimal(str(purchases[-1]['price_at_buy']))

        # Subscribe to websocket data for price & balances (zero REST weight)
        if subscribe:
            symbol = self.config['symbol']
            self.bus.subscribe(f"market.ticker.{symbol}", self._on_ticker)  # type: ignore[arg-type]
            self.bus.subscribe("account.balances", self._on_balances)  # type: ignore[arg-type]
            self.bus.subscribe("account.execution_report", self._on_execution_report)  # type: ignore[arg-type]

        symbol = self.config['symbol']
        subacct = self.config.get('subaccount', 'bluechip')
        epoch_id = self.active_epoch['epoch_id'] if self.active_epoch else 'None'
        logger.info(f"Initialized LouiseBot {self.bot_id} on {symbol} "
                   f"(subaccount: {subacct}). Active epoch: {epoch_id}")
        return True

    def _on_ticker(self, data: Dict[str, Any]):
        """Callback for websocket live price stream."""
        if 'c' in data:
            self.current_price = Decimal(str(data['c']))
            self.last_price_timestamp = int(time.time())

    def _on_balances(self, balances: list):
        """Callback for websocket balances stream."""
        for b in balances:
            if b.get("asset") == "USDT":
                self.usdt_free_balance = Decimal(str(b.get("free", "0")))
                break

    def _on_execution_report(self, event: Dict[str, Any]):
        client_oid = str(event.get('c', ''))
        status = event.get('X')
        order_id = str(event.get('i', ''))

        if client_oid in self.pending_orders and status == 'FILLED':
            meta = self.pending_orders.pop(client_oid)

            if meta['type'] == 'BUY':
                volume = Decimal(str(event.get('z', '0')))  # cumulative filled qty
                cost_usdt = Decimal(str(event.get('Z', '0')))  # cumulative quote transacted
                price_at_buy = (
                    cost_usdt / volume if volume > Decimal("0")
                    else Decimal(str(event.get('p', '0')))
                )

                # Lucky Strike classification: a fill is "lucky" when it was
                # explicitly flagged in pending_orders metadata (set in _execute_buy
                # when the price qualifies as a Heikin-Ashi extreme entry).
                is_lucky = bool(meta.get('is_lucky_fill', False))
                self._last_fill_was_lucky = is_lucky
                if is_lucky:
                    logger.info(
                        f"{self.bot_id}: LUCKY STRIKE fill at {price_at_buy:.4f} — "
                        "recording in DB but NOT updating last_purchase_price to "
                        "preserve DCA rhythm"
                    )

                # order_id makes this unique even if two fills arrive in the same second
                purchase_id = f"pur_{self.bot_id}_{int(time.time())}_{order_id or client_oid}"
                self.db.add_purchase(
                    purchase_id, self.bot_id, meta['epoch_id'],
                    float(price_at_buy), float(volume), float(cost_usdt),
                    order_id, "FILLED", is_lucky_fill=is_lucky,
                )

                # Record to global budget guard
                from runtime.core.budget_guard import get_budget_guard
                get_budget_guard().record_spend(  # type: ignore[index]
                    self.bot_id, self.config["symbol"], "BUY", cost_usdt
                )

                # Update epoch stats
                epoch = meta['epoch']
                new_purchases = epoch['num_purchases'] + 1

                old_cost = Decimal(str(epoch['total_cost']))
                old_avg_price = Decimal(str(epoch['avg_buy_price']))

                new_cost = old_cost + cost_usdt
                current_total_vol = (
                    (old_cost / old_avg_price) if old_avg_price > Decimal("0") else Decimal("0")
                )
                new_total_vol = current_total_vol + volume
                new_avg_price = new_cost / new_total_vol if new_total_vol > Decimal("0") else price_at_buy

                self.db.update_epoch_stats(meta['epoch_id'], new_purchases, float(new_cost), float(new_avg_price))
                self.active_epoch = self.db.get_active_epoch(self.bot_id)

                # Lucky fills do NOT update last_purchase_price so the DCA rhythm
                # continues uninterrupted from the pre-lucky reference point.
                if not is_lucky:
                    self.last_purchase_price = price_at_buy
                logger.info(
                    f"{self.bot_id}: Buy WS confirmed {'[LUCKY] ' if is_lucky else ''}"
                    f"avg={new_avg_price:.4f} last_ref={self.last_purchase_price:.4f}"
                )

            elif meta['type'] == 'SELL':
                status = meta.get('status', 'CLOSED_SUCCESSFUL')
                self.db.close_epoch(
                    meta['epoch_id'],
                    float(meta['current_price']),
                    float(meta['final_value']),
                    float(meta['profit_usdt']),
                    float(meta['profit_pct']),
                    status=status
                )
                self.active_epoch = None
                profit_pct = meta['profit_pct']
                logger.info(f"{self.bot_id}: Sell WS confirmed. Epoch closed "
                           f"with status {status} ({profit_pct:.2f}%)!")

    async def start(self):
        if not self.config:
            raise RuntimeError("Bot not initialized. Call initialize() first.")

        self._running = True
        self.db.update_bot_status(self.bot_id, "RUNNING")
        self.config['status'] = "RUNNING"
        self._task = asyncio.create_task(self._main_loop())
        logger.info(f"Bot {self.bot_id} started loop.")

    async def _main_loop(self):
        """Main loop that runs every poll_interval_seconds. Respects shutdown flag."""
        while self._running:
            # Check if graceful shutdown was requested
            try:
                from runtime.api.lifespan import is_shutdown_requested
                if is_shutdown_requested():
                    logger.info(f"Shutdown flag detected, stopping {self.bot_id}")
                    break
            except Exception:
                pass

            try:
                await self.poll_market()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in {self.bot_id} main loop: {e}")

            # Sleep for the configured interval
            interval = self.config.get('poll_interval_seconds', 300)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info(f"{self.bot_id} sleep interrupted by cancellation")
                break

    async def poll_market(self):
        """
        Main logic for the pure DCA bot.
        """
        now = int(time.time())
        if self.config["status"] not in ["ACCUMULATING", "RUNNING"]:
            return

        if now < self.cooldown_until:
            return

        symbol = self.config["symbol"]

        # Ensure filters are loaded
        filters = get_exchange_filters().get(symbol)
        if not filters and getattr(self.gateway, "_client", None):
            try:
                filters = await get_exchange_filters().ensure_loaded(symbol, self.gateway._client)
            except Exception as e:
                logger.warning(f"{self.bot_id}: Failed to load exchange filters: {e}")

        # Check for stale price data (env-tunable, default 15s)
        staleness_sec = louise_price_staleness_sec()
        if self.current_price <= Decimal("0") or (now - self.last_price_timestamp > staleness_sec):
            logger.debug(f"{self.bot_id}: Waiting for fresh price feed (>{staleness_sec}s stale)...")
            return

        min_balance = Decimal(str(louise_min_usdt_balance()))
        if self.usdt_free_balance < min_balance:
            logger.warning(
                f"{self.bot_id}: Insufficient USDT in spot wallet (< {min_balance}). "
                f"Currently have {self.usdt_free_balance}."
            )
            return

        fuse = get_api_fuse()
        if fuse.is_tripped():
            logger.warning(f"{self.bot_id}: API Fuse is tripped. Skipping cycle.")
            return

        gov = get_api_governor()
        if not gov.can_execute("binance", P_TRADING, 1):
            logger.warning(f"{self.bot_id}: WeightGovernor blocked execution (limits reached).")
            return

        # Check if we need to create an epoch
        if not self.active_epoch:
            epoch_id = f"epoch_{self.bot_id}_{int(time.time())}"
            self.db.create_epoch(epoch_id, self.bot_id, "RUNNING")
            self.active_epoch = self.db.get_active_epoch(self.bot_id)
            logger.info(f"{self.bot_id}: Created new epoch {epoch_id}")

        epoch = self.active_epoch
        buy_volume = Decimal(str(self.config["buy_volume"]))

        # Check exit conditions first
        if epoch['num_purchases'] > 0:
            avg_price = Decimal(str(epoch['avg_buy_price']))
            total_cost = Decimal(str(epoch['total_cost']))

            # Current value of accumulated assets
            current_value = (total_cost / avg_price) * self.current_price
            profit_usdt = current_value - total_cost
            profit_pct = (profit_usdt / total_cost) * Decimal("100")

            target_profit = Decimal(str(self.config["target_profit_pct"]))

            logger.info(f"{self.bot_id}: Current PnL: {profit_pct:.2f}% (Target: {target_profit:.2f}%)")

            # Record snapshot for time-series charting
            realized = self.db.get_total_realized_pnl(self.bot_id)
            self.db.record_pnl_snapshot(
                bot_id=self.bot_id,
                bot_type="louise",
                current_price=float(self.current_price),
                epoch_id=epoch['epoch_id'],
                avg_entry_price_usdt=float(avg_price),
                num_entries=epoch['num_purchases'],
                total_committed_usdt=float(total_cost),
                unrealized_pnl_usdt=float(profit_usdt),
                unrealized_pnl_pct=float(profit_pct),
                cumulative_realized_pnl_usdt=realized,
            )
            _publish_pnl_snapshot_ws(
                self.bot_id, "louise", epoch, self.current_price, avg_price,
                total_cost, profit_usdt, profit_pct, realized,
            )

            # Take Profit: only exit condition
            if profit_pct >= target_profit:
                await self._execute_sell(epoch, current_value, profit_usdt, profit_pct)
                return

        # Only buy if current price is strictly below the last purchase price
        if epoch['num_purchases'] > 0:
            if self.current_price >= self.last_purchase_price:
                logger.debug(f"{self.bot_id}: Price {self.current_price:.4f} not below "
                           f"last buy {self.last_purchase_price:.4f}. Waiting for lower price.")
                return

        # BudgetGuard is the source of truth for global spend limits (checked first)
        bg = get_budget_guard()
        if not bg.can_spend(buy_volume, self.bot_id):
            logger.warning(f"{self.bot_id}: Global BudgetGuard rejected buy of {buy_volume} USDT. Throttling.")
            return

        # Local sanity check: daily_budget is per-bot limit, BudgetGuard is global truth
        daily_budget = Decimal(str(self.config.get("daily_budget_usdt", 500.0)))
        total_cost_so_far = Decimal(str(epoch.get('total_cost', 0.0)))

        if total_cost_so_far + buy_volume > daily_budget:
            logger.warning(f"{self.bot_id}: Daily budget reached "
                          f"({daily_budget} USDT). Pausing DCA until target reached.")
            return

        # Check spot balance
        if self.usdt_free_balance < buy_volume:
            logger.warning(f"{self.bot_id}: Insufficient USDT in spot wallet. "
                          f"Need {buy_volume}, have {self.usdt_free_balance}.")
            return

        # Check MIN_NOTIONAL before buy
        filters = get_exchange_filters().get(symbol)
        if filters:
            if buy_volume < filters.min_notional:
                logger.warning(f"{self.bot_id}: buy_volume {buy_volume} is below MIN_NOTIONAL {filters.min_notional}")
                self.cooldown_until = now + louise_cooldown_buy_fail_sec()
                return

        await self._execute_buy(epoch, buy_volume)

    def _is_lucky_entry(self) -> bool:
        """True if current price is at or below the HA low of the last closed daily candle.

        A Lucky Strike LONG entry: price touches the Heikin-Ashi downside extreme,
        meaning we are accumulating at a historically anomalous low — worth marking
        separately so the DCA rhythm is not disrupted by a single extreme fill.
        Returns False on any error (fail-safe → normal DCA behaviour).
        """
        try:
            vault = get_telemetry_vault()
            klines = vault.get_klines(self.config["symbol"], "1d", limit=2)
            closed = [k for k in klines if k.get("is_closed", 1) == 1]
            if not closed:
                return False
            ha_low = closed[0].get("ha_low")
            if ha_low is None:
                return False
            return float(self.current_price) <= float(ha_low)
        except Exception:
            return False

    async def _execute_buy(self, epoch: Dict[str, Any], cost_usdt: Decimal):
        symbol = self.config["symbol"]  # type: ignore[index]
        alerts = get_alert_dispatcher()

        is_lucky = self._is_lucky_entry()
        logger.info(
            f"{self.bot_id}: Executing {'LUCKY STRIKE ' if is_lucky else ''}"
            f"MARKET BUY of {cost_usdt} USDT on {symbol}"
        )
        client_oid = f"l_{self.bot_id}_{int(time.time())}"

        try:
            is_simulation = os.environ.get("LOUISE_PAPER_TRADE", "true").lower() == "true"

            if self.gateway and getattr(self.gateway, "_client", None):
                client = self.gateway._client

                # Register intent for WS confirmation; is_lucky_fill read in _on_execution_report
                self.pending_orders[client_oid] = {
                    'type': 'BUY',
                    'epoch_id': epoch['epoch_id'],
                    'epoch': epoch,
                    'is_lucky_fill': is_lucky,
                }

                if is_simulation:
                    logger.info(f"{self.bot_id}: [SIMULATION] Paper-trading MARKET BUY of {cost_usdt} USDT")
                    qty = cost_usdt / self.current_price
                    sim_payload = {
                        'e': 'executionReport',
                        'x': 'TRADE',
                        'X': 'FILLED',
                        'c': client_oid,
                        's': symbol,
                        'S': 'BUY',
                        'q': str(qty),
                        'p': str(self.current_price),
                        'Z': str(cost_usdt)
                    }
                    # Delay slightly to mimic network
                    asyncio.create_task(self._delay_sim(sim_payload))
                else:
                    await client.create_order(
                        symbol=symbol,
                        side="BUY",
                        type="MARKET",
                        newClientOrderId=client_oid,
                        quoteOrderQty=str(cost_usdt)
                    )
                self.usdt_free_balance -= cost_usdt
            else:
                logger.error(f"{self.bot_id}: No gateway available to execute BUY")
                alerts.warning(
                    "NO_GATEWAY",
                    f"Bot {self.bot_id}: Gateway not available, cannot execute buy",
                    payload={"bot_id": self.bot_id, "symbol": symbol, "amount_usdt": float(cost_usdt)},
                    silent=True  # Don't spam if gateway is temporarily disconnecting
                )
                self.cooldown_until = int(time.time()) + louise_cooldown_gateway_fail_sec()

        except Exception as e:
            logger.error(f"{self.bot_id}: Failed to execute buy: {e}")
            err_str = str(e)[:100]
            alerts.warning(
                "BUY_FAILED",
                f"Bot {self.bot_id} failed to execute BUY on {symbol}: {err_str}",
                payload={
                    "bot_id": self.bot_id,
                    "symbol": symbol,
                    "amount_usdt": float(cost_usdt),
                    "error": err_str
                },
                silent=False
            )
            self.cooldown_until = int(time.time()) + louise_cooldown_buy_fail_sec()

    async def _execute_sell(
        self,
        epoch: Dict[str, Any],
        final_value: Decimal,
        profit_usdt: Decimal,
        profit_pct: Decimal,
        status: str = "CLOSED_SUCCESSFUL"
    ):
        symbol = self.config["symbol"]  # type: ignore[index]
        alerts = get_alert_dispatcher()

        avg = Decimal(str(epoch['avg_buy_price']))
        total_cost = Decimal(str(epoch['total_cost']))
        if avg <= Decimal("0") or total_cost <= Decimal("0"):
            alerts.critical(
                "SELL_INVALID_EPOCH",
                f"Louise {self.bot_id}: cannot sell epoch {epoch['epoch_id']} "
                f"— avg={avg} total={total_cost}",
                payload={"bot_id": self.bot_id, "epoch_id": epoch['epoch_id']},
                silent=False,
            )
            self.cooldown_until = int(time.time()) + louise_cooldown_buy_fail_sec()
            return
        total_vol = total_cost / avg

        filters = get_exchange_filters().get(symbol)
        if filters is None:
            alerts.critical(
                "SELL_BLOCKED_NO_FILTERS",
                f"Louise {self.bot_id} reached take-profit on {symbol} but "
                f"exchange filters are unavailable — aborting sell until filters load.",
                payload={"bot_id": self.bot_id, "symbol": symbol,
                         "epoch_id": epoch['epoch_id']},
                silent=False,
            )
            self.cooldown_until = int(time.time()) + louise_cooldown_gateway_fail_sec()
            return
        quantized_vol = filters.quantize_qty(total_vol)

        logger.info(f"{self.bot_id}: Target reached! Executing MARKET SELL of {quantized_vol} {symbol}")
        client_oid = f"ls_{self.bot_id}_{int(time.time())}"

        try:
            is_simulation = os.environ.get("LOUISE_PAPER_TRADE", "true").lower() == "true"

            if self.gateway and getattr(self.gateway, "_client", None):
                client = self.gateway._client

                self.pending_orders[client_oid] = {
                    'type': 'SELL',
                    'epoch_id': epoch['epoch_id'],
                    'current_price': self.current_price,
                    'final_value': final_value,
                    'profit_usdt': profit_usdt,
                    'profit_pct': profit_pct,
                    'status': status
                }

                if is_simulation:
                    logger.info(f"{self.bot_id}: [SIMULATION] Paper-trading MARKET SELL of {quantized_vol} {symbol}")
                    sim_payload = {
                        'e': 'executionReport',
                        'x': 'TRADE',
                        'X': 'FILLED',
                        'c': client_oid,
                        's': symbol,
                        'S': 'SELL',
                        'q': str(quantized_vol),
                        'p': str(self.current_price),
                        'Z': str(quantized_vol * self.current_price)
                    }
                    asyncio.create_task(self._delay_sim(sim_payload))
                else:
                    await client.create_order(
                        symbol=symbol,
                        side="SELL",
                        type="MARKET",
                        newClientOrderId=client_oid,
                        quantity=str(quantized_vol)
                    )
            else:
                logger.error(f"{self.bot_id}: No gateway available to execute SELL")
                msg = (f"CRITICAL: Bot {self.bot_id} reached take-profit but "
                      f"gateway unavailable. Position STUCK with {quantized_vol} "
                      f"{symbol} at {self.current_price}")
                alerts.critical(
                    "SELL_BLOCKED_NO_GATEWAY",
                    msg,
                    payload={
                        "bot_id": self.bot_id,
                        "symbol": symbol,
                        "quantity": float(quantized_vol),
                        "current_price": float(self.current_price),
                        "target_pnl_pct": float(profit_pct),
                        "epoch_id": epoch['epoch_id']
                    },
                    silent=False
                )
                self.cooldown_until = int(time.time()) + louise_cooldown_gateway_fail_sec()

        except Exception as e:
            logger.error(f"{self.bot_id}: Failed to execute sell: {e}")
            err = str(e)[:80]
            msg = (f"CRITICAL: Bot {self.bot_id} reached take-profit on {symbol} "
                  f"but SELL execution failed. Position STUCK: {err}")
            alerts.critical(
                "SELL_EXECUTION_FAILED",
                msg,
                payload={
                    "bot_id": self.bot_id,
                    "symbol": symbol,
                    "quantity": float(quantized_vol),
                    "error": str(e)[:100],
                    "epoch_id": epoch['epoch_id']
                },
                silent=False
            )
            self.cooldown_until = int(time.time()) + louise_cooldown_buy_fail_sec()

    async def _delay_sim(self, payload):
        await asyncio.sleep(1.5)
        self._on_execution_report(payload)

    async def stop(self, shutdown_db=True):
        """Clean shutdown of the bot."""
        logger.info(f"Shutting down LouiseBot {self.bot_id}")
        self._running = False
        if self._task:
            self._task.cancel()
        if shutdown_db:
            self.db.update_bot_status(self.bot_id, "SHUTDOWN")
            self.config['status'] = "SHUTDOWN"
