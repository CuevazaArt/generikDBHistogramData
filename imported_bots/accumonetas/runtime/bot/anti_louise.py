"""AntiLouise bot — mirror-image DCA using margin SHORT positions.

Strategy:
  - Opens SHORT positions (margin SELL with auto-borrow) each time the price
    rises strictly above the last short entry price.
  - Covers the full position (margin BUY with auto-repay) when unrealized
    profit reaches target_profit_pct.
  - No stop-loss, no position limit — designed for long-horizon operation
    alongside Louise (long) as a dual-direction DCA hub.

P&L for shorts (inverted vs. Louise):
  total_received   = USDT collected across all short sells  (≡ epoch.total_cost)
  avg_short_price  = average price at which shorts were opened (≡ epoch.avg_buy_price)
  total_volume     = total_received / avg_short_price        (base asset owed)
  current_exposure = total_volume × current_price            (cost to cover now)
  profit_usdt      = total_received − current_exposure       (positive when price fell)
  profit_pct       = profit_usdt / total_received × 100

Simulation mode (LOUISE_PAPER_TRADE=true):
  Margin orders are faked with a 1.5-second delayed executionReport.
  Live mode uses client.create_margin_order() with sideEffectType=MARGIN_BUY
  (open) and AUTO_REPAY (cover).

NOTE: Live margin mode requires the user data stream subscription to target the
MARGIN account, not the SPOT account. The current BinanceGateway subscribes to
the spot stream; for production margin trading, the gateway must be extended to
also listen to the cross/isolated-margin user data stream.
"""

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

logger = logging.getLogger("anti_louise_bot")


