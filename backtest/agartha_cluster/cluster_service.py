"""Cluster service main loop (synchronous, but cooperative).

This is a deliberately simple loop that ties scheduler, throttle, bot
runner and reconciler together. It avoids asyncio in this skeleton so
it can be exercised by tests without the event loop boilerplate; the
production live process can wrap it in an asyncio task or run it under
NSSM/systemd as-is.
"""
from __future__ import annotations

import os
import platform
import time
from dataclasses import dataclass
from typing import Optional

from backtest.agartha_cluster.api_throttle import ApiThrottle
from backtest.agartha_cluster.bot_runner import BotRunner
from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.event_logger import EventLogger
from backtest.agartha_cluster.live_client import LiveClient
from backtest.agartha_cluster.models import (
    BotState,
    EventKind,
    EventLevel,
    EventSource,
    SymbolFilters,
)
from backtest.agartha_cluster.reconciler import Reconciler
from backtest.agartha_cluster.scheduler import DeployScheduler

CLUSTER_VERSION = "0.1.0"


@dataclass
class ServiceConfig:
    mode: str = "live"                          # 'live' | 'dry-run'
    tick_seconds: float = 5.0
    reconcile_every_seconds: int = 300
    capital_usdt_per_bot: float = 10.0
    initial_correlation_prefix: str = "agc"


class ClusterService:
    def __init__(
        self,
        *,
        db: ClusterDB,
        client: LiveClient,
        events: EventLogger,
        throttle: ApiThrottle,
        scheduler: DeployScheduler,
        runner: BotRunner,
        reconciler: Reconciler,
        config: Optional[ServiceConfig] = None,
    ):
        self.db = db
        self.client = client
        self.events = events
        self.throttle = throttle
        self.scheduler = scheduler
        self.runner = runner
        self.reconciler = reconciler
        self.config = config or ServiceConfig()
        self._stop = False
        self._last_reconcile = 0.0
        self._run_id: Optional[int] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._run_id = self.db.start_service_run(
            mode=self.config.mode,
            pid=os.getpid(),
            host=platform.node(),
            version=CLUSTER_VERSION,
        )
        self.events.info(
            kind=EventKind.SERVICE_START,
            source=EventSource.SERVICE,
            payload={"mode": self.config.mode, "run_id": self._run_id, "version": CLUSTER_VERSION},
        )

    def stop(self, reason: str = "graceful_shutdown") -> None:
        self._stop = True
        if self._run_id is not None:
            self.db.stop_service_run(self._run_id, reason=reason)
        self.events.info(
            kind=EventKind.SERVICE_STOP,
            source=EventSource.SERVICE,
            payload={"reason": reason},
        )

    def request_stop(self) -> None:
        self._stop = True

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        if self._run_id is None:
            self.start()
        try:
            while not self._stop:
                self.tick_once()
                time.sleep(self.config.tick_seconds)
        finally:
            if not self._stop:
                self.stop()

    def tick_once(self) -> None:
        # 1. Try to deploy the next due bot (one per tick max).
        row = self.scheduler.pick_next()
        if row is not None:
            self._deploy_from_queue(row)

        # 2. Walk through in-flight bots and advance their state.
        for bot in self.db.list_bots(state=BotState.IN_POSITION):
            filters = self.db.get_symbol_filters(bot.symbol) or SymbolFilters(symbol=bot.symbol)
            self.runner.place_exit(bot, filters)

        # 3. Periodic reconciliation.
        if (time.time() - self._last_reconcile) >= self.config.reconcile_every_seconds:
            try:
                self.reconciler.run_once()
            except NotImplementedError:
                # Real client not wired yet; reconciler is a no-op in this case.
                pass
            self._last_reconcile = time.time()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _deploy_from_queue(self, row) -> None:
        symbol = row["symbol"]
        params = self.db.get_symbol_params(symbol)
        if params is None:
            self.db.mark_queue_status(
                row["queue_id"],
                "failed",
                reason="no_params; run optimizer first",
            )
            self.events.error(
                kind=EventKind.OPTIMIZATION_FAILED,
                source=EventSource.SCHEDULER,
                symbol=symbol,
                payload={"queue_id": row["queue_id"], "reason": "missing_symbol_params"},
            )
            return

        filters = self.db.get_symbol_filters(symbol)
        if filters is None:
            try:
                raw = self.client.get_filters(symbol)
                filters = SymbolFilters(
                    symbol=symbol,
                    tick_size=float(raw.get("tick_size", 1e-8)),
                    step_size=float(raw.get("step_size", 1e-8)),
                    min_notional=float(raw.get("min_notional", 0.1)),
                    bid_multiplier_up=float(raw.get("bid_multiplier_up", 5.0)),
                    bid_multiplier_down=float(raw.get("bid_multiplier_down", 0.2)),
                    ask_multiplier_up=float(raw.get("ask_multiplier_up", 5.0)),
                    ask_multiplier_down=float(raw.get("ask_multiplier_down", 0.2)),
                    refreshed_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                )
                self.db.upsert_symbol_filters(filters, raw=raw)
            except NotImplementedError:
                self.db.mark_queue_status(
                    row["queue_id"], "failed", reason="filters_unavailable_live_client_not_wired"
                )
                self.events.error(
                    kind=EventKind.NEEDS_MANUAL_ACTION,
                    source=EventSource.SCHEDULER,
                    symbol=symbol,
                    payload={"reason": "BinanceAlphaClient.get_filters_not_wired"},
                )
                return

        correlation_id = f"{self.config.initial_correlation_prefix}-{row['queue_id']}-{int(time.time())}"
        bot_id = self.db.create_bot(
            symbol=symbol,
            capital_usdt=self.config.capital_usdt_per_bot,
            params_snapshot=params.as_runtime_dict(),
            correlation_id=correlation_id,
            state=BotState.QUEUED,
        )
        self.db.mark_queue_status(
            row["queue_id"], "deployed", bot_id=bot_id, actual_deploy_ts=int(time.time() * 1000)
        )
        self.events.info(
            kind=EventKind.BOT_DEPLOYED,
            source=EventSource.SCHEDULER,
            bot_id=bot_id,
            symbol=symbol,
            correlation_id=correlation_id,
            payload={"queue_id": row["queue_id"], "capital_usdt": self.config.capital_usdt_per_bot},
        )

        bot = self.db.get_bot(bot_id)
        if bot is not None:
            self.runner.place_entry(bot, filters)