class AntiLouiseBotRunner:
    """
    AntiLouise: DCA short-side mirror of LouiseBotRunner.

    Opens margin SHORT positions when price rises above last entry.
    Covers (closes) on take-profit. No stop-loss, no position limit.
    """

    BOT_TYPE = "anti_louise"

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
        # Last price at which a short was opened (used for entry gating)
        self.last_short_price: Decimal = Decimal("0")
        # Lucky Strike: True when the last fill was classified as a lucky extreme entry
        self._last_fill_was_lucky: bool = False

        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ── Lifecycle ────────────────────────────────────────────────────

    def initialize(self, subscribe: bool = True) -> bool:
        self.config = self.db.get_bot(self.bot_id)
        if not self.config:
            logger.error(f"Bot {self.bot_id} not found in database.")
            return False

        self.active_epoch = self.db.get_active_epoch(self.bot_id)

        # Restore last_short_price from DB for crash recovery
        if self.active_epoch:
            purchases = self.db.get_purchases_by_epoch(self.active_epoch['epoch_id'])
            if purchases:
                self.last_short_price = Decimal(str(purchases[-1]['price_at_buy']))

        if subscribe:
            symbol = self.config['symbol']
            self.bus.subscribe(f"market.ticker.{symbol}", self._on_ticker)  # type: ignore[arg-type]
            self.bus.subscribe("account.balances", self._on_balances)  # type: ignore[arg-type]
            self.bus.subscribe("account.execution_report", self._on_execution_report)  # type: ignore[arg-type]

        symbol = self.config['symbol']
        subacct = self.config.get('subaccount', 'bluechip')
        epoch_id = self.active_epoch['epoch_id'] if self.active_epoch else 'None'
        logger.info(
            f"Initialized AntiLouiseBot {self.bot_id} on {symbol} "
            f"(subaccount: {subacct}, margin SHORT). Active epoch: {epoch_id}"
        )
        return True

    async def start(self):
        if not self.config:
            raise RuntimeError("Bot not initialized. Call initialize() first.")
        self._running = True
        self.db.update_bot_status(self.bot_id, "RUNNING")
        self.config['status'] = "RUNNING"
        self._task = asyncio.create_task(self._main_loop())
        logger.info(f"AntiLouiseBot {self.bot_id} started loop.")

    async def stop(self, shutdown_db: bool = True):
        logger.info(f"Shutting down AntiLouiseBot {self.bot_id}")
        self._running = False
        if self._task:
            self._task.cancel()
        if shutdown_db:
            self.db.update_bot_status(self.bot_id, "SHUTDOWN")
            self.config['status'] = "SHUTDOWN"

    # ── Event callbacks ──────────────────────────────────────────────

    def _on_ticker(self, data: Dict[str, Any]):
        if 'c' in data:
            self.current_price = Decimal(str(data['c']))
            self.last_price_timestamp = int(time.time())

    def _on_balances(self, balances: list):
        for b in balances:
            if b.get("asset") == "USDT":
                self.usdt_free_balance = Decimal(str(b.get("free", "0")))
                break

    def _on_execution_report(self, event: Dict[str, Any]):
        client_oid = str(event.get('c', ''))
        status = event.get('X')
        order_id = str(event.get('i', ''))

        if client_oid not in self.pending_orders or status != 'FILLED':
            return

        meta = self.pending_orders.pop(client_oid)

        if meta['type'] == 'SHORT_OPEN':
            # A short-sell fill: record it as a "purchase" (entry) in the DB
            volume = Decimal(str(event.get('z', '0')))      # base asset sold (borrowed)
            received_usdt = Decimal(str(event.get('Z', '0')))  # USDT received
            price_at_short = (
                received_usdt / volume
                if volume > Decimal("0")
                else Decimal(str(event.get('p', '0')))
            )

            is_lucky = bool(meta.get('is_lucky_fill', False))
            self._last_fill_was_lucky = is_lucky
            if is_lucky:
                logger.info(
                    f"{self.bot_id}: LUCKY STRIKE short fill at {price_at_short:.4f} — "
                    "recording in DB but NOT updating last_short_price to preserve DCA rhythm"
                )

            purchase_id = f"al_{self.bot_id}_{int(time.time())}_{order_id or client_oid}"
            self.db.add_purchase(
                purchase_id, self.bot_id, meta['epoch_id'],
                float(price_at_short), float(volume), float(received_usdt),
                order_id, "FILLED", is_lucky_fill=is_lucky,
            )

            get_budget_guard().record_spend(
                self.bot_id, self.config["symbol"], "SHORT", received_usdt  # type: ignore[index]
            )

            # Update epoch stats (total_cost = total USDT received from shorts)
            epoch = meta['epoch']
            new_entries = epoch['num_purchases'] + 1
            old_received = Decimal(str(epoch['total_cost']))
            old_avg = Decimal(str(epoch['avg_buy_price']))

            new_received = old_received + received_usdt
            old_vol = (old_received / old_avg) if old_avg > Decimal("0") else Decimal("0")
            new_vol = old_vol + volume
            new_avg = new_received / new_vol if new_vol > Decimal("0") else price_at_short

            self.db.update_epoch_stats(
                meta['epoch_id'], new_entries, float(new_received), float(new_avg)
            )
            self.active_epoch = self.db.get_active_epoch(self.bot_id)
            # Lucky fills do NOT update last_short_price so the DCA rhythm
            # continues uninterrupted from the pre-lucky reference point.
            if not is_lucky:
                self.last_short_price = price_at_short
            logger.info(
                f"{self.bot_id}: Short-open WS confirmed {'[LUCKY] ' if is_lucky else ''}"
                f"avg={new_avg:.4f} last_ref={self.last_short_price:.4f}"
            )

        elif meta['type'] == 'SHORT_COVER':
            close_status = meta.get('status', 'CLOSED_SUCCESSFUL')
            self.db.close_epoch(
                meta['epoch_id'],
                float(meta['current_price']),
                float(meta['final_value']),
                float(meta['profit_usdt']),
                float(meta['profit_pct']),
                status=close_status
            )
            self.active_epoch = None
            logger.info(
                f"{self.bot_id}: Cover WS confirmed. Epoch closed "
                f"({close_status}, {meta['profit_pct']:.2f}%)."
            )

    # ── Main loop ────────────────────────────────────────────────────

    async def _main_loop(self):
        while self._running:
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

            interval = self.config.get('poll_interval_seconds', 300)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info(f"{self.bot_id} sleep interrupted by cancellation")
                break

    # ── Core strategy ────────────────────────────────────────────────

    async def poll_market(self):
        """One decision cycle for the anti-Louise short strategy."""
        now = int(time.time())
        if self.config["status"] not in ["ACCUMULATING", "RUNNING"]:
            return

        if now < self.cooldown_until:
            return

        symbol = self.config["symbol"]

        # Ensure exchange filters are loaded
        filters = get_exchange_filters().get(symbol)
        if not filters and getattr(self.gateway, "_client", None):
            try:
                filters = await get_exchange_filters().ensure_loaded(
                    symbol, self.gateway._client
                )
            except Exception as e:
                logger.warning(f"{self.bot_id}: Failed to load exchange filters: {e}")

        # Fresh price check
        staleness_sec = louise_price_staleness_sec()
        if self.current_price <= Decimal("0") or (now - self.last_price_timestamp > staleness_sec):
            logger.debug(f"{self.bot_id}: Waiting for fresh price (>{staleness_sec}s stale)...")
            return

        # Minimum USDT balance (needed for margin collateral)
        min_balance = Decimal(str(louise_min_usdt_balance()))
        if self.usdt_free_balance < min_balance:
            logger.warning(
                f"{self.bot_id}: Insufficient USDT margin collateral "
                f"(< {min_balance}, have {self.usdt_free_balance})."
            )
            return

        fuse = get_api_fuse()
        if fuse.is_tripped():
            logger.warning(f"{self.bot_id}: API Fuse tripped. Skipping cycle.")
            return

        gov = get_api_governor()
        if not gov.can_execute("binance", P_TRADING, 1):
            logger.warning(f"{self.bot_id}: WeightGovernor blocked execution.")
            return

        # Create epoch if none exists
        if not self.active_epoch:
            epoch_id = f"epoch_{self.bot_id}_{int(time.time())}"
            self.db.create_epoch(epoch_id, self.bot_id, "RUNNING")
            self.active_epoch = self.db.get_active_epoch(self.bot_id)
            logger.info(f"{self.bot_id}: Created new epoch {epoch_id}")

        epoch = self.active_epoch
        short_volume = Decimal(str(self.config["buy_volume"]))  # USDT to short each entry

        # ── P&L calculation and exit check ──────────────────────────
        if epoch['num_purchases'] > 0:
            avg_short_price = Decimal(str(epoch['avg_buy_price']))
            total_received = Decimal(str(epoch['total_cost']))

            # Defensive guards: should not happen when num_purchases > 0, but a
            # corrupted/partial DB row could leave these at zero and crash the loop.
            if avg_short_price <= Decimal("0") or total_received <= Decimal("0"):
                logger.warning(
                    f"{self.bot_id}: Epoch {epoch['epoch_id']} has num_purchases>0 "
                    f"but avg={avg_short_price} total={total_received}. Skipping cycle."
                )
                return

            total_volume = total_received / avg_short_price   # base asset owed
            current_exposure = total_volume * self.current_price  # cost to cover now
            profit_usdt = total_received - current_exposure
            profit_pct = (profit_usdt / total_received) * Decimal("100")

            logger.info(
                f"{self.bot_id}: Short P&L: {profit_pct:.2f}% "
                f"(avg_short={avg_short_price:.4f}, current={self.current_price:.4f}, "
                f"target={self.config['target_profit_pct']:.2f}%)"
            )

            # Record snapshot for time-series charting
            realized = self.db.get_total_realized_pnl(self.bot_id)
            self.db.record_pnl_snapshot(
                bot_id=self.bot_id,
                bot_type=self.BOT_TYPE,
                current_price=float(self.current_price),
                epoch_id=epoch['epoch_id'],
                avg_entry_price_usdt=float(avg_short_price),
                num_entries=epoch['num_purchases'],
                total_committed_usdt=float(total_received),
                unrealized_pnl_usdt=float(profit_usdt),
                unrealized_pnl_pct=float(profit_pct),
                cumulative_realized_pnl_usdt=realized,
            )
            _publish_pnl_snapshot_ws(
                self.bot_id, self.BOT_TYPE, epoch, self.current_price,
                avg_short_price, total_received, profit_usdt, profit_pct, realized,
            )

            # Take-profit: only exit condition
            target_profit = Decimal(str(self.config["target_profit_pct"]))
            if profit_pct >= target_profit:
                await self._execute_cover(epoch, current_exposure, profit_usdt, profit_pct)
                return

        # ── Entry gate: only SHORT if price is strictly ABOVE last short ─
        if epoch['num_purchases'] > 0:
            if self.current_price <= self.last_short_price:
                logger.debug(
                    f"{self.bot_id}: Price {self.current_price:.4f} not above "
                    f"last short {self.last_short_price:.4f}. Waiting for higher price."
                )
                return

        # ── Spend guards ─────────────────────────────────────────────
        bg = get_budget_guard()
        if not bg.can_spend(short_volume, self.bot_id):
            logger.warning(
                f"{self.bot_id}: BudgetGuard rejected short of {short_volume} USDT."
            )
            return

        daily_budget = Decimal(str(self.config.get("daily_budget_usdt", 500.0)))
        total_so_far = Decimal(str(epoch.get('total_cost', 0.0)))
        if total_so_far + short_volume > daily_budget:
            logger.warning(
                f"{self.bot_id}: Daily budget reached ({daily_budget} USDT). "
                f"Pausing until take-profit."
            )
            return

        if self.usdt_free_balance < short_volume:
            logger.warning(
                f"{self.bot_id}: Insufficient USDT collateral. "
                f"Need {short_volume}, have {self.usdt_free_balance}."
            )
            return

        filters = get_exchange_filters().get(symbol)
        if filters and short_volume < filters.min_notional:
            logger.warning(
                f"{self.bot_id}: short_volume {short_volume} < MIN_NOTIONAL {filters.min_notional}"
            )
            self.cooldown_until = now + louise_cooldown_buy_fail_sec()
            return

        await self._execute_short_open(epoch, short_volume)

    # ── Order execution ──────────────────────────────────────────────

    def _is_lucky_entry(self) -> bool:
        """True if current price is at or above the HA high of the last closed daily candle.

        A Lucky Strike SHORT entry: price touches the Heikin-Ashi upside extreme,
        meaning we are shorting at a historically anomalous high — worth marking
        separately so the DCA rhythm is not disrupted by a single extreme fill.
        Returns False on any error (fail-safe → normal DCA behaviour).
        """
        try:
            vault = get_telemetry_vault()
            klines = vault.get_klines(self.config["symbol"], "1d", limit=2)
            closed = [k for k in klines if k.get("is_closed", 1) == 1]
            if not closed:
                return False
            ha_high = closed[0].get("ha_high")
            if ha_high is None:
                return False
            return float(self.current_price) >= float(ha_high)
        except Exception:
            return False

    async def _execute_short_open(self, epoch: Dict[str, Any], usdt_amount: Decimal):
        """Open a new short position: margin SELL with auto-borrow."""
        symbol = self.config["symbol"]  # type: ignore[index]
        alerts = get_alert_dispatcher()

        is_lucky = self._is_lucky_entry()
        logger.info(
            f"{self.bot_id}: Opening {'LUCKY STRIKE ' if is_lucky else ''}"
            f"SHORT {usdt_amount} USDT on {symbol} (margin)"
        )
        client_oid = f"al_{self.bot_id}_{int(time.time())}"

        try:
            is_simulation = os.environ.get("LOUISE_PAPER_TRADE", "true").lower() == "true"

            if self.gateway and getattr(self.gateway, "_client", None):
                client = self.gateway._client

                self.pending_orders[client_oid] = {
                    'type': 'SHORT_OPEN',
                    'epoch_id': epoch['epoch_id'],
                    'epoch': epoch,
                    'is_lucky_fill': is_lucky,
                }

                if is_simulation:
                    logger.info(
                        f"{self.bot_id}: [SIM] Paper-trade SHORT_OPEN {usdt_amount} USDT"
                    )
                    qty = usdt_amount / self.current_price
                    sim_payload = {
                        'X': 'FILLED',
                        'c': client_oid,
                        's': symbol,
                        'S': 'SELL',
                        'z': str(qty),
                        'p': str(self.current_price),
                        'Z': str(usdt_amount),
                        'i': client_oid,
                    }
                    asyncio.create_task(self._delay_sim(sim_payload))
                else:
                    # Live: margin SELL with auto-borrow
                    await client.create_margin_order(
                        symbol=symbol,
                        side="SELL",
                        type="MARKET",
                        newClientOrderId=client_oid,
                        quoteOrderQty=str(usdt_amount),
                        sideEffectType="MARGIN_BUY",  # auto-borrow the base asset
                    )
                # Reserve collateral balance approximation
                self.usdt_free_balance -= usdt_amount

            else:
                logger.error(f"{self.bot_id}: No gateway for SHORT_OPEN")
                alerts.warning(
                    "NO_GATEWAY",
                    f"AntiLouise {self.bot_id}: gateway unavailable for short open",
                    payload={"bot_id": self.bot_id, "symbol": symbol},
                    silent=True,
                )
                self.cooldown_until = int(time.time()) + louise_cooldown_gateway_fail_sec()

        except Exception as e:
            err = str(e)[:100]
            logger.error(f"{self.bot_id}: SHORT_OPEN failed: {err}")
            alerts.warning(
                "SHORT_OPEN_FAILED",
                f"AntiLouise {self.bot_id} failed to open short on {symbol}: {err}",
                payload={"bot_id": self.bot_id, "symbol": symbol, "error": err},
                silent=False,
            )
            self.cooldown_until = int(time.time()) + louise_cooldown_buy_fail_sec()

    async def _execute_cover(
        self,
        epoch: Dict[str, Any],
        cover_cost: Decimal,
        profit_usdt: Decimal,
        profit_pct: Decimal,
        status: str = "CLOSED_SUCCESSFUL",
    ):
        """Cover the full short position: margin BUY with auto-repay."""
        symbol = self.config["symbol"]  # type: ignore[index]
        alerts = get_alert_dispatcher()

        avg_short_price = Decimal(str(epoch['avg_buy_price']))
        total_received = Decimal(str(epoch['total_cost']))

        # Defensive: should be guarded upstream, but never let division blow the loop
        if avg_short_price <= Decimal("0") or total_received <= Decimal("0"):
            alerts.critical(
                "COVER_INVALID_EPOCH",
                f"AntiLouise {self.bot_id}: cannot cover epoch "
                f"{epoch['epoch_id']} — avg={avg_short_price} total={total_received}",
                payload={"bot_id": self.bot_id, "epoch_id": epoch['epoch_id']},
                silent=False,
            )
            self.cooldown_until = int(time.time()) + louise_cooldown_buy_fail_sec()
            return

        total_volume = total_received / avg_short_price  # total base asset owed

        filters = get_exchange_filters().get(symbol)
        if filters is None:
            # Without filters we cannot quantize to LOT_SIZE — Binance would reject
            # the cover with -1013. Abort with critical alert and retry next cycle
            # (poll_market will attempt to ensure_loaded the filters).
            alerts.critical(
                "COVER_BLOCKED_NO_FILTERS",
                f"AntiLouise {self.bot_id} reached take-profit on {symbol} but "
                f"exchange filters are unavailable — aborting cover until filters load.",
                payload={"bot_id": self.bot_id, "symbol": symbol,
                         "epoch_id": epoch['epoch_id']},
                silent=False,
            )
            self.cooldown_until = int(time.time()) + louise_cooldown_gateway_fail_sec()
            return
        quantized_vol = filters.quantize_qty(total_volume)

        logger.info(
            f"{self.bot_id}: Covering SHORT {quantized_vol} {symbol} at market "
            f"(profit {profit_pct:.2f}%)"
        )
        client_oid = f"alc_{self.bot_id}_{int(time.time())}"

        try:
            is_simulation = os.environ.get("LOUISE_PAPER_TRADE", "true").lower() == "true"

            if self.gateway and getattr(self.gateway, "_client", None):
                client = self.gateway._client

                self.pending_orders[client_oid] = {
                    'type': 'SHORT_COVER',
                    'epoch_id': epoch['epoch_id'],
                    'current_price': self.current_price,
                    'final_value': cover_cost,
                    'profit_usdt': profit_usdt,
                    'profit_pct': profit_pct,
                    'status': status,
                }

                if is_simulation:
                    logger.info(
                        f"{self.bot_id}: [SIM] Paper-trade SHORT_COVER {quantized_vol} {symbol}"
                    )
                    sim_payload = {
                        'X': 'FILLED',
                        'c': client_oid,
                        's': symbol,
                        'S': 'BUY',
                        'z': str(quantized_vol),
                        'p': str(self.current_price),
                        'Z': str(quantized_vol * self.current_price),
                        'i': client_oid,
                    }
                    asyncio.create_task(self._delay_sim(sim_payload))
                else:
                    # Live: margin BUY to repay borrow
                    await client.create_margin_order(
                        symbol=symbol,
                        side="BUY",
                        type="MARKET",
                        newClientOrderId=client_oid,
                        quantity=str(quantized_vol),
                        sideEffectType="AUTO_REPAY",
                    )

            else:
                logger.error(f"{self.bot_id}: No gateway for SHORT_COVER")
                msg = (
                    f"CRITICAL: AntiLouise {self.bot_id} reached take-profit but "
                    f"gateway unavailable. Short position STUCK: "
                    f"{quantized_vol} {symbol} owed at {self.current_price}"
                )
                alerts.critical(
                    "COVER_BLOCKED_NO_GATEWAY",
                    msg,
                    payload={
                        "bot_id": self.bot_id,
                        "symbol": symbol,
                        "quantity": float(quantized_vol),
                        "current_price": float(self.current_price),
                        "profit_pct": float(profit_pct),
                        "epoch_id": epoch['epoch_id'],
                    },
                    silent=False,
                )
                self.cooldown_until = int(time.time()) + louise_cooldown_gateway_fail_sec()

        except Exception as e:
            err = str(e)[:80]
            logger.error(f"{self.bot_id}: SHORT_COVER failed: {err}")
            alerts.critical(
                "COVER_EXECUTION_FAILED",
                f"CRITICAL: AntiLouise {self.bot_id} cover failed on {symbol}: {err}",
                payload={
                    "bot_id": self.bot_id,
                    "symbol": symbol,
                    "quantity": float(quantized_vol),
                    "error": str(e)[:100],
                    "epoch_id": epoch['epoch_id'],
                },
                silent=False,
            )
            self.cooldown_until = int(time.time()) + louise_cooldown_buy_fail_sec()

    async def _delay_sim(self, payload: Dict[str, Any]):
        await asyncio.sleep(1.5)
        self._on_execution_report(payload)
